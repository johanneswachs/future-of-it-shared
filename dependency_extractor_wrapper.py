from github import Github
import os, subprocess, pathlib
import time
import json
import sys

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

outdir = "gh_outputs"
os.makedirs(outdir, exist_ok=True)


for r in org.get_repos():
    if not r.private:     
        continue
    repo_url = r.clone_url
    out = os.path.join(outdir, f"{r.name}_deps_weekly.json")
    
    if os.path.exists(out):
        print(f"Output file already exists for {r.name}, skipping: {out}")
        continue
    
    subprocess.check_call([
        "python", "extractor_monthly.py", repo_url,
        "--weekly","--out", str(out),
    ])
