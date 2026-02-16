#!/usr/bin/env python3
import argparse
import glob
import os
import shutil
import sys
import re
import json
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Iterable, Callable, Any, Optional, Set

import pandas as pd
from github import Github
from github.GithubException import GithubException
import requests.exceptions
import urllib3.exceptions

from extractor_trilingual import GitCommitAnalyzer


# -------------------------
# Incremental data helpers
# -------------------------

def get_cutoff_from_csv(csv_path: str, timestamp_column: str, fallback_column: str = None, verbose: bool = True) -> Optional[datetime]:
    """
    Extract max timestamp from existing CSV to use as cutoff for incremental fetching.
    Returns None if file doesn't exist or has no valid timestamps.
    """
    if not os.path.exists(csv_path):
        if verbose:
            print(f"    No existing file found: {os.path.basename(csv_path)}")
        return None

    try:
        # Read only the timestamp column(s) to minimize memory usage
        cols_to_read = [timestamp_column]
        if fallback_column:
            cols_to_read.append(fallback_column)

        df = pd.read_csv(csv_path, usecols=lambda c: c in cols_to_read)
        if df.empty:
            if verbose:
                print(f"    Existing file is empty: {os.path.basename(csv_path)}")
            return None

        row_count = len(df)

        # Try primary column first
        if timestamp_column in df.columns:
            ts_series = pd.to_datetime(df[timestamp_column], errors='coerce')
            max_ts = ts_series.max()
            if pd.notna(max_ts):
                # Ensure timezone-aware (assume UTC if naive)
                if max_ts.tzinfo is None:
                    max_ts = max_ts.replace(tzinfo=timezone.utc)
                if verbose:
                    print(f"    Found {row_count} existing records in {os.path.basename(csv_path)}")
                    print(f"    Latest {timestamp_column}: {max_ts.isoformat()}")
                return max_ts.to_pydatetime()

        # Try fallback column if primary didn't work
        if fallback_column and fallback_column in df.columns:
            ts_series = pd.to_datetime(df[fallback_column], errors='coerce')
            max_ts = ts_series.max()
            if pd.notna(max_ts):
                if max_ts.tzinfo is None:
                    max_ts = max_ts.replace(tzinfo=timezone.utc)
                if verbose:
                    print(f"    Found {row_count} existing records in {os.path.basename(csv_path)}")
                    print(f"    Latest {fallback_column} (fallback): {max_ts.isoformat()}")
                return max_ts.to_pydatetime()

        if verbose:
            print(f"    No valid timestamps found in {os.path.basename(csv_path)}")
        return None
    except Exception as e:
        print(f"  Warning: Could not extract cutoff from {csv_path}: {e}")
        return None


def load_existing_shas(csv_path: str, verbose: bool = True) -> Set[str]:
    """Load existing commit SHAs from a commits CSV file."""
    if not os.path.exists(csv_path):
        if verbose:
            print(f"    No existing commits file found: {os.path.basename(csv_path)}")
        return set()

    try:
        df = pd.read_csv(csv_path, usecols=['sha'])
        shas = set(df['sha'].dropna().astype(str))
        if verbose:
            print(f"    Loaded {len(shas)} existing commit SHAs from {os.path.basename(csv_path)}")
        return shas
    except Exception as e:
        print(f"  Warning: Could not load existing SHAs from {csv_path}: {e}")
        return set()


def get_primary_key(file_type: str) -> Tuple[List[str], bool]:
    """
    Return (key_columns, upsert_flag) for a given file type.
    upsert=True means existing records should be replaced if key matches.
    """
    key_map = {
        'commits': (['repo', 'sha'], False),
        'pull_requests': (['repo', 'number'], True),
        'issues': (['repo', 'number'], True),
        'pr_comments': (['repo', 'pull_number', 'user', 'created_at'], False),
        'issue_comments': (['repo', 'issue_number', 'user.login', 'created_at'], False),
    }
    return key_map.get(file_type, (['repo'], False))


def merge_csv(main_file: str, delta_file: str, key_columns: List[str], upsert: bool = False):
    """
    Merge delta CSV into main CSV with deduplication.
    If upsert=True, existing records with matching keys are replaced by delta records.
    """
    if os.path.exists(main_file):
        existing = pd.read_csv(main_file)
        existing_count = len(existing)
    else:
        existing = pd.DataFrame()
        existing_count = 0

    delta = pd.read_csv(delta_file)
    delta_count = len(delta)

    if delta.empty:
        print(f"    Delta is empty, nothing to merge")
        return

    if existing.empty:
        delta.to_csv(main_file, index=False)
        print(f"    Created new file with {delta_count} records")
        return

    replaced_count = 0
    if upsert and key_columns:
        # Remove old versions of records that appear in delta
        # Create a composite key for comparison
        existing_keys = existing[key_columns].astype(str).agg('|'.join, axis=1)
        delta_keys = delta[key_columns].astype(str).agg('|'.join, axis=1)
        mask = existing_keys.isin(delta_keys)
        replaced_count = mask.sum()
        existing = existing[~mask]

    combined = pd.concat([existing, delta], ignore_index=True)

    before_dedup = len(combined)
    if key_columns:
        combined = combined.drop_duplicates(subset=key_columns, keep='last')
    after_dedup = len(combined)
    deduped_count = before_dedup - after_dedup

    print(f"    Merged: {existing_count} existing + {delta_count} delta = {after_dedup} total", end="")
    if replaced_count > 0:
        print(f" ({replaced_count} updated)", end="")
    if deduped_count > 0:
        print(f" ({deduped_count} duplicates removed)", end="")
    print()

    combined.to_csv(main_file, index=False)


def merge_all(staging_folder: str, output_folder: str):
    """Merge all staged files from staging_folder into output_folder."""
    print("\n" + "=" * 60)
    print("[Merge Pass] Merging staged data into main folder...")
    print(f"  From: {staging_folder}/")
    print(f"  To:   {output_folder}/")
    print("=" * 60)

    staged_files = glob.glob(os.path.join(staging_folder, "*.csv"))
    if not staged_files:
        print("\nNo staged files found to merge.")
        print("\n" + "=" * 60)
        print("[Merge Pass] Complete (nothing to do)")
        print("=" * 60)
        return

    print(f"\nFound {len(staged_files)} staged file(s) to process:")

    for staged_file in sorted(staged_files):
        basename = os.path.basename(staged_file)
        main_file = os.path.join(output_folder, basename)

        # Determine file type and merge strategy
        file_type = None
        for ft in ['commits', 'pull_requests', 'issues', 'pr_comments', 'issue_comments']:
            if f"_{ft}.csv" in basename:
                file_type = ft
                break

        if file_type:
            # Data file - merge with dedup/upsert
            key_columns, upsert = get_primary_key(file_type)

            if os.path.exists(main_file):
                # Merge staged into existing
                strategy = "upsert" if upsert else "append"
                print(f"\n  [{strategy}] {basename}")
                merge_csv(main_file, staged_file, key_columns, upsert)
            else:
                # No existing file - just copy
                print(f"\n  [new] {basename}")
                try:
                    df = pd.read_csv(staged_file)
                    print(f"    Creating new file with {len(df)} records")
                except:
                    pass
                shutil.copy2(staged_file, main_file)
        else:
            # Other files (repositories.csv, contributors, branches) - replace
            print(f"\n  [replace] {basename}")
            try:
                df = pd.read_csv(staged_file)
                record_count = len(df)
                print(f"    Replacing with {record_count} records")
            except:
                pass
            shutil.copy2(staged_file, main_file)

        # Remove staged file after successful merge
        os.remove(staged_file)
        print(f"    Removed staged file")

    # Clean up empty staging folder (optional)
    remaining = os.listdir(staging_folder)
    if not remaining:
        print(f"\n  Staging folder is empty")
    else:
        print(f"\n  Remaining in staging folder: {len(remaining)} item(s)")

    print("\n" + "=" * 60)
    print("[Merge Pass] Complete!")
    print("=" * 60)

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

def get_branches_sorted_by_date(repo_path: str) -> List[Tuple[str, Optional[datetime]]]:
    """
    Get all branches sorted by last commit date (newest first).
    Returns list of (branch_name, last_commit_date) tuples.
    """
    # Get branches with their last commit date, sorted by date descending
    out = run([
        "git", "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)|%(committerdate:iso-strict)",
        "refs/heads", "refs/remotes/origin"
    ], cwd=repo_path)

    branches = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 1)
        branch_name = parts[0].strip()
        if branch_name == "origin/HEAD":
            continue

        commit_date = None
        if len(parts) == 2 and parts[1].strip():
            try:
                commit_date = datetime.fromisoformat(parts[1].strip())
                if commit_date.tzinfo is None:
                    commit_date = commit_date.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        branches.append((branch_name, commit_date))

    return branches


def get_oldest_commit_date(repo_path: str, commit_shas: List[str]) -> Optional[datetime]:
    """Get the date of the oldest commit in the list."""
    if not commit_shas:
        return None

    # Get dates for all commits in one call
    cmd = ["git", "log", "--format=%H|%ad", "--date=iso-strict", "--no-walk"] + commit_shas[:1000]  # Limit to avoid arg list too long
    out = run(cmd, cwd=repo_path)

    oldest_date = None
    for line in out.splitlines():
        if "|" not in line:
            continue
        _, date_str = line.split("|", 1)
        try:
            commit_date = datetime.fromisoformat(date_str.strip())
            if commit_date.tzinfo is None:
                commit_date = commit_date.replace(tzinfo=timezone.utc)
            if oldest_date is None or commit_date < oldest_date:
                oldest_date = commit_date
        except ValueError:
            continue

    return oldest_date


def get_branches_for_commits_incremental(
    repo_path: str,
    new_commit_shas: Set[str],
) -> Dict[str, List[str]]:
    """
    Get branch mapping for new commits by processing branches in date order.

    Strategy:
    - Process branches from newest to oldest (most recent activity first)
    - For each branch, run git rev-list and collect branch info only for new commits
    - Stop when branch's last commit is older than our oldest new commit
      (such branches cannot contain any new commits)
    """
    if not new_commit_shas:
        return {}

    # Get branches sorted by date
    print(f"  Getting branches sorted by activity date...")
    branches = get_branches_sorted_by_date(repo_path)
    total_branches = len(branches)

    # Get oldest new commit date for cutoff
    print(f"  Finding oldest new commit date...")
    oldest_new_commit_date = get_oldest_commit_date(repo_path, list(new_commit_shas)[:1000])

    # Find cutoff index - first branch older than our oldest new commit
    branches_to_process = total_branches
    if oldest_new_commit_date:
        print(f"  Oldest new commit: {oldest_new_commit_date.isoformat()}")
        for i, (branch_name, branch_date) in enumerate(branches):
            if branch_date and branch_date < oldest_new_commit_date:
                branches_to_process = i
                break

    branches_skipped = total_branches - branches_to_process
    print(f"  Will process {branches_to_process} branches (skipping {branches_skipped} older branches)")

    sha_to_branches: Dict[str, List[str]] = defaultdict(list)
    commits_found = 0

    BRANCH_LOG_INTERVAL = 50

    for i in range(branches_to_process):
        branch_name, branch_date = branches[i]

        if i > 0 and i % BRANCH_LOG_INTERVAL == 0:
            print(f"    Processing branches: {i}/{branches_to_process} ({commits_found} commit-branch mappings)", flush=True)

        # Get commits for this branch
        try:
            out = run(["git", "rev-list", branch_name], cwd=repo_path)
            for sha in out.splitlines():
                if sha and sha in new_commit_shas:
                    sha_to_branches[sha].append(branch_name)
                    commits_found += 1
        except Exception as e:
            print(f"    Warning: failed to process branch {branch_name}: {e}")
            continue

    print(f"    Processing branches: {branches_to_process}/{branches_to_process} - done")
    print(f"  Commits with branch info: {len(sha_to_branches)}/{len(new_commit_shas)}")

    return dict(sha_to_branches)


def get_all_commits_and_count_branches(repo_path: str) -> Tuple[List[str], int]:
    """Get all commits reachable from any ref, and count of branches."""
    print(f"  Getting all commits (git rev-list --all)...")
    out = run(["git", "rev-list", "--all"], cwd=repo_path)
    all_unique = [s for s in out.splitlines() if s]

    # Count branches
    branch_out = run([
        "git", "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads", "refs/remotes/origin"
    ], cwd=repo_path)
    branches = [b for b in branch_out.splitlines() if b.strip() and b.strip() != "origin/HEAD"]

    print(f"  Found {len(all_unique)} commits across {len(branches)} branches")
    return all_unique, len(branches)


def commits_by_branch(repo_path: str, branches: Iterable[str]) -> Tuple[Dict[str, List[str]], List[str]]:
    """
    Build a mapping from commit SHA -> list of branch names that contain it,
    and return a deduped list of all commits reachable from all refs (git rev-list --all).
    """
    branches_list = list(branches)
    total_branches = len(branches_list)
    print(f"  Building branch-commit mapping for {total_branches} branches...")

    sha_to_branches: Dict[str, List[str]] = defaultdict(list)

    # Process branches in batches for progress reporting
    BRANCH_LOG_INTERVAL = 50
    for i, br in enumerate(branches_list):
        if i > 0 and i % BRANCH_LOG_INTERVAL == 0:
            print(f"    Processing branches: {i}/{total_branches}", flush=True)

        out = run(["git", "rev-list", br], cwd=repo_path)
        for sha in out.splitlines():
            if sha:
                sha_to_branches[sha].append(br)

    print(f"    Processing branches: {total_branches}/{total_branches} - done")

    # Use the full all-refs traversal order; do NOT restrict to seen SHAs.
    print(f"  Getting all commits (git rev-list --all)...")
    all_unique = run(["git", "rev-list", "--all"], cwd=repo_path).splitlines()
    # keep only real SHAs (defensive, though rev-list emits SHAs)
    all_unique = [s for s in all_unique if s]
    print(f"  Found {len(all_unique)} total commits across all refs")
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
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='Collect GitHub organization data with incremental support')
    parser.add_argument('--skip-merge', action='store_true',
                        help='Skip the merge pass (staged data stays in gh_outputs_current/, main data unchanged)')
    parser.add_argument('--full-fetch', action='store_true',
                        help='Force full fetch, ignore existing data')
    parser.add_argument('--since', type=str, default=None,
                        help='Override auto-detected cutoff with explicit date (ISO format, e.g., 2025-01-15)')
    args = parser.parse_args()

    # Parse --since date if provided
    global_cutoff = None
    if args.since:
        try:
            global_cutoff = datetime.fromisoformat(args.since)
            if global_cutoff.tzinfo is None:
                global_cutoff = global_cutoff.replace(tzinfo=timezone.utc)
            print(f"Using global cutoff date: {global_cutoff.isoformat()}")
        except ValueError as e:
            print(f"Error: Invalid --since date format: {e}")
            sys.exit(1)

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

    # Main output folder (existing/merged data)
    output_folder = "gh_outputs"
    os.makedirs(output_folder, exist_ok=True)

    # Staging folder (current run's collected data)
    staging_folder = "gh_outputs_current"
    os.makedirs(staging_folder, exist_ok=True)

    print(f"\nOutput configuration:")
    print(f"  Main data folder:    {output_folder}/")
    print(f"  Staging folder:      {staging_folder}/")
    print(f"  Skip merge:          {args.skip_merge}")
    print()

    local_root = os.path.join(output_folder, "local_repos")
    os.makedirs(local_root, exist_ok=True)

    # repositories.csv - write to staging
    repo_rows = [{
        "name": repo.name,
        "full_name": repo.full_name,
        "private": repo.private,
        "created_at": repo.created_at,
        "default_branch": repo.default_branch
    } for repo in repos]
    pd.DataFrame(repo_rows).to_csv(os.path.join(staging_folder, "repositories.csv"), index=False)
    print(f"[Repositories] Written {len(repo_rows)} repos to staging")

    # contributors.csv (per-repo) - always re-fetch, write to staging
    for repo in repos:
        # Write to staging folder
        contributors_staging_file = os.path.join(staging_folder, f"{repo.name}_contributors.csv")

        # Skip if already staged (resumable)
        if os.path.exists(contributors_staging_file):
            print(f"[Contributors] {repo.name} - already staged, skipping")
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
        pd.DataFrame(contributors_rows).to_csv(contributors_staging_file, index=False)
        print(f"  Staged {len(contributors_rows)} contributors")

    # branches.csv (per-repo) - always re-fetch, write to staging
    for repo in repos:
        # Write to staging folder
        branches_staging_file = os.path.join(staging_folder, f"{repo.name}_branches.csv")

        # Skip if already staged (resumable)
        if os.path.exists(branches_staging_file):
            print(f"[Branches] {repo.name} - already staged, skipping")
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
        pd.DataFrame(branches_rows).to_csv(branches_staging_file, index=False)
        print(f"  Staged {len(branches_rows)} branches")

    # commits.csv (per-repo, local git, de-duped, ALL refs) - with incremental support
    for repo in repos:
        # Check existing data in main folder for cutoffs
        commits_main_file = os.path.join(output_folder, f"{repo.name}_commits.csv")
        # Write to staging folder
        commits_staging_file = os.path.join(staging_folder, f"{repo.name}_commits.csv")

        # Skip if already staged (resumable)
        if os.path.exists(commits_staging_file):
            print(f"[Commits: local git] {repo.name} - already staged, skipping")
            continue

        # Determine if we're doing incremental or full fetch
        existing_shas: Set[str] = set()
        is_incremental = False

        print(f"[Commits: local git] {repo.name}")
        if not args.full_fetch and os.path.exists(commits_main_file):
            print(f"  Checking existing data in main folder...")
            existing_shas = load_existing_shas(commits_main_file)
            if existing_shas:
                is_incremental = True
                print(f"  -> INCREMENTAL MODE: will filter out {len(existing_shas)} existing commits")
            else:
                print(f"  -> FULL FETCH: existing file is empty or has no valid SHAs")
        else:
            if args.full_fetch:
                print(f"  -> FULL FETCH: --full-fetch flag specified")
            else:
                print(f"  -> FULL FETCH: no existing data found")

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

            # OPTIMIZATION: For incremental mode, get commits first, then use smart branch mapping
            if is_incremental:
                # Fast path: get all commits and branch count first
                all_commits, branch_count = get_all_commits_and_count_branches(repo_path)

                if not all_commits:
                    print(f"  no commits found in repository, skipping")
                    continue

                new_commits = [sha for sha in all_commits if sha not in existing_shas]
                new_commits_set = set(new_commits)
                print(f"  Found {len(new_commits)} new commits (out of {len(all_commits)} total, {branch_count} branches)")

                if not new_commits:
                    # Write empty file to staging to mark as processed
                    pd.DataFrame(columns=["repo", "sha", "author.name", "author.email", "commit.author.date",
                                         "commit.message", "branches", "issues_referenced", "additions",
                                         "deletions", "total_changes", "changed_files"]).to_csv(commits_staging_file, index=False)
                    print(f"  Staged empty delta (no new commits)")
                    continue

                commits_to_process = new_commits

                # Use incremental branch mapping: process branches by date, stop at old branches
                # This is efficient because new commits are likely on recently active branches
                branch_map = get_branches_for_commits_incremental(repo_path, new_commits_set)
            else:
                # Full fetch: need complete branch mapping
                branch_map, all_commits = commits_by_branch(repo_path, local_branches)

                # Double-check: if no commits found, skip
                if not all_commits:
                    print(f"  no commits found in repository, skipping")
                    continue

                commits_to_process = all_commits

            # Batch process commit data in chunks
            print(f"  processing {len(commits_to_process)} commits in batches of {COMMIT_BATCH_SIZE}...")
            commit_data = get_commit_data_chunked(repo_path, commits_to_process)

            individual_calls = 0
            for i, sha in enumerate(commits_to_process, 1):
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
                            print(f"    Expected total commits: {len(commits_to_process)}")
                            # Check if this SHA is in the beginning or end of the list
                            sha_pos = commits_to_process.index(sha) if sha in commits_to_process else -1
                            print(f"    SHA position in list: {sha_pos}")

                        author_name, author_email, author_date, subject = commit_header(repo_path, sha)
                        changed_files = parse_name_status(repo_path, sha)
                        total_adds, total_dels, _ = parse_numstat(repo_path, sha)

                    # Running counter with carriage return
                    print(f"\r  processed: {i}/{len(commits_to_process)} (individual: {individual_calls})", end="", flush=True)

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

            # Save to staging folder
            pd.DataFrame(commits_rows).to_csv(commits_staging_file, index=False)
            mode_str = "delta" if is_incremental else "full"
            print(f"  Staged {len(commits_rows)} commits ({mode_str})")
        except Exception as e:
            print(f"  failed on repo {repo.name}: {e}")
            continue

    # pull_requests.csv and pr_comments.csv (per-repo, combined to avoid duplicate API calls) - with incremental support
    for repo in repos:
        # Check existing data in main folder for cutoffs
        pulls_main_file = os.path.join(output_folder, f"{repo.name}_pull_requests.csv")
        # Write to staging folder
        pulls_staging_file = os.path.join(staging_folder, f"{repo.name}_pull_requests.csv")
        pr_comments_staging_file = os.path.join(staging_folder, f"{repo.name}_pr_comments.csv")

        # Skip if already staged (resumable)
        if os.path.exists(pulls_staging_file) and os.path.exists(pr_comments_staging_file):
            print(f"[Pull Requests & Comments] {repo.name} - already staged, skipping")
            continue

        # Determine cutoff for incremental fetch
        cutoff_date = None
        is_incremental = False

        print(f"[Pull Requests & Comments] {repo.name}")
        if not args.full_fetch and os.path.exists(pulls_main_file):
            # Use global cutoff if provided, otherwise extract from existing data
            if global_cutoff:
                cutoff_date = global_cutoff
                print(f"  Using global cutoff override: {cutoff_date.isoformat()}")
            else:
                # Use updated_at for cutoff (PRs can be updated after creation)
                print(f"  Checking existing data in main folder for cutoff...")
                cutoff_date = get_cutoff_from_csv(pulls_main_file, 'updated_at', fallback_column='merged_at')

            if cutoff_date:
                is_incremental = True
                print(f"  -> INCREMENTAL MODE: will fetch PRs updated after {cutoff_date.isoformat()}")
            else:
                print(f"  -> FULL FETCH: no valid cutoff found in existing data")
        else:
            if args.full_fetch:
                print(f"  -> FULL FETCH: --full-fetch flag specified")
            else:
                print(f"  -> FULL FETCH: no existing data found")

        pulls_rows = []
        pr_comments_rows = []

        # Fetch PRs sorted by updated_at descending for early-break optimization
        prs = retry_network_operation(lambda: list(repo.get_pulls(state='all', sort='updated', direction='desc')))
        total_prs = len(prs)

        # Find cutoff index if incremental
        prs_to_process = total_prs
        if is_incremental and cutoff_date:
            for idx, pr in enumerate(prs):
                pr_updated = pr.updated_at
                if pr_updated and pr_updated.tzinfo is None:
                    pr_updated = pr_updated.replace(tzinfo=timezone.utc)
                if pr_updated and pr_updated < cutoff_date:
                    prs_to_process = idx
                    break
            print(f"  Found {total_prs} PRs total, will process {prs_to_process} (skipping {total_prs - prs_to_process} older)")
        else:
            print(f"  Found {total_prs} PRs to process")

        for i in range(prs_to_process):
            pr = prs[i]

            if i > 0 and i % 50 == 0:
                print(f"\r  Processing PRs: {i}/{prs_to_process}...", end="", flush=True)

            # Collect PR data (now including updated_at for future incremental runs)
            pulls_rows.append({
                "repo": repo.full_name,
                "number": pr.number,
                "user.login": pr.user.login if pr.user else None,
                "created_at": pr.created_at,
                "updated_at": pr.updated_at,
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

        print(f"\r  Processing PRs: {prs_to_process}/{prs_to_process} - done")

        # Save to staging folder
        pd.DataFrame(pulls_rows).to_csv(pulls_staging_file, index=False)
        pd.DataFrame(pr_comments_rows).to_csv(pr_comments_staging_file, index=False)
        mode_str = "delta" if is_incremental else "full"
        print(f"  Staged {len(pulls_rows)} PRs and {len(pr_comments_rows)} comments ({mode_str})")

    # issues.csv and issue_comments.csv (per-repo, combined to avoid duplicate API calls) - with incremental support
    for repo in repos:
        # Check existing data in main folder for cutoffs
        issues_main_file = os.path.join(output_folder, f"{repo.name}_issues.csv")
        # Write to staging folder
        issues_staging_file = os.path.join(staging_folder, f"{repo.name}_issues.csv")
        issue_comments_staging_file = os.path.join(staging_folder, f"{repo.name}_issue_comments.csv")

        # Skip if already staged (resumable)
        if os.path.exists(issues_staging_file) and os.path.exists(issue_comments_staging_file):
            print(f"[Issues & Comments] {repo.name} - already staged, skipping")
            continue

        # Determine cutoff for incremental fetch
        cutoff_date = None
        is_incremental = False

        print(f"[Issues & Comments] {repo.name}")
        if not args.full_fetch and os.path.exists(issues_main_file):
            # Use global cutoff if provided, otherwise extract from existing data
            if global_cutoff:
                cutoff_date = global_cutoff
                print(f"  Using global cutoff override: {cutoff_date.isoformat()}")
            else:
                # Use updated_at for cutoff (issues can be updated after creation)
                print(f"  Checking existing data in main folder for cutoff...")
                cutoff_date = get_cutoff_from_csv(issues_main_file, 'updated_at', fallback_column='created_at')

            if cutoff_date:
                is_incremental = True
                print(f"  -> INCREMENTAL MODE: will fetch issues updated after {cutoff_date.isoformat()}")
                print(f"  -> Using GitHub API 'since' parameter for server-side filtering")
            else:
                print(f"  -> FULL FETCH: no valid cutoff found in existing data")
        else:
            if args.full_fetch:
                print(f"  -> FULL FETCH: --full-fetch flag specified")
            else:
                print(f"  -> FULL FETCH: no existing data found")

        issues_rows = []
        issue_comments_rows = []

        # Fetch issues - use 'since' parameter for API-level filtering when incremental
        if is_incremental and cutoff_date:
            # PyGithub's get_issues supports 'since' parameter for filtering by updated_at
            print(f"  Fetching issues from GitHub API with since={cutoff_date.isoformat()}...")
            issues = retry_network_operation(lambda: list(repo.get_issues(state='all', since=cutoff_date, sort='updated', direction='desc')))
        else:
            print(f"  Fetching all issues from GitHub API...")
            issues = retry_network_operation(lambda: list(repo.get_issues(state='all')))

        total_issues = len(issues)
        print(f"  Found {total_issues} issues to process")

        for i, issue in enumerate(issues):
            if i > 0 and i % 50 == 0:
                print(f"\r  Processing issues: {i}/{total_issues}...", end="", flush=True)

            # Collect issue data (now including updated_at for future incremental runs)
            issues_rows.append({
                "repo": repo.full_name,
                "number": issue.number,
                "title": issue.title,
                "user.login": issue.user.login if issue.user else None,
                "assignees": [a.login for a in issue.assignees],
                "comments_count": issue.comments,
                "state": issue.state,
                "created_at": issue.created_at,
                "updated_at": issue.updated_at,
                "closed_at": issue.closed_at
            })

            # Collect issue comments
            comments = retry_network_operation(lambda: list(issue.get_comments()))
            for comment in comments:
                issue_comments_rows.append({
                    "repo": repo.full_name,
                    "issue_number": issue.number,
                    "user.login": comment.user.login if comment.user else None,
                    "created_at": comment.created_at
                })

        print(f"\r  Processing issues: {total_issues}/{total_issues} - done")

        # Save to staging folder
        pd.DataFrame(issues_rows).to_csv(issues_staging_file, index=False)
        pd.DataFrame(issue_comments_rows).to_csv(issue_comments_staging_file, index=False)
        mode_str = "delta" if is_incremental else "full"
        print(f"  Staged {len(issues_rows)} issues and {len(issue_comments_rows)} comments ({mode_str})")

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

    # Merge pass - merge staged files into main folder
    if not args.skip_merge:
        merge_all(staging_folder, output_folder)
    else:
        print("\n" + "=" * 60)
        print("[Merge Pass] Skipped (--skip-merge flag)")
        print(f"  Staged data remains in: {staging_folder}/")
        print(f"  Main data unchanged in: {output_folder}/")
        print("  To retry: delete gh_outputs_current/ and re-run")
        print("  To merge: re-run without --skip-merge")
        print("=" * 60)

    # Print elapsed time
    end_time = time.time()
    elapsed_seconds = int(end_time - start_time)
    elapsed_minutes = elapsed_seconds // 60
    elapsed_seconds = elapsed_seconds % 60
    print(f"\nElapsed time: {elapsed_minutes}m {elapsed_seconds}s")

if __name__ == "__main__":
    main()
