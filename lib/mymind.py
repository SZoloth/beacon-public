"""Explicit local import of mymind's cards.csv export. No network access."""

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from lib import db


ALIASES = {
    "external_id": ("id", "card id", "card uuid", "uuid", "external id"),
    "title": ("title", "name"),
    "body": ("content", "text", "note", "notes", "quote", "highlight", "description"),
    "url": ("url", "link", "source url", "source link", "bookmark url"),
    "author": ("author", "creator"),
    "tags": ("tags", "tag"),
    "created_at": ("created", "created at", "created date", "date created"),
    "saved_at": ("saved", "saved at", "saved date", "date saved", "added at", "added", "date added", "date"),
    "updated_at": ("updated", "updated at", "updated date", "date updated", "modified at"),
}


def _header(value):
    return re.sub(r"[\s_-]+", " ", value.strip()).lower()


def _date(value, row_number, field):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timezone required")
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError(f"Row {row_number}: invalid {field}; use ISO 8601 with timezone or YYYY-MM-DD.") from None


def _tags(value, row_number):
    if not value:
        return []
    if value.startswith("["):
        try:
            tags = json.loads(value)
        except ValueError:
            tags = None
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError(f"Row {row_number}: tags must be a JSON string array or delimited text.")
    else:
        tags = re.split(r"[,;|]", value)
    return list(dict.fromkeys(tag.strip() for tag in tags if tag.strip()))


def parse_export(path):
    """Validate the complete CSV before the caller opens a destination database."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "cards.csv"
    items = []
    seen = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        try:
            headers = next(reader, None)
            if not headers or any(not header.strip() for header in headers):
                raise ValueError("CSV requires non-empty column headers.")
            normalized = [_header(header) for header in headers]
            if len(set(normalized)) != len(normalized):
                raise ValueError("CSV has duplicate or ambiguous column headers.")
            fields = {}
            for field, aliases in ALIASES.items():
                matches = [normalized.index(alias) for alias in aliases if alias in normalized]
                if field == "body":
                    if matches:
                        fields[field] = matches
                    continue
                if len(matches) > 1:
                    raise ValueError(f"CSV has conflicting columns for {field}; keep one mapped column.")
                if matches:
                    fields[field] = matches[0]
            if not {"title", "body", "url"}.intersection(fields):
                raise ValueError("CSV needs a recognized title, content or URL column; see docs/mymind-import.md.")
            for row_number, row in enumerate(reader, start=2):
                if not row:
                    continue
                if len(row) != len(headers):
                    raise ValueError(f"Row {row_number}: column count does not match header.")
                raw = dict(zip(headers, row))
                values = {field: row[index].strip() for field, index in fields.items() if field != "body"}
                bodies = list(dict.fromkeys(row[index] for index in fields.get("body", []) if row[index].strip()))
                values["body"] = "\n\n".join(bodies)
                item = {field: values.get(field) or None for field in ("title", "body", "url")}
                item["author"] = values.get("author") or None
                if not any(item[field] for field in ("title", "body", "url")):
                    raise ValueError(f"Row {row_number}: no title, content or URL.")
                for field in ("created_at", "saved_at", "updated_at"):
                    item[field] = _date(values.get(field), row_number, field)
                identity_kind = "id"
                external_id = values.get("external_id")
                if not external_id:
                    if item["url"] and item["saved_at"]:
                        identity = [item["url"], item["saved_at"]]
                        identity_kind = "url_saved_at"
                    else:
                        identity = [item["url"], item["title"], item["body"]]
                        identity_kind = "content"
                    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
                    external_id = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                item["external_id"] = external_id
                item["raw_json"] = json.dumps({
                    "import_format": "mymind-cards-csv-v1", "record": raw,
                    "source_tags": _tags(values.get("tags"), row_number),
                    "updated_at": item["updated_at"], "identity_kind": identity_kind,
                }, ensure_ascii=False, sort_keys=True)
                if external_id in seen:
                    if seen[external_id] != item:
                        raise ValueError(f"Row {row_number}: conflicting duplicate item identity.")
                    continue
                seen[external_id] = item
                items.append(item)
        except csv.Error:
            raise ValueError(f"Invalid CSV syntax near line {reader.line_num}.") from None
    diagnostics = {
        "items": len(items),
        "fallback_ids": sum(json.loads(item["raw_json"])["identity_kind"] != "id" for item in items),
        "missing_created_at": sum(item["created_at"] is None for item in items),
        "missing_saved_at": sum(item["saved_at"] is None for item in items),
        "unmapped_columns": len(headers) - sum(len(value) if isinstance(value, list) else 1 for value in fields.values()),
        "mapped_fields": sorted(fields),
    }
    return items, diagnostics


def import_export(path, db_path=None, preview=False):
    items, diagnostics = parse_export(path)
    result = {"source": "mymind", "preview": preview, **diagnostics}
    if preview:
        return result
    if db_path is not None and not str(db_path).strip():
        raise ValueError("Database path cannot be empty.")
    db.init_db(db_path)
    conn = db.get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    try:
        with conn:
            for item in items:
                existing = conn.execute(
                    "SELECT id FROM items WHERE source='mymind' AND external_id=?", (item["external_id"],)
                ).fetchone()
                added += existing is None
                conn.execute(
                    """INSERT INTO items
                       (source, external_id, title, body, url, author, raw_json, created_at, saved_at, synced_at)
                       VALUES ('mymind', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, external_id) DO UPDATE SET
                         title=excluded.title, body=excluded.body, url=excluded.url,
                         author=COALESCE(excluded.author, items.author),
                         raw_json=excluded.raw_json,
                         created_at=COALESCE(excluded.created_at, items.created_at),
                         saved_at=COALESCE(excluded.saved_at, items.saved_at), synced_at=excluded.synced_at""",
                    tuple(item[field] for field in
                          ("external_id", "title", "body", "url", "author", "raw_json", "created_at", "saved_at")) + (now,),
                )
                item_id = conn.execute(
                    "SELECT id FROM items WHERE source='mymind' AND external_id=?", (item["external_id"],)
                ).fetchone()["id"]
                for tag in json.loads(item["raw_json"])["source_tags"]:
                    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
                    conn.execute(
                        "INSERT OR IGNORE INTO item_tags (item_id, tag_id) SELECT ?, id FROM tags WHERE name=?",
                        (item_id, tag),
                    )
    finally:
        conn.close()
    return {**result, "added": added, "refreshed": len(items) - added}
