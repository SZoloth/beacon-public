"""Bounded, local-first curator runs for explicitly configured quiet mode."""

import hashlib
import json
import sqlite3
import uuid
import fcntl
from contextlib import contextmanager
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import advisory, db, deck, save_review

TABLE_SQL = """CREATE TABLE IF NOT EXISTS save_reviews (
    id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), source_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT 'local',
    status TEXT NOT NULL CHECK(status IN ('held','failed')), result_json TEXT NOT NULL,
    reviewed_at TEXT NOT NULL, UNIQUE(item_id, source_hash, context_hash, provider)
);
CREATE TABLE IF NOT EXISTS curator_runs (
    run_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    phase TEXT NOT NULL, result_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT
)"""


def init(path):
    with closing(sqlite3.connect(Path(path).expanduser().resolve())) as conn, conn:
        conn.executescript(TABLE_SQL)
        for sql in ("ALTER TABLE save_reviews ADD COLUMN context_hash TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE save_reviews ADD COLUMN provider TEXT NOT NULL DEFAULT 'local'"):
            db.apply_migration(conn, sql)
        unique_columns = []
        for index in conn.execute("PRAGMA index_list(save_reviews)").fetchall():
            if index[2]:
                unique_columns = [row[2] for row in conn.execute(f"PRAGMA index_info('{index[1]}')").fetchall()]
                break
        if unique_columns and unique_columns != ["item_id", "source_hash", "context_hash", "provider"]:
            conn.execute("ALTER TABLE save_reviews RENAME TO save_reviews_legacy")
            conn.executescript(TABLE_SQL.split("CREATE TABLE IF NOT EXISTS curator_runs", 1)[0])
            conn.execute("INSERT OR IGNORE INTO save_reviews(item_id,source_hash,status,result_json,reviewed_at) SELECT item_id,source_hash,status,result_json,reviewed_at FROM save_reviews_legacy")
            conn.execute("DROP TABLE save_reviews_legacy")


def _digest(path, limit=24001):
    if not path:
        return ""
    try:
        with Path(path).expanduser().open("rb") as handle:
            data = handle.read(limit)
    except OSError:
        return "missing:" + hashlib.sha256(str(Path(path).expanduser()).encode()).hexdigest()
    if len(data) >= limit:
        raise ValueError("Configured context exceeds 24 KB.")
    return hashlib.sha256(data).hexdigest()


def _db_revision(path):
    """Hash source identity/content incrementally; no database payload leaves the process."""
    digest = hashlib.sha256()
    uri = Path(path).expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT id,source,external_id,title,body,url,author,status,saved_at,synced_at FROM items "
            "ORDER BY id"
        )
        for row in rows:
            digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _provider(settings):
    provider = settings.get("provider", "local")
    if provider not in {"local", "typesafe"}:
        raise ValueError("curator provider must be local or typesafe")
    allow = settings.get("allow_upload") is True
    if provider == "typesafe" and not allow:
        raise ValueError("typesafe curator requires explicit saved allow_upload=true")
    return provider, allow


def review_queue(path):
    from .review_actions import exists, is_filed
    with closing(sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if not exists(conn, "save_reviews"): return []
        rows = conn.execute("""SELECT r.id,r.item_id,r.status,r.result_json,r.reviewed_at,i.title,i.url
            FROM save_reviews r JOIN items i ON i.id=r.item_id
            WHERE r.status IN ('held','failed') ORDER BY r.reviewed_at DESC,r.id DESC""").fetchall()
        rows = [row for row in rows if not is_filed(conn, f"save:{row['id']}")]
    result = []
    for row in rows:
        item = dict(row)
        payload = json.loads(item.pop("result_json"))
        item["reason"] = payload.get("error") or "Local save review requires human review."
        item["hold"] = True
        result.append(item)
    return result


def review_new_revisions(db_path, context_path, *, limit=20, now=None, provider="local",
                         allow_upload=False, max_calls=20, review_hash=None, retry_failed=False):
    """Review unseen revisions, paging past already-reviewed rows fairly."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Save review limit must be between 1 and 20.")
    if type(max_calls) is not int or not 0 <= max_calls <= 20:
        raise ValueError("max_calls must be between 0 and 20.")
    init(db_path)
    live_clock = now is None
    now = now or datetime.now(timezone.utc)
    clock = (lambda: datetime.now(timezone.utc)) if live_clock else (lambda: now)
    context_digest = _digest(context_path)
    context_hash = review_hash or context_digest
    provider, allow_upload = _provider({"provider": provider, "allow_upload": allow_upload})
    with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as conn:
        conn.row_factory = sqlite3.Row
        candidates, offset = [], 0
        while len(candidates) < limit:
            rows = conn.execute("SELECT id FROM items WHERE status='new' ORDER BY synced_at DESC,id LIMIT 100 OFFSET ?", (offset,)).fetchall()
            if not rows:
                break
            offset += len(rows)
            for row in rows:
                source_hash = save_review._revision(db_path, row["id"])
                prior = conn.execute("SELECT status FROM save_reviews WHERE item_id=? AND source_hash=? AND context_hash=? AND provider=?",
                                     (row["id"], source_hash, context_hash, provider)).fetchone()
                if not prior or (prior["status"] == "failed" and retry_failed):
                    candidates.append((row["id"], source_hash))
                    if len(candidates) >= limit:
                        break
    results, failures, calls = [], [], 0
    batched = {}
    batch_error = None
    batch_usage = {"input_tokens": 0, "output_tokens": 0}
    if provider == "typesafe" and candidates:
        try:
            batch = save_review.generate_many([item_id for item_id, _ in candidates], db_path, context_path,
                allow_upload=allow_upload, max_calls=max_calls, clock=clock)
            calls, batch_usage = batch["network_calls"], batch["usage"]
            batched = {row["item"]["id"]: row for row in batch["reviews"]}
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
            batch_error = str(error)
            calls = getattr(error, "network_calls", 0)
    for item_id, source_hash in candidates:
        try:
            if provider == "typesafe":
                if batch_error:
                    raise ValueError(batch_error)
                result = batched[item_id]
            else:
                result = save_review.generate(item_id, db_path, context_path, provider=provider,
                                              allow_upload=allow_upload, clock=clock)
            status = "held"
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
            result = {"item_id": item_id, "error": str(error), "automation_allowed": False}
            status, failures = "failed", [*failures, {"item_id": item_id, "error": str(error)}]
        with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if save_review._revision(db_path, item_id) != source_hash or _digest(context_path) != context_digest:
                failures.append({"item_id": item_id, "error": "Source or context changed before persistence; result discarded."})
                continue
            conn.execute("INSERT OR REPLACE INTO save_reviews(item_id,source_hash,context_hash,provider,status,result_json,reviewed_at) VALUES (?,?,?,?,?,?,?)",
                         (item_id, source_hash, context_hash, provider, status, json.dumps(result, ensure_ascii=False, allow_nan=False), now.isoformat()))
        results.append({"item_id": item_id, "status": status})
    return {"examined": len(candidates), "results": results, "failures": failures,
            "database_writes": len(results), "network_calls": calls, "usage": batch_usage,
            "failed_call_usage": "Unknown if a provider request failed; no retry.",
            "provider": provider, "automation_allowed": False}


@contextmanager
def _run_lock(path):
    lock_path = str(Path(path).expanduser().resolve()) + ".curator.lock"
    with open(lock_path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another curator run is active") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _window(settings, now):
    if settings.get("window"):
        window = settings["window"]
        if not isinstance(window, dict) or not window.get("since") or not window.get("until"):
            raise ValueError("curator window requires since and until")
        return window["since"], window["until"]
    hours = settings.get("rolling_window_hours")
    if type(hours) is int and 1 <= hours <= 168:
        return (now - timedelta(hours=hours)).isoformat(), now.isoformat()
    return None, None


def _inputs_unchanged(db_path, settings, db_hash, inbox_hash, context_hash, preferences_hash):
    return (_db_revision(db_path) == db_hash
            and (not settings.get("advisory_inbox") or _digest(settings.get("advisory_inbox")) == inbox_hash)
            and _digest(settings.get("context_path")) == context_hash
            and _digest(settings.get("preferences_path")) == preferences_hash)


def run_once(db_path, config, *, now=None):
    with _run_lock(db_path):
        return _run_once_locked(db_path, config, now=now)


def _run_once_locked(db_path, config, *, now=None):
    supplied_now = now
    now = now or datetime.now(timezone.utc)
    settings = config.get("curator", {})
    if not isinstance(settings, dict):
        raise ValueError("curator configuration must be an object")
    provider, allow_upload = _provider(settings)
    context_hash = _digest(settings.get("context_path"))
    preferences_hash = _digest(settings.get("preferences_path"))
    since, until = _window(settings, now)
    init(db_path)
    db_hash = _db_revision(db_path)
    inbox_hash = _digest(settings.get("advisory_inbox")) if settings.get("advisory_inbox") else ""
    identity = {"model": save_review.useful.MODEL, "save_review_policy": save_review.BATCH_POLICY,
                "task": settings.get("task"), "since": since, "until": until,
                "context": context_hash, "preferences": preferences_hash, "provider": provider,
                "allow_upload": allow_upload, "max_calls": settings.get("max_calls", 20),
                "max_candidates": settings.get("max_candidates", settings.get("candidates", 8)), "max_save_reviews": settings.get("max_save_reviews", 20),
                "db": db_hash, "inbox": inbox_hash}
    window_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    review_hash = hashlib.sha256(json.dumps({"model": save_review.useful.MODEL, "policy": save_review.BATCH_POLICY,
                                              "context": context_hash, "provider": provider,
                                              "allow_upload": allow_upload, "max_calls": settings.get("max_calls", 20),
                                              "max_save_reviews": settings.get("max_save_reviews", 20)}, sort_keys=True).encode()).hexdigest()
    with closing(sqlite3.connect(Path(db_path).expanduser().resolve(), timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute("SELECT run_id,phase,result_json FROM curator_runs WHERE status='running'").fetchall()
        for stale in running:
            interrupted = json.loads(stale["result_json"])
            interrupted.update(status="failed", interrupted=True,
                               error="curator stopped during this phase; remote completion is unknown")
            interrupted.setdefault("phases", {}).setdefault(stale["phase"], {}).update(
                status="failed", hold=True, reason="interrupted_unknown_completion")
            conn.execute("UPDATE curator_runs SET status='failed',result_json=?,finished_at=? WHERE run_id=?",
                         (json.dumps(interrupted, ensure_ascii=False, allow_nan=False), now.isoformat(), stale["run_id"]))
        previous = conn.execute("SELECT result_json FROM curator_runs WHERE status IN ('complete','failed') ORDER BY started_at DESC").fetchall()
        for row in previous:
            cached = json.loads(row["result_json"])
            if cached.get("window_key") == window_key:
                conn.commit()
                return {**cached, "network_calls": 0, "database_writes": 0,
                        "prior_network_calls": cached.get("network_calls", 0),
                        "prior_database_writes": cached.get("database_writes", 0), "reused": True}
        run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        phase, result = "advisory", {"run_id": run_id, "window_key": window_key, "phases": {}, "network_calls": 0, "database_writes": 0, "automation_allowed": False}
        conn.execute("INSERT INTO curator_runs VALUES (?,?,?,?,?,NULL)", (run_id, "running", phase, json.dumps(result), now.isoformat()))
        conn.commit()
    failed = False
    try:
        if not _inputs_unchanged(db_path, settings, db_hash, inbox_hash, context_hash, preferences_hash):
            raise ValueError("configured source/context changed before curator phases")
        if phase == "advisory":
            inbox = settings.get("advisory_inbox")
            if inbox:
                try:
                    result["phases"]["advisory"] = advisory.ingest_packet(inbox, db_path, now=now)
                except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
                    failed = True; result["phases"]["advisory"] = {"status": "failed", "error": str(error), "hold": True}
            else:
                result["phases"]["advisory"] = {"status": "not_configured", "hold": True}
            phase = "save_reviews"; _checkpoint(db_path, run_id, phase, result)
        if phase == "save_reviews":
            if _digest(settings.get("context_path")) != context_hash or _digest(settings.get("preferences_path")) != preferences_hash:
                raise ValueError("context or preferences changed before save review")
            result["phases"]["save_reviews"] = review_new_revisions(db_path, settings.get("context_path"), limit=settings.get("max_save_reviews", 20), now=supplied_now, provider=provider, allow_upload=allow_upload, max_calls=settings.get("max_calls", 20), review_hash=review_hash)
            result["network_calls"] += result["phases"]["save_reviews"]["network_calls"]; result["database_writes"] += result["phases"]["save_reviews"]["database_writes"]
            failed |= bool(result["phases"]["save_reviews"]["failures"]); phase = "deck"; _checkpoint(db_path, run_id, phase, result)
        if phase == "deck":
            if not _inputs_unchanged(db_path, settings, db_hash, inbox_hash, context_hash, preferences_hash):
                raise ValueError("configured source/context changed before deck")
            if provider == "typesafe" and result["network_calls"] >= settings.get("max_calls", 20):
                failed = True
                result["phases"]["deck"] = {"status": "budget_exhausted", "hold": True, "network_calls": 0, "automation_allowed": False}
            elif not since or not until or not settings.get("task"):
                failed = True; result["phases"]["deck"] = {"status": "not_configured", "hold": True, "automation_allowed": False}
            else:
                if provider == "typesafe":
                    result["network_calls"] += 1
                result["phases"]["deck"] = deck.generate(settings["task"], db_path, since, until, candidates=settings.get("max_candidates", settings.get("candidates", 8)), provider=provider, allow_upload=allow_upload, preferences_path=settings.get("preferences_path"))
            phase = "complete"
        changed = not _inputs_unchanged(db_path, settings, db_hash, inbox_hash, context_hash, preferences_hash)
        if changed:
            failed = True
            result["source_changed"] = True
        result["status"] = "failed" if failed else "complete"
        with closing(sqlite3.connect(Path(db_path).expanduser().resolve())) as conn, conn:
            conn.execute("UPDATE curator_runs SET status=?,phase=?,result_json=?,finished_at=? WHERE run_id=?", (result["status"], phase, json.dumps(result, ensure_ascii=False, allow_nan=False), now.isoformat(), run_id))
        return result
    except Exception as error:
        result.update(status="failed", error=str(error)); _checkpoint(db_path, run_id, phase, result, status="failed"); raise


def _checkpoint(path, run_id, phase, result, *, status="running"):
    with closing(sqlite3.connect(Path(path).expanduser().resolve())) as conn, conn:
        conn.execute("UPDATE curator_runs SET status=?,phase=?,result_json=? WHERE run_id=?", (status, phase, json.dumps(result, ensure_ascii=False, allow_nan=False), run_id))
