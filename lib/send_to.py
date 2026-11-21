"""Saves sent from the phone's share sheet straight to one or more projects, with the owner's why.

The share sheet asks Beacon which projects to offer (`ranked`), then sends the link with the projects the
owner picked and an optional why (`route_save`). The save is queued in each project's backlog at once,
the why is kept as the owner's own note, and a copy goes to mymind (`mirror_to_mymind`) with each project
as a tag and the why as a note, so mymind stays the one library.
"""
import json
import re
import subprocess
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import db, references, route, settings

SOURCE = "share"  # owner_notes.source for a why typed in the share sheet
RECENT_DAYS = 30
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "this", "that", "is", "it", "my", "i"}


def _words(text):
    return {word for word in _WORD.findall((text or "").lower()) if len(word) > 2 and word not in _STOP}


def _described():
    config = settings.load()
    return {entry.get("slug"): entry.get("description") or "" for entry in config.get("workstreams") or []}


def _projects(conn):
    return conn.execute("SELECT id, slug, name FROM workstreams WHERE kind = 'project' AND active = 1 "
                        "AND backlog_path IS NOT NULL AND backlog_path != '' ORDER BY slug").fetchall()


def ranked(text="", db_path=None):
    """Active projects with a backlog, best guess first: words shared with the shared text, then recent use."""
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat()
    shared, described = _words(text), _described()
    with closing(db.get_connection(db_path)) as conn:
        recent = Counter({row["slug"]: row["n"] for row in conn.execute(
            "SELECT w.slug, COUNT(*) AS n FROM item_workstreams iw JOIN workstreams w ON w.id = iw.workstream_id "
            "WHERE iw.routed = 1 AND iw.routed_at >= ? GROUP BY w.slug", (since,))})
        rows = []
        for project in _projects(conn):
            about = _words(f"{project['slug'].replace('-', ' ')} {project['name']} {described.get(project['slug'], '')}")
            rows.append({"slug": project["slug"], "name": project["name"],
                         "matches": len(shared & about), "recent": recent[project["slug"]]})
    rows.sort(key=lambda row: (-row["matches"], -row["recent"], row["slug"]))
    return rows


def route_save(item_id, slugs, note="", db_path=None):
    """Queue a kept save in each named project, keep the why as the owner's note, rewrite those backlogs.

    Returns {"routed": [...], "kept_out": [...]}; kept_out lists projects the client-work rule refused.
    Raises ValueError for a project that isn't active with a backlog.
    """
    slugs = list(dict.fromkeys(slugs))
    note = (note or "").strip()[:1000]
    now = datetime.now(timezone.utc).isoformat()
    with closing(db.get_connection(db_path)) as conn:
        projects = {row["slug"]: row["id"] for row in _projects(conn)}
        unknown = [slug for slug in slugs if slug not in projects]
        if unknown:
            raise ValueError(f"not an active project with a backlog: {', '.join(unknown)}")
        with conn:
            if note:
                # Stored first, so a why that names client work keeps the save out of personal projects below.
                conn.execute("INSERT INTO owner_notes (item_id, source, external_id, kind, origin, body, created_at, synced_at) "
                             "VALUES (?, ?, ?, 'note', 'owner', ?, ?, ?) ON CONFLICT(source, external_id) DO UPDATE SET "
                             "body = excluded.body, synced_at = excluded.synced_at",
                             (item_id, SOURCE, f"{item_id}:note", note, now, now))
        routed, kept_out = [], []
        with conn:
            for slug in slugs:
                if item_id in references._client_filter(conn, slug):
                    kept_out.append(slug)
                    continue
                conn.execute("INSERT INTO item_workstreams (item_id, workstream_id, confidence, routed) VALUES (?, ?, 1.0, 0) "
                             "ON CONFLICT(item_id, workstream_id) DO UPDATE SET routed = 0", (item_id, projects[slug]))
                routed.append(slug)
            if routed:
                db.set_item_status(conn, item_id, "queued", "share sheet: " + ", ".join(routed))
    for slug in routed:
        # As `beacon route` does: the file is written before the link is marked routed, so a failed write retries.
        if route.regenerate_backlog(slug) is not None:
            db.mark_routed_batch([item_id], slug, now)
    return {"routed": routed, "kept_out": kept_out}


def _cli(cli_dir, *args, stdin=None, timeout=60):
    done = subprocess.run([f"{cli_dir}/node_modules/.bin/tsx", f"{cli_dir}/bin/mymind.ts", *args], input=stdin,
                          capture_output=True, text=True, timeout=timeout, check=False)
    envelope = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
    if done.returncode != 0 or not envelope.get("ok"):
        raise RuntimeError((envelope.get("error") or {}).get("message") or done.stderr.strip()[:300] or "mymind failed")
    return envelope.get("result") or {}


def mirror_to_mymind(url, slugs, note="", run=_cli):
    """Create the mymind card for a share-sheet save: the link, each project as a tag, the why as a note.

    Returns the mymind object id, or None when no mymind CLI is configured. mymind's next sync brings the card
    back with those tags; tag_routes skips it because the same link is already in those projects.
    """
    cli_dir = settings.path("mymind_cli")
    if not cli_dir:
        return None
    args = ["objects", "create", "--url", url]
    for slug in slugs:
        args += ["--tag", slug]
    object_id = (run(cli_dir, *args).get("object") or {}).get("id")
    if object_id and note.strip():
        run(cli_dir, "objects", "notes", "create", object_id, stdin=note.strip())
    return object_id
