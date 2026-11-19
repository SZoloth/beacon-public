"""Fetch external items into Beacon."""

import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, x_handoff


def _utcnow():
    return datetime.now(timezone.utc)


def _now_iso():
    return _utcnow().isoformat()


def _iso_from_unix_ms(value):
    if value in (None, ""):
        return ""
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


# A run this long of already-stored bookmarks means the sync has caught up,
# even when the saved marker tweet was later unbookmarked.
KNOWN_RUN_TO_STOP = 20


# X lets a signed-in account read only so fast, and bird reads as that account. Go slowly,
# and when X pushes back, stop for a while instead of asking again on the next run.
PAGE_DELAY_SECONDS = 1.5
COOLDOWN_KEY = "x-cooldown"
RATE_LIMIT_COOLDOWN = timedelta(minutes=15)  # a 429 that names no wait
CHALLENGE_COOLDOWN = timedelta(hours=24)  # X asked for a human check; asking again can lock the account
MAX_COOLDOWN = timedelta(hours=24)
_RETRY_AFTER = re.compile(r"retry[- ]after\D{0,3}(\d+)", re.IGNORECASE)
_RATE_LIMITED = re.compile(r"\b429\b|rate.?limit|too many requests", re.IGNORECASE)
_CHALLENGE = re.compile(r"captcha|challenge|checkpoint|suspended|unusual activity", re.IGNORECASE)


def _cooldown_for(stderr):
    """How long to leave X alone after a failed bird call, and why; None if the error isn't X pushing back."""
    if _CHALLENGE.search(stderr):
        return CHALLENGE_COOLDOWN, "X asked for a human check"
    retry_after = _RETRY_AFTER.search(stderr)
    if retry_after:
        return min(timedelta(seconds=int(retry_after.group(1))), MAX_COOLDOWN), "X rate limit"
    if _RATE_LIMITED.search(stderr):
        return RATE_LIMIT_COOLDOWN, "X rate limit"
    return None


def _start_cooldown(stderr):
    cooldown = _cooldown_for(stderr or "")
    if not cooldown:
        return
    wait, reason = cooldown
    now = _utcnow()
    db.update_sync_state(COOLDOWN_KEY, json.dumps({"until": (now + wait).isoformat(), "reason": reason}), now.isoformat(), 0)


def _active_cooldown():
    state = db.get_sync_state(COOLDOWN_KEY)
    try:
        cooldown = json.loads(state["last_id"]) if state and state["last_id"] else None
        until = datetime.fromisoformat(cooldown["until"])
    except (ValueError, KeyError, TypeError):
        return None
    return (until, cooldown.get("reason") or "X rate limit") if until > _utcnow() else None


def _stored_x_ids(ids):
    if not ids:
        return set()
    conn = db.get_connection()
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT external_id FROM items WHERE source='x' AND external_id IN ({placeholders})",
            list(ids),
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def _expand(path):
    return os.path.expanduser(path) if path else path


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _body_from_keep_local(row, content_root):
    for key in ("ai_text_key", "content_key"):
        rel_path = row.get(key)
        if not rel_path:
            continue
        body = _read_text(Path(content_root) / rel_path)
        if body:
            return body
    return row.get("notes") or row.get("title") or ""


def _markdown_title(path, body):
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ")


def _fetch_bird_bookmark_page(cursor=None):
    command = ["bird", "bookmarks"]
    if cursor:
        command.extend(["--cursor", cursor])
    else:
        command.append("--all")
    command.extend(["--max-pages", "1", "--json"])
    keychain = os.environ.get("BEACON_X_KEYCHAIN") == "1"
    if keychain:
        command = [str(Path.home() / ".local/bin/with-secret"), "BEACON_X_AUTH_TOKEN", "BEACON_X_CT0", "--",
                   "/bin/sh", "-c", 'export AUTH_TOKEN="$BEACON_X_AUTH_TOKEN" CT0="$BEACON_X_CT0"; '
                   'unset BEACON_X_AUTH_TOKEN BEACON_X_CT0; exec "$@"', "beacon-x", *command]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        if keychain:
            print("  bird could not fetch X bookmarks using the configured Keychain credentials.")
        elif result.stderr.strip():
            print(f"  bird error: {result.stderr.strip()}")
        if not result.stdout.strip():
            _start_cooldown(result.stderr)
            return [], None, False

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("  Failed to parse bird JSON output.")
        return [], None, False

    if isinstance(payload, list):
        return payload, None, True
    if isinstance(payload, dict) and isinstance(payload.get("tweets"), list):
        return payload["tweets"], payload.get("nextCursor"), True

    print("  Unexpected bird output format.")
    return [], None, False


def store_tweet(tweet, synced_at, strict=False):
    """Save one post as bird returns it.

    True when the post was not already a source x save. Promoting an x-like row counts.
    """
    return x_handoff.record_bird_bookmark(tweet, synced_at, strict=strict)


INCREMENTAL_KEY = "x-incremental"


def _pending_incremental():
    """Where an unfinished incremental sync stopped: {"cursor": page to read next ("" = newest), "latest": ID to promote}."""
    state = db.get_sync_state(INCREMENTAL_KEY)
    if not state or not state.get("last_id"):
        return None
    try:
        pending = json.loads(state["last_id"])
    except ValueError:
        return None
    if not isinstance(pending, dict):
        return None
    return {"cursor": pending.get("cursor") or "", "latest": pending.get("latest") or ""}


def _save_pending_incremental(cursor, latest, synced_at):
    value = json.dumps({"cursor": cursor, "latest": latest}) if cursor is not None else ""
    db.update_sync_state(INCREMENTAL_KEY, value, synced_at, 0)


def fetch_x_bookmarks(count=1200, stop_at_known=True):
    """Fetch X bookmarks page by page until a known ID or safety cap.

    The saved marker (`x`) only moves once a run has read every page between the newest
    bookmark and the old marker. A run that stops early (a failed page, a timeout, the count
    cap, a failed write) saves the page to resume from instead, and the next run continues
    there rather than starting again at the newest page and stopping at what it just stored.
    """
    print(f"Fetching up to {count} X bookmarks...")
    if count <= 0:
        return 0
    cooldown = _active_cooldown()
    if cooldown:
        until, reason = cooldown
        raise RuntimeError(
            f"X bookmarks are cooling down until {until.astimezone().strftime('%Y-%m-%d %H:%M %Z')} ({reason}). "
            "Syncing now would ask X again too soon; run it after that time. Nothing is lost: the sync resumes where it stopped.")
    sync_state = db.get_sync_state("x")
    known_id = sync_state.get("last_id") if sync_state and stop_at_known else None
    seen_ids = set()
    cursor = None
    latest_id = None
    # Deep backfills get rate limited; remember where one stopped so the next
    # --full run continues from there instead of re-reading the newest pages.
    backfill_state = None if stop_at_known else db.get_sync_state("x-backfill")
    backfill_resumed = bool(backfill_state and backfill_state.get("last_id"))
    if backfill_resumed:
        cursor = backfill_state["last_id"]
        print("  Resuming previous backfill.")
    pending = _pending_incremental() if stop_at_known else None
    if pending:
        cursor = pending["cursor"] or None
        # From a later page the newest ID is already known; from the top, the first page says.
        latest_id = (pending["latest"] or None) if cursor else None
        print("  Resuming previous sync.")
    # Counted across pages: bird pages can be smaller than the stop run.
    known_run = 0
    pages_read = 0
    fetched = 0
    count_new = 0
    synced_at = _now_iso()
    resume_cursor = ""  # page the next run must start at; "" means this run finished
    failed_write = False

    while fetched < count:
        if pages_read:
            time.sleep(PAGE_DELAY_SECONDS)
        try:
            page, next_cursor, ok = _fetch_bird_bookmark_page(cursor=cursor)
        except FileNotFoundError:
            if not pages_read:
                raise RuntimeError("X bookmarks didn't sync: bird CLI not found.") from None
            print("  bird CLI not found.")
            ok, page, next_cursor = False, [], None
        except subprocess.TimeoutExpired:
            if not pages_read:
                raise RuntimeError("X bookmarks didn't sync: bird CLI timed out.") from None
            print("  bird CLI timed out.")
            ok, page, next_cursor = False, [], None
        if not ok:
            if not pages_read:
                # Nothing fetched at all (signed out, locked Keychain, no network): fail loudly
                # so the weekly job and the app's Sync don't report a quiet "0 new".
                raise RuntimeError("X bookmarks didn't sync: bird couldn't fetch the first page (see the bird error above).")
            resume_cursor = cursor or ""
            break
        pages_read += 1
        if page and latest_id is None and not backfill_resumed:
            latest_id = str(page[0].get("id", ""))

        reached_known = False
        # Once a run has resumed, "already stored" can't mean "caught up": the page that
        # failed may sit behind bookmarks this very run (or the last one) saved.
        stored = _stored_x_ids([str(t.get("id", "")) for t in page]) if stop_at_known and not pending else set()
        fresh = []
        for tweet in page:
            tweet_id = str(tweet.get("id", ""))
            if not tweet_id or tweet_id in seen_ids:
                continue
            if known_id and tweet_id == known_id:
                reached_known = True
                break
            if tweet_id in stored:
                known_run += 1
                if known_run >= KNOWN_RUN_TO_STOP:
                    reached_known = True
                    break
                continue
            known_run = 0
            seen_ids.add(tweet_id)
            # Always keep the whole page: the saved cursor already points past it.
            fresh.append(tweet)

        for tweet in fresh:
            try:
                if store_tweet(tweet, synced_at, strict=True):
                    count_new += 1
            except Exception as error:
                print(f"  Error saving X bookmark {tweet.get('id', '')}: {error}")
                failed_write = True
        fetched += len(fresh)
        if failed_write:
            # Read this page again next time; nothing past it counts as done.
            resume_cursor = cursor or ""
            break

        if reached_known or not next_cursor:
            break
        if fetched >= count:
            resume_cursor = next_cursor
            break
        cursor = next_cursor

    complete = not resume_cursor and not failed_write and (pages_read and (reached_known or not next_cursor))
    previous_id = sync_state.get("last_id") if sync_state else ""

    if stop_at_known:
        if complete:
            marker = latest_id or known_id or previous_id or ""
            _save_pending_incremental(None, None, synced_at)
        else:
            marker = previous_id or ""
            _save_pending_incremental(resume_cursor, latest_id or "", synced_at)
        db.update_sync_state("x", marker, synced_at, count_new)
    else:
        if not failed_write:
            db.update_sync_state("x", latest_id or known_id or previous_id or "", synced_at, count_new)
        # A failed save on the first page leaves no cursor to keep; the backfill starts over there.
        if resume_cursor or not failed_write:
            db.update_sync_state("x-backfill", resume_cursor, synced_at, count_new)
        if complete:
            # A finished full pass makes any half-done incremental sync moot.
            _save_pending_incremental(None, None, synced_at)
        elif resume_cursor:
            print("  Backfill incomplete; run `beacon sync --source x --full` again later to continue.")

    if failed_write:
        raise RuntimeError(
            "X bookmarks didn't fully sync: some posts couldn't be saved (see above). "
            "The next sync retries from that page.")
    print(f"  Synced {count_new} X bookmarks.")
    return count_new


def fetch_github_stars():
    """Fetch GitHub starred repos using gh api."""
    print("Fetching GitHub stars...")
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github.star+json",
                "/user/starred?per_page=100",
                "--paginate",
                "--jq",
                '.[] | {starred_at, full_name: .repo.full_name, description: .repo.description, html_url: .repo.html_url, owner: .repo.owner.login, created_at: .repo.created_at, stargazers_count: .repo.stargazers_count, language: .repo.language, topics: .repo.topics}',
            ],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        print("  gh CLI not found.")
        raise RuntimeError("GitHub stars didn't sync: gh CLI not found. Install it (brew install gh), "
                           "then run gh auth login.") from None
    except subprocess.TimeoutExpired:
        print("  gh API timed out.")
        raise RuntimeError("GitHub stars didn't sync: gh API timed out.") from None

    if result.returncode != 0:
        detail = result.stderr.strip() or "gh failed"
        print(f"  gh error: {detail}")
        raise RuntimeError(f"GitHub stars didn't sync: {detail}")

    synced_at = _now_iso()
    count_new = 0

    # gh --jq outputs one JSON object per line
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            repo = json.loads(line)
        except json.JSONDecodeError:
            continue

        full_name = repo.get("full_name", "")
        if not full_name:
            continue

        if db.upsert_item(
            source="github",
            external_id=full_name,
            title=full_name.split("/")[-1],
            body=repo.get("description", "") or "",
            url=repo.get("html_url", f"https://github.com/{full_name}"),
            author=repo.get("owner", ""),
            raw_json=json.dumps(repo),
            created_at=repo.get("created_at", ""),
            synced_at=synced_at,
            saved_at=repo.get("starred_at", ""),
        ):
            count_new += 1

    db.update_sync_state("github", "", synced_at, count_new)
    print(f"  Synced {count_new} GitHub stars.")
    return count_new


def fetch_keep_local(count=50, db_path=None, content_root=None):
    """Fetch recent keep-local items from its local SQLite store."""
    if not db_path:
        print("  keep-local needs sources.keep-local.db_path in config.json. Skipping.")
        return 0
    db_path = _expand(db_path)
    content_root = _expand(content_root or os.path.dirname(db_path))

    if not os.path.exists(db_path):
        print(f"  keep-local DB not found at {db_path}. Skipping.")
        return 0

    print(f"Fetching up to {count} keep-local items...")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, url, title, author, platform, created_at, updated_at, status,
                   notes, content_key, ai_text_key, deleted_at
            FROM items
            WHERE status = 'active' AND deleted_at IS NULL
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (count,),
        ).fetchall()
    finally:
        conn.close()

    synced_at = _now_iso()
    count_new = 0
    for row in rows:
        record = dict(row)
        if db.upsert_item(
            source="keep-local",
            external_id=record["id"],
            title=record.get("title") or record["id"],
            body=_body_from_keep_local(record, content_root),
            url=record.get("url"),
            author=record.get("author") or record.get("platform") or "keep-local",
            raw_json=json.dumps(record),
            created_at=_iso_from_unix_ms(record.get("created_at")),
            synced_at=_iso_from_unix_ms(record.get("updated_at")) or synced_at,
        ):
            count_new += 1

    db.update_sync_state("keep-local", "", synced_at, count_new)
    print(f"  Synced {count_new} keep-local items.")
    return count_new


def fetch_markdown_directory(source, root, count=50, exclude_names=None, author=None):
    """Fetch markdown artifacts from a local directory tree."""
    root_path = Path(_expand(root))
    exclude_names = set(exclude_names or set())

    if not root_path.exists():
        print(f"  {source} root not found at {root_path}. Skipping.")
        return 0

    print(f"Fetching up to {count} {source} markdown artifacts...")
    files = sorted(
        (path for path in root_path.rglob("*.md") if path.name not in exclude_names),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:count]

    count_new = 0
    for path in files:
        body = _read_text(path)
        if body is None:
            continue
        stat = path.stat()
        synced_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
        created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
        if db.upsert_item(
            source=source,
            external_id=str(path.relative_to(root_path)),
            title=_markdown_title(path, body),
            body=body,
            url=None,
            author=author or source,
            raw_json=json.dumps({"path": str(path)}),
            created_at=created_at,
            synced_at=synced_at,
        ):
            count_new += 1

    db.update_sync_state(source, "", _now_iso(), count_new)
    print(f"  Synced {count_new} {source} artifacts.")
    return count_new


def _fetch_last30days_from_db(db_path, count):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT f.id, t.name AS topic, f.source, f.source_url, f.source_title, f.author,
                   f.summary, f.content, f.first_seen, f.last_seen
            FROM findings f
            JOIN topics t ON t.id = f.topic_id
            ORDER BY f.last_seen DESC
            LIMIT ?
            """,
            (count,),
        ).fetchall()
    finally:
        conn.close()

    count_new = 0
    for row in rows:
        record = dict(row)
        body_parts = [record.get("summary") or "", record.get("content") or ""]
        body = "\n\n".join(part for part in body_parts if part).strip()
        synced_at = record.get("last_seen") or _now_iso()
        if db.upsert_item(
            source="last30days",
            external_id=f"finding:{record['id']}",
            title=record.get("source_title") or record.get("topic") or f"finding {record['id']}",
            body=body,
            url=record.get("source_url"),
            author=record.get("author") or record.get("topic") or "last30days",
            raw_json=json.dumps(record),
            created_at=record.get("first_seen") or synced_at,
            synced_at=synced_at,
        ):
            count_new += 1
    return count_new


def _fetch_last30days_from_output(output_dir):
    context_path = Path(output_dir) / "last30days.context.md"
    if not context_path.exists():
        return 0

    body = _read_text(context_path)
    if body is None:
        return 0

    stat = context_path.stat()
    synced_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    created_at = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
    if db.upsert_item(
        source="last30days",
        external_id=f"context:{context_path.name}",
        title="last30days context",
        body=body,
        url=None,
        author="last30days",
        raw_json=json.dumps({"path": str(context_path)}),
        created_at=created_at,
        synced_at=synced_at,
    ):
        return 1
    return 0


def fetch_last30days(count=50, db_path=None, output_dir=None):
    """Fetch recent last30days findings from its local store or latest output artifact."""
    db_path = _expand(db_path or "~/.local/share/last30days/research.db")
    output_dir = _expand(output_dir or "~/.local/share/last30days/out")

    print(f"Fetching up to {count} last30days findings...")
    count_new = 0
    if os.path.exists(db_path):
        count_new = _fetch_last30days_from_db(db_path, count)
        if count_new == 0 and os.path.isdir(output_dir):
            count_new = _fetch_last30days_from_output(output_dir)
    elif os.path.isdir(output_dir):
        count_new = _fetch_last30days_from_output(output_dir)
    else:
        print(f"  last30days store not found at {db_path} or {output_dir}. Skipping.")
        return 0

    db.update_sync_state("last30days", "", _now_iso(), count_new)
    print(f"  Synced {count_new} last30days items.")
    return count_new
