import json
import re
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from lib import db


_STATUS_URL = re.compile(r"^https://(?:x|twitter)\.com/([^/]+)/status/([0-9]+)(?:/.*)?$")
_SIGNALS = ("bookmark", "like")
_HANDOFF_OWNED = {"source_types", "source_ranks", "fetched_at", "saved_at", "liked_at", "created_at"}

_BOOKMARK_ONLY = "Bookmarked save. Eligible for classification and routing."
_BOTH = "Bookmarked save, also liked. The bookmark is the save. The like is extra interest, not a second task."
_LIKE_ONLY = "Liked only. Weaker interest, not a save, so it stays out of classification, triage, routing, and the library."
_TITLE_ONLY = "Title only from Readwise. Post text is not here yet. Pending hydration, not classified or routed."
_EMPTY = "No text. Not classified or routed."
_ECHO = "Already sent to Reader by Beacon. Not a new save."
_READWISE_FULL = "Readwise save with post text. Eligible for classification and routing. Reader time is not an X bookmark time."
_ALSO_READWISE = " Also recorded in Readwise. One save. Reader time is not an X bookmark time."
_CATALOG_SOURCES = ("x", "x-like", "readwise")


def explain(signals, extra=None):
    has_bookmark = "bookmark" in signals
    has_like = "like" in signals
    readwise = (extra or {}).get("readwise")
    if extra and extra.get("quality") == "empty":
        if has_bookmark:
            return "No text. The bookmark is kept so later sync still recognizes the post. Not classified or routed."
        return _EMPTY
    if readwise and not has_bookmark:
        if extra.get("echo"):
            return _ECHO
        quality = extra.get("quality")
        if quality == "title_only":
            return _TITLE_ONLY
        if quality != "full":
            return _EMPTY
        if has_like:
            return _READWISE_FULL + " A like on the same post is extra interest, not a second task."
        return _READWISE_FULL
    if has_bookmark and has_like:
        base = _BOTH
    elif has_bookmark:
        base = _BOOKMARK_ONLY
    elif has_like:
        base = _LIKE_ONLY
    else:
        raise ValueError("A post needs a bookmark or a like.")
    if readwise:
        return base + _ALSO_READWISE
    return base


def parse_handoff(path):
    text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON.") from None
    posts = _merge_posts([_parse_post(post) for post in _post_list(payload)])
    return posts, _diagnostics(posts)


def import_handoff(path, db_path=None, preview=False):
    posts, diagnostics = parse_handoff(path)
    result = {"source": "x", "preview": bool(preview), **diagnostics}
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
                    created_at=post["created_at"], raw_for=partial(_catalog_raw, post), synced_at=synced_at,
                )
                counts[outcome] += 1
    finally:
        conn.close()
    return {**result, **counts}


def record_bird_bookmark(tweet, synced_at, strict=False):
    """Write one bird bookmark. True when it was not already a source x save."""
    tweet_id = str(tweet.get("id", ""))
    username = tweet.get("author", {}).get("username", "unknown")
    text = tweet.get("text", "") or ""
    incoming = {"bookmark": {"rank": None, "retrieved_at": synced_at, "acted_at": None}}
    try:
        conn = db.get_connection()
        try:
            with conn:
                outcome = _apply(
                    conn, tweet_id, incoming,
                    title=text[:100] if text else "", body=text,
                    url=f"https://x.com/{username}/status/{tweet_id}", author=f"@{username}",
                    created_at=_bird_created_at(tweet.get("createdAt", "")),
                    raw_for=partial(_bird_raw, tweet), synced_at=synced_at,
                )
            return _counts_as_new_bookmark(outcome)
        finally:
            conn.close()
    except Exception as error:
        if strict:
            raise
        print(f"  Error upserting x/{tweet_id}: {error}")
        return False


def _counts_as_new_bookmark(outcome):
    return outcome != "refreshed"


def merge_signals(existing, incoming):
    merged = {}
    existing = existing or {}
    incoming = incoming or {}
    for name in _SIGNALS:
        old = existing.get(name) if isinstance(existing.get(name), dict) else None
        new = incoming.get(name) if isinstance(incoming.get(name), dict) else None
        if old is None and new is None:
            continue
        if old is None:
            merged[name] = _signal(new.get("rank"), new.get("retrieved_at"), new.get("acted_at"))
            continue
        if new is None:
            merged[name] = _signal(old.get("rank"), old.get("retrieved_at"), old.get("acted_at"))
            continue
        rank = new.get("rank") if new.get("rank") is not None else old.get("rank")
        retrieved = new.get("retrieved_at") or old.get("retrieved_at")
        acted = new.get("acted_at") if new.get("acted_at") is not None else old.get("acted_at")
        merged[name] = _signal(rank, retrieved, acted)
    return merged


def _signal(rank, retrieved_at, acted_at):
    return {"rank": rank, "retrieved_at": retrieved_at, "acted_at": acted_at}


def _post_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and payload.get("format") == "x-handoff-v1" and isinstance(payload.get("posts"), list):
        return payload["posts"]
    raise ValueError("Handoff must be a JSON array of posts or an x-handoff-v1 object.")


def _parse_post(post):
    if not isinstance(post, dict):
        raise ValueError("Each post must be an object.")
    missing = [field for field in ("post_id", "url", "text", "created_at", "source_types", "fetched_at") if field not in post]
    if missing:
        raise ValueError("A post is missing " + ", ".join(missing) + ".")
    post_id = post["post_id"]
    if not isinstance(post_id, str) or re.fullmatch(r"[0-9]+", post_id) is None:
        raise ValueError("post_id must be a digit string.")
    url = post["url"]
    match = _STATUS_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None or match.group(2) != post_id:
        raise ValueError("url must be an https x.com or twitter.com status URL for that post_id.")
    if not isinstance(post["text"], str):
        raise ValueError("text must be a string.")
    names = _source_types(post["source_types"])
    ranks = _source_ranks(post.get("source_ranks"), names)
    created_at = _iso(post["created_at"], "created_at")
    fetched_at = _iso(post["fetched_at"], "fetched_at")
    saved = _optional_iso(post["saved_at"], "saved_at") if "saved_at" in post else None
    liked = _optional_iso(post["liked_at"], "liked_at") if "liked_at" in post else None
    signals = {}
    if "bookmark" in names:
        signals["bookmark"] = _signal(ranks.get("bookmark"), fetched_at, saved)
    if "like" in names:
        signals["like"] = _signal(ranks.get("like"), fetched_at, liked)
    return {
        "post_id": post_id, "url": url, "text": post["text"], "created_at": created_at,
        "signals": signals, "author": _author(post), "media": _media(post),
        "like_count": _like_count(post), "handoff": post,
    }


def _source_types(value):
    if not isinstance(value, list) or not value:
        raise ValueError("source_types must list bookmark, like, or both.")
    if any(not isinstance(name, str) or name not in _SIGNALS for name in value):
        raise ValueError("source_types must list bookmark, like, or both.")
    if len(value) != len(set(value)):
        raise ValueError("source_types must be unique.")
    return set(value)


def _source_ranks(value, names):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("source_ranks must be an object.")
    ranks = {}
    for name, rank in value.items():
        if name not in names:
            raise ValueError("source_ranks has a signal that is not in source_types.")
        ranks[name] = _integer(rank, "source_ranks")
    return ranks


def _author(post):
    author = {"username": None, "name": None}
    if "author" not in post or post["author"] is None:
        return author
    raw = post["author"]
    if not isinstance(raw, dict):
        raise ValueError("author must be an object.")
    for key in ("username", "name"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"author.{key} must be a string.")
        author[key] = value
    return author


def _media(post):
    if "media" not in post or post["media"] is None:
        return None
    media = post["media"]
    if not isinstance(media, list):
        raise ValueError("media must be a list.")
    for item in media:
        if not isinstance(item, dict):
            raise ValueError("media entries must be objects.")
        for key in ("type", "url", "preview_image_url", "previewUrl"):
            if key in item and item[key] is not None and not isinstance(item[key], str):
                raise ValueError("media text fields must be strings.")
        for key in ("width", "height"):
            if key in item and item[key] is not None:
                _integer(item[key], f"media.{key}")
    return media


def _like_count(post):
    if "public_metrics" not in post or post["public_metrics"] is None:
        return None
    metrics = post["public_metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("public_metrics must be an object.")
    if "like_count" not in metrics or metrics["like_count"] is None:
        return None
    return _integer(metrics["like_count"], "public_metrics.like_count")


def _integer(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer.")
    return value


def _iso(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid {field}; use ISO 8601 with a timezone.")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Invalid {field}; use ISO 8601 with a timezone.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Invalid {field}; use ISO 8601 with a timezone.")
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_iso(value, field):
    if value is None:
        return None
    return _iso(value, field)


def _merge_posts(posts):
    merged = {}
    order = []
    for post in posts:
        prior = merged.get(post["post_id"])
        if prior is None:
            merged[post["post_id"]] = post
            order.append(post["post_id"])
            continue
        if prior["text"] != post["text"] or prior["created_at"] != post["created_at"]:
            raise ValueError("Conflicting duplicate post_id.")
        signals = dict(prior["signals"])
        for name, signal in post["signals"].items():
            signals[name] = _merge_duplicate_signal(signals[name], signal) if name in signals else signal
        prior["signals"] = signals
        prior["url"] = _prefer(prior["url"], post["url"])
        prior["author"] = {
            "username": _prefer(prior["author"].get("username"), post["author"].get("username")),
            "name": _prefer(prior["author"].get("name"), post["author"].get("name")),
        }
        prior["media"] = _prefer(prior["media"], post["media"])
        prior["like_count"] = _prefer(prior["like_count"], post["like_count"])
        prior["handoff"] = _merge_handoff(prior["handoff"], post["handoff"], signals)
    return [merged[post_id] for post_id in order]


def _prefer(left, right):
    if left is None:
        return right
    return left


def _merge_duplicate_signal(left, right):
    if left["rank"] != right["rank"] and None not in (left["rank"], right["rank"]):
        raise ValueError("Conflicting duplicate post_id.")
    if left["acted_at"] != right["acted_at"] and None not in (left["acted_at"], right["acted_at"]):
        raise ValueError("Conflicting duplicate post_id.")
    return _signal(
        left["rank"] if left["rank"] is not None else right["rank"],
        max(left["retrieved_at"], right["retrieved_at"]),
        left["acted_at"] if left["acted_at"] is not None else right["acted_at"],
    )


def _merge_handoff(left, right, signals):
    merged = dict(left)
    for key, value in right.items():
        if key in _HANDOFF_OWNED:
            continue
        if key not in merged or merged[key] is None:
            merged[key] = value
    merged["source_types"] = [name for name in _SIGNALS if name in signals]
    ranks = {name: signal["rank"] for name, signal in signals.items() if signal["rank"] is not None}
    if ranks:
        merged["source_ranks"] = ranks
    else:
        merged.pop("source_ranks", None)
    if "bookmark" in signals:
        merged["saved_at"] = signals["bookmark"]["acted_at"]
    if "like" in signals:
        merged["liked_at"] = signals["like"]["acted_at"]
    retrieved = [signal["retrieved_at"] for signal in signals.values() if signal.get("retrieved_at")]
    if retrieved:
        merged["fetched_at"] = max(retrieved)
    return merged


def _diagnostics(posts):
    bookmark_only = like_only = both = missing_saved = missing_liked = 0
    for post in posts:
        has_bookmark = "bookmark" in post["signals"]
        has_like = "like" in post["signals"]
        if has_bookmark and has_like:
            both += 1
        elif has_bookmark:
            bookmark_only += 1
        else:
            like_only += 1
        if has_bookmark and post["signals"]["bookmark"]["acted_at"] is None:
            missing_saved += 1
        if has_like and post["signals"]["like"]["acted_at"] is None:
            missing_liked += 1
    return {
        "posts": len(posts),
        "bookmark_only": bookmark_only,
        "like_only": like_only,
        "both": both,
        "missing_saved_at": missing_saved,
        "missing_liked_at": missing_liked,
        "eligible_saves": bookmark_only + both,
        "interest_only": like_only,
    }


def _author_column(author):
    if author.get("username"):
        return f"@{author['username']}"
    return author.get("name") or ""


def _catalog_raw(post):
    author = {}
    if post["author"].get("name"):
        author["name"] = post["author"]["name"]
    if post["author"].get("username"):
        author["username"] = post["author"]["username"]
    raw = {"author": author, "createdAt": post["created_at"], "handoff": post["handoff"]}
    raw["media"] = [_preview_media(item) for item in post["media"]] if post["media"] is not None else []
    if post["like_count"] is not None:
        raw["likeCount"] = post["like_count"]
    return raw


def _preview_media(item):
    entry = {}
    for key in ("type", "url", "width", "height"):
        if item.get(key) is not None:
            entry[key] = item[key]
    preview = item.get("preview_image_url")
    if preview is None:
        preview = item.get("previewUrl")
    if preview is not None:
        entry["previewUrl"] = preview
    return entry


def _bird_raw(tweet):
    return {key: value for key, value in tweet.items() if key not in ("x_signals", "x_explanation")}


def _keep_library_pictures(old_media, new_media):
    if not new_media and isinstance(old_media, list) and old_media:
        return old_media
    return new_media if new_media is not None else []


def _preserve_raw(existing_raw_json, raw):
    try:
        old = json.loads(existing_raw_json or "{}")
    except (TypeError, ValueError):
        return raw
    if not isinstance(old, dict):
        return raw
    raw["media"] = _keep_library_pictures(old.get("media"), raw.get("media"))
    old_author = old.get("author") if isinstance(old.get("author"), dict) else {}
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    for key in ("name", "username"):
        if not author.get(key) and old_author.get(key):
            author[key] = old_author[key]
    if author:
        raw["author"] = author
    if "likeCount" not in raw and old.get("likeCount") is not None:
        raw["likeCount"] = old["likeCount"]
    for key in ("readwise", "content_quality", "echo"):
        if key not in raw and key in old:
            raw[key] = old[key]
    return raw


def _bird_created_at(value):
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc).isoformat()
    except ValueError:
        return value


def _signals_in(raw_json):
    try:
        raw = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        return {}
    found = raw.get("x_signals") if isinstance(raw, dict) else None
    if not isinstance(found, dict):
        return {}
    signals = {}
    for name in _SIGNALS:
        signal = found.get(name)
        if isinstance(signal, dict):
            signals[name] = _signal(signal.get("rank"), signal.get("retrieved_at"), signal.get("acted_at"))
    return signals


def _legacy_source_x_is_a_bookmark(source, raw_json):
    signals = _signals_in(raw_json)
    if source == "x" and "bookmark" not in signals:
        signals["bookmark"] = _signal(None, None, None)
    return signals


def _crawl_source(signals):
    return "x" if "bookmark" in signals else "x-like"


def _extra_from_raw(raw_json):
    try:
        raw = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("readwise"), dict):
        return None
    return {
        "readwise": raw["readwise"],
        "quality": raw.get("content_quality") or "full",
        "echo": bool(raw.get("echo")),
    }


def _catalog_source(merged, extra):
    if "bookmark" in merged:
        return "x"
    if (extra or {}).get("readwise"):
        return "readwise"
    return _crawl_source(merged)


def _actionable(merged, extra):
    if extra and extra.get("quality") == "empty":
        return False
    if "bookmark" in merged:
        return True
    if not (extra or {}).get("readwise"):
        return False
    return extra.get("quality") == "full" and not extra.get("echo")


def _pending_text(merged, extra):
    """Readwise has not supplied the post. That is waiting, not a decision to ignore it."""
    if "bookmark" in merged or not (extra or {}).get("readwise") or extra.get("echo"):
        return False
    return extra.get("quality") in ("title_only", "empty")


def _row_status(actionable, pending):
    if actionable:
        return "new"
    if pending:
        return "pending"
    return "interest"


def _apply(conn, external_id, incoming, title, body, url, author, created_at, raw_for, synced_at, extra=None):
    placeholders = ",".join("?" for _ in _CATALOG_SOURCES)
    rows = conn.execute(
        f"""SELECT id, source, status, title, body, url, author, raw_json
           FROM items WHERE external_id=? AND source IN ({placeholders})""",
        (external_id, *_CATALOG_SOURCES),
    ).fetchall()
    if len(rows) > 1:
        raise ValueError("One post id has more than one catalog row.")
    existing = rows[0] if rows else None
    merged = merge_signals(_legacy_source_x_is_a_bookmark(existing["source"], existing["raw_json"]) if existing else {}, incoming)
    kept = extra or (_extra_from_raw(existing["raw_json"]) if existing is not None else None)
    if existing is not None and extra and extra.get("quality") in ("title_only", "empty"):
        if len(existing["body"] or "") > len(body or ""):
            body = existing["body"] or ""
            title = existing["title"] or title
    source = _catalog_source(merged, kept)
    actionable = _actionable(merged, kept)
    raw = raw_for()
    if existing is not None:
        raw = _preserve_raw(existing["raw_json"], raw)
        title = title or existing["title"] or ""
        body = body or existing["body"] or ""
        url = url or existing["url"] or ""
        author = author or existing["author"] or ""
    if kept and kept.get("readwise"):
        raw["readwise"] = kept["readwise"]
        raw["echo"] = bool(kept.get("echo"))
        raw["content_quality"] = "full" if "bookmark" in merged and (body or "").strip() else kept.get("quality")
    raw["x_signals"] = merged
    raw["x_explanation"] = explain(merged, kept)
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    saved_at = merged["bookmark"]["acted_at"] if "bookmark" in merged else None
    if existing is None:
        conn.execute(
            """INSERT INTO items
               (source, external_id, title, body, url, author, raw_json, created_at, synced_at, saved_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, external_id, title, body, url, author, raw_json, created_at, synced_at, saved_at,
             _row_status(actionable, _pending_text(merged, kept))),
        )
        return "added"
    opens = existing["status"] in ("interest", "pending") and actionable
    mark_pending = (
        not actionable and existing["status"] == "interest" and source == "readwise" and _pending_text(merged, kept)
    )
    became_save = existing["source"] != "x" and source == "x"
    next_status = "new" if opens else ("pending" if mark_pending else "")
    conn.execute(
        """UPDATE items SET
             source=?, title=?, body=?, url=?, author=?, raw_json=?,
             created_at=CASE WHEN created_at IS NOT NULL AND created_at != '' THEN created_at ELSE ? END,
             synced_at=?,
             saved_at=COALESCE(?, saved_at),
             status=CASE WHEN ? = 'new' THEN 'new' WHEN ? = 'pending' THEN 'pending' ELSE status END
           WHERE id=?""",
        (source, title, body, url, author, raw_json, created_at, synced_at, saved_at,
         next_status, next_status, existing["id"]),
    )
    return "promoted" if opens or became_save else "refreshed"
