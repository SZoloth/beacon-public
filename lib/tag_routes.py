"""Owner tags that name a project route the save there, with no model call.

The owner tags saves in mymind (or Readwise) while saving them. Until now those saves stayed at
`interest` and reached a project only when an agent asked (lib/references.py). A tag is the owner
saying where a save goes, so it routes: a tag that names an active project, by its own name or
through an owner row in the vocabulary (lib/vocabulary.py), queues the save in that project's backlog.

Only the owner's own word counts: literal names and owner vocabulary rows. A model-proposed row
waits until the owner confirms it with `beacon vocab set`. Areas (home, food) never get backlogs, and a save
whose notes or tags name client work reaches only client projects, as everywhere else in Beacon.
"""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone

from . import db, references, route, vocabulary

OPEN = ("new", "interest")  # a save the owner already triaged keeps its status
OWNER_WORD = ("literal", "owner")


def plan(conn):
    """[{item_id, title, slug, tag}] for each open save with an owner tag naming an active project it isn't in yet."""
    projects = {row["slug"] for row in conn.execute(
        "SELECT slug FROM workstreams WHERE kind = 'project' AND active = 1 AND backlog_path IS NOT NULL AND backlog_path != ''")}
    rows = conn.execute(
        f"SELECT n.item_id, n.body, i.title, i.url FROM owner_notes n JOIN items i ON i.id = n.item_id "
        f"WHERE n.kind = 'tag' AND n.origin = 'owner' AND i.status IN ({','.join('?' * len(OPEN))}) ORDER BY n.item_id",
        OPEN).fetchall()
    if not rows:
        return []
    pointed = vocabulary.resolve(conn, {row["body"] for row in rows})
    linked, linked_urls = set(), set()
    for row in conn.execute("SELECT iw.item_id, w.slug, i.url FROM item_workstreams iw "
                            "JOIN workstreams w ON w.id = iw.workstream_id JOIN items i ON i.id = iw.item_id"):
        linked.add((row["item_id"], row["slug"]))
        if row["url"]:
            linked_urls.add((row["url"], row["slug"]))  # a mymind copy of a share-sheet save comes back as its own save
    kept_out = {}  # per project, the saves naming client work that must stay out of it (references.py's rule)
    found, seen = [], set()
    for row in rows:
        for entry in pointed.get(row["body"], []):
            slug, key = entry["target"], (row["item_id"], entry["target"])
            if entry["how"] not in OWNER_WORD or slug not in projects or key in linked or key in seen \
                    or (row["url"], slug) in linked_urls:
                continue
            if slug not in kept_out:
                kept_out[slug] = references._client_filter(conn, slug)
            if row["item_id"] not in kept_out[slug]:
                seen.add(key)
                found.append({"item_id": row["item_id"], "title": row["title"], "slug": entry["target"], "tag": row["body"]})
    return found


def route_tagged(dry_run=False, db_path=None):
    """Queue every save an owner tag points at a project, then rewrite those projects' backlogs.

    Returns {"routed": {slug: count}, "saves": [...]}; a dry run changes nothing.
    """
    db.init_db(db_path)
    with closing(db.get_connection(db_path)) as conn:
        found = plan(conn)
        if dry_run or not found:
            return {"routed": dict(_count(found)), "saves": found, "dry_run": dry_run}
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            ids = {row["slug"]: row["id"] for row in conn.execute("SELECT id, slug FROM workstreams")}
            tags = defaultdict(list)
            for save in found:
                conn.execute("INSERT INTO item_workstreams (item_id, workstream_id, confidence, routed) VALUES (?, ?, 1.0, 0) "
                             "ON CONFLICT DO NOTHING", (save["item_id"], ids[save["slug"]]))
                tags[save["item_id"]].append(save["tag"])
            for item_id, names in tags.items():
                note = "owner tag: " + ", ".join(dict.fromkeys(names))
                db.set_item_status(conn, item_id, "queued", note)
    routed = {}
    for slug, saves in _by_slug(found).items():
        # The file is written before the link is marked routed, as `beacon route` does: a failed write retries.
        if route.regenerate_backlog(slug) is None:
            continue
        db.mark_routed_batch([save["item_id"] for save in saves], slug, now)
        routed[slug] = len(saves)
    return {"routed": routed, "saves": found, "dry_run": False}


def _by_slug(found):
    grouped = defaultdict(list)
    for save in found:
        grouped[save["slug"]].append(save)
    return grouped


def _count(found):
    return {slug: len(saves) for slug, saves in _by_slug(found).items()}
