"""Autopilot: Beacon handles Triage itself, and the owner approves a few picks.

Every queued save already carries Beacon's suggestion (item_gists: do, read, reference or skip). `run`
acts on three of them: skip is passed (status rejected), read is sent to Readwise Reader, and reference
is filed (status adopted, note "reference") where agents find it through `project_references`. Do stays
for the owner, as today's picks. Each action goes into auto_actions, so the app can list what Beacon did
and undo it; an undone save is never handled again.

The owner's own choices go into choice_log with the suggestion they saw. That log is the comparison set
for checking, later, how often Beacon's suggestions match them.
"""
from contextlib import closing
from datetime import datetime, timezone

from . import db, reader

# Reader saves per run, so one run can't flood the Reader inbox; the rest wait for the next run.
# 45 because the weekly sweep turns saves queued over 21 days stale, and autopilot only reads queued
# ones: three weekly runs must clear a backlog (125 on 2026-10-10) before it expires.
READ_LIMIT = 45


def _now():
    return datetime.now(timezone.utc).isoformat()


def _waiting(suggestion):
    """Queued saves Beacon suggests `suggestion` for and hasn't handled before, oldest first."""
    with closing(db.get_connection()) as conn:
        return conn.execute(
            "SELECT i.id, g.reason FROM items i JOIN item_gists g ON g.item_id = i.id "
            "LEFT JOIN auto_actions a ON a.item_id = i.id "
            "WHERE i.status = 'queued' AND g.suggestion = ? AND a.item_id IS NULL AND " + db.NOT_HELD_SQL + " ORDER BY i.id",
            (suggestion,)).fetchall()


def _record(item_id, action, reason):
    with closing(db.get_connection()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO auto_actions(item_id, action, reason, acted_at) VALUES(?,?,?,?)",
                     (item_id, action, reason, _now()))


def file_as_reference(item_id, reason):
    """Keep a save for later work in its project or area, out of Triage and Briefs; undo brings it back."""
    db.update_item_status(item_id, "adopted", note=db.REFERENCE)
    _record(item_id, "reference", reason)


def run(*, dry_run=False, read_limit=READ_LIMIT, send=reader.read_item):
    """Pass what Beacon judged noise, file references, and send up to `read_limit` reads to Reader."""
    passes, reads, references = _waiting("skip"), _waiting("read"), _waiting("reference")
    result = {"passed": len(passes), "read": min(len(reads), read_limit), "referenced": len(references),
              "read_waiting": max(0, len(reads) - read_limit), "read_error": None}
    if dry_run:
        return result
    for row in passes:
        if db.is_held(row["id"]):  # held since the list was made
            result["passed"] -= 1
            continue
        db.update_item_status(row["id"], "rejected", note=f"Passed by Beacon: {row['reason']}")
        _record(row["id"], "pass", row["reason"])
    for row in references:
        if db.is_held(row["id"]):  # held since the list was made
            result["referenced"] -= 1
            continue
        file_as_reference(row["id"], row["reason"])
    sent = held = 0
    for row in reads[:read_limit]:
        if db.is_held(row["id"]):  # held since the list was made, possibly by an earlier send
            held += 1
            continue
        try:
            send(row["id"])
        except RuntimeError as error:  # no token, Reader down: stop, and try the rest next run
            result["read_error"] = str(error)
            break
        _record(row["id"], "read", row["reason"])
        sent += 1
    result["read"] = sent
    result["read_waiting"] = len(reads) - sent - held
    return result


def handled(days=14):
    """What Beacon did in the last `days` days and you haven't undone, newest first."""
    with closing(db.get_connection()) as conn:
        rows = conn.execute(
            "SELECT a.item_id, a.action, a.reason, a.acted_at, i.title, i.url FROM auto_actions a "
            "JOIN items i ON i.id = a.item_id WHERE a.undone_at IS NULL AND a.acted_at >= datetime('now', ?) "
            "ORDER BY a.acted_at DESC, a.item_id DESC", (f"-{days} days",)).fetchall()
    return [{"id": row["item_id"], "action": row["action"], "reason": row["reason"], "acted_at": row["acted_at"],
             "title": row["title"], "url": row["url"]} for row in rows]


def unhandle(item_id):
    """Undo Beacon's action: back in the queue, and logged as Beacon getting it wrong.

    A save Beacon sent to Reader stays in Reader; only Beacon's record of it changes.
    A reference-only plan is dropped, so choosing Do again plans the save afresh.
    """
    db.update_item_status(item_id, "queued", note="Brought back from Beacon's handled list")
    with closing(db.get_connection()) as conn, conn:
        conn.execute("DELETE FROM briefs WHERE item_id = ? AND status = 'dismissed' "
                     "AND json_extract(brief_json, '$.verdict') = 'reference only'", (item_id,))
        conn.execute("UPDATE auto_actions SET undone_at = ? WHERE item_id = ?", (_now(), item_id))
    record_choices([item_id], "undo")


def record_choices(item_ids, choice, source="you"):
    """Keep each choice with the suggestion Beacon showed for it."""
    with closing(db.get_connection()) as conn, conn:
        log_choices(conn, item_ids, choice, source)


def log_choices(conn, item_ids, choice, source="you"):
    """record_choices inside the caller's transaction; the caller commits."""
    for item_id in item_ids:
        suggestion = conn.execute("SELECT suggestion FROM item_gists WHERE item_id = ?", (item_id,)).fetchone()
        conn.execute("INSERT INTO choice_log(item_id, suggestion, choice, source, chosen_at) VALUES(?,?,?,?,?)",
                     (item_id, suggestion[0] if suggestion else None, choice, source, _now()))


def choices():
    with closing(db.get_connection()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT item_id, suggestion, choice, source FROM choice_log ORDER BY id")]
