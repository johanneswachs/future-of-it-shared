# Future of IT - Data Collection Scripts

Python scripts for collecting and analyzing data from GitHub organizations and Slack workspaces.

## Prerequisites

- Python 3.8+
- Install dependencies:
  ```bash
  pip install pandas PyGithub requests urllib3
  ```

## Configuration

Copy the example config and fill in your values:

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "GITHUB_TOKEN": "your-github-token",
  "ORG_NAME": "your-organization-name"
}
```

A fine-grained GitHub personal access token is recommended. It needs read access to the organization's repositories (including private ones).

## Typical Workflow

```bash
# 1. Collect GitHub data
python gh_trilingual_combined.py

# 2. Run dependency extraction across the org
python dependency_extractor_wrapper.py

# 3. (Optional) Process Slack export (exported seperately)
python slack_metadata.py
```

## Scripts

### `gh_trilingual_combined.py` - GitHub Data Collector

Collects repository metadata, commits, PRs, and issues from all private repos in a GitHub organization.

```bash
python gh_trilingual_combined.py
```

**Output:** CSV files in `gh_outputs/` -- per-repo commits, PRs, issues, and a combined `repositories.csv`.

### `dependency_extractor_wrapper.py` - Dependency Extractor

Runs dependency extraction across all private repos in the organization. Clones each repo locally and extracts import/dependency information from TypeScript, JavaScript, and Swift files, sampling one commit per ISO week.

```bash
python dependency_extractor_wrapper.py
```

**Output:** `{repo_name}_deps_weekly.json` files in `gh_outputs/`.

### `slack_metadata.py` - Slack Export Processor

Parses a Slack workspace export and produces a clean CSV of message metadata (user, timestamp, reactions, replies, threads).

Place the unzipped Slack export folder at `slack_inputs/` in the project root, then run:

```bash
python slack_metadata.py
```

**Output:** `slack_outputs/slack_export_clean.csv`

## Advanced: Incremental Runs

When you already have data from a previous collection, the scripts support incremental mode to fetch only what's new. This avoids re-downloading the entire history on every run.

### `gh_trilingual_combined.py`

By default, the script auto-detects existing data in `gh_outputs/` and only fetches commits/PRs/issues newer than what's already collected. New data is first staged in `gh_outputs_current/`, then merged into `gh_outputs/`.

```bash
# Force full re-collection (ignore existing data)
python gh_trilingual_combined.py --full-fetch

# Override the auto-detected cutoff with an explicit date
python gh_trilingual_combined.py --since 2025-01-15

# Collect without merging (staged data stays in gh_outputs_current/)
python gh_trilingual_combined.py --skip-merge

# Only merge previously staged data into main output (no API calls)
python gh_trilingual_combined.py --merge-only
```

### `dependency_extractor_wrapper.py`

In incremental mode, the wrapper reads staged commit data from `gh_outputs_current/` to determine which ISO weeks have new commits, and only re-analyzes those weeks.

```bash
# Incremental mode (only analyze weeks with new commits)
python dependency_extractor_wrapper.py --incremental

# Force re-analysis even if delta files already exist
python dependency_extractor_wrapper.py --incremental --force

# Collect deltas without merging them
python dependency_extractor_wrapper.py --incremental --skip-merge

# Only merge existing deltas into main output (no analysis)
python dependency_extractor_wrapper.py --merge-only

# Custom folders
python dependency_extractor_wrapper.py --staging gh_outputs_current --output gh_outputs --workdir deps_work
```
