#!/usr/bin/env python3
import os
import re
import json
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Set, Optional
from pathlib import Path
from datetime import datetime

# -------------------------
# Utilities (UTF-8 safe)
# -------------------------

def run(cmd: List[str], cwd: Optional[str] = None) -> str:
    """
    Run a subprocess and decode stdout as UTF-8 (replace invalid).
    Force Git to emit UTF-8 to avoid locale issues on Windows.
    """
    if cmd and cmd[0] == "git":
        cmd = ["git", "-c", "i18n.logOutputEncoding=UTF-8"] + cmd[1:]
    res = subprocess.run(cmd, cwd=cwd, capture_output=True)
    if res.returncode != 0:
        err = (res.stderr or b"").decode("utf-8", "replace")
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDERR: {err}")
    return (res.stdout or b"").decode("utf-8", "replace")

def ensure_local_clone(repo_url: str, dest_root: str, token: Optional[str]) -> str:
    """
    Clone or fetch a repo locally using HTTPS. If a token is provided, authenticate clone.
    Returns the local repo path.
    """
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    dest = os.path.join(dest_root, name)
    auth_url = repo_url
    if token and auth_url.startswith("https://"):
        auth_url = auth_url.replace("https://", f"https://{token}@")
    if os.path.isdir(os.path.join(dest, ".git")):
        run(["git", "remote", "set-url", "origin", auth_url], cwd=dest)
        run(["git", "fetch", "--all", "--prune"], cwd=dest)
    else:
        os.makedirs(dest_root, exist_ok=True)
        run(["git", "clone", "--no-tags", "--quiet", auth_url, dest])
        run(["git", "fetch", "--all", "--prune"], cwd=dest)
    return dest

# -------------------------
# Analyzer
# -------------------------

@dataclass
class CommitResult:
    sha: str
    files: List[str]
    typescript_imports: Dict[str, List[str]]
    javascript_imports: Dict[str, List[str]]
    swift_imports: Dict[str, List[str]]

class GitCommitAnalyzer:
    def __init__(self, repo_url: str, workdir: str = "deps_work"):
        self.repo_url = repo_url
        self.workdir = workdir
        self.token = os.environ.get("GITHUB_TOKEN")  # optional for private repos
        os.makedirs(self.workdir, exist_ok=True)
        self.repo_path = ensure_local_clone(repo_url, self.workdir, self.token)

    # ------------ commit enumeration ------------
    def list_all_commits(self, branch: Optional[str] = None) -> List[str]:
        if branch:
            out = run(["git", "rev-list", branch], cwd=self.repo_path)
        else:
            out = run(["git", "rev-list", "--all"], cwd=self.repo_path)
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def list_monthly_latest_commits(self, branch: Optional[str] = None) -> List[str]:
        """
        One commit per calendar month (YYYY-MM), selecting the latest commit in each month.
        Git prints newest first, so the first SHA we see for a month is the winner.
        """
        fmt = r"%H|%ad"
        if branch:
            log_out = run(["git", "log", branch, "--date=iso-strict", f"--pretty=format:{fmt}"], cwd=self.repo_path)
        else:
            log_out = run(["git", "log", "--all", "--date=iso-strict", f"--pretty=format:{fmt}"], cwd=self.repo_path)

        latest_by_month: Dict[str, str] = {}
        for line in log_out.splitlines():
            if "|" not in line:
                continue
            sha, ad = line.split("|", 1)
            sha = sha.strip(); ad = ad.strip()
            if not sha or not ad:
                continue
            # "YYYY-MM"
            month_key = ad[:7]
            if month_key not in latest_by_month:
                latest_by_month[month_key] = sha

        return list(latest_by_month.values())  # newest months first

    def list_weekly_latest_commits(self, branch: Optional[str] = None) -> List[str]:
        """
        One commit per ISO week (YYYY-Www), selecting the latest commit in each week.
        Git prints newest first, so first hit per week is the winner.
        """
        fmt = r"%H|%ad"
        if branch:
            log_out = run(["git", "log", branch, "--date=iso-strict", f"--pretty=format:{fmt}"], cwd=self.repo_path)
        else:
            log_out = run(["git", "log", "--all", "--date=iso-strict", f"--pretty=format:{fmt}"], cwd=self.repo_path)

        latest_by_week: Dict[str, str] = {}

        for line in log_out.splitlines():
            if "|" not in line:
                continue
            sha, ad = line.split("|", 1)
            sha = sha.strip(); ad = ad.strip()
            if not sha or not ad:
                continue
            # Parse ISO date with TZ; robust to trailing 'Z'
            try:
                ad_norm = ad.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ad_norm)
            except Exception:
                # Fallback: take first 10 chars YYYY-MM-DD (ignores time/tz)
                dt = datetime.strptime(ad[:10], "%Y-%m-%d")
            iso_year, iso_week, _ = dt.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            if week_key not in latest_by_week:
                latest_by_week[week_key] = sha

        return list(latest_by_week.values())  # newest weeks first

    # ------------ file helpers ------------
    def list_files_at_commit(self, sha: str) -> List[str]:
        out = run(["git", "ls-tree", "-r", "--name-only", sha], cwd=self.repo_path)
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def get_file_content_at_commit(self, sha: str, path: str) -> Optional[str]:
        try:
            out = run(["git", "show", f"{sha}:{path}"], cwd=self.repo_path)
            return out
        except Exception:
            return None

    # ------------ type guards ------------
    @staticmethod
    def is_typescript_file(p: str) -> bool:
        p = p.lower()
        return p.endswith(".ts") or p.endswith(".tsx")

    @staticmethod
    def is_javascript_file(p: str) -> bool:
        p = p.lower()
        return p.endswith(".js") or p.endswith(".jsx")

    @staticmethod
    def is_swift_file(p: str) -> bool:
        return p.lower().endswith(".swift")

    # ------------ import extractors ------------
    ts_import_re = re.compile(
        r"(?:(?:import\s+[^;]+?from\s+['\"]([^'\"]+)['\"])|(?:import\s+['\"]([^'\"]+)['\"]))",
        re.MULTILINE,
    )
    js_import_re = ts_import_re
    swift_import_re = re.compile(r"^\s*import\s+([A-Za-z0-9_\.]+)", re.MULTILINE)

    @classmethod
    def extract_typescript_imports(cls, text: str) -> Set[str]:
        imports: Set[str] = set()
        for m in cls.ts_import_re.finditer(text):
            mod = m.group(1) or m.group(2)
            if mod:
                imports.add(mod)
        return imports

    @classmethod
    def extract_javascript_imports(cls, text: str) -> Set[str]:
        imports: Set[str] = set()
        for m in cls.js_import_re.finditer(text):
            mod = m.group(1) or m.group(2)
            if mod:
                imports.add(mod)
        for m in re.finditer(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", text):
            imports.add(m.group(1))
        return imports

    @classmethod
    def extract_swift_imports(cls, text: str) -> Set[str]:
        return set(m.group(1) for m in cls.swift_import_re.finditer(text))

    # ------------ main analysis ------------
    def analyze_commit(self, sha: str) -> 'CommitResult':
        files = self.list_files_at_commit(sha)
        ts_imports: Dict[str, List[str]] = {}
        js_imports: Dict[str, List[str]] = {}
        swift_imports: Dict[str, List[str]] = {}

        for fp in files:
            try:
                if self.is_typescript_file(fp):
                    content = self.get_file_content_at_commit(sha, fp)
                    if content:
                        mods = self.extract_typescript_imports(content)
                        if mods:
                            ts_imports[fp] = sorted(mods)
                elif self.is_javascript_file(fp):
                    content = self.get_file_content_at_commit(sha, fp)
                    if content:
                        mods = self.extract_javascript_imports(content)
                        if mods:
                            js_imports[fp] = sorted(mods)
                elif self.is_swift_file(fp):
                    content = self.get_file_content_at_commit(sha, fp)
                    if content:
                        mods = self.extract_swift_imports(content)
                        if mods:
                            swift_imports[fp] = sorted(mods)
            except Exception:
                continue

        return CommitResult(
            sha=sha,
            files=files,
            typescript_imports=ts_imports,
            javascript_imports=js_imports,
            swift_imports=swift_imports,
        )

    def analyze_commits(self, commits: List[str]) -> List[Dict]:
        results: List[Dict] = []
        total = len(commits)
        for i, sha in enumerate(commits, 1):
            print(f"\r[deps] processed {i}/{total} commits...", end="", flush=True)
            res = self.analyze_commit(sha)
            results.append({
                "sha": res.sha,
                "files": res.files,
                "typescript_imports": res.typescript_imports,
                "javascript_imports": res.javascript_imports,
                "swift_imports": res.swift_imports,
            })
        print()  # New line after progress tracking
        return results

    @staticmethod
    def save_results(results: List[Dict], path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    def cleanup(self) -> None:
        pass

# -------------------------
# CLI
# -------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Local commit dependency analysis for TS/JS/Swift files.")
    parser.add_argument("repo", help="HTTPS clone URL")
    parser.add_argument("--out", default="deps.json", help="Output JSON file")
    parser.add_argument("--limit", type=int, default=None, help="Optional commit limit for testing")
    parser.add_argument("--weekly", action="store_true",
                        help="Analyze one commit per ISO week (latest in each week).")
    parser.add_argument("--monthly", action="store_true",
                        help="Analyze one commit per calendar month (latest in each month).")
    parser.add_argument("--branch", default=None,
                        help="Limit analysis to a single branch (e.g., 'main'). If omitted, use all refs.")
    parser.add_argument("--workdir", default="deps_work",
                        help="Working directory for local clones.")
    args = parser.parse_args()

    analyzer = GitCommitAnalyzer(args.repo, workdir=args.workdir)

    # Choose commits: weekly > monthly > all
    if args.weekly:
        selected_commits = analyzer.list_weekly_latest_commits(branch=args.branch)
    elif args.monthly:
        selected_commits = analyzer.list_monthly_latest_commits(branch=args.branch)
    else:
        selected_commits = analyzer.list_all_commits(branch=args.branch)

    if args.limit:
        selected_commits = selected_commits[:args.limit]

    print(f"[deps] Selected {len(selected_commits)} commit(s) "
          f"{'(weekly latest)' if args.weekly else '(monthly latest)' if args.monthly else '(all)'} "
          f"{'on ' + args.branch if args.branch else 'on all refs'}")

    results = analyzer.analyze_commits(selected_commits)
    GitCommitAnalyzer.save_results(results, args.out)
    print(f"Wrote {args.out} with {len(results)} commit entries.")
