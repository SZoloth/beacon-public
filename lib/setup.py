"""`beacon init`: write a first config.json for a new install from a few answers.

It never overwrites an existing config. Everything it writes can be edited by hand afterwards.
"""
import json
import os
import re

from . import settings

MAX_PROJECTS = 8


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"Project name needs a letter or digit: {name!r}")
    return slug


def _tilde(path):
    path = os.path.abspath(os.path.expanduser(path))
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def parse_project(text):
    """'Name|folder|description' -> (name, folder, description). Description is required."""
    parts = [part.strip() for part in text.split("|", 2)]
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"Use NAME|FOLDER|DESCRIPTION for each project: {text!r}")
    return tuple(parts)


def build_config(owner, about, projects, root=settings.ROOT, example=None):
    if not owner.strip():
        raise ValueError("Owner name is required.")
    if not projects:
        raise ValueError("Add at least one project; saves are matched against project descriptions.")
    if len(projects) > MAX_PROJECTS:
        raise ValueError(f"Start with at most {MAX_PROJECTS} projects; add more later in config.json.")
    base = example if example is not None else settings.load(os.path.join(root, "config.example.json"))
    workstreams, seen = [], set()
    for name, folder, description in projects:
        slug = slugify(name)
        if slug in seen:
            raise ValueError(f"Two projects share the slug {slug!r}; rename one.")
        seen.add(slug)
        workstreams.append({"slug": slug, "name": name.strip(),
                            "backlog_path": _tilde(os.path.join(folder, "docs", "beacon-backlog.md")),
                            "description": description.strip()})
    return {
        "owner": {"name": owner.strip(), "about": about.strip() or f"The user is {owner.strip()}."},
        "paths": {"live_db": _tilde(os.path.join(root, "beacon.db")),
                  "reading_inbox": _tilde(os.path.join(root, "reading-inbox.md"))},
        "workstreams": workstreams,
        "sources": base.get("sources", {"github": {"method": "gh_api"}}),
        "practices": [],
        "classification": base.get("classification", {"model": "sonnet", "batch_size": 15}),
        "areas": base.get("areas", []),
    }


def write(path, config):
    if os.path.exists(path):
        raise FileExistsError(f"{path} already exists; edit it instead. beacon init never overwrites a config.")
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def ask(prompt_fn=input, print_fn=print):
    """Interactive answers: owner, one line about them, then projects until a blank name."""
    owner = prompt_fn("Your name (prompts use it): ").strip()
    about = prompt_fn("One line on what you read and build (optional): ").strip()
    print_fn("Projects: give each a name, its folder, and a sentence on what saves would help it. Blank name to finish.")
    projects = []
    while len(projects) < MAX_PROJECTS:
        name = prompt_fn("  Project name: ").strip()
        if not name:
            break
        folder = prompt_fn("  Folder: ").strip()
        description = prompt_fn("  What would help it: ").strip()
        projects.append((name, folder, description))
    return owner, about, projects


def next_steps(config):
    first = config["workstreams"][0]["backlog_path"]
    return "\n".join([
        "Wrote config.json. Next:",
        "  python3 beacon.py sync --source github   # your GitHub stars",
        "  python3 beacon.py import-x FILE.json     # X bookmarks, see docs/x-handoff.md",
        "  python3 beacon.py classify",
        "  python3 beacon.py route --dry-run",
        f"Backlogs will be written to each project's docs/beacon-backlog.md (first: {first}).",
    ])
