#!/usr/bin/env python3
from github import Github
import os
import subprocess
import time
import json
import sys
import glob
import shutil
import argparse
import pandas as pd
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# Import from extractor_monthly for shared utilities
from extractor_monthly import run, ensure_local_clone, GitCommitAnalyzer

# -------------------------
# Incremental Helper Functions
# -------------------------

def get_week_key_from_date(commit_date_str: str) -> str:
    """Derive ISO week key (YYYY-Www) from commit_date string."""
    dt = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def get_affected_weeks_from_delta(staging_folder: str, repo_name: str) -> Dict[str, List[Tuple[str, datetime]]]:
    """
    Read staged commits and determine which ISO weeks are affected.
    Returns: {week_key: [(sha, commit_date), ...]} - all commits per week, sorted newest first.
    This allows fallback to alternate commits if the latest one is not in local clone.
    """
    commits_file = os.path.join(staging_folder, f"{repo_name}_commits.csv")
    if not os.path.exists(commits_file):
        return {}

    try:
        df = pd.read_csv(commits_file)
    except Exception as e:
        print(f"[deps] Warning: Could not read {commits_file}: {e}")
        return {}

    if df.empty or 'sha' not in df.columns or 'commit.author.date' not in df.columns:
        return {}

    # Collect ALL commits per week
    affected_weeks: Dict[str, List[Tuple[str, datetime]]] = {}

    for _, row in df.iterrows():
        sha = row['sha']
        try:
            commit_date = pd.to_datetime(row['commit.author.date']).to_pydatetime()
            iso_year, iso_week, _ = commit_date.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"

            if week_key not in affected_weeks:
                affected_weeks[week_key] = []
            affected_weeks[week_key].append((sha, commit_date))
        except Exception as e:
            print(f"[deps] Warning: Could not parse date for commit {sha}: {e}")
            continue

    # Sort each week's commits by date descending (newest first)
    for week_key in affected_weeks:
        affected_weeks[week_key].sort(key=lambda x: x[1], reverse=True)

    return affected_weeks


def load_existing_deps(main_folder: str, repo_name: str) -> Dict[str, Tuple[str, datetime]]:
    """
    Load existing deps_weekly.json and extract week -> (sha, commit_date) mapping.
    Returns: {week_key: (sha, commit_date)} for weeks already analyzed.
    """
    deps_file = os.path.join(main_folder, f"{repo_name}_deps_weekly.json")
    if not os.path.exists(deps_file):
        return {}

    try:
        with open(deps_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"[deps] Warning: Could not load {deps_file}: {e}")
        return {}

    existing_weeks: Dict[str, Tuple[str, datetime]] = {}
    for entry in data:
        sha = entry.get('sha')
        commit_date_str = entry.get('commit_date')
        if not sha or not commit_date_str:
            continue

        try:
            commit_date = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))
            iso_year, iso_week, _ = commit_date.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            existing_weeks[week_key] = (sha, commit_date)
        except Exception:
            continue

    return existing_weeks


def get_weeks_to_analyze(
    affected_weeks: Dict[str, List[Tuple[str, datetime]]],
    existing_weeks: Dict[str, Tuple[str, datetime]],
) -> Dict[str, List[Tuple[str, datetime]]]:
    """
    Determine which weeks need (re-)analysis.
    Rules:
    1. New week (in affected, not in existing): Analyze
    2. Updated week (delta has NEWER commit by date): Re-analyze
    3. Unchanged week (existing commit is same or newer): Skip

    Returns all candidate commits per week (sorted newest first) so we can
    fallback to alternates if the latest commit is not in local clone.
    """
    weeks_to_analyze: Dict[str, List[Tuple[str, datetime]]] = {}

    for week_key, commits in affected_weeks.items():
        if not commits:
            continue
        # Latest commit is first in list
        delta_sha, delta_date = commits[0]

        if week_key not in existing_weeks:
            weeks_to_analyze[week_key] = commits
            print(f"[deps] Week {week_key}: NEW (sha={delta_sha[:8]}, {len(commits)} candidate(s))")
        else:
            existing_sha, existing_date = existing_weeks[week_key]
            if delta_date > existing_date:
                weeks_to_analyze[week_key] = commits
                print(f"[deps] Week {week_key}: UPDATE (newer commit {delta_sha[:8]} > {existing_sha[:8]}, {len(commits)} candidate(s))")
            else:
                print(f"[deps] Week {week_key}: SKIP (existing {existing_sha[:8]} is up-to-date)")

    return weeks_to_analyze


def backfill_commit_dates(repo_path: str, deps_file: str) -> bool:
    """
    Add commit_date to existing deps entries that don't have it.
    Returns True if any entries were modified.
    """
    if not os.path.exists(deps_file):
        return False

    try:
        with open(deps_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return False

    modified = False
    for entry in data:
        if 'commit_date' not in entry:
            sha = entry.get('sha')
            if sha:
                try:
                    date_str = run(["git", "log", "-1", "--format=%aI", sha], cwd=repo_path).strip()
                    entry['commit_date'] = date_str
                    modified = True
                except Exception as e:
                    print(f"[deps] Warning: Could not get date for {sha}: {e}")

    if modified:
        with open(deps_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        count = len([e for e in data if 'commit_date' in e])
        print(f"[deps] Backfilled commit_date for {count} entries in {deps_file}")

    return modified


def analyze_weeks(
    analyzer: GitCommitAnalyzer,
    weeks_to_analyze: Dict[str, List[Tuple[str, datetime]]],
) -> List[Dict]:
    """
    Analyze specific commits for affected weeks.
    Tries multiple commits per week if the first one is not in local clone.
    """
    results = []
    total = len(weeks_to_analyze)
    skipped_weeks = 0

    for i, (week_key, commits) in enumerate(weeks_to_analyze.items(), 1):
        analyzed = False

        for attempt, (sha, commit_date) in enumerate(commits):
            if attempt == 0:
                print(f"[deps] Analyzing {week_key} ({i}/{total}, sha={sha[:8]})...")
            else:
                print(f"[deps]   Trying alternate commit {attempt+1}/{len(commits)} (sha={sha[:8]})...")

            try:
                result = analyzer.analyze_commit(sha)
                results.append({
                    "sha": result.sha,
                    "commit_date": commit_date.isoformat(),
                    "files": result.files,
                    "typescript_imports": result.typescript_imports,
                    "javascript_imports": result.javascript_imports,
                    "swift_imports": result.swift_imports,
                })
                analyzed = True
                break  # Success, move to next week
            except RuntimeError as e:
                # Commit doesn't exist in local clone (force-pushed, deleted branch, etc.)
                print(f"[deps]   Commit {sha[:8]} not in local clone, trying next...")
                continue

        if not analyzed:
            print(f"[deps] WARNING: Could not analyze {week_key} - no valid commits found in local clone")
            skipped_weeks += 1

    if skipped_weeks:
        print(f"[deps] Skipped {skipped_weeks} week(s) - no commits available in local clone")

    return results


def save_deps_delta(staging_folder: str, repo_name: str, results: List[Dict]) -> None:
    """Write incremental deps to staging folder."""
    if not results:
        print(f"[deps] No new/updated weeks for {repo_name}, skipping write")
        return

    os.makedirs(staging_folder, exist_ok=True)
    delta_file = os.path.join(staging_folder, f"{repo_name}_deps_weekly_delta.json")
    with open(delta_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"[deps] Wrote {len(results)} week(s) to {delta_file}")


def merge_deps(staging_folder: str, main_folder: str, repo_name: str) -> None:
    """
    Merge deps delta into main deps file.
    Uses week derived from commit_date as primary key for upsert.
    """
    main_file = os.path.join(main_folder, f"{repo_name}_deps_weekly.json")
    delta_file = os.path.join(staging_folder, f"{repo_name}_deps_weekly_delta.json")

    if not os.path.exists(delta_file):
        return

    # Load existing
    existing_data = []
    if os.path.exists(main_file):
        try:
            with open(main_file, 'r', encoding='utf-8') as f:
                existing_data = json.load(f)
        except Exception:
            existing_data = []

    # Load delta
    with open(delta_file, 'r', encoding='utf-8') as f:
        delta_data = json.load(f)

    # Build week_key -> entry map (existing)
    existing_by_week = {}
    for entry in existing_data:
        if 'commit_date' in entry:
            week_key = get_week_key_from_date(entry['commit_date'])
            existing_by_week[week_key] = entry

    # Upsert delta entries
    for entry in delta_data:
        week_key = get_week_key_from_date(entry['commit_date'])
        existing_by_week[week_key] = entry

    # Sort by commit_date descending (newest first)
    merged = sorted(existing_by_week.values(), key=lambda x: x['commit_date'], reverse=True)

    # Write merged result
    os.makedirs(main_folder, exist_ok=True)
    with open(main_file, 'w', encoding='utf-8') as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    # Remove delta file
    os.remove(delta_file)
    print(f"[deps] Merged {len(delta_data)} week(s) into {main_file} (total: {len(merged)} weeks)")


def merge_all_deps(staging_folder: str, main_folder: str) -> None:
    """Merge all deps delta files from staging to main."""
    delta_pattern = os.path.join(staging_folder, "*_deps_weekly_delta.json")
    for delta_file in glob.glob(delta_pattern):
        basename = os.path.basename(delta_file)
        repo_name = basename.replace("_deps_weekly_delta.json", "")
        print(f"[deps] Merging deps for {repo_name}...")
        merge_deps(staging_folder, main_folder, repo_name)


def run_incremental_deps(
    repo_name: str,
    repo_url: str,
    staging_folder: str,
    main_folder: str,
    workdir: str = "deps_work",
    force: bool = False,
) -> None:
    """Run incremental dependency analysis for a single repo."""

    # Step 0: Skip if delta already exists (resumable)
    delta_file = os.path.join(staging_folder, f"{repo_name}_deps_weekly_delta.json")
    if os.path.exists(delta_file) and not force:
        print(f"[deps] Delta already exists for {repo_name}, skipping")
        return

    # Step 1: Get affected weeks from staged commits
    affected_weeks = get_affected_weeks_from_delta(staging_folder, repo_name)
    if not affected_weeks:
        print(f"[deps] No staged commits for {repo_name}, skipping dep analysis")
        return

    print(f"[deps] Found {len(affected_weeks)} affected week(s) in staged commits for {repo_name}")

    # Step 2: Load existing deps (and backfill commit_date if needed)
    deps_file = os.path.join(main_folder, f"{repo_name}_deps_weekly.json")
    if os.path.exists(deps_file):
        # Ensure repo is cloned for backfill
        token = os.environ.get("GITHUB_TOKEN")
        repo_path = ensure_local_clone(repo_url, workdir, token)
        backfill_commit_dates(repo_path, deps_file)

    existing_weeks = load_existing_deps(main_folder, repo_name)
    print(f"[deps] Existing deps cover {len(existing_weeks)} week(s) for {repo_name}")

    # Step 3: Determine what needs analysis
    weeks_to_analyze = get_weeks_to_analyze(affected_weeks, existing_weeks)
    if not weeks_to_analyze:
        print(f"[deps] All weeks up-to-date for {repo_name}")
        return

    print(f"[deps] {len(weeks_to_analyze)} week(s) need analysis for {repo_name}")

    # Step 4: Clone/fetch repo and analyze
    token = os.environ.get("GITHUB_TOKEN")
    # Use GitCommitAnalyzer which handles clone internally
    analyzer = GitCommitAnalyzer(repo_url, workdir=workdir)
    results = analyze_weeks(analyzer, weeks_to_analyze)

    # Step 5: Write to staging
    save_deps_delta(staging_folder, repo_name, results)


def run_full_deps(
    repo_name: str,
    repo_url: str,
    output_folder: str,
    workdir: str = "deps_work",
) -> None:
    """Run full dependency analysis for a single repo (non-incremental)."""
    out_file = os.path.join(output_folder, f"{repo_name}_deps_weekly.json")

    if os.path.exists(out_file):
        print(f"[deps] Output file already exists for {repo_name}, skipping: {out_file}")
        return

    print(f"[deps] Running full dep analysis for {repo_name}...")
    subprocess.check_call([
        sys.executable, "extractor_monthly.py", repo_url,
        "--weekly", "--out", str(out_file),
        "--workdir", workdir,
    ])


# -------------------------
# Main
# -------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Dependency extraction wrapper for GitHub repos."
    )
    parser.add_argument("--incremental", action="store_true",
                        help="Use incremental mode (analyze only weeks with new commits)")
    parser.add_argument("--skip-merge", action="store_true",
                        help="Skip merge pass (keep deltas in staging)")
    parser.add_argument("--full-fetch", action="store_true",
                        help="Force full re-analysis of all weeks")
    parser.add_argument("--force", action="store_true",
                        help="Re-analyze even if delta already exists (override skip-existing)")
    parser.add_argument("--staging", default="gh_outputs_current",
                        help="Staging folder for incremental data (default: gh_outputs_current)")
    parser.add_argument("--output", default="gh_outputs",
                        help="Output folder for main data (default: gh_outputs)")
    parser.add_argument("--workdir", default="deps_work",
                        help="Working directory for local clones")
    args = parser.parse_args()

    start_time = time.time()

    script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    os.chdir(script_dir)

    config_file = "config.json"
    with open(config_file, 'r') as f:
        config = json.load(f)

    GITHUB_TOKEN = config.get("GITHUB_TOKEN")
    ORG_NAME = config.get("ORG_NAME")

    # Set token in environment for ensure_local_clone
    if GITHUB_TOKEN:
        os.environ["GITHUB_TOKEN"] = GITHUB_TOKEN

    g = Github(GITHUB_TOKEN)
    org = g.get_organization(ORG_NAME)

    staging_folder = args.staging
    main_folder = args.output
    os.makedirs(main_folder, exist_ok=True)

    # Process repos
    for r in org.get_repos():
        if not r.private:
            continue

        repo_name = r.name
        repo_url = r.clone_url

        if args.incremental and not args.full_fetch:
            # Incremental mode: analyze only weeks with new commits
            run_incremental_deps(
                repo_name=repo_name,
                repo_url=repo_url,
                staging_folder=staging_folder,
                main_folder=main_folder,
                workdir=args.workdir,
                force=args.force,
            )
        else:
            # Full mode: run full analysis (original behavior)
            run_full_deps(
                repo_name=repo_name,
                repo_url=repo_url,
                output_folder=main_folder,
                workdir=args.workdir,
            )

    # Merge pass
    if args.incremental and not args.skip_merge:
        print("\n[deps] Running merge pass...")
        merge_all_deps(staging_folder, main_folder)

    elapsed = time.time() - start_time
    print(f"\n[deps] Completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
