"""Local historical-save handoff. JSONL or x-saves-v1. No network and no model."""

import json
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from lib import db
from lib.x_handoff import (
    _STATUS_URL,
    _apply,
    _author_column,
    _catalog_raw,
    _iso,
    _signal,
)


_SOURCE_TYPES = ("bookmark", "like", "readwise_tweet")
_SIGNALS = ("bookmark", "like")


def parse_saves(path):
    text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    posts = _merge_posts([_parse_post(post) for post in _documents(text)])
    return posts, _diagnostics(posts)


def import_saves(path, db_path=None, preview=False):
    posts, diagnostics = parse_saves(path)
    result = {
        "source": "saves", "preview": bool(preview), "live": False, "mode": "handoff",
        "reader_saved_at_max": _file_watermark(posts), **diagnostics,
    }
    if preview:
        return result
    if db_path is not None and not str(db_path).strip():
        raise ValueError("Database path cannot be empty.")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    synced_at = datetime.now(timezone.utc).isoformat()
    counts = {"added": 0, "refreshed": 0, "promoted": 0}
    try:
        with conn:
            for post in posts:
                outcome = _apply(
                    conn, post["post_id"], post["signals"],
                    title=post["text"][:100] if post["text"] else "",
                    body=post["text"], url=post["url"], author=_author_column(post["author"]),
                    created_at=post["created_at"], raw_for=partial(_catalog_raw, post),
                    synced_at=synced_at, extra=post["extra"],
                )
                counts[outcome] += 1
            watermark = _advance_readwise_checkpoint(conn, posts, synced_at, counts["added"])
    finally:
        conn.close()
    return {**result, **counts, "live": False, "mode": "handoff", "readwise_checkpoint": watermark}


def _documents(text):
    stripped = text.lstrip()
    if not stripped:
        raise ValueError("Handoff file is empty.")
    if stripped[0] in "[{":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            if payload.get("format") not in (None, "x-saves-v1"):
                raise ValueError("Unsupported handoff format. Use x-saves-v1.")
            if "posts" in payload:
                posts = payload.get("posts")
                if not isinstance(posts, list):
                    raise ValueError("Handoff posts must be a list.")
                return posts
            if "post_id" in payload:
                return [payload]
        if stripped[0] == "[":
            raise ValueError("Invalid JSON.")
    posts = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON on line {line_number}.") from None
        if not isinstance(item, dict):
            raise ValueError(f"Line {line_number} must be an object.")
        posts.append(item)
    if not posts:
        raise ValueError("Handoff file is empty.")
    return posts


def _parse_post(post):
    if not isinstance(post, dict):
        raise ValueError("Each post must be an object.")
    post_id = post.get("post_id")
    if not isinstance(post_id, str) or not post_id.isdigit():
        raise ValueError("post_id must be a digit string.")
    url = post.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("url must be an https X status URL.")
    match = _STATUS_URL.match(url)
    if match is None or match.group(2) != post_id:
        raise ValueError("url must match post_id.")
    text = post.get("text")
    if not isinstance(text, str):
        raise ValueError("text must be a string.")
    names = post.get("source_types")
    if not isinstance(names, list) or not names or len(names) != len(set(names)):
        raise ValueError("source_types must be a non-empty unique list.")
    if any(name not in _SOURCE_TYPES for name in names):
        raise ValueError("source_types may only contain bookmark, like, and readwise_tweet.")
    created = post.get("created_at", None)
    if "created_at" not in post:
        raise ValueError("created_at is required and may be null.")
    if created is not None:
        created = _iso(created, "created_at")
    else:
        created = ""
    fetched = post.get("fetched_at")
    if fetched is not None:
        fetched = _iso(fetched, "fetched_at")
    ranks = post.get("source_ranks") if isinstance(post.get("source_ranks"), dict) else {}
    signals = {}
    for name in _SIGNALS:
        if name not in names:
            continue
        signals[name] = _signal(
            ranks.get(name) if isinstance(ranks.get(name), int) else None,
            fetched,
            _optional_time(post, "saved_at" if name == "bookmark" else "liked_at"),
        )
    readwise = _readwise(post, "readwise_tweet" in names)
    quality = _quality(text, post.get("text_is_title_only"))
    echo = bool(readwise) and readwise["reader_source"].strip().casefold() == "beacon"
    return {
        "post_id": post_id,
        "url": url,
        "text": text,
        "created_at": created,
        "signals": signals,
        "author": _author(post.get("author")),
        "media": None,
        "like_count": None,
        "handoff": post,
        "extra": {"readwise": readwise, "quality": quality, "echo": echo},
    }


def _readwise(post, required):
    raw = post.get("readwise")
    if raw is None:
        if required:
            raise ValueError("readwise_tweet requires a readwise object.")
        return None
    if not isinstance(raw, dict):
        raise ValueError("readwise must be an object.")
    reader_id = raw.get("reader_id")
    if not isinstance(reader_id, str) or not reader_id.strip():
        raise ValueError("readwise.reader_id must be a string.")
    saved = raw.get("reader_saved_at")
    if saved is not None:
        saved = _iso(saved, "readwise.reader_saved_at")
    source = raw.get("reader_source")
    if source is None:
        source = ""
    if not isinstance(source, str):
        raise ValueError("readwise.reader_source must be a string.")
    return {"reader_id": reader_id, "reader_saved_at": saved, "reader_source": source}


def _quality(text, title_only):
    if title_only is not None and not isinstance(title_only, bool):
        raise ValueError("text_is_title_only must be a boolean.")
    if not text.strip():
        return "empty"
    if title_only is True:
        return "title_only"
    return "full"


def _optional_time(post, key):
    if key not in post or post[key] is None:
        return None
    return _iso(post[key], key)


def _author(author):
    if author is None:
        return {}
    if not isinstance(author, dict):
        raise ValueError("author must be an object.")
    username = author.get("username") or ""
    name = author.get("name") or ""
    if not isinstance(username, str) or not isinstance(name, str):
        raise ValueError("author username and name must be strings.")
    return {"username": username, "name": name}


def _merge_posts(posts):
    by_id = {}
    for post in posts:
        previous = by_id.get(post["post_id"])
        if previous is None:
            by_id[post["post_id"]] = post
            continue
        if previous["created_at"] != post["created_at"]:
            raise ValueError("Duplicate post_id has conflicting created_at.")
        if previous["text"] == post["text"]:
            chosen = previous
        else:
            fulls = [item for item in (previous, post) if item["extra"]["quality"] == "full"]
            if len(fulls) > 1:
                raise ValueError("Duplicate post_id has conflicting text.")
            if fulls:
                chosen = fulls[0]
            else:
                chosen = post if len(post["text"]) >= len(previous["text"]) else previous
        other = post if chosen is previous else previous
        signals = {**other["signals"], **chosen["signals"]}
        readwise = chosen["extra"]["readwise"] or other["extra"]["readwise"]
        echo = bool(readwise) and readwise["reader_source"].strip().casefold() == "beacon"
        quality = chosen["extra"]["quality"]
        chosen = {**chosen, "signals": signals, "extra": {"readwise": readwise, "quality": quality, "echo": echo}}
        if not chosen["author"].get("username") and other["author"].get("username"):
            chosen["author"] = other["author"]
        by_id[post["post_id"]] = chosen
    return list(by_id.values())


def _diagnostics(posts):
    def has(post, name):
        return name in post["handoff"].get("source_types", [])

    def readwise(post):
        return bool(post["extra"]["readwise"])

    readwise_only = [post for post in posts if readwise(post) and not has(post, "bookmark")]
    return {
        "posts": len(posts),
        "bookmarks": sum(has(post, "bookmark") for post in posts),
        "likes_only": sum(has(post, "like") and not has(post, "bookmark") and not readwise(post) for post in posts),
        "readwise_only": len(readwise_only),
        "overlap": sum(has(post, "bookmark") and readwise(post) for post in posts),
        "title_only": sum(post["extra"]["quality"] == "title_only" for post in readwise_only),
        "empty_text": sum(post["extra"]["quality"] == "empty" for post in posts),
        "echo": sum(post["extra"]["echo"] and not has(post, "bookmark") for post in posts),
        "actionable": sum(_would_route(post) for post in posts),
        "abstained": sum(not _would_route(post) for post in posts),
        "missing_created_at": sum(not post["created_at"] for post in posts),
        "missing_reader_saved_at": sum(
            bool(post["extra"]["readwise"]) and not post["extra"]["readwise"].get("reader_saved_at")
            for post in posts
        ),
    }


def _would_route(post):
    if post["extra"]["quality"] == "empty":
        return False
    if "bookmark" in post["signals"]:
        return True
    extra = post["extra"]
    return bool(extra["readwise"]) and extra["quality"] == "full" and not extra["echo"]


def _file_watermark(posts):
    times = []
    for post in posts:
        readwise = post["extra"]["readwise"]
        if readwise and readwise.get("reader_saved_at"):
            times.append(readwise["reader_saved_at"])
    return max(times, key=_time_key) if times else None


def _advance_readwise_checkpoint(conn, posts, synced_at, added):
    """High-water mark of Reader ingestion times. Not an X bookmark time and not a live feed."""
    row = conn.execute("SELECT last_id FROM sync_state WHERE source='readwise'").fetchone()
    previous = row["last_id"] if row and row["last_id"] else ""
    candidates = [previous] if previous else []
    for post in posts:
        readwise = post["extra"]["readwise"]
        if readwise and readwise.get("reader_saved_at"):
            candidates.append(readwise["reader_saved_at"])
    watermark = max(candidates, key=_time_key) if candidates else ""
    conn.execute(
        """INSERT INTO sync_state (source, last_id, last_sync, items_synced)
           VALUES ('readwise', ?, ?, ?)
           ON CONFLICT(source) DO UPDATE SET
             last_id=excluded.last_id, last_sync=excluded.last_sync, items_synced=excluded.items_synced""",
        (watermark, synced_at, added),
    )
    return watermark or None


def _time_key(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
