#!/usr/bin/env python3
import os
import sys
import re
import json
import subprocess
import time
from collections import defaultdict
from typing import Dict, List, Tuple, Iterable, Callable, Any

import pandas as pd
from github import Github
from github.GithubException import GithubException
import requests.exceptions
import urllib3.exceptions

from extractor_trilingual import GitCommitAnalyzer

# -------------------------
# Network retry utilities
# -------------------------

def retry_network_operation(operation: Callable, max_wait_seconds: int = 60, initial_delay: float = 1.0, backoff_factor: float = 2.0) -> Any:
    """
    Retry a network operation with exponential backoff until it succeeds.
    
    Args:
        operation: Function to execute that may fail due to network issues
        max_wait_seconds: Maximum time to wait between retries (default 60 seconds)
        initial_delay: Initial delay between retries in seconds (default 1 second)
        backoff_factor: Exponential backoff multiplier (default 2.0)
    
    Returns:
        Result of the operation when it succeeds
    """
    delay = initial_delay
    
    while True:
        try:
            return operation()
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.RequestException,
            urllib3.exceptions.MaxRetryError,
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.NewConnectionError,
            GithubException,
            OSError,  # Covers network-related OS errors
        ) as e:
            # Check if it's a network-related error
            error_str = str(e).lower()
            if any(keyword in error_str for keyword in [
                'max retries exceeded', 'failed to resolve', 'connection', 
                'network', 'timeout', 'nodename nor servname', 'temporary failure',
                'name resolution', 'no route to host'
            ]):
                print(f"\r  Network error: {type(e).__name__}: {str(e)[:100]}...")
                print(f"  Retrying in {delay:.1f} seconds...")
                time.sleep(delay)
                delay = min(delay * backoff_factor, max_wait_seconds)
                continue
            else:
                # Re-raise non-network errors
                raise

# -------------------------
# Local git helpers
# -------------------------

def run(cmd: List[str], cwd: str = None) -> str:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDERR: {res.stderr.strip()}")
    return res.stdout

def ensure_local_clone(repo_name: str, clone_url: str, dest_root: str, token: str = None) -> str:
    dest = os.path.join(dest_root, repo_name)
    auth_url = clone_url
    if token and clone_url.startswith("https://"):
        auth_url = clone_url.replace("https://", f"https://{token}@")
    if os.path.exists(dest) and os.path.isdir(os.path.join(dest, ".git")):
        run(["git", "remote", "set-url", "origin", auth_url], cwd=dest)
        # fetch everything, prune, and tags for completeness
        run(["git", "fetch", "--all", "--prune", "--tags"], cwd=dest)
        # also fetch PR heads into refs/remotes/origin/pr/* (harmless if none)
        try:
            run(["git", "fetch", "origin", "+refs/pull/*/head:refs/remotes/origin/pr/*"], cwd=dest)
        except Exception:
            pass
    else:
        os.makedirs(dest_root, exist_ok=True)
        run(["git", "clone", "--no-tags", "--quiet", auth_url, dest])
        run(["git", "fetch", "--all", "--prune", "--tags"], cwd=dest)
        try:
            run(["git", "fetch", "origin", "+refs/pull/*/head:refs/remotes/origin/pr/*"], cwd=dest)
        except Exception:
            pass
    return dest

def list_all_branches(repo_path: str) -> List[str]:
    """
    Return both local and remote-tracking branches (origin/*), excluding symbolic origin/HEAD.
    """
    out = run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin"],
        cwd=repo_path
    )
    branches = [ln.strip() for ln in out.splitlines() if ln.strip()]
    branches = [b for b in branches if b != "origin/HEAD"]
    return branches

def commits_by_branch(repo_path: str, branches: Iterable[str]) -> Tuple[Dict[str, List[str]], List[str]]:
    """
    Build a mapping from commit SHA -> list of branch names that contain it,
    and return a deduped list of all commits reachable from all refs (git rev-list --all).
    """
    sha_to_branches: Dict[str, List[str]] = defaultdict(list)
    for br in branches:
        out = run(["git", "rev-list", br], cwd=repo_path)
        for sha in out.splitlines():
            if sha:
                sha_to_branches[sha].append(br)

    # Use the full all-refs traversal order; do NOT restrict to seen SHAs.
    all_unique = run(["git", "rev-list", "--all"], cwd=repo_path).splitlines()
    # keep only real SHAs (defensive, though rev-list emits SHAs)
    all_unique = [s for s in all_unique if s]
    return sha_to_branches, all_unique

def parse_numstat(repo_path: str, sha: str):
    out = run(["git", "show", "--numstat", "--format=", sha], cwd=repo_path)
    total_adds = 0
    total_dels = 0
    per_file: Dict[str, Tuple[int, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        adds, dels, fname = parts
        try:
            adds_i = int(adds) if adds.isdigit() else 0
            dels_i = int(dels) if dels.isdigit() else 0
        except Exception:
            adds_i, dels_i = 0, 0
        per_file[fname] = (adds_i, dels_i)
        total_adds += adds_i
        total_dels += dels_i
    return total_adds, total_dels, per_file

def parse_name_status(repo_path: str, sha: str):
    out = run(["git", "diff-tree", "--no-commit-id", "--name-status", "-r", sha], cwd=repo_path)
    add_del_map = parse_numstat(repo_path, sha)[2]
    changed = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status = parts[0]
            filename = parts[-1]  # for renames, last is new name
            adds, dels = add_del_map.get(filename, (0, 0))
            changed.append({
                "filename": filename,
                "status": status,
                "additions": adds,
                "deletions": dels,
                "changes": adds + dels,
            })
    return changed

# Batch size constant for processing commits
COMMIT_BATCH_SIZE = 1000

def get_commit_data_chunked(repo_path: str, commits: List[str]) -> Dict[str, Dict]:
    """Get all commit data in chunked batches to avoid 'Argument list too long' errors."""
    if not commits:
        return {}
    
    all_results = {}
    total_commits = len(commits)
    
    # Process commits in chunks
    for i in range(0, total_commits, COMMIT_BATCH_SIZE):
        chunk = commits[i:i + COMMIT_BATCH_SIZE]
        chunk_end = min(i + COMMIT_BATCH_SIZE, total_commits)
        print(f"\r    processing batch {i//COMMIT_BATCH_SIZE + 1}/{(total_commits + COMMIT_BATCH_SIZE - 1)//COMMIT_BATCH_SIZE} (commits {i+1}-{chunk_end})...", end="", flush=True)
        
        chunk_results = get_single_batch_commit_data(repo_path, chunk)
        all_results.update(chunk_results)
    
    print()  # New line after batch processing
    return all_results

def get_single_batch_commit_data(repo_path: str, commits: List[str]) -> Dict[str, Dict]:
    """Get commit data for a single batch of commits."""
    if not commits:
        return {}
    
    # First call: get commit headers (include merge commits to match commits_by_branch)
    fmt = "%H|%an|%ae|%ad|%s"
    cmd = ["git", "log", f"--format={fmt}", "--date=iso-strict"] + commits
    header_out = run(cmd, cwd=repo_path)
    
    # Second call: get numstat (include merge commits with -m flag)
    cmd = ["git", "log", "-m", "--format=%H", "--numstat"] + commits
    numstat_out = run(cmd, cwd=repo_path)
    
    # Third call: get name-status (include merge commits with -m flag)
    cmd = ["git", "log", "-m", "--format=%H", "--name-status"] + commits
    status_out = run(cmd, cwd=repo_path)
    
    # Parse headers first
    result = {}
    for line in header_out.splitlines():
        if "|" in line and len(line.split("|")) >= 5:
            parts = line.split("|", 4)  # Split into max 5 parts, keeping any extra pipes in the subject
            sha = parts[0]
            result[sha] = {
                "author_name": parts[1],
                "author_email": parts[2], 
                "author_date": parts[3],
                "subject": parts[4],  # This can contain additional pipes
                "changed_files": [],
                "total_adds": 0,
                "total_dels": 0
            }
    
    # Parse numstat (store per-SHA numstat data, only first occurrence for merge commits)
    current_sha = None
    sha_numstat = {}
    seen_shas = set()  # Track which SHAs we've already processed
    processing_first_occurrence = False  # Track if we're in first occurrence of current SHA
    
    for line in numstat_out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Check if this is a commit SHA (40 hex chars)
        if len(line) == 40 and all(c in '0123456789abcdef' for c in line.lower()):
            current_sha = line
            # Only process first occurrence of each SHA (merge commits appear multiple times with -m)
            if current_sha not in seen_shas:
                seen_shas.add(current_sha)
                sha_numstat[current_sha] = {}
                processing_first_occurrence = True
            else:
                processing_first_occurrence = False
        elif current_sha and processing_first_occurrence and "\t" in line:
            parts = line.split("\t")
            if len(parts) == 3:
                # Numstat line: additions, deletions, filename
                adds, dels, fname = parts
                try:
                    adds_i = int(adds) if adds.isdigit() else 0
                    dels_i = int(dels) if dels.isdigit() else 0
                except:
                    adds_i, dels_i = 0, 0
                sha_numstat[current_sha][fname] = (adds_i, dels_i)
                if current_sha in result:
                    result[current_sha]["total_adds"] += adds_i
                    result[current_sha]["total_dels"] += dels_i
    
    # Parse name-status and combine with numstat (only first occurrence for merge commits)
    current_sha = None
    seen_shas_status = set()  # Track which SHAs we've already processed for name-status
    processing_first_occurrence_status = False  # Track if we're in first occurrence of current SHA
    
    for line in status_out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Check if this is a commit SHA (40 hex chars)
        if len(line) == 40 and all(c in '0123456789abcdef' for c in line.lower()):
            current_sha = line
            # Only process first occurrence of each SHA (merge commits appear multiple times with -m)
            if current_sha not in seen_shas_status:
                seen_shas_status.add(current_sha)
                processing_first_occurrence_status = True
            else:
                processing_first_occurrence_status = False
        elif current_sha and current_sha in result and processing_first_occurrence_status and "\t" in line:
            parts = line.split("\t")
            if len(parts) == 2:
                # Name-status line: status, filename
                status, filename = parts
                # Get numstat data for this file from stored data
                adds, dels = sha_numstat.get(current_sha, {}).get(filename, (0, 0))
                result[current_sha]["changed_files"].append({
                    "filename": filename,
                    "status": status,
                    "additions": adds,
                    "deletions": dels,
                    "changes": adds + dels,
                })
            elif len(parts) == 3:
                # Rename/copy line: status, old_filename, new_filename
                status, old_filename, new_filename = parts
                # For renames, use the new filename and get stats for both old and new names
                adds_old, dels_old = sha_numstat.get(current_sha, {}).get(old_filename, (0, 0))
                adds_new, dels_new = sha_numstat.get(current_sha, {}).get(new_filename, (0, 0))
                # Use the sum of both (though for pure renames this is usually 0,0)
                total_adds = adds_old + adds_new
                total_dels = dels_old + dels_new
                result[current_sha]["changed_files"].append({
                    "filename": new_filename,
                    "status": status,
                    "additions": total_adds,
                    "deletions": total_dels,
                    "changes": total_adds + total_dels,
                })
    
    return result

def commit_header(repo_path: str, sha: str):
    fmt = "%H|%an|%ae|%ad|%s"
    out = run(["git", "show", "-s", f"--format={fmt}", "--date=iso-strict", sha], cwd=repo_path)
    parts = out.strip().split("|", 4)
    if len(parts) != 5:
        return ("", "", "", "")
    _, author_name, author_email, author_date, subject = parts
    return (author_name, author_email, author_date, subject)

# -------------------------
# Main
# -------------------------

def main():
    start_time = time.time()
    
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    os.chdir(script_dir)

    config_file = "config.json"
    with open(config_file, 'r') as f:
        config = json.load(f)

    GITHUB_TOKEN = config.get("GITHUB_TOKEN")
    ORG_NAME = config.get("ORG_NAME")

    g = Github(GITHUB_TOKEN)
    org = g.get_organization(ORG_NAME)

    # private-only
    repos = [repo for repo in org.get_repos() if repo.private]

    output_folder = "gh_outputs"
    os.makedirs(output_folder, exist_ok=True)

    local_root = os.path.join(output_folder, "local_repos")
    os.makedirs(local_root, exist_ok=True)

    # repositories.csv
    repo_rows = [{
        "name": repo.name,
        "full_name": repo.full_name,
        "private": repo.private,
        "created_at": repo.created_at,
        "default_branch": repo.default_branch
    } for repo in repos]
    pd.DataFrame(repo_rows).to_csv(os.path.join(output_folder, "repositories.csv"), index=False)

    # contributors.csv (per-repo)
    for repo in repos:
        contributors_file = os.path.join(output_folder, f"{repo.name}_contributors.csv")
        if os.path.exists(contributors_file):
            print(f"[Contributors] {repo.name} - output file already exists, skipping: {contributors_file}")
            continue
            
        print(f"[Contributors] {repo.name}")
        contributors_rows = []
        contributors = retry_network_operation(lambda: list(repo.get_contributors()))
        for contributor in contributors:
            contributors_rows.append({
                "repo": repo.full_name,
                "login": contributor.login if contributor else None,
                "contributions": getattr(contributor, "contributions", None)
            })
        pd.DataFrame(contributors_rows).to_csv(contributors_file, index=False)

    # branches.csv (per-repo)
    for repo in repos:
        branches_file = os.path.join(output_folder, f"{repo.name}_branches.csv")
        if os.path.exists(branches_file):
            print(f"[Branches] {repo.name} - output file already exists, skipping: {branches_file}")
            continue
            
        print(f"[Branches] {repo.name}")
        branches_rows = []
        branches = retry_network_operation(lambda: list(repo.get_branches()))
        for branch in branches:
            branches_rows.append({
                "repo": repo.full_name,
                "branch": branch.name,
                "commit_sha": branch.commit.sha
            })
        pd.DataFrame(branches_rows).to_csv(branches_file, index=False)

    # commits.csv (per-repo, local git, de-duped, ALL refs)
    for repo in repos:
        commits_file = os.path.join(output_folder, f"{repo.name}_commits.csv")
        if os.path.exists(commits_file):
            print(f"[Commits: local git] {repo.name} - output file already exists, skipping: {commits_file}")
            continue
            
        print(f"[Commits: local git] {repo.name}")
        commits_rows = []
        try:
            repo_path = ensure_local_clone(repo.name, repo.clone_url, local_root, token=GITHUB_TOKEN)

            # Check if repository is empty (has no commits)
            try:
                run(["git", "rev-parse", "HEAD"], cwd=repo_path)
            except RuntimeError:
                print(f"  repository is empty (no commits), skipping")
                continue

            local_branches = list_all_branches(repo_path)
            if not local_branches:
                try:
                    run(["git", "checkout", repo.default_branch], cwd=repo_path)
                    local_branches = list_all_branches(repo_path)
                except RuntimeError:
                    print(f"  failed to checkout default branch '{repo.default_branch}', repository may be empty")
                    continue

            branch_map, all_commits = commits_by_branch(repo_path, local_branches)
            
            # Double-check: if no commits found, skip
            if not all_commits:
                print(f"  no commits found in repository, skipping")
                continue

            # Batch process all commit data in chunks
            print(f"  processing {len(all_commits)} commits in batches of {COMMIT_BATCH_SIZE}...")
            commit_data = get_commit_data_chunked(repo_path, all_commits)
            
            individual_calls = 0
            for i, sha in enumerate(all_commits, 1):
                try:
                    data = commit_data.get(sha, {})
                    if data:
                        # Use all batched data
                        author_name = data["author_name"]
                        author_email = data["author_email"]
                        author_date = data["author_date"]
                        subject = data["subject"]
                        changed_files = data["changed_files"]
                        total_adds = data["total_adds"]
                        total_dels = data["total_dels"]
                    else:
                        # Fallback to individual calls
                        individual_calls += 1
                        if individual_calls <= 5:  # Log first few failures for debugging
                            print(f"\r  DEBUG: batch miss for {sha[:8]} (#{individual_calls})")
                            print(f"    Total parsed commits in batch: {len(commit_data)}")
                            print(f"    Expected total commits: {len(all_commits)}")
                            # Check if this SHA is in the beginning or end of the list
                            sha_pos = all_commits.index(sha) if sha in all_commits else -1
                            print(f"    SHA position in list: {sha_pos}")
                    
                        author_name, author_email, author_date, subject = commit_header(repo_path, sha)
                        changed_files = parse_name_status(repo_path, sha)
                        total_adds, total_dels, _ = parse_numstat(repo_path, sha)
                    
                    # Running counter with carriage return
                    print(f"\r  processed: {i}/{len(all_commits)} (individual: {individual_calls})", end="", flush=True)
                    
                    commits_rows.append({
                        "repo": repo.full_name,
                        "sha": sha,
                        "author.name": author_name,
                        "author.email": author_email,
                        "commit.author.date": author_date,
                        "commit.message": subject,
                        "branches": branch_map.get(sha, []),
                        "issues_referenced": re.findall(r"#(\d+)", subject or ""),
                        "additions": total_adds,
                        "deletions": total_dels,
                        "total_changes": (total_adds + total_dels),
                        "changed_files": json.dumps(changed_files),
                    })
                except Exception as e:
                    print(f"\r    error on {sha[:8]}: {e}")
                    continue
            
            # Complete the progress line
            print()  # newline after carriage return progress
            
            # Save per-repo commits file
            pd.DataFrame(commits_rows).to_csv(commits_file, index=False)
        except Exception as e:
            print(f"  failed on repo {repo.name}: {e}")
            continue

    # pull_requests.csv and pr_comments.csv (per-repo, combined to avoid duplicate API calls)
    for repo in repos:
        pulls_file = os.path.join(output_folder, f"{repo.name}_pull_requests.csv")
        pr_comments_file = os.path.join(output_folder, f"{repo.name}_pr_comments.csv")
        
        if os.path.exists(pulls_file) and os.path.exists(pr_comments_file):
            print(f"[Pull Requests & Comments] {repo.name} - output files already exist, skipping: {pulls_file}, {pr_comments_file}")
            continue
            
        print(f"[Pull Requests & Comments] {repo.name}")
        pulls_rows = []
        pr_comments_rows = []
        
        # Fetch PRs once and use for both datasets with retry logic
        prs = retry_network_operation(lambda: list(repo.get_pulls(state='all')))
        total_prs = len(prs)
        print(f"  Found {total_prs} PRs to process")
        
        for i, pr in enumerate(prs, 1):
            print(f"\r  processed {i}/{total_prs} PRs...", end="", flush=True)
            # Collect PR data
            pulls_rows.append({
                "repo": repo.full_name,
                "number": pr.number,
                "user.login": pr.user.login if pr.user else None,
                "created_at": pr.created_at,
                "merged_at": pr.merged_at,
                "files_impacted": getattr(pr, "changed_files", None)
            })
            
            # Collect PR comments with retry logic
            issue_comments = retry_network_operation(lambda: list(pr.get_issue_comments()))
            for comment in issue_comments:
                pr_comments_rows.append({
                    "repo": repo.full_name,
                    "pull_number": pr.number,
                    "user": comment.user.login if comment.user else None,
                    "created_at": comment.created_at,
                    "type": "issue"
                })
            
            review_comments = retry_network_operation(lambda: list(pr.get_review_comments()))
            for review_comment in review_comments:
                pr_comments_rows.append({
                    "repo": repo.full_name,
                    "pull_number": pr.number,
                    "user": review_comment.user.login if review_comment.user else None,
                    "created_at": review_comment.created_at,
                    "position": review_comment.position,
                    "type": "review"
                })
        
        print()  # New line after progress tracking
        
        # Save per-repo files
        pd.DataFrame(pulls_rows).to_csv(pulls_file, index=False)
        pd.DataFrame(pr_comments_rows).to_csv(pr_comments_file, index=False)

    # issues.csv (per-repo)
    for repo in repos:
        issues_file = os.path.join(output_folder, f"{repo.name}_issues.csv")
        if os.path.exists(issues_file):
            print(f"[Issues] {repo.name} - output file already exists, skipping: {issues_file}")
            continue
            
        print(f"[Issues] {repo.name}")
        issues_rows = []
        issues = retry_network_operation(lambda: list(repo.get_issues(state='all')))
        for issue in issues:
            issues_rows.append({
                "repo": repo.full_name,
                "number": issue.number,
                "title": issue.title,
                "user.login": issue.user.login if issue.user else None,
                "assignees": [a.login for a in issue.assignees],
                "comments_count": issue.comments,
                "state": issue.state,
                "created_at": issue.created_at,
                "closed_at": issue.closed_at
            })
        pd.DataFrame(issues_rows).to_csv(issues_file, index=False)

    # issue_comments.csv (per-repo)
    for repo in repos:
        issue_comments_file = os.path.join(output_folder, f"{repo.name}_issue_comments.csv")
        if os.path.exists(issue_comments_file):
            print(f"[Issue Comments] {repo.name} - output file already exists, skipping: {issue_comments_file}")
            continue
            
        print(f"[Issue Comments] {repo.name}")
        issue_comments_rows = []
        issues = retry_network_operation(lambda: list(repo.get_issues(state='all')))
        for issue in issues:
            comments = retry_network_operation(lambda: list(issue.get_comments()))
            for comment in comments:
                issue_comments_rows.append({
                    "repo": repo.full_name,
                    "issue_number": issue.number,
                    "user.login": comment.user.login if comment.user else None,
                    "created_at": comment.created_at
                })
        pd.DataFrame(issue_comments_rows).to_csv(issue_comments_file, index=False)

    # dependency analysis (per-repo, local, reuses analyzer) - COMMENTED OUT, using separate monthly script
    # for repo in repos:
    #     dep_file = os.path.join(output_folder, f"{repo.name}_deps.json")
    #     if os.path.exists(dep_file):
    #         print(f"[Dependency Analysis] {repo.name} - output file already exists, skipping: {dep_file}")
    #         continue
    #         
    #     print(f"[Dependency Analysis] {repo.name}")
    #     analyzer = GitCommitAnalyzer(repo.clone_url)
    #     try:
    #         results = analyzer.analyze_all_commits()
    #         analyzer.save_results(results, dep_file)
    #     except Exception as e:
    #         print(f"Dependency analysis failed for {repo.name}: {e}")
    
    # Print elapsed time
    end_time = time.time()
    elapsed_seconds = int(end_time - start_time)
    elapsed_minutes = elapsed_seconds // 60
    elapsed_seconds = elapsed_seconds % 60
    print(f"Elapsed time: {elapsed_minutes}m {elapsed_seconds}s")

if __name__ == "__main__":
    main()
