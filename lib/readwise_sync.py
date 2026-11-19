"""Incremental Reader tweet intake. Uses the documented list API and the existing token.

This module does not read the token until a sync runs. A unit test can pass a token and an opener.
Nothing here claims the token was accepted unless a list response was HTTP 200 in that run.
"""

import hashlib
import html
import json
import os
import re
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from lib import db, reader, saves_handoff
from lib.x_handoff import _STATUS_URL, _apply, _author_column, _catalog_raw


LIST_URL = "https://readwise.io/api/v3/list/"
_API_KEY = "readwise-api"
_FILES_KEY = "readwise-files"
_HYDRATE_LIMIT = 50


def fetch_readwise(count=200, handoff_dir=None, db_path=None, token=None, opener=None, full=False):
    """Import dropped files, then list Reader tweets. Missing token fails after the files."""
    if full:
        count = max(count, 2000)
    files = _ingest_handoff_dir(handoff_dir, db_path)
    resolved = reader._token() if token is None else token
    if not resolved:
        raise RuntimeError(
            "READWISE_ACCESS_TOKEN is not set; add it to ~/.config/shell/secrets.zsh. "
            "Reader was not called. Auth was not tested. "
            f"Handoff files imported this run: {files['imported']}."
        )
    api = _ingest_api(count, db_path, resolved, opener or _open)
    return {"live": True, "auth_tested": api["list_ok"], "files": files, **api}


def _open(url, token):
    request = Request(url, headers={"Authorization": f"Token {token}"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode() or "{}")
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise RuntimeError(f"Readwise Reader list failed: {error}") from None


def _ingest_handoff_dir(handoff_dir, db_path):
    if not handoff_dir or not os.path.isdir(os.path.expanduser(handoff_dir)):
        return {"imported": 0, "directory": handoff_dir or "", "present": False}
    directory = Path(handoff_dir).expanduser()
    seen = set(_json_state(db_path, _FILES_KEY))
    imported = 0
    for path in sorted(directory.glob("*.jsonl")) + sorted(directory.glob("*.json")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            continue
        saves_handoff.import_saves(path, db_path)
        seen.add(digest)
        imported += 1
        _write_state(db_path, _FILES_KEY, sorted(seen))
    return {"imported": imported, "directory": str(directory), "present": True}


def _ingest_api(count, db_path, token, opener):
    db.init_db(db_path)
    state = _api_state(db_path)
    cursor = state.get("page_cursor") or ""
    updated_after = "" if cursor else (state.get("updated_after") or "")
    newest = updated_after
    fetched = 0
    added = 0
    hydrated = 0
    skipped = 0
    list_ok = False
    conn = db.get_connection(db_path)
    synced_at = datetime.now(timezone.utc).isoformat()
    try:
        while fetched < count:
            payload = opener(_list_url(cursor, updated_after), token)
            list_ok = True
            results = _results(payload)
            if not results:
                next_cursor = payload.get("nextPageCursor") or ""
                _write_state(db_path, _API_KEY, {
                    "updated_after": newest if not next_cursor else updated_after,
                    "page_cursor": next_cursor, "list_ok": True,
                })
                break
            with conn:
                for document in results:
                    if fetched >= count:
                        break
                    fetched += 1
                    updated = document.get("updated_at") or ""
                    if updated and (not newest or updated > newest):
                        newest = updated
                    post = document_to_post(document)
                    if post is None:
                        skipped += 1
                        continue
                    outcome = _write_post(conn, post, synced_at)
                    if outcome == "added":
                        added += 1
            next_cursor = payload.get("nextPageCursor") or ""
            if fetched >= count and next_cursor:
                _write_state(db_path, _API_KEY, {"updated_after": updated_after, "page_cursor": next_cursor, "list_ok": True})
                break
            if not next_cursor:
                _write_state(db_path, _API_KEY, {"updated_after": newest, "page_cursor": "", "list_ok": True})
                cursor = ""
                break
            cursor = next_cursor
            _write_state(db_path, _API_KEY, {"updated_after": updated_after, "page_cursor": cursor, "list_ok": True})
        else:
            _write_state(db_path, _API_KEY, {"updated_after": newest, "page_cursor": cursor, "list_ok": list_ok})
        hydrated = _hydrate_pending(conn, opener, token, synced_at, _HYDRATE_LIMIT)
    finally:
        conn.close()
    return {
        "fetched": fetched, "added": added, "skipped_not_status": skipped, "hydrated": hydrated,
        "list_ok": list_ok, "page_cursor": cursor, "updated_after": newest,
    }


def _hydrate_pending(conn, opener, token, synced_at, limit):
    rows = conn.execute(
        """SELECT raw_json FROM items
           WHERE source='readwise' AND status='pending'
           ORDER BY id LIMIT ?""",
        (limit,),
    ).fetchall()
    hydrated = 0
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError):
            continue
        reader_id = (raw.get("readwise") or {}).get("reader_id")
        if not reader_id:
            continue
        payload = opener(_list_url("", "", reader_id), token)
        results = _results(payload)
        if not results:
            continue
        post = document_to_post(results[0])
        if post is None or post["extra"]["quality"] != "full":
            continue
        with conn:
            outcome = _write_post(conn, post, synced_at)
        if outcome == "promoted":
            hydrated += 1
    return hydrated


def _results(payload):
    if not isinstance(payload, dict):
        raise RuntimeError("Readwise Reader list returned no document object.")
    if isinstance(payload.get("results"), list):
        return payload["results"]
    if payload.get("id"):
        return [payload]
    raise RuntimeError("Readwise Reader list returned no results list.")


def document_to_post(document):
    if not isinstance(document, dict) or document.get("parent_id"):
        return None
    url = (document.get("source_url") or "").split("?")[0].split("#")[0]
    match = _STATUS_URL.match(url)
    if match is None:
        return None
    title = document.get("title") if isinstance(document.get("title"), str) else ""
    body, quality = _body(title, document.get("html_content"))
    source = document.get("source") if isinstance(document.get("source"), str) else ""
    saved = document.get("saved_at") or document.get("created_at") or None
    readwise = {
        "reader_id": str(document.get("id") or ""),
        "reader_saved_at": saved,
        "reader_source": source,
        "reader_updated_at": document.get("updated_at") or "",
    }
    if not readwise["reader_id"]:
        return None
    author_name = document.get("author") if isinstance(document.get("author"), str) else ""
    return {
        "post_id": match.group(2),
        "url": url,
        "text": body,
        "created_at": "",
        "signals": {},
        "author": {"username": "", "name": author_name},
        "media": None,
        "like_count": None,
        "handoff": {"origin": "reader_api", "reader_id": readwise["reader_id"]},
        "extra": {
            "readwise": readwise,
            "quality": quality,
            "echo": source.strip().casefold() == "beacon",
        },
    }


def _body(title, html_content):
    plain = _plain(html_content)
    title = (title or "").strip()
    if plain and plain.casefold() != title.casefold() and len(plain) > len(title):
        return plain, "full"
    if title:
        return title, "title_only"
    if plain:
        return plain, "full"
    return "", "empty"


def _plain(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?>.*?</\1>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _list_url(cursor, updated_after, document_id=""):
    params = [("category", "tweet"), ("withHtmlContent", "true")]
    if document_id:
        params = [("id", document_id), ("withHtmlContent", "true")]
    else:
        if cursor:
            params.append(("pageCursor", cursor))
        elif updated_after:
            params.append(("updatedAfter", updated_after))
    return LIST_URL + "?" + urlencode(params)


def _write_post(conn, post, synced_at):
    return _apply(
        conn, post["post_id"], post["signals"],
        title=post["text"][:100] if post["text"] else "",
        body=post["text"], url=post["url"], author=_author_column(post["author"]),
        created_at=post["created_at"], raw_for=partial(_catalog_raw, post),
        synced_at=synced_at, extra=post["extra"],
    )


def _api_state(db_path):
    stored = _json_state(db_path, _API_KEY)
    if isinstance(stored, dict):
        return stored
    watermark = _scalar_state(db_path, "readwise")
    if watermark and watermark[:4].isdigit():
        return {"updated_after": watermark, "page_cursor": ""}
    return {"updated_after": "", "page_cursor": ""}


def _json_state(db_path, key):
    raw = _scalar_state(db_path, key)
    if not raw:
        return [] if key == _FILES_KEY else {}
    try:
        return json.loads(raw)
    except ValueError:
        return [] if key == _FILES_KEY else {}


def _scalar_state(db_path, key):
    if db_path and not os.path.exists(db_path):
        return ""
    conn = db.get_connection(db_path)
    try:
        row = conn.execute("SELECT last_id FROM sync_state WHERE source=?", (key,)).fetchone()
    except Exception:
        return ""
    finally:
        conn.close()
    return row["last_id"] if row and row["last_id"] else ""


def _write_state(db_path, key, value):
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO sync_state (source, last_id, last_sync, items_synced)
               VALUES (?, ?, ?, 0)
               ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id, last_sync=excluded.last_sync""",
            (key, json.dumps(value, sort_keys=True), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
