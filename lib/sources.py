"""Which intakes are live, and which are file handoffs. Reading this does not fetch."""

import os
import sqlite3


LIVE_ORDER = ("x", "github", "keep-local", "wiki", "omnisearch", "last30days", "readwise", "mymind", "readwise-notes", "granola-digests")

HANDOFFS = (
    {
        "name": "readwise-file",
        "mode": "handoff",
        "live": False,
        "command": "beacon import-saves PATH",
        "checkpoint_key": "readwise",
        "note": "Historical JSONL, and files dropped in handoffs/readwise. The weekly sync also lists Reader when READWISE_ACCESS_TOKEN is set. This status command does not call Reader and does not test that token.",
    },
    {
        "name": "mymind",
        "mode": "handoff",
        "live": False,
        "command": "beacon import-mymind PATH",
        "checkpoint_key": None,
        "note": "Local cards.csv import. The live source is beacon sync --source mymind, which reads the owner's tags and notes through the mymind CLI.",
    },
    {
        "name": "x-handoff",
        "mode": "handoff",
        "live": False,
        "command": "beacon import-x PATH",
        "checkpoint_key": None,
        "note": "Optional bookmark and like file. Live X bookmarks stay on bird via beacon sync --source x.",
    },
    {
        "name": "manual",
        "mode": "manual",
        "live": False,
        "command": "beacon add URL",
        "checkpoint_key": None,
        "note": "One URL at a time.",
    },
)


def coverage(config, db_path=None):
    configured = config.get("sources", {})
    states = _checkpoints(db_path)
    live = []
    for name in sorted(configured):
        settings = configured[name] if isinstance(configured[name], dict) else {}
        state = states.get(name)
        live.append({
            "name": name,
            "mode": "live",
            "live": True,
            "method": settings.get("method"),
            "last_sync": state.get("last_sync") if state else None,
            "checkpoint": state.get("last_id") if state else None,
            "items_synced": state.get("items_synced") if state else None,
        })
    handoffs = []
    for spec in HANDOFFS:
        state = states.get(spec["checkpoint_key"]) if spec["checkpoint_key"] else None
        handoffs.append({
            **spec,
            "last_sync": state.get("last_sync") if state else None,
            "checkpoint": state.get("last_id") if state else None,
            "items_synced": state.get("items_synced") if state else None,
        })
    return {
        "live_sources": live,
        "handoffs": handoffs,
        "configured_live": sorted(configured),
        "missing_live_sources": [name for name in LIVE_ORDER if name not in configured],
        "readwise_is_live": configured.get("readwise", {}).get("method") == "reader_api",
        "readwise_auth_tested": False,
    }


def _checkpoints(db_path):
    if not db_path or not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_state'").fetchone()
        if row is None:
            return {}
        found = conn.execute("SELECT source, last_id, last_sync, items_synced FROM sync_state").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {item["source"]: dict(item) for item in found}
