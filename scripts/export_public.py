#!/usr/bin/env python3
"""Copy the tracked files a public Beacon repo may carry into an empty folder, then scan them.

Usage: python3 scripts/export_public.py TARGET [--allow-hits]

Leaves out the owner's config, run logs, evals on their decisions, generated backlogs, agent
notes, the roadmap and design record, and the hosted beta, and empties the backlog blocks generated
from their saves. Then it scans every copied text file for home-folder paths plus the terms in
~/.config/beacon/public-denylist.txt (or the file named by BEACON_DENYLIST). Keep names, emails,
client and project names there, not here, so this script stays impersonal. That file holds:

    Ada Lovelace                     a term; matches whole words, any case
    allow: Copyright (c) 2026 Ada    text that may stay although it holds a term (a license line)
    replace: com.ada => com.example  rewritten in the exported copy only (bundle ids, team ids)

Pictures and other binary files can't be scanned; they are listed so a person can open them.

Exit 0: clean. Exit 1: personal text found (the folder is kept so you can look). Exit 2: bad
target or no denylist. This never pushes or publishes anything.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Run logs, evals on the owner's decisions, dated plans and reviews of their own setup, agent notes,
# the tests of the owner's own config.json (they name its clients and projects), and tests of experiments in runs/.
# The roadmap, design record, research notes and audits are the owner's working history, and the
# hosted beta is a separate invite-only product. PR screenshots show demo data under real people's
# handles, which must not ship as if they were real posts.
EXCLUDED_DIRS = ("runs/", "docs/perf/", "docs/plans/", "plans/", ".pstack/", ".agents/", ".claude/",
                 "docs/design/", "docs/research/", "docs/audits/", "docs/screenshots/", "hosted/")
EXCLUDED_FILES = {"config.json", "THEORY.MD", "docs/ROADMAP.md", "docs/beacon-backlog.md", "docs/beacon-backlog-archive.md",
                  "docs/useful-now.md", "docs/review-vault-2026-04-16.md", "docs/review-agent-workflows-2026-04-16.md",
                  "app/docs/screenshot.jpg", "app/docs/briefs.jpg",
                  "tests/test_private_setup.py",
                  "tests/test_arrange_clusters.py"}  # loads an experiment from runs/, which isn't exported
ESCAPE = re.compile(r"\\[ntr]")
HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s\"']+/")
# Blocks regenerated from the owner's database (their real saves); kept as empty markers.
GENERATED_BLOCK = re.compile(r"(<!-- beacon:backlog:start -->).*?(<!-- beacon:backlog:end -->)", re.DOTALL)


def tracked_files():
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [name for name in listing.stdout.split("\0") if name]


def is_excluded(name):
    return (name in EXCLUDED_FILES or name.startswith(EXCLUDED_DIRS)
            or ".bak-" in os.path.basename(name))


IMAGE_LINE = re.compile(r"^!\[[^\]]*\]\(([^)\s]+)\)\s*$")


def drop_left_out_images(path, name):
    """Remove a markdown line that only shows a picture the export left out."""
    folder = os.path.dirname(name)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not ((m := IMAGE_LINE.match(line.strip()))
                                           and is_excluded(os.path.normpath(os.path.join(folder, m.group(1)))))]
    if len(kept) != len(lines):
        path.write_text("".join(kept), encoding="utf-8")


def blank_generated(path):
    text = path.read_text(encoding="utf-8")
    emptied = GENERATED_BLOCK.sub(r"\1\n\2", text)
    if emptied != text:
        path.write_text(emptied, encoding="utf-8")


def denylist_path():
    return Path(os.environ.get("BEACON_DENYLIST") or Path.home() / ".config" / "beacon" / "public-denylist.txt")


def denylist(path):
    """(term pattern or None, allowed texts, [(old, new)]) from the denylist file."""
    terms, allowed, replacements = [], [], []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("allow:"):
            allowed.append(line[len("allow:"):].strip())
        elif line.startswith("replace:") and "=>" in line:
            old, new = line[len("replace:"):].split("=>", 1)
            replacements.append((old.strip(), new.strip()))
        else:
            terms.append(line)
    pattern = re.compile("|".join(rf"(?<!\w){re.escape(term)}(?!\w)" for term in terms), re.IGNORECASE) if terms else None
    return pattern, [text for text in allowed if text], [pair for pair in replacements if pair[0]]


def rewrite(path, replacements):
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    changed = text
    for old, new in replacements:
        changed = changed.replace(old, new)
    if changed != text:
        path.write_text(changed, encoding="utf-8")


def scan(target, extra, allowed=()):
    """({file: lines with personal text}, [files that aren't text])."""
    hits, unscanned = {}, []
    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        name = str(path.relative_to(target))
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            unscanned.append(name)
            continue
        count = 0
        for line in text.splitlines():
            for ok in allowed:
                line = line.replace(ok, "")
            line = ESCAPE.sub(" ", line)  # "\nAda" in source code is a newline, then a name
            if HOME_PATH.search(line) or extra.search(line):
                count += 1
        if count:
            hits[name] = count
    return hits, unscanned


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target")
    parser.add_argument("--allow-hits", action="store_true", help="exit 0 even if personal text remains")
    args = parser.parse_args(argv)
    target = Path(args.target).expanduser().resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        print(f"Target must be a new or empty folder: {target}", file=sys.stderr)
        return 2
    if target == ROOT or ROOT in target.parents:
        print("Target must be outside this checkout.", file=sys.stderr)
        return 2
    source_list = denylist_path()
    extra, allowed, replacements = denylist(source_list) if source_list.is_file() else (None, [], [])
    if extra is None:
        print(f"No denylist terms in {source_list}. Write the owner's names, emails, client and project names there "
              "(one per line), or point BEACON_DENYLIST at such a file.", file=sys.stderr)
        return 2

    kept = left_out = 0
    for name in tracked_files():
        source = ROOT / name
        if is_excluded(name) or not source.is_file():
            left_out += 1
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if name.endswith(".md"):
            blank_generated(destination)
            drop_left_out_images(destination, name)
        rewrite(destination, replacements)
        kept += 1

    hits, unscanned = scan(target, extra, allowed)
    print(f"Copied {kept} files to {target}; left out {left_out}.")
    if unscanned:
        folders = sorted({str(Path(name).parent) for name in unscanned})
        print(f"{len(unscanned)} pictures and other binary files were not scanned; open them before publishing:")
        for folder in folders:
            print(f"  {folder}/ ({sum(1 for name in unscanned if str(Path(name).parent) == folder)})")
    if not hits:
        print("No personal text found.")
        return 0
    print(f"{len(hits)} files still contain personal text ({sum(hits.values())} lines):")
    for name, count in sorted(hits.items(), key=lambda pair: (-pair[1], pair[0])):
        print(f"  {count:5d}  {name}")
    return 0 if args.allow_hits else 1


if __name__ == "__main__":
    sys.exit(main())
