"""Resolve merge conflicts in docs/ROADMAP.md row by row.

Two PRs that each add a roadmap row, or update the same row, conflict on every merge. Inside each
conflict this keeps main's rows, lets the PR's version replace a row with the same key, and adds
the PR's new rows after main's. A table row's key is its first cell, or its first two cells when
the first is a date (the Shipped table has many rows per day).

Usage: python3 scripts/roadmap_merge.py [path]   (default docs/ROADMAP.md; rewrites it in place)
"""

import re
import sys
from pathlib import Path

CONFLICT = re.compile(r"<<<<<<< [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", re.S)
DATE = re.compile(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d")


def key(line):
    if not (line.startswith("|") and line.count("|") > 2):
        return line
    cells = [cell.strip() for cell in line.split("|")[1:3]]
    return " | ".join(cells) if DATE.match(cells[0]) else cells[0]


def merge_block(pr_side, main_side):
    """`pr_side` is HEAD (the PR branch merging main in); `main_side` is main."""
    pr_lines, main_lines = pr_side.splitlines(True), main_side.splitlines(True)
    by_key = {key(line): line for line in pr_lines}
    merged = [by_key.pop(key(line), line) for line in main_lines]
    return "".join(merged + [line for line in pr_lines if key(line) in by_key])


def resolve(text):
    return CONFLICT.sub(lambda m: merge_block(m.group(1), m.group(2)), text)


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/ROADMAP.md")
    path.write_text(resolve(path.read_text()))
