"""The owner's own tags and notes from mymind, Readwise and Granola, kept apart from what models wrote.

They point at projects by naming them, often projects that did not exist when the note was written, so
nothing here decides which project a note is for: references.for_project matches them when an agent asks.

A record is one thing the owner annotated (a mymind card, a Readwise highlight, a Granola meeting):
    {"source", "external_id", "title", "body", "url", "author", "created_at", "raw_json",
     "x_ids": [X status ids it is a copy of], "scopes": [prefixes whose notes it lists in full],
     "notes": [{"external_id", "kind": "note"|"tag", "origin": "owner"|"owner_digest", "body", "context", "created_at"}]}
A note's external_id starts with one of its record's scopes and a colon, so a record that comes back with
fewer notes drops the ones it lost, and a complete sync of a source drops the notes of records that are gone.
"""

import hashlib
import json
import re
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from lib import db, mymind, settings

# Each sync and the `paths` key naming what it reads. An unset path leaves the sync off.
SYNCS = {"mymind": "mymind_cli", "readwise-notes": "readwise_cli", "granola-digests": "granola_digests"}

X_STATUS = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/?#]+/status(?:es)?/(\d+)", re.I)
MYMIND_OWNER_TAG = 8  # bit in a mymind tag's flags: the owner added it (2 means mymind's AI did)
_SCOPE = re.compile(r"^(.*?):(?:note|tag|idea)(?::|$)")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _note(external_id, kind, body, *, origin="owner", context=None, created_at=None):
    return {"external_id": external_id, "kind": kind, "origin": origin, "body": body,
            "context": context, "created_at": created_at}


def _x_ids(*urls):
    return [found[1] for found in (X_STATUS.match(url or "") for url in urls) if found]


def mymind_records(objects):
    """Records for the mymind objects the owner annotated: a note, or a tag the owner added.

    mymind's own AI tags are left out; they say what mymind thinks, not what the owner meant.
    """
    records = []
    for obj in objects:
        notes, tags = [], set()
        for note in obj.get("notes") or []:
            body = ((note.get("content") or {}).get("body") or "").strip()
            if body:
                notes.append(_note(f"{obj['id']}:note:{note['id']}", "note", body, created_at=note.get("created")))
        for tag in obj.get("tags") or []:
            name = (tag.get("name") or "").strip()
            if name and (tag.get("flags") or 0) & MYMIND_OWNER_TAG and name.lower() not in tags:
                tags.add(name.lower())
                notes.append(_note(f"{obj['id']}:tag:{name.lower()}", "tag", name))
        if not notes:
            continue
        entity = obj.get("mainEntity") if isinstance(obj.get("mainEntity"), dict) else {}
        content = entity.get("content") if isinstance(entity.get("content"), dict) else obj.get("content") or {}
        author = entity.get("author") if isinstance(entity.get("author"), dict) else {}
        url = (obj.get("source") or {}).get("url")
        records.append({
            "source": "mymind", "external_id": obj["id"], "title": obj.get("title") or entity.get("title"),
            "body": (content.get("body") or "").strip() or obj.get("summary"), "url": url,
            "author": author.get("name"), "created_at": obj.get("created"),
            "raw_json": json.dumps({"import_format": "mymind-cli-v1", "object": obj}, ensure_ascii=False),
            "x_ids": _x_ids(url), "scopes": [obj["id"]], "notes": notes,
        })
    return records


def _item_id(conn, record, now):
    """The item a record's notes attach to: the X post it copies, else its own save.

    Its own save starts as `interest`, so no stage that sends new saves to a model (areas, classify, pile)
    picks it up: a Granola title can name people. An existing item keeps its status.
    """
    for status in record["x_ids"]:
        found = conn.execute("SELECT id FROM items WHERE source='x' AND external_id=?", (status,)).fetchone()
        if found:
            return found[0], True
    conn.execute(
        "INSERT INTO items (source, external_id, title, body, url, author, raw_json, created_at, saved_at, synced_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'interest') ON CONFLICT(source, external_id) DO UPDATE SET "
        "title=excluded.title, body=excluded.body, url=excluded.url, author=COALESCE(excluded.author, items.author), "
        "raw_json=excluded.raw_json, synced_at=excluded.synced_at",
        (record["source"], record["external_id"], record["title"], record["body"], record["url"], record["author"],
         record["raw_json"], record["created_at"], record["created_at"], now))
    return conn.execute("SELECT id FROM items WHERE source=? AND external_id=?",
                        (record["source"], record["external_id"])).fetchone()[0], False


def store(source, records, *, complete, db_path=None, owns=lambda external_id: True):
    """Write the records' notes for `source`, attached to the X post each copies when Beacon has it.

    `complete` means records hold everything the source has, so notes missing from them are removed. Only
    notes this sync `owns` are ever removed: another import may write notes for the same source.
    Items are never deleted. Returns counts for the sync log.
    """
    db.init_db(db_path)
    now = _now()
    result = {"source": source, "records": len(records), "annotated": 0, "attached_to_x": 0, "own_items": 0,
              "notes": 0, "tags": 0, "removed": 0}
    with closing(db.get_connection(db_path)) as conn, conn:
        existing = {row[0] for row in conn.execute("SELECT external_id FROM owner_notes WHERE source=?", (source,))}
        listed, scopes = set(), set()
        for record in records:
            scopes.update(record["scopes"])
            if not record["notes"]:
                continue
            item_id, attached = _item_id(conn, record, now)
            result["annotated"] += 1
            result["attached_to_x" if attached else "own_items"] += 1
            for note in record["notes"]:
                listed.add(note["external_id"])
                result["notes" if note["kind"] == "note" else "tags"] += 1
                conn.execute(
                    "INSERT INTO owner_notes (item_id, source, external_id, kind, origin, body, context, created_at, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source, external_id) DO UPDATE SET "
                    "item_id=excluded.item_id, kind=excluded.kind, origin=excluded.origin, body=excluded.body, "
                    "context=excluded.context, created_at=excluded.created_at, synced_at=excluded.synced_at",
                    (item_id, source, note["external_id"], note["kind"], note["origin"], note["body"],
                     note["context"], note["created_at"], now))
        gone = [external_id for external_id in existing - listed if owns(external_id)
                and (complete or ((scope := _SCOPE.match(external_id)) and scope[1] in scopes))]
        for external_id in gone:
            conn.execute("DELETE FROM owner_notes WHERE source=? AND external_id=?", (source, external_id))
        result["removed"] = len(gone)
        result["moved_to_x"] = _reattach(conn, source)
    return result


def _reattach(conn, source):
    """Move notes from the source's own saves to the X post each copies, once Beacon has that post.

    An incremental sync never sees an old record again, so this pass, not the record, finds the post. The own
    save stays. Returns how many saves' notes moved.
    """
    moved = 0
    for item_id, url in conn.execute("SELECT DISTINCT i.id, i.url FROM items i JOIN owner_notes n ON n.item_id = i.id "
                                     "WHERE i.source = ?", (source,)).fetchall():
        post = next((found[0] for status in _x_ids(url) for found in [conn.execute(
            "SELECT id FROM items WHERE source='x' AND external_id=?", (status,)).fetchone()] if found), None)
        if post:
            conn.execute("UPDATE owner_notes SET item_id=? WHERE item_id=?", (post, item_id))
            moved += 1
    return moved


def _run(command, timeout):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"{command[0]} didn't run: {error}") from None
    if done.returncode != 0:
        raise RuntimeError(f"{command[0]} exited {done.returncode}: {done.stderr.strip()[:300]}")
    return done.stdout


def sync_mymind(cli_dir, db_path=None, run=_run):
    """Read every mymind object through the mymind CLI (read-only) and mirror the owner's notes and tags."""
    lines = run([f"{cli_dir}/node_modules/.bin/tsx", f"{cli_dir}/bin/mymind.ts",
                 "objects", "list", "--jsonl", "--limit", "1000000"], 900).split("\n")  # splitlines() breaks on U+2028
    objects = [json.loads(line) for line in lines if line.strip()]
    if not objects:
        raise RuntimeError("mymind listed no objects, so no notes were changed.")
    return store("mymind", mymind_records(objects), complete=True, db_path=db_path,
                 owns=lambda external_id: not external_id.endswith(mymind.EXPORT_NOTE))


READWISE_FIELDS = ["text", "note", "tags", "url", "highlighted_at", "updated", "book_id", "book_title", "book_author",
                   "book_source_url", "book_category", "book_tags", "book_document_note"]  # the id always comes back
READWISE_START = "2000-01-01T00:00:00Z"


def readwise_records(highlights):
    """A record per highlight, with its note and tags (the passage it annotates as context), and one per book,
    with the book's tags and document note.

    A book's record attaches to its own source (the X post a tweet book copies, else the book's save), so its notes
    land in one place whichever of its highlights comes last. Every highlight and book is listed, annotated or not,
    so one whose note or tags were removed loses them.
    """
    records, books = [], {}
    for highlight in highlights:
        highlight_scope, book_scope = f"highlight:{highlight['id']}", f"book:{highlight['book_id']}"
        passage, notes = highlight.get("text") or None, []
        if (highlight.get("note") or "").strip():
            notes.append(_note(f"{highlight_scope}:note", "note", highlight["note"].strip(), context=passage,
                               created_at=highlight.get("highlighted_at")))
        notes += _tags(highlight_scope, highlight.get("tags"), passage)
        record = {
            "source": "readwise", "external_id": book_scope, "title": highlight.get("book_title"), "body": None,
            "url": highlight.get("book_source_url"), "author": highlight.get("book_author"),
            "created_at": highlight.get("highlighted_at"),
            "raw_json": json.dumps({"import_format": "readwise-highlights-v1", "book_id": highlight["book_id"],
                                    "book_category": highlight.get("book_category")}),
            "x_ids": _x_ids(highlight.get("url"), highlight.get("book_source_url")),
            "scopes": [highlight_scope], "notes": notes,
        }
        records.append(record)
        if book_scope not in books:
            book_notes = _tags(book_scope, highlight.get("book_tags"), None)
            if (highlight.get("book_document_note") or "").strip():
                book_notes.append(_note(f"{book_scope}:note", "note", highlight["book_document_note"].strip()))
            books[book_scope] = {**record, "x_ids": _x_ids(highlight.get("book_source_url")), "scopes": [book_scope],
                                 "notes": book_notes}
    return records + list(books.values())


def _tags(scope, tags, context):
    notes, seen = [], set()
    for tag in tags or []:
        name = (tag.get("name") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            notes.append(_note(f"{scope}:tag:{name.lower()}", "tag", name, context=context))
    return notes


def _state(conn, key):
    row = conn.execute("SELECT last_id FROM sync_state WHERE source=?", (key,)).fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def _save_state(conn, key, value, count):
    conn.execute("INSERT INTO sync_state (source, last_id, last_sync, items_synced) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id, last_sync=excluded.last_sync, "
                 "items_synced=excluded.items_synced", (key, json.dumps(value), _now(), count))
    conn.commit()


def sync_readwise(cli, full=False, db_path=None, run=_run, pause=time.sleep):
    """Highlights updated since the last run, through the readwise CLI (read-only), a page of 100 at a time.

    Pages come newest first, and the page reached is saved, so a run that stops resumes where it was. `full`
    starts from the beginning and, once every page is read, removes notes no highlight carries any more.
    """
    db.init_db(db_path)
    key, started = "readwise-notes", _now()
    with closing(db.get_connection(db_path)) as conn:
        state = {} if full else _state(conn, key)
    since = state.get("updated_gt") or READWISE_START
    page, newest = state.get("page") or 1, state.get("newest") or since
    totals = {"source": "readwise", "highlights": 0, "annotated": 0, "attached_to_x": 0, "own_items": 0,
              "notes": 0, "tags": 0, "removed": 0, "pages": 0}
    while True:
        command = [cli, "--json", "readwise-list-highlights", "--page-size", "100", "--updated-gt", since,
                   "--response-fields", json.dumps(READWISE_FIELDS), "--page", str(page)]
        for attempt in range(3):  # Readwise fails one page in about 80 at random; the same page works a little later
            try:
                listed = json.loads(run(command, 300))
                break
            except RuntimeError:
                if attempt == 2:
                    raise
                pause(30)
        highlights = listed.get("results") or []
        result = store("readwise", readwise_records(highlights), complete=False, db_path=db_path)
        totals["highlights"] += len(highlights)
        totals["pages"] += 1
        for field in ("annotated", "attached_to_x", "own_items", "notes", "tags", "removed"):
            totals[field] += result[field]
        newest = max([newest] + [highlight["updated"] for highlight in highlights if highlight.get("updated")])
        page += 1
        with closing(db.get_connection(db_path)) as conn:
            if not listed.get("next"):
                if full:
                    totals["removed"] += conn.execute("DELETE FROM owner_notes WHERE source='readwise' AND synced_at < ?",
                                                      (started,)).rowcount
                _save_state(conn, key, {"updated_gt": newest}, totals["highlights"])
                return totals
            _save_state(conn, key, {"updated_gt": since, "page": page, "newest": newest}, totals["highlights"])


DIGEST_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
GRANOLA_DOC = re.compile(r"https://notes\.granola\.ai/d/([0-9a-f-]{36})")


def plain(text):
    """Lowercase words with one space between, so pencil-review, Pencil_Review and "pencil review" read alike."""
    return " ".join(re.findall(r"[^\W_]+", (text or "").lower()))


def _cells(line):
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def granola_records(folder):
    """One record per Granola meeting in the daily digests (YYYY-MM-DD.md), one note per idea filed under a project.

    The ideas are a model's summary of what the owner said, so they are owner_digest, and their "Belongs to"
    projects are kept as context. `## Needs you` and ideas that belong to no project are skipped.
    """
    records = {}
    for path in sorted(path for path in folder.iterdir() if DIGEST_FILE.match(path.name)):
        title = doc = header = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                title, doc, header = line[3:].strip(), None, None
            elif (link := GRANOLA_DOC.search(line)) and title and title.lower() != "needs you":
                doc = link[1]
            elif doc and line.startswith("|"):
                cells = _cells(line)
                if header is None:
                    header = [cell.lower() for cell in cells]
                    continue
                row = dict(zip(header, cells))
                idea = row.get("idea", "").strip()
                projects = [slug.strip().lower() for slug in row.get("belongs to", "").split(",")
                            if slug.strip() and slug.strip().lower() != "none"]
                if not idea or not projects or not plain(idea):
                    continue
                record = records.setdefault(doc, {
                    "source": "granola", "external_id": doc, "title": title, "body": None,
                    "url": f"https://notes.granola.ai/d/{doc}", "author": None,
                    "created_at": f"{path.stem}T00:00:00+00:00",
                    "raw_json": json.dumps({"import_format": "granola-digest-v1", "digest": path.name}),
                    "x_ids": [], "scopes": [doc], "notes": []})
                external_id = f"{doc}:idea:{hashlib.sha256(plain(idea).encode()).hexdigest()[:16]}"
                if all(note["external_id"] != external_id for note in record["notes"]):
                    record["notes"].append(_note(external_id, "note", idea, origin="owner_digest",
                                                 context=json.dumps(projects), created_at=record["created_at"]))
    return list(records.values())


def sync_granola(folder, db_path=None):
    """Mirror the ideas in the Granola digest folder. A folder with no digests changes nothing."""
    folder = Path(folder)
    if not folder.is_dir() or not any(DIGEST_FILE.match(path.name) for path in folder.iterdir()):
        raise RuntimeError(f"No Granola digests in {folder}, so no notes were changed.")
    return store("granola", granola_records(folder), complete=True, db_path=db_path)


def sync(name, full=False):
    """Run one owner-notes sync from config, or skip it when its path is unset. Returns the counts or None."""
    path = settings.path(SYNCS[name])
    if not path:
        print(f"  {name}: skipped, paths.{SYNCS[name]} is not set")
        return None
    if name == "mymind":
        result = sync_mymind(path)
    elif name == "readwise-notes":
        result = sync_readwise(path, full=full)
    else:
        result = sync_granola(path)
    print(f"  {name}: " + json.dumps(result))
    return result
