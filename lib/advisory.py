"""Local ingestion and manual review of explicit external advisories."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import useful


SEVERITIES = {"info", "warning", "material", "urgent"}
RISK_TYPES = {"general", "safety", "financial"}
PACKET_FIELDS = {"version", "advisories"}
ADVISORY_FIELDS = {
    "source_owner", "action_owner", "source_ref", "current_version",
    "advisory_reason", "observed_at", "expires_at", "severity", "risk_type",
}
IMMUTABLE_FIELDS = tuple(sorted(ADVISORY_FIELDS))
TABLE_SQL = """CREATE TABLE IF NOT EXISTS advisory_reviews (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT UNIQUE NOT NULL,
    source_owner TEXT NOT NULL,
    action_owner TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    current_version TEXT NOT NULL,
    advisory_reason TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','warning','material','urgent')),
    risk_type TEXT NOT NULL CHECK(risk_type IN ('general','safety','financial')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    resolution_note TEXT,
    resolved_at TEXT,
    received_at TEXT NOT NULL
)"""


def _text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} requires 1–{limit} characters.")
    return value.strip()


def _time(value, name):
    return useful.parse_utc(value, f"{name} must be a timezone-aware ISO timestamp.")


def _dedup_key(row):
    identity = {key: row[key] for key in ("source_owner", "action_owner", "source_ref", "current_version")}
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_packet(raw, *, now=None):
    if (not isinstance(raw, dict) or set(raw) != PACKET_FIELDS
            or type(raw.get("version")) is not int or raw["version"] != 1):
        raise ValueError("Use a version-1 advisory packet with an advisories array.")
    advisories = raw["advisories"]
    if not isinstance(advisories, list) or not advisories or len(advisories) > 100:
        raise ValueError("Supply 1–100 advisories.")
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime.")
    now = now.astimezone(timezone.utc)
    result = []
    for raw_row in advisories:
        if not isinstance(raw_row, dict) or set(raw_row) != ADVISORY_FIELDS:
            raise ValueError("Invalid advisory fields.")
        row = {key: _text(value, key, 2000 if key == "advisory_reason" else 500)
               for key, value in raw_row.items()}
        if row["severity"] not in SEVERITIES:
            raise ValueError("severity must be info, warning, material or urgent.")
        if row["risk_type"] not in RISK_TYPES:
            raise ValueError("risk_type must be general, safety or financial.")
        observed = _time(row["observed_at"], "observed_at")
        expires = _time(row["expires_at"], "expires_at")
        if observed > now:
            raise ValueError("observed_at cannot be in the future.")
        if expires <= observed:
            raise ValueError("expires_at must follow observed_at.")
        row["observed_at"] = observed.isoformat()
        row["expires_at"] = expires.isoformat()
        row["dedup_key"] = _dedup_key(row)
        result.append(row)
    return result


def ingest_packet(path, db_path, *, now=None):
    packet_path = Path(path).expanduser()
    with packet_path.open("rb") as handle:
        data = handle.read(24001)
    if len(data) > 24000:
        raise ValueError("Advisory packet exceeds 24 KB.")
    advisories = validate_packet(json.loads(data), now=now)
    by_key = {}
    for row in advisories:
        payload = tuple(row[key] for key in IMMUTABLE_FIELDS)
        previous = by_key.setdefault(row["dedup_key"], payload)
        if previous != payload:
            raise ValueError("Conflicting advisory content for the same identity; use a new version.")
    unique = list({row["dedup_key"]: row for row in advisories}.values())
    with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute(TABLE_SQL)
        conn.execute("BEGIN IMMEDIATE")
        existing = {}
        for row in unique:
            found = conn.execute(
                "SELECT " + ",".join(IMMUTABLE_FIELDS) + " FROM advisory_reviews WHERE dedup_key=?",
                (row["dedup_key"],),
            ).fetchone()
            if found:
                existing[row["dedup_key"]] = found
                if tuple(found[key] for key in IMMUTABLE_FIELDS) != tuple(row[key] for key in IMMUTABLE_FIELDS):
                    raise ValueError("Conflicting advisory content for the same identity; use a new version.")
        inserted = len(unique) - len(existing)
        deduplicated = len(advisories) - inserted
        received_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        for row in unique:
            conn.execute(
                """INSERT INTO advisory_reviews
                   (dedup_key,source_owner,action_owner,source_ref,current_version,advisory_reason,
                    observed_at,expires_at,severity,risk_type,status,received_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?)
                   ON CONFLICT(dedup_key) DO NOTHING""",
                tuple(row[key] for key in (
                    "dedup_key", "source_owner", "action_owner", "source_ref", "current_version",
                    "advisory_reason", "observed_at", "expires_at", "severity", "risk_type",
                )) + (received_at,),
            )
        open_count = conn.execute("SELECT COUNT(*) FROM advisory_reviews WHERE status='open'").fetchone()[0]
        severe_count = conn.execute(
            "SELECT COUNT(*) FROM advisory_reviews WHERE status='open' AND severity IN ('material','urgent')"
        ).fetchone()[0]
    return {
        "events_received": len(advisories),
        "unique_events": len({row["dedup_key"] for row in advisories}),
        "inserted": inserted,
        "updated": 0,
        "deduplicated": deduplicated,
        "open_queue_count": open_count,
        "material_or_urgent_open": severe_count,
        "network_calls": 0,
        "automation_allowed": False,
    }


def review_queue(path, *, now=None):
    now = now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='advisory_reviews'"
        ).fetchone()
        if not table:
            return []
        rows = conn.execute(
            """SELECT id,source_owner,action_owner,source_ref,current_version,advisory_reason,
                      observed_at,expires_at,severity,risk_type,status,resolution_note
               FROM advisory_reviews WHERE status='open' ORDER BY
                 CASE severity WHEN 'urgent' THEN 0 WHEN 'material' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                 expires_at,id"""
        ).fetchall()
    current = now.astimezone(timezone.utc)
    result = []
    for row in rows:
        item = dict(row)
        item["expired"] = _time(item["expires_at"], "expires_at") <= current
        item["hold"] = True
        result.append(item)
    return result


def resolve(db_path, advisory_id, note=None):
    note = note or "Manually resolved."
    _text(note, "resolution_note", 2000)
    with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute(TABLE_SQL)
        row = conn.execute("SELECT id,status FROM advisory_reviews WHERE id=?", (advisory_id,)).fetchone()
        if not row:
            raise ValueError("Advisory review not found.")
        if row["status"] == "resolved":
            return {"id": advisory_id, "status": "resolved", "already_resolved": True}
        resolved_at = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE advisory_reviews SET status='resolved',resolution_note=?,resolved_at=? WHERE id=?",
                     (note, resolved_at, advisory_id))
    return {"id": advisory_id, "status": "resolved", "already_resolved": False}
