"""Explicit local decisions over saved holds; never runs a model or router."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import intake, review
from .conditions import _table_exists as exists

TABLES = {"intake": ("intake_reviews", "item_id"), "save": ("save_reviews", "id"), "advisory": ("advisory_reviews", "id")}
CLASSIFIABLE_INTENTS = {key: value for key, value in review.INTENTS.items() if key != "unclear"}
HISTORY_SQL = """CREATE TABLE IF NOT EXISTS review_resolutions (
    id INTEGER PRIMARY KEY, target TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL, decision_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
    UNIQUE(target,snapshot_hash)
)"""


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def target_parts(target):
    if not isinstance(target, str): raise ValueError("Choose intake:ID, save:ID or advisory:ID.")
    kind, _, identifier = target.partition(":")
    if kind not in TABLES or not identifier.isascii() or not identifier.isdecimal() or not 0 < int(identifier) < 2**63:
        raise ValueError("Choose intake:ID, save:ID or advisory:ID.")
    if str(int(identifier)) != identifier: raise ValueError("Use the displayed ID without leading zeroes.")
    return kind, int(identifier)


def _snapshot(conn, target):
    kind, identifier = target_parts(target)
    table, key = TABLES[kind]
    row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (identifier,)).fetchone() if exists(conn, table) else None
    if row is None: raise ValueError("Saved review not found.")
    hold = dict(row)
    snapshot = {"target": target, "kind": kind, "hold": hold, "item": None, "assignments": [], "destinations": [], "last_resolution": None}
    if exists(conn, "review_resolutions"):
        prior = conn.execute("SELECT id,decision_json,recorded_at FROM review_resolutions WHERE target=? ORDER BY id DESC LIMIT 1", (target,)).fetchone()
        snapshot["last_resolution"] = dict(prior) if prior else None
    if kind in ("intake", "save"):
        fields = (*intake.SOURCE_FIELDS, "status", "status_at", "status_note", "classified_at", "intent", "one_liner", "human_read")
        item = conn.execute("SELECT " + ",".join(fields) + " FROM items WHERE id=?", (hold["item_id"],)).fetchone()
        snapshot["item"] = dict(item) if item else None
    if kind == "intake":
        snapshot["assignments"] = [dict(row) for row in conn.execute("SELECT iw.*,w.slug FROM item_workstreams iw JOIN workstreams w ON w.id=iw.workstream_id WHERE item_id=? ORDER BY w.slug", (identifier,))]
        snapshot["destinations"] = [dict(row) for row in conn.execute("SELECT * FROM workstreams ORDER BY slug")]
    return snapshot


def digest(snapshot):
    return hashlib.sha256(encoded(snapshot).encode()).hexdigest()


def is_filed(conn, target):
    if not exists(conn, "review_resolutions"): return False
    snapshot = _snapshot(conn, target)
    prior = conn.execute("SELECT snapshot_json,decision_json FROM review_resolutions WHERE target=? ORDER BY id DESC LIMIT 1", (target,)).fetchone()
    if not prior or json.loads(prior["decision_json"])["action"] != "file": return False
    original = json.loads(prior["snapshot_json"])
    return {key: value for key, value in original.items() if key != "last_resolution"} == {key: value for key, value in snapshot.items() if key != "last_resolution"}


def inspect(path, target):
    with closing(review._connect(path)) as conn:
        conn.execute("BEGIN")
        snapshot = _snapshot(conn, target)
        kind, item, hold = snapshot["kind"], snapshot["item"], snapshot["hold"]
        closed = (kind == "intake" and hold["disposition"] == "approved") or (kind == "advisory" and hold["status"] == "resolved") or is_filed(conn, target)
        actions = [] if closed else ["file"]
        if kind == "intake" and item and item["status"] not in ("stale", "rejected"):
            actions.insert(0, "classify")
        return {**snapshot, "digest": digest(snapshot), "actions": actions, "closed": closed}


def apply(path, target, *, expected, action, note, intent=None, workstreams=None):
    kind, identifier = target_parts(target)
    if action not in ("classify", "file") or action == "classify" and kind != "intake":
        raise ValueError("Choose an available review action.")
    if not isinstance(note, str) or not note.strip() or len(note) > 2000:
        raise ValueError("Supply a review note of 1–2000 characters.")
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("Use the displayed snapshot digest.")
    workstreams = [] if workstreams is None else workstreams
    if not isinstance(workstreams, list) or any(not isinstance(slug, str) for slug in workstreams) or len(set(workstreams)) != len(workstreams):
        raise ValueError("Choose distinct destination slugs.")
    if action == "classify" and intent not in CLASSIFIABLE_INTENTS:
        raise ValueError("Choose an explicit intent.")
    if action == "file" and (intent is not None or workstreams):
        raise ValueError("Filing a historical report cannot classify or route it.")
    decision = {"action": action, "intent": intent, "workstreams": sorted(workstreams), "note": note.strip(), "method": "explicit_local_review"}
    with closing(sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=rw", uri=True)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        if exists(conn, "review_resolutions"):
            prior = conn.execute("SELECT id,decision_json FROM review_resolutions WHERE target=? AND snapshot_hash=?", (target, expected)).fetchone()
            if prior:
                if prior["decision_json"] != encoded(decision): raise ValueError("This snapshot already has a different decision. Reopen the review.")
                return {"target": target, "resolution_id": prior["id"], "already_applied": True, "historical_receipt": True, "network_calls": 0, "backlog_writes": 0,
                        "refresh_workstreams": [], "message": "Earlier decision already saved. Nothing reapplied. Reopen to inspect current state; this is the historical receipt."}
        snapshot = _snapshot(conn, target)
        if digest(snapshot) != expected: raise ValueError("Source, review or routing state changed. Reopen the review; nothing was saved.")
        item, hold = snapshot["item"], snapshot["hold"]
        if kind == "advisory" and hold["status"] == "resolved" or action == "file" and (kind == "intake" and hold["disposition"] == "approved" or is_filed(conn, target)):
            raise ValueError("This review is already closed.")
        now = datetime.now(timezone.utc).isoformat()
        refresh = []
        if action == "classify":
            if not item or item["status"] in ("stale", "rejected"):
                raise ValueError("Terminal or missing items cannot be classified here. File the historical review instead.")
            active = {row["slug"]: row["id"] for row in snapshot["destinations"] if row["active"]}
            if not set(workstreams) <= active.keys(): raise ValueError("A selected destination is not active.")
            old = {row["slug"]: row for row in snapshot["assignments"]}
            unchanged = hold["source_hash"] == intake.source_hash(item) and item["intent"] == intent
            conn.execute("DELETE FROM item_workstreams WHERE item_id=?", (identifier,))
            for slug in workstreams:
                previous = old.get(slug, {}) if unchanged else {}
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed,routed_at) VALUES(?,?,?,?,?)",
                             (identifier, active[slug], previous.get("confidence"), previous.get("routed", 0), previous.get("routed_at")))
            conn.execute("UPDATE items SET intent=?,classified_at=? WHERE id=?", (intent, now, identifier))
            result = {"assessment": "approved", **decision, "snapshot_sha256": expected}
            conn.execute("UPDATE intake_reviews SET source_hash=?,disposition='approved',result_json=?,reviewed_at=? WHERE item_id=?",
                         (intake.source_hash(item), encoded(result), now, identifier))
            refresh = sorted(set(old) | set(workstreams))
        elif kind == "advisory":
            conn.execute("UPDATE advisory_reviews SET status='resolved',resolution_note=?,resolved_at=? WHERE id=?", (note.strip(), now, identifier))
        conn.execute(HISTORY_SQL)
        receipt = conn.execute("INSERT INTO review_resolutions(target,snapshot_hash,snapshot_json,decision_json,recorded_at) VALUES(?,?,?,?,?)",
                              (target, expected, encoded(snapshot), encoded(decision), now))
        return {"target": target, "resolution_id": receipt.lastrowid, "already_applied": False, "action": action, "refresh_workstreams": refresh,
                "network_calls": 0, "backlog_writes": 0, "message": "Saved locally. Backlog files unchanged. Filing does not verify a model judgment or complete its suggested action."}


def interactive(path, target, *, ask=input, emit=print):
    def safe(value):
        return "".join(char for char in str(value or "") if char.isprintable() or char in "\n\t")
    snapshot = inspect(path, target)
    item, hold = snapshot["item"], snapshot["hold"]
    emit(f"Review {target}. No model calls or backlog writes.")
    if item:
        emit(safe(item["title"]) + "\n" + safe(item["url"]))
        emit("Saved excerpt (first 800 characters):\n" + safe(item["body"][:800] if item["body"] else ""))
        emit("Lifecycle: " + safe(item["status"]) + ". Existing summary: " + safe(item["one_liner"]))
    if snapshot["kind"] == "advisory":
        emit(safe({key: hold[key] for key in ("source_owner", "action_owner", "source_ref", "current_version", "advisory_reason", "expires_at")}))
    else:
        emit("Saved assessment: " + safe(hold["result_json"][:4000]))
        emit("Filing acknowledges this historical report only, not current condition or collision truth.")
    if not snapshot["actions"]:
        emit("This exact review is already closed. Saved history remains available.")
        return
    try:
        action = ask("Choose " + " / ".join(snapshot["actions"]) + " / Enter to leave held: ").strip()
        if not action: emit("Left held. Nothing changed."); return
        if action not in snapshot["actions"]: raise ValueError("Choose an action shown for this review.")
        intent, destinations = None, []
        if action == "classify":
            emit("Existing assignments: " + safe(", ".join(row["slug"] for row in snapshot["assignments"]) or "none"))
            emit("Available destinations: " + safe(", ".join(row["slug"] + " (" + row["name"] + ")" for row in snapshot["destinations"] if row["active"])))
            intent = ask("Intent (" + ", ".join(CLASSIFIABLE_INTENTS) + "): ").strip()
            choice = ask("Complete destination set, comma-separated slugs, or none: ").strip()
            if not choice: emit("Left held. Nothing changed."); return
            destinations = [] if choice == "none" else [slug.strip() for slug in choice.split(",")]
            emit("New intent: " + safe(intent) + ". Destinations: " + safe(", ".join(destinations) or "none"))
        note = ask("Review note: ").strip()
        if ask("Save this explicit decision? Type yes: ").strip() != "yes":
            emit("Left held. Nothing changed."); return
    except (EOFError, KeyboardInterrupt):
        emit("Left held. Nothing changed.")
        return
    result = apply(path, target, expected=snapshot["digest"], action=action, note=note, intent=intent, workstreams=destinations)
    emit(result["message"])
    if result["refresh_workstreams"]:
        emit("Pending backlog refresh: " + safe(", ".join(result["refresh_workstreams"])) + ". Removed assignments can remain in old backlog files until regeneration.")
