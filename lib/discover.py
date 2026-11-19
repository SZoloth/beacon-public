"""Discover new workstreams by scanning local projects, vault manifest, and GitHub repos."""

import json
import os
import subprocess

from . import settings
from .ai import run_json_prompt


MANIFEST_PATH = settings.path("systems_manifest") or ""
PROJECTS_DIR = settings.path("projects_dir") or os.path.expanduser("~/Projects")


def load_manifest(path=MANIFEST_PATH):
    """Read systems-manifest.json, return {id: entry} for project-type entries."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)
    return {
        s["id"]: s
        for s in data.get("systems", [])
        if s.get("type") in ("project", "vault-project")
    }


def scan_local_projects(projects_dir=PROJECTS_DIR):
    """Scan the projects folder (paths.projects_dir, default ~/Projects), derive slug from dirname."""
    if not os.path.isdir(projects_dir):
        return []
    candidates = []
    for name in sorted(os.listdir(projects_dir)):
        full = os.path.join(projects_dir, name)
        if not os.path.isdir(full) or name.startswith("."):
            continue
        slug = name.lower().replace(" ", "-")
        candidates.append({
            "slug": slug,
            "name": name,
            "description": "",
            "path": full,
            "backlog_path": os.path.join(full, "docs", "beacon-backlog.md"),
            "source": "local",
        })
    return candidates


def scan_github_repos():
    """Fetch own repos via gh api, skip forks and archived."""
    try:
        result = subprocess.run(
            ["gh", "api", "user/repos", "--paginate", "-q",
             '.[] | select(.fork == false and .archived == false) | {name: .name, description: .description, html_url: .html_url}'],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    candidates = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            repo = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = repo["name"].lower().replace(" ", "-")
        local_path = os.path.join(PROJECTS_DIR, repo["name"])
        candidates.append({
            "slug": slug,
            "name": repo["name"],
            "description": repo.get("description") or "",
            "path": local_path if os.path.isdir(local_path) else None,
            "backlog_path": os.path.join(local_path, "docs", "beacon-backlog.md"),
            "source": "github",
        })
    return candidates


def merge_candidates(local, manifest, github, existing_slugs):
    """Merge sources by slug. Local path wins. Manifest description wins.
    GitHub fills gaps. Filter out already-registered slugs."""
    merged = {}

    # Start with local
    for c in local:
        merged[c["slug"]] = dict(c)

    # Enrich from manifest
    for mid, entry in manifest.items():
        slug = mid.lower().replace(" ", "-")
        if slug in merged:
            if entry.get("description"):
                merged[slug]["description"] = entry["description"]
            if entry.get("name"):
                merged[slug]["name"] = entry["name"]
        else:
            path = os.path.expanduser(entry.get("path", ""))
            merged[slug] = {
                "slug": slug,
                "name": entry.get("name", mid),
                "description": entry.get("description", ""),
                "path": path if os.path.isdir(path) else None,
                "backlog_path": os.path.join(path, "docs", "beacon-backlog.md") if path else None,
                "source": "manifest",
            }

    # Fill from GitHub (only new slugs)
    for c in github:
        if c["slug"] not in merged:
            merged[c["slug"]] = dict(c)
        elif not merged[c["slug"]].get("description") and c.get("description"):
            merged[c["slug"]]["description"] = c["description"]

    # Remove already-registered
    return {k: v for k, v in merged.items() if k not in existing_slugs}


def build_discovery_prompt(candidates, existing_workstreams):
    """Build prompt for AI filtering of workstream candidates."""
    existing_text = "\n".join(
        f"- {ws['slug']}: {ws['name']}" for ws in existing_workstreams
    )
    candidates_text = "\n".join(
        f"- slug: {c['slug']}, name: {c['name']}, description: {c['description']}, "
        f"source: {c['source']}, has_local_path: {bool(c.get('path'))}"
        for c in candidates
    )

    return f"""You are helping configure Beacon, a bookmark & star intake system that routes X bookmarks and GitHub stars to project backlogs.

Already registered workstreams:
{existing_text}

Candidate projects to evaluate:
{candidates_text}

For each candidate, decide: should this become a beacon workstream? A workstream should be added if:
- The project would benefit from having bookmarks and GitHub stars routed to its backlog
- It represents active work that consumes external references (tools, libraries, articles)
- It's substantial enough to warrant its own backlog

Do NOT recommend:
- Infrastructure projects (dotfiles, skill-packs, config repos)
- Archived or inactive projects
- Projects too small or narrow for a backlog
- Vault areas (health, cooking, travel, etc.) unless they actively consume technical bookmarks
- Scheduled tasks, skills, or automation configs

Return ONLY valid JSON array, no markdown fences:
[{{"slug": "project-slug", "recommend": true, "reason": "one sentence why"}}]

Include ALL candidates in the response with recommend true or false."""


def run_ai_filter(candidates, existing_workstreams):
    """Run AI filtering via the configured Beacon AI CLI."""
    if not candidates:
        return []

    batch_size = int(os.environ.get("BEACON_DISCOVER_BATCH_SIZE", "10"))
    timeout = int(os.environ.get("BEACON_DISCOVER_TIMEOUT_SEC", "45"))
    candidate_list = list(candidates.values())
    combined = []

    for start in range(0, len(candidate_list), batch_size):
        batch = candidate_list[start:start + batch_size]
        prompt = build_discovery_prompt(batch, existing_workstreams)
        results = run_json_prompt(prompt, timeout=timeout)
        if not results:
            continue
        combined.extend(results)

    return combined


def present_and_register(recommendations, candidates, config_path, dry_run=False):
    """Show AI-recommended candidates. User approves each. Register approved."""
    recommended = [r for r in recommendations if r.get("recommend")]
    not_recommended = [r for r in recommendations if not r.get("recommend")]

    if not recommended and not not_recommended:
        print("No candidates to evaluate.")
        return 0

    registered = 0

    if recommended:
        print(f"\nRecommended workstreams:\n")
        for i, rec in enumerate(recommended, 1):
            slug = rec["slug"]
            candidate = candidates.get(slug)
            if not candidate:
                continue
            reason = rec.get("reason", "")
            path = candidate.get("backlog_path", "")

            print(f"  {i}. [+] {candidate['name']} — {reason}")
            if path:
                print(f"     -> {path}")

            if dry_run:
                print(f"     (dry run, skipping)")
                continue

            answer = input(f"     Add? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes"):
                _register_workstream(candidate, config_path)
                registered += 1
                print(f"     Registered.")
            else:
                print(f"     Skipped.")

    if not_recommended:
        slugs = [r["slug"] for r in not_recommended]
        print(f"\nNot recommended: {', '.join(slugs[:10])}", end="")
        if len(slugs) > 10:
            print(f"... ({len(slugs)} total)", end="")
        print()

    return registered


def present_all(recommendations, candidates, config_path, dry_run=False):
    """Show all candidates including non-recommended. User approves each."""
    recommended = [r for r in recommendations if r.get("recommend")]
    not_recommended = [r for r in recommendations if not r.get("recommend")]

    registered = 0

    if recommended:
        print(f"\nRecommended workstreams:\n")
        for i, rec in enumerate(recommended, 1):
            slug = rec["slug"]
            candidate = candidates.get(slug)
            if not candidate:
                continue
            reason = rec.get("reason", "")
            path = candidate.get("backlog_path", "")

            print(f"  {i}. [+] {candidate['name']} — {reason}")
            if path:
                print(f"     -> {path}")

            if dry_run:
                print(f"     (dry run, skipping)")
                continue

            answer = input(f"     Add? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes"):
                _register_workstream(candidate, config_path)
                registered += 1
                print(f"     Registered.")
            else:
                print(f"     Skipped.")

    if not_recommended:
        print(f"\nNot recommended:\n")
        offset = len(recommended)
        for i, rec in enumerate(not_recommended, offset + 1):
            slug = rec["slug"]
            candidate = candidates.get(slug)
            if not candidate:
                continue
            reason = rec.get("reason", "")
            path = candidate.get("backlog_path", "")

            print(f"  {i}. [-] {candidate['name']} — {reason}")
            if path:
                print(f"     -> {path}")

            if dry_run:
                print(f"     (dry run, skipping)")
                continue

            answer = input(f"     Add? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                _register_workstream(candidate, config_path)
                registered += 1
                print(f"     Registered.")

    return registered


def _register_workstream(candidate, config_path):
    """Append workstream to config.json and DB."""
    from . import db

    slug = candidate["slug"]
    name = candidate["name"]
    backlog_path = candidate.get("backlog_path", "")

    # Shorten path with ~ for config
    home = os.path.expanduser("~")
    config_backlog = backlog_path.replace(home, "~") if backlog_path else ""

    # Update config.json
    with open(config_path) as f:
        config = json.load(f)

    config["workstreams"].append({
        "slug": slug,
        "name": name,
        "backlog_path": config_backlog,
    })

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    # Add to DB
    db.add_workstream(slug, name, config_backlog)


def discover_workstreams(config_path, dry_run=False, skip_github=False, show_all=False):
    """Main entry: scan -> merge -> AI filter -> present -> register."""
    from . import db

    print("Scanning...")

    # Gather existing slugs
    existing = db.get_workstreams()
    existing_slugs = {ws["slug"] for ws in existing}

    # Scan sources
    local = scan_local_projects()
    print(f"  ~/Projects/: {len(local)} dirs")

    manifest = load_manifest()
    print(f"  systems-manifest: {len(manifest)} entries")

    if skip_github:
        github = []
        print(f"  GitHub: skipped")
    else:
        github = scan_github_repos()
        print(f"  GitHub: {len(github)} repos")

    print(f"  Already registered: {len(existing_slugs)}")

    # Merge
    candidates = merge_candidates(local, manifest, github, existing_slugs)
    if not candidates:
        print("\nNo new candidates found.")
        return

    print(f"  {len(candidates)} candidates -> AI filtering...")

    # AI filter
    recommendations = run_ai_filter(candidates, existing)
    if not recommendations:
        print("AI filtering returned no results.")
        return

    recommended_count = sum(1 for r in recommendations if r.get("recommend"))
    print(f"  AI recommends {recommended_count} of {len(recommendations)}")

    # Present and register
    if show_all:
        registered = present_all(recommendations, candidates, config_path, dry_run)
    else:
        registered = present_and_register(recommendations, candidates, config_path, dry_run)

    if not dry_run and registered:
        print(f"\nRegistered {registered} new workstream{'s' if registered != 1 else ''}.")
    elif not dry_run:
        print(f"\nNo new workstreams registered.")
