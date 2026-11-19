"""Route classified items to project backlog files."""

import json
import os
import tempfile
from datetime import datetime, timezone
from collections import defaultdict

from . import db, match, settings


def format_item_entry(item, tags):
    """Format a single item as markdown for a backlog file."""
    title = item["title"] or "Untitled"
    url = item["url"] or ""
    body = (item["body"] or "")[:200]
    intent = item["intent"] or "explore"

    # Strip newlines from title to avoid broken markdown headings
    title = title.replace("\n", " ").strip()

    lines = []
    if url:
        lines.append(f"### [{title}]({url}) ({item['source']})")
    else:
        lines.append(f"### {title} ({item['source']})")

    if body and body != title:
        lines.append(f"> {body}")

    lines.append(f"- **Intent**: {intent}")
    if tags:
        lines.append(f"- **Tags**: {', '.join(tags)}")

    return "\n".join(lines)


def format_active_item(item, tags, workstream=None):
    """Format a single item for the regenerated active backlog."""
    title = item["title"] or "Untitled"
    url = item["url"] or ""
    body = (item["body"] or "")[:200]
    status = item["status"] or "queued"

    title = title.replace("\n", " ").strip()

    lines = []
    if url:
        lines.append(f"### [{title}]({url}) — {status}")
    else:
        lines.append(f"### {title} — {status}")

    if body and body != title:
        lines.append(f"> {body}")

    if tags:
        lines.append(f"- **Tags**: {', '.join(tags)}")
    if workstream:
        # The line names the step. Regenerating the file does not perform it.
        lines.append(f"- **Next**: {match.assigned_next_action(item, workstream)}")

    return "\n".join(lines)


def _catalog_by_slug():
    path = settings.config_path()
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return {}
    found = {}
    for workstream in config.get("workstreams") or []:
        if isinstance(workstream, dict) and workstream.get("slug"):
            found[workstream["slug"]] = workstream
    return found


def _with_catalog(workstream, catalog):
    extra = catalog.get(workstream.get("slug"), {})
    merged = dict(workstream)
    for key in ("description", "keywords", "aliases"):
        if not merged.get(key) and extra.get(key):
            merged[key] = extra[key]
    return merged


def format_archive_item(item, tags):
    """Format a single item for the archive file."""
    title = item["title"] or "Untitled"
    url = item["url"] or ""
    body = (item["body"] or "")[:200]
    status = item["status"] or "stale"
    note = item.get("status_note") or ""

    title = title.replace("\n", " ").strip()

    lines = []
    if url:
        lines.append(f"### [{title}]({url}) — {status}")
    else:
        lines.append(f"### {title} — {status}")

    if body and body != title:
        lines.append(f"> {body}")

    if tags:
        lines.append(f"- **Tags**: {', '.join(tags)}")
    if note:
        lines.append(f"- {note}")

    return "\n".join(lines)


READING_INBOX = settings.path("reading_inbox")
BLOCK_START = "<!-- beacon:backlog:start -->"
BLOCK_END = "<!-- beacon:backlog:end -->"


def atomic_write(path, content):
    """Write content to path atomically via temp file + rename."""
    if db.skipping_outside_effects():
        return
    dirpath = os.path.dirname(path)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.rename(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def regenerate_backlog(workstream_slug):
    """Rewrite the active backlog file from DB state."""
    items = db.get_active_items_for_workstream(workstream_slug)
    workstreams = db.get_workstreams()
    ws = next((w for w in workstreams if w["slug"] == workstream_slug), None)
    if not ws or not ws["backlog_path"]:
        return

    backlog_path = os.path.expanduser(ws["backlog_path"])
    ws_name = ws["name"]
    ws = _with_catalog(ws, _catalog_by_slug())

    # Group by intent (build-with first)
    intent_order = ["build-with", "reference", "explore", "non-technical"]
    intent_labels = {"build-with": "Build With", "reference": "Reference", "explore": "Explore", "non-technical": "Non-Technical"}
    by_intent = defaultdict(list)
    for item in items:
        by_intent[item["intent"] or "explore"].append(item)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_active = len(items)

    # A backlog inside a hand-written doc (a roadmap) is one marked block, a level down.
    current = ""
    if os.path.exists(backlog_path):
        with open(backlog_path) as f:
            current = f.read()
    embedded = BLOCK_START in current and BLOCK_END in current
    heading = "###" if embedded else "##"
    lines = [] if embedded else [f"# Beacon Backlog — {ws_name}", ""]
    lines += [f"_{total_active} active | Last updated: {now}_", ""]

    for intent in intent_order:
        group = by_intent.get(intent)
        if not group:
            continue
        label = intent_labels.get(intent, intent)
        lines.append(f"{heading} {label} ({len(group)})")
        lines.append("")
        for item in group:
            tags = db.get_item_tags(item["id"])
            entry = format_active_item(item, tags, ws)
            lines.append("#" + entry if embedded else entry)
            lines.append("")

    content = "\n".join(lines)
    if embedded:
        before, rest = current.split(BLOCK_START, 1)
        content = before + BLOCK_START + "\n" + content.rstrip() + "\n" + BLOCK_END + rest.split(BLOCK_END, 1)[1]
    atomic_write(backlog_path, content)
    return total_active


def regenerate_archive(workstream_slug):
    """Rewrite the archive file from DB state."""
    items = db.get_stale_items_for_workstream(workstream_slug)
    workstreams = db.get_workstreams()
    ws = next((w for w in workstreams if w["slug"] == workstream_slug), None)
    if not ws or not ws["backlog_path"]:
        return

    backlog_path = os.path.expanduser(ws["backlog_path"])
    archive_path = os.path.join(os.path.dirname(backlog_path), "beacon-backlog-archive.md")

    if not items:
        return

    # Group by date (status_at date)
    by_date = defaultdict(list)
    for item in items:
        date = (item["status_at"] or "")[:10] or "unknown"
        by_date[date].append(item)

    ws_name = ws["name"]
    lines = [f"# Beacon Archive — {ws_name}", ""]

    for date in sorted(by_date.keys(), reverse=True):
        lines.append(f"## {date}")
        lines.append("")
        for item in by_date[date]:
            tags = db.get_item_tags(item["id"])
            lines.append(format_archive_item(item, tags))
            lines.append("")

    atomic_write(archive_path, "\n".join(lines))


def regenerate_all_backlogs():
    """Regenerate all workstream backlog + archive files."""
    if db.skipping_outside_effects():
        return
    workstreams = db.get_workstreams()
    for ws in workstreams:
        if ws["backlog_path"]:
            active = regenerate_backlog(ws["slug"])
            regenerate_archive(ws["slug"])
            if active:
                print(f"  {ws['slug']}: {active} active items")


def route_items(dry_run=False):
    """Push unrouted classified items to their backlog files."""
    unrouted = db.get_unrouted_items()
    if not unrouted:
        print("No items to route.")
        return 0

    # Group by workstream
    by_workstream = defaultdict(list)
    for item in unrouted:
        by_workstream[item["workstream_slug"]].append(item)

    now = datetime.now(timezone.utc)
    routed_count = 0

    for ws_slug, items in by_workstream.items():
        backlog_path = items[0]["backlog_path"]
        if not backlog_path:
            continue

        if dry_run:
            print(f"\n[DRY RUN] Would route {len(items)} items to {ws_slug}:")
            for item in items:
                print(f"  - {item['title'][:60]} ({item['intent']})")
            continue

        # Set status to queued for newly routed items
        for item in items:
            if item["status"] == "new":
                db.update_item_status(item["id"], "queued")

        # Persist the file before acknowledging delivery. A failed write stays
        # pending; retrying regenerates the same backlog rather than appending.
        if regenerate_backlog(ws_slug) is None:
            continue
        db.mark_routed_batch([item["id"] for item in items], ws_slug, now.isoformat())
        routed_count += len(items)

        print(f"  Routed {len(items)} items to {ws_slug}")

    # Route human_read items to reading inbox
    reading_items = db.get_unrouted_reading_items()
    if reading_items and not READING_INBOX:
        print(f"  {len(reading_items)} reads left unrouted: paths.reading_inbox is not set in config")
    elif reading_items:
        date_str = now.strftime("%Y-%m-%d")
        if dry_run:
            print(f"\n[DRY RUN] Would route {len(reading_items)} items to reading inbox:")
            for item in reading_items:
                print(f"  - {(item['title'] or '')[:60]}")
        else:
            entries = []
            for item in reading_items:
                tags = db.get_item_tags(item["id"])
                title = (item["title"] or "Untitled").replace("\n", " ").strip()
                url = item["url"] or ""
                one_liner = item.get("one_liner") or ""

                lines = []
                if url:
                    lines.append(f"- [{title}]({url})")
                else:
                    lines.append(f"- {title}")
                if one_liner:
                    lines.append(f"  {one_liner}")
                if tags:
                    lines.append(f"  *{', '.join(tags)}*")
                entries.append("\n".join(lines))

            block = f"\n## {date_str}\n\n" + "\n".join(entries) + "\n"

            if not db.skipping_outside_effects():
                os.makedirs(os.path.dirname(READING_INBOX), exist_ok=True)
                with open(READING_INBOX, "a") as f:
                    f.write(block)

            db.mark_reading_routed([item["id"] for item in reading_items])
            print(f"  Routed {len(reading_items)} items to reading inbox")

    print(f"Routed {routed_count} items total.")
    return routed_count
