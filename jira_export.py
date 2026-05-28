"""
Jira Data Export Script
=======================
- Discovers all accessible projects
- Extracts core ticket data for each issue
- Retrieves "closed by" user from changelog
- Saves everything to a CSV file

Usage:
    pip install requests
    python jira_export.py

Configuration:
    Fill in the variables below, or load them from a .env file.
"""

import requests
import csv
import os
from datetime import datetime
from requests.auth import HTTPBasicAuth

JIRA_URL    = "https://cegdomain.atlassian.net"   # Jira base URL
EMAIL       = "you@email.com"                      # Jira account email
API_TOKEN   = "your_api_token_here"                # Jira API token
                                                   # → https://id.atlassian.com/manage-profile/security                                                   # → https://id.atlassian.com/manage-profile/security                                                  # → https://id.atlassian.com/manage-profile/security
# To export only specific projects, list their keys here:
# e.g. PROJECT_FILTER = ["ABC", "DEF"]
# Leave empty to export ALL accessible projects.
PROJECT_FILTER = []

OUTPUT_FILE = "jira_export.csv"
# ─────────────────────────────────────────────────────────────────────────────

auth    = HTTPBasicAuth(EMAIL, API_TOKEN)
headers = {"Accept": "application/json"}


def get_all_projects():
    """Fetches all accessible projects from Jira."""
    print("📋 Discovering projects...")
    projects = []
    start_at = 0
    max_results = 50

    while True:
        url = f"{JIRA_URL}/rest/api/3/project/search"
        params = {"startAt": start_at, "maxResults": max_results}
        resp = requests.get(url, auth=auth, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("values", [])
        projects.extend(batch)

        print(f"  {len(projects)} projects found so far...")

        if data.get("isLast", True) or len(batch) < max_results:
            break
        start_at += max_results

    print(f"✅ Total projects found: {len(projects)}\n")
    return projects


def get_issues_for_project(project_key):
    """Fetches all issues for a given project, with pagination."""
    issues = []
    start_at = 0
    max_results = 100

    while True:
        url = f"{JIRA_URL}/rest/api/3/search/jql"
        params = {
            "jql": f"project = {project_key} ORDER BY created ASC",
            "startAt": start_at,
            "maxResults": max_results,
            "fields": "summary,status,priority,issuetype,reporter,assignee,created,resolutiondate,resolution,updated,duedate,labels,parent,issuelinks,timetracking,subtasks"
        }
        resp = requests.get(url, auth=auth, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("issues", [])
        issues.extend(batch)

        total = data.get("total", 0)
        if len(issues) >= total or len(batch) < max_results:
            break
        start_at += max_results

    return issues


def get_closed_by(issue_key):
    """
    Scans the changelog to find who transitioned the issue
    to Done / Closed / Resolved status (i.e. who closed it).
    """
    url = f"{JIRA_URL}/rest/api/3/issue/{issue_key}/changelog"
    closed_statuses = {"done", "closed", "resolved"}
    closed_by = None
    closed_at = None

    start_at = 0
    max_results = 100

    while True:
        params = {"startAt": start_at, "maxResults": max_results}
        resp = requests.get(url, auth=auth, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

        for history in data.get("values", []):
            for item in history.get("items", []):
                if item.get("field") == "status":
                    to_status = (item.get("toString") or "").lower()
                    if to_status in closed_statuses:
                        author = history.get("author", {})
                        closed_by = author.get("emailAddress", "")
                        closed_at = history.get("created", "")

        if data.get("isLast", True):
            break
        start_at += max_results

    return closed_by, closed_at


def format_date(iso_string):
    """Converts an ISO datetime string to a readable format (YYYY-MM-DD HH:MM)."""
    if not iso_string:
        return ""
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_string


def format_issue_links(links):
    parts = []
    for link in links:
        link_type = link.get("type", {})
        if "outwardIssue" in link:
            parts.append(f"{link_type.get('outward', 'links to')} {link['outwardIssue']['key']}")
        elif "inwardIssue" in link:
            parts.append(f"{link_type.get('inward', 'linked from')} {link['inwardIssue']['key']}")
    return "; ".join(parts)


def extract_field(fields, *keys):
    """Safely extracts a nested field value from an issue's fields dict."""
    val = fields
    for key in keys:
        if not isinstance(val, dict):
            return ""
        val = val.get(key, "")
    return val or ""


def main():
    # 1. Discover projects
    all_projects = get_all_projects()

    if PROJECT_FILTER:
        projects = [p for p in all_projects if p["key"] in PROJECT_FILTER]
        print(f"🔍 Exporting {len(projects)} filtered project(s): {[p['key'] for p in projects]}\n")
    else:
        projects = all_projects
        print(f"🔍 Exporting all {len(projects)} project(s):\n")
        for p in projects:
            print(f"  [{p['key']}] {p['name']}")
        print()

    # 2. Write CSV
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as csvfile:
        fieldnames = [
            "Project Key", "Project Name",
            "Issue ID", "Summary",
            "Issue Type", "Status", "Priority",
            "Reporter", "Created", "Updated",
            "Assignee",
            "Closed By", "Closed At",
            "Resolution Date", "Resolution Type",
            "Due Date", "Labels", "Parent",
            "Issue Links",
            "Time Estimate", "Time Remaining", "Time Spent",
            "Subtasks"
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        total_issues = 0

        for project in projects:
            key  = project["key"]
            name = project["name"]
            print(f"⏳ [{key}] {name} – fetching issues...")

            issues = get_issues_for_project(key)
            print(f"   {len(issues)} issues found. Processing changelogs...")

            for i, issue in enumerate(issues, 1):
                issue_key = issue["key"]
                fields    = issue.get("fields", {})

                # Who closed the issue (from changelog)
                closed_by, closed_at = get_closed_by(issue_key)

                timetracking = fields.get("timetracking") or {}
                writer.writerow({
                    "Project Key":      key,
                    "Project Name":     name,
                    "Issue ID":         issue_key,
                    "Summary":          extract_field(fields, "summary"),
                    "Issue Type":       extract_field(fields, "issuetype", "name"),
                    "Status":           extract_field(fields, "status", "name"),
                    "Priority":         extract_field(fields, "priority", "name"),
                    "Reporter":         extract_field(fields, "reporter", "emailAddress"),
                    "Created":          format_date(extract_field(fields, "created")),
                    "Updated":          format_date(extract_field(fields, "updated")),
                    "Assignee":         extract_field(fields, "assignee", "emailAddress"),
                    "Closed By":        closed_by or "",
                    "Closed At":        format_date(closed_at) if closed_at else "",
                    "Resolution Date":  format_date(extract_field(fields, "resolutiondate")),
                    "Resolution Type":  extract_field(fields, "resolution", "name"),
                    "Due Date":         extract_field(fields, "duedate"),
                    "Labels":           ", ".join(fields.get("labels") or []),
                    "Parent":           extract_field(fields, "parent", "key"),
                    "Issue Links":      format_issue_links(fields.get("issuelinks") or []),
                    "Time Estimate":    timetracking.get("originalEstimate", ""),
                    "Time Remaining":   timetracking.get("remainingEstimate", ""),
                    "Time Spent":       timetracking.get("timeSpent", ""),
                    "Subtasks":         ", ".join(s["key"] for s in (fields.get("subtasks") or [])),
                })

                if i % 50 == 0:
                    print(f"   ...{i}/{len(issues)} processed")

            total_issues += len(issues)
            print(f"   ✅ [{key}] done.\n")

    print(f"🎉 Export complete! {total_issues} issues saved to → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()