"""SQLite schema, migrations, and queries for Beacon."""

import html
import json
from contextlib import closing
import re
import sqlite3
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from lib import settings

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "beacon.db")
# The one database whose runs may touch files and services outside it. Unset means none may.
LIVE_DB_PATH = settings.path("live_db")
# A database chosen for the current request or job (one person's file on a hosted server).
# Context-local: each server thread and each `using()` block sees only its own.
_ACTIVE_PATH = ContextVar("beacon_db_path", default=None)


def current_path():
    """The database this code is working on: the one set by `using()`, else DB_PATH."""
    return _ACTIVE_PATH.get() or DB_PATH


class using:
    """Point every query in this block (and this thread or task) at `path`, then put it back."""

    def __init__(self, path):
        self.path = os.fspath(path)

    def __enter__(self):
        self.token = _ACTIVE_PATH.set(self.path)
        return self.path

    def __exit__(self, *exc):
        _ACTIVE_PATH.reset(self.token)
        return False


def is_live():
    return bool(LIVE_DB_PATH) and os.path.realpath(current_path()) == os.path.realpath(LIVE_DB_PATH)


def skipping_outside_effects():
    if is_live():
        return False
    print(f"skipped: not the live database ({os.path.realpath(current_path())})", file=sys.stderr)
    return True


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT,
    body TEXT,
    url TEXT,
    author TEXT,
    intent TEXT,
    human_read INTEGER DEFAULT 0,
    one_liner TEXT,
    raw_json TEXT,
    created_at TEXT,
    synced_at TEXT NOT NULL,
    classified_at TEXT,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER REFERENCES items(id),
    tag_id INTEGER REFERENCES tags(id),
    PRIMARY KEY (item_id, tag_id)
);

CREATE TABLE IF NOT EXISTS workstreams (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    backlog_path TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS area_reviews (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    area TEXT NOT NULL,
    probability REAL NOT NULL,
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_workstreams (
    item_id INTEGER REFERENCES items(id),
    workstream_id INTEGER REFERENCES workstreams(id),
    confidence REAL,
    routed INTEGER DEFAULT 0,
    routed_at TEXT,
    PRIMARY KEY (item_id, workstream_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    source TEXT PRIMARY KEY,
    last_id TEXT,
    last_sync TEXT,
    items_synced INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS intake_reviews (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    source_hash TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK(disposition IN ('approved','review','no_fit')),
    result_json TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS advisory_reviews (
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
);

CREATE TABLE IF NOT EXISTS save_reviews (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id),
    source_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'local',
    status TEXT NOT NULL CHECK(status IN ('held','failed')),
    result_json TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    UNIQUE(item_id, source_hash, context_hash, provider)
);

CREATE TABLE IF NOT EXISTS curator_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    phase TEXT NOT NULL,
    result_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS item_stacks (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    theme TEXT NOT NULL,
    description TEXT NOT NULL,
    rank INTEGER NOT NULL,
    duplicate_of INTEGER,
    built_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_gists (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    takeaway TEXT NOT NULL,
    kind TEXT NOT NULL,
    suggestion TEXT NOT NULL CHECK(suggestion IN ('do','read','skip','reference')),
    reason TEXT NOT NULL,
    built_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pile_decisions (
    item_id INTEGER PRIMARY KEY REFERENCES items(id),
    destination TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('do','read','reference','pass')),
    takeaway TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_plans (
    workstream TEXT NOT NULL,
    created_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    overlaps_json TEXT NOT NULL DEFAULT '[]',
    item_ids_json TEXT NOT NULL,
    PRIMARY KEY (workstream, created_at)
);

CREATE TABLE IF NOT EXISTS briefs (
    item_id INTEGER NOT NULL REFERENCES items(id),
    workstream TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('skipped','ready','dismissed','dispatched','failed')),
    actionable REAL,
    impact REAL,
    brief_json TEXT,
    unsupported_json TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, workstream)
);

DROP TRIGGER IF EXISTS invalidate_intake_review;
CREATE TRIGGER invalidate_intake_review AFTER UPDATE OF title,body,url,author,source,external_id ON items
WHEN (OLD.title IS NOT NEW.title OR OLD.body IS NOT NEW.body OR OLD.url IS NOT NEW.url
      OR OLD.author IS NOT NEW.author OR OLD.source IS NOT NEW.source OR OLD.external_id IS NOT NEW.external_id)
     AND EXISTS(SELECT 1 FROM intake_reviews WHERE item_id=NEW.id)
BEGIN
    UPDATE items SET classified_at=NULL WHERE id=NEW.id;
    UPDATE intake_reviews SET disposition='review',
      result_json=json_set(result_json,'$.assessment','review','$.reasons',json_array('Source changed after classification; explicit review required.'))
      WHERE item_id=NEW.id AND (OLD.classified_at IS NOT NULL OR OLD.status!='new');
    DELETE FROM intake_reviews WHERE item_id=NEW.id AND OLD.classified_at IS NULL AND OLD.status='new';
END;

-- A gist (takeaway + do/read/skip) describes the text it was written from; autopilot acts on it.
DROP TRIGGER IF EXISTS invalidate_item_gist;
CREATE TRIGGER invalidate_item_gist AFTER UPDATE OF title,body,url,author ON items
WHEN OLD.title IS NOT NEW.title OR OLD.body IS NOT NEW.body OR OLD.url IS NOT NEW.url OR OLD.author IS NOT NEW.author
BEGIN
    DELETE FROM item_gists WHERE item_id=NEW.id;
END;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, body, author, content=items, content_rowid=id
);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, body, author)
    VALUES (new.id, new.title, new.body, new.author);
END;

CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, author)
    VALUES ('delete', old.id, old.title, old.body, old.author);
    INSERT INTO items_fts(rowid, title, body, author)
    VALUES (new.id, new.title, new.body, new.author);
END;

CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, body, author)
    VALUES ('delete', old.id, old.title, old.body, old.author);
END;
"""


# Library word search. A design post on X often says little ("this is so clean"), so a save is found by
# the poster's words (title, text, author, the post it quotes) and by the words Beacon wrote about it
# (gist takeaway, pile takeaway, one-liner, tags), and by the owner's own notes and tags. Porter stemming lets "drag" find "dragging". Kept
# apart from items_fts, which triage, routing and meaning search use as measured.
LIBRARY_FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(words, about, tokenize='porter unicode61');"
_LIBRARY_ROW = """
SELECT i.id,
       COALESCE(i.title, '') || ' ' || COALESCE(i.body, '') || ' ' || COALESCE(i.author, '') || ' ' ||
       COALESCE(CASE WHEN json_valid(i.raw_json) THEN json_extract(i.raw_json, '$.quotedTweet.text') END, ''),
       COALESCE((SELECT takeaway || ' ' || kind FROM item_gists WHERE item_id = i.id), '') || ' ' ||
       COALESCE((SELECT takeaway FROM pile_decisions WHERE item_id = i.id), '') || ' ' || COALESCE(i.one_liner, '') || ' ' ||
       COALESCE((SELECT group_concat(t.name, ' ') FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE it.item_id = i.id), '') || ' ' ||
       COALESCE((SELECT group_concat(n.body || ' ' || COALESCE(n.context, ''), ' ') FROM owner_notes n WHERE n.item_id = i.id), '')
FROM items i"""
_REFRESH = "DELETE FROM library_fts WHERE rowid = {id}; INSERT INTO library_fts(rowid, words, about) " + _LIBRARY_ROW + " WHERE i.id = {id};"
_LIBRARY_TRIGGERS = [  # (name, event, the id it refreshes, whether the save itself is gone)
    ("items_insert", "AFTER INSERT ON items", "NEW.id", False),
    ("items_update", "AFTER UPDATE OF title, body, author, raw_json, one_liner ON items", "NEW.id", False),
    ("items_delete", "AFTER DELETE ON items", "OLD.id", True),
    ("gist_insert", "AFTER INSERT ON item_gists", "NEW.item_id", False),
    ("gist_update", "AFTER UPDATE ON item_gists", "NEW.item_id", False),
    ("gist_delete", "AFTER DELETE ON item_gists", "OLD.item_id", False),
    ("pile_insert", "AFTER INSERT ON pile_decisions", "NEW.item_id", False),
    ("pile_update", "AFTER UPDATE ON pile_decisions", "NEW.item_id", False),
    ("pile_delete", "AFTER DELETE ON pile_decisions", "OLD.item_id", False),
    ("tag_insert", "AFTER INSERT ON item_tags", "NEW.item_id", False),
    ("tag_delete", "AFTER DELETE ON item_tags", "OLD.item_id", False),
    ("note_insert", "AFTER INSERT ON owner_notes", "NEW.item_id", False),
    ("note_update", "AFTER UPDATE ON owner_notes", "NEW.item_id", False),
    ("note_moved", "AFTER UPDATE OF item_id ON owner_notes WHEN OLD.item_id != NEW.item_id", "OLD.item_id", False),
    ("note_delete", "AFTER DELETE ON owner_notes", "OLD.item_id", False),
]


def _library_search(conn):
    """Create the Library's word index (filled from every save the first time) and the triggers that keep it current.

    Runs after the table rebuilds in _widen_checks, which would drop triggers on a rebuilt table.
    """
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        new = not conn.execute("SELECT 1 FROM sqlite_master WHERE name='library_fts'").fetchone()
        conn.execute(LIBRARY_FTS_SCHEMA)
        # Triggers made before owner notes would rewrite a row without them, so they are made again and the index refilled.
        old = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='library_fts_items_insert'").fetchone()
        if old and "owner_notes" not in old[0]:
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'library_fts_%'").fetchall():
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute("DELETE FROM library_fts")
            new = True
        if new:
            conn.execute("INSERT INTO library_fts(rowid, words, about) " + _LIBRARY_ROW)
        for name, event, item_id, gone in _LIBRARY_TRIGGERS:
            body = f"DELETE FROM library_fts WHERE rowid = {item_id};" if gone else _REFRESH.format(id=item_id)
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS library_fts_{name} {event} BEGIN {body} END")


def get_connection(path=None):
    conn = sqlite3.connect(current_path() if path is None else path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


ACTIVATION_STATES = ("running", "needs_you", "ready_to_review", "done", "didnt_work", "handed_off")
ACTIVATION_OPEN = ("running", "needs_you", "ready_to_review", "handed_off")


def sql_list(states):
    return "(" + ", ".join(f"'{state}'" for state in states) + ")"


# Pinterest pins are pictures like X media posts, so they take the same media test.
IN_LIBRARY_EXPRESSION = (
    "(status != 'rejected' AND "
    "(source IN ('github', 'add') OR (source IN ('x', 'pinterest') AND CASE WHEN json_valid(raw_json) "
    "THEN json_array_length(json_extract(raw_json, '$.media')) > 0 ELSE 0 END)))"
)

MIGRATIONS = [
    "ALTER TABLE items ADD COLUMN human_read INTEGER DEFAULT 0",
    "ALTER TABLE items ADD COLUMN one_liner TEXT",
    "ALTER TABLE items ADD COLUMN reading_routed INTEGER DEFAULT 0",
    "ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'new'",
    "ALTER TABLE items ADD COLUMN status_at TEXT",
    "ALTER TABLE items ADD COLUMN status_note TEXT",
    "ALTER TABLE items ADD COLUMN saved_at TEXT",
    "ALTER TABLE briefs ADD COLUMN plan_rank INTEGER",
    "ALTER TABLE briefs ADD COLUMN for_mac INTEGER DEFAULT 0",
    # 'project' (a codebase with a backlog and an agent) or 'area' (a lifestyle shelf such as Food).
    "ALTER TABLE workstreams ADD COLUMN kind TEXT DEFAULT 'project'",
    # Whether a save shows in the Library: not rejected, and a GitHub star, your own add, or an X
    # post or Pinterest pin with a picture. Computed by SQLite and indexed, so a Library page never reads every
    # save's JSON (450 ms before, under 5 ms after, on 23,000 saves).
    f"ALTER TABLE items ADD COLUMN in_library INTEGER GENERATED ALWAYS AS {IN_LIBRARY_EXPRESSION} VIRTUAL",
    # (CASE, not AND: SQLite computes this on every write, and AND may read bad JSON and fail the write.)
    "CREATE INDEX IF NOT EXISTS items_in_library ON items(in_library, id)",
    # Autopilot (lib/autopilot.py): what Beacon did on its own, and the owner's choices with the suggestion they saw.
    "CREATE TABLE IF NOT EXISTS auto_actions (item_id INTEGER PRIMARY KEY REFERENCES items(id), "
    "action TEXT NOT NULL CHECK(action IN ('pass', 'read', 'reference')), reason TEXT, acted_at TEXT NOT NULL, undone_at TEXT)",
    "CREATE TABLE IF NOT EXISTS choice_log (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, "
    "suggestion TEXT, choice TEXT NOT NULL, source TEXT NOT NULL, chosen_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS activations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "item_id INTEGER NOT NULL REFERENCES items(id), destination TEXT NOT NULL, runner TEXT NOT NULL, target TEXT, "
    f"state TEXT NOT NULL CHECK(state IN {sql_list(ACTIVATION_STATES)}), summary TEXT, needs TEXT, "
    "links_json TEXT NOT NULL DEFAULT '[]', thread_ref TEXT, prompt TEXT, pid INTEGER, "
    "started_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS activations_one_open ON activations(item_id) WHERE state IN {sql_list(ACTIVATION_OPEN)}",
    "CREATE INDEX IF NOT EXISTS activations_recent ON activations(updated_at)",
    "ALTER TABLE activations ADD COLUMN runner_pgid INTEGER",
    "ALTER TABLE activations ADD COLUMN git_dir TEXT",
    "ALTER TABLE activations ADD COLUMN runner_identity TEXT",
    "ALTER TABLE activations ADD COLUMN base TEXT",
    # What agents asked `beacon mcp` (lib/mcp.py), read back by `beacon stats`. Stays on this Mac like the rest.
    "CREATE TABLE IF NOT EXISTS agent_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, client TEXT, "
    "tool TEXT NOT NULL, found INTEGER, ids TEXT, ok INTEGER NOT NULL, ms INTEGER, called_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS agent_calls_called_at ON agent_calls(called_at)",
    "CREATE TABLE IF NOT EXISTS match_recommendations (item_id INTEGER NOT NULL REFERENCES items(id), "
    "target_kind TEXT NOT NULL, target_slug TEXT NOT NULL, disposition TEXT NOT NULL, confidence REAL NOT NULL, "
    "explanation TEXT NOT NULL, next_action TEXT NOT NULL, evidence_json TEXT NOT NULL, citation_json TEXT NOT NULL, "
    "matched_at TEXT NOT NULL, PRIMARY KEY (item_id, target_kind, target_slug))",
    "CREATE TABLE IF NOT EXISTS match_outcomes (item_id INTEGER PRIMARY KEY REFERENCES items(id), "
    "outcome TEXT NOT NULL, reason TEXT NOT NULL, matched_at TEXT NOT NULL)",
    # A plan or follow-up being written (lib/briefs.py): who claimed it, and the state of the save it started from.
    "CREATE TABLE IF NOT EXISTS brief_claims (item_id INTEGER NOT NULL, workstream TEXT NOT NULL, token TEXT NOT NULL, "
    "snapshot TEXT NOT NULL, claimed_at TEXT NOT NULL, PRIMARY KEY (item_id, workstream))",
    # Pick (lib/pick.py): Haiku's score per save and axis (NULL when the axis doesn't fit), prepared rounds,
    # and your picks. origin separates your deliberate choices from saves, likes and Beacon's own passes.
    "CREATE TABLE IF NOT EXISTS pick_scores (item_id INTEGER NOT NULL REFERENCES items(id), axis TEXT NOT NULL, "
    "score REAL, scored_at TEXT NOT NULL, PRIMARY KEY (item_id, axis))",
    "CREATE TABLE IF NOT EXISTS pick_rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, axis TEXT NOT NULL, "
    "pairs_json TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT)",
    "CREATE TABLE IF NOT EXISTS pick_log (id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL "
    "REFERENCES pick_rounds(id), position INTEGER NOT NULL, left_id INTEGER NOT NULL, right_id INTEGER NOT NULL, "
    "axis TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('matched','foil','cross','repeat')), "
    "choice TEXT NOT NULL CHECK(choice IN ('left','right','neither','skip','same')), why TEXT, "
    "beacon_guess TEXT NOT NULL CHECK(beacon_guess IN ('left','right','none')), repeat_of INTEGER, shown_at TEXT, "
    "chosen_at TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'you-deliberate', UNIQUE(round_id, position))",
    # Which scorer made a pick score or built a round: NULL before stricter `applies` (lib/pick.py SCORER).
    "ALTER TABLE pick_scores ADD COLUMN scorer INTEGER",
    "ALTER TABLE pick_rounds ADD COLUMN scorer INTEGER",
    # Calls (lib/calls.py): sessions of cards, your checks of Beacon's keep/pass calls, the changes they made
    # (with what to put back), nightly suspect scores, neighbours flagged by a change, and group re-decides.
    "CREATE TABLE IF NOT EXISTS call_sessions (id TEXT PRIMARY KEY, size INTEGER NOT NULL, cards_json TEXT NOT NULL, "
    "created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS call_checks (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL "
    "REFERENCES call_sessions(id), card_id TEXT NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id), "
    "stratum TEXT NOT NULL CHECK(stratum IN ('readable','near-empty','keeps')), "
    "kind TEXT NOT NULL CHECK(kind IN ('random','suspect','repeat')), repeat_of INTEGER, "
    "beacon_outcome TEXT NOT NULL CHECK(beacon_outcome IN ('do','read','reference','pass')), "
    "your_outcome TEXT CHECK(your_outcome IN ('keep','pass')), unsure INTEGER NOT NULL DEFAULT 0, destination TEXT, "
    "chip TEXT, checked_at TEXT NOT NULL, voided_at TEXT)",
    "CREATE INDEX IF NOT EXISTS call_checks_item ON call_checks(item_id)",
    "CREATE INDEX IF NOT EXISTS call_checks_session ON call_checks(session_id, card_id)",
    "CREATE TABLE IF NOT EXISTS call_overrides (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL "
    "REFERENCES items(id), check_id INTEGER REFERENCES call_checks(id), "
    "source TEXT NOT NULL CHECK(source IN ('check','redecide')), old_outcome TEXT NOT NULL, old_destination TEXT, "
    "old_status TEXT, old_status_note TEXT, old_status_at TEXT, old_routes_json TEXT NOT NULL DEFAULT '[]', "
    "new_outcome TEXT NOT NULL, new_destination TEXT, new_status TEXT, at TEXT NOT NULL, undone_at TEXT, flagged_at TEXT)",
    "CREATE INDEX IF NOT EXISTS call_overrides_item ON call_overrides(item_id)",
    "CREATE TABLE IF NOT EXISTS call_suspects (item_id INTEGER PRIMARY KEY REFERENCES items(id), score REAL NOT NULL, "
    "neighbours INTEGER NOT NULL, suggested_destination TEXT, ranked_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS call_flags (item_id INTEGER PRIMARY KEY REFERENCES items(id), change_id INTEGER NOT NULL "
    "REFERENCES call_overrides(id), flagged_at TEXT NOT NULL, used_session TEXT)",
    "CREATE TABLE IF NOT EXISTS call_redecides (id INTEGER PRIMARY KEY AUTOINCREMENT, stratum TEXT NOT NULL, "
    "started_at TEXT NOT NULL, finished_at TEXT, result_json TEXT)",
    # Generated Library layouts. The app no longer writes it (Arrange was dropped); the experiment's composers in
    # runs/2026-11-19-arrange-r3 still log to it on database copies.
    "CREATE TABLE IF NOT EXISTS compose_log (id INTEGER PRIMARY KEY AUTOINCREMENT, search TEXT NOT NULL, "
    "ids_json TEXT NOT NULL, layout_json TEXT, why TEXT, model TEXT NOT NULL, cost_usd REAL, created_at TEXT NOT NULL, "
    "choice TEXT CHECK(choice IN ('kept','dismissed','ignored')), chosen_at TEXT)",
    # The owner's own tags and notes from mymind, Readwise and Granola (lib/owner_notes.py), kept apart from
    # anything a model wrote. owner_digest is a model's summary of what the owner said (Granola).
    "CREATE TABLE IF NOT EXISTS owner_notes (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), "
    "source TEXT NOT NULL, external_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('note','tag')), "
    "origin TEXT NOT NULL CHECK(origin IN ('owner','owner_digest')), body TEXT NOT NULL, context TEXT, "
    "created_at TEXT, synced_at TEXT NOT NULL, UNIQUE(source, external_id))",
    "CREATE INDEX IF NOT EXISTS owner_notes_item ON owner_notes(item_id)",
    # The owner's vocabulary (lib/vocabulary.py): which projects and areas an owner term points at, and which
    # unresolved terms a model was already asked about against which set of projects and areas.
    "CREATE TABLE IF NOT EXISTS owner_vocabulary (term TEXT NOT NULL, target TEXT NOT NULL, "
    "origin TEXT NOT NULL CHECK(origin IN ('owner','model_proposed')), created_at TEXT NOT NULL, PRIMARY KEY(term, target))",
    "CREATE TABLE IF NOT EXISTS vocab_proposals (term TEXT NOT NULL, slug_set TEXT NOT NULL, proposed_at TEXT NOT NULL, "
    "PRIMARY KEY(term, slug_set))",
]

VALID_STATUSES = {"new", "queued", "reviewed", "tried", "adopted", "rejected", "stale", "interest", "pending"}
REFERENCE = "reference"
FILED_NOTES = ("read", REFERENCE)  # adopted with one of these, a save is filed away: it gets no area follow-up

# SQLite can't change a CHECK in place, so a table made before references existed is rebuilt with the wider list.
WIDENED_CHECKS = [("item_gists", "('do','read','skip')", "('do','read','skip','reference')"),
                  ("auto_actions", "('pass', 'read')", "('pass', 'read', 'reference')"),
                  ("pick_log", "('left','right','neither','skip')", "('left','right','neither','skip','same')")]


def _widen_checks(conn):
    for table, old, new in WIDENED_CHECKS:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row and old in row[0]:
            conn.execute("PRAGMA legacy_alter_table=ON")  # else the rename rewrites the trigger that names this table
            try:
                with conn:
                    conn.execute("BEGIN")
                    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                    conn.execute(row[0].replace(old, new))
                    conn.execute(f"INSERT INTO {table} SELECT * FROM {table}_old")
                    conn.execute(f"DROP TABLE {table}_old")
            finally:
                conn.execute("PRAGMA legacy_alter_table=OFF")


def apply_migration(conn, sql):
    """Run one schema step. Only a column that is already there is skipped; any other error (a lock,
    a missing table, an unsupported expression) stops initialization instead of leaving a half-upgraded database."""
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as error:
        if not str(error).startswith("duplicate column name"):
            raise sqlite3.OperationalError(f"Beacon couldn't upgrade its database: {error} (in: {sql[:80]})") from error


def init_db(path=None):
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(FTS_SCHEMA)
        conn.executescript(FTS_TRIGGERS)
        for migration in MIGRATIONS:
            apply_migration(conn, migration)
        _refresh_in_library(conn)
        conn.commit()
        _widen_checks(conn)
        _library_search(conn)
    finally:
        conn.close()


def _refresh_in_library(conn):
    """A virtual column's expression can't be altered, so a database made before Pinterest rebuilds it once."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='items'").fetchone()
    if not sql or "in_library" not in sql[0] or "'pinterest'" in sql[0]:
        return
    try:
        conn.execute("BEGIN")
        conn.execute("DROP INDEX IF EXISTS items_in_library")
        conn.execute("ALTER TABLE items DROP COLUMN in_library")
        conn.execute(f"ALTER TABLE items ADD COLUMN in_library INTEGER GENERATED ALWAYS AS {IN_LIBRARY_EXPRESSION} VIRTUAL")
        conn.execute("CREATE INDEX IF NOT EXISTS items_in_library ON items(in_library, id)")
        conn.execute("COMMIT")
    except sqlite3.OperationalError as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        print(f"  Couldn't update the Library column for Pinterest pins: {error}")


def seed_workstreams(workstreams, kind="project"):
    conn = get_connection()
    try:
        for ws in workstreams:
            # "active": false in config parks a project: nothing routes to it and its items leave triage.
            conn.execute(
                """INSERT INTO workstreams (slug, name, backlog_path, active, kind) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET name=excluded.name, backlog_path=excluded.backlog_path,
                   active=excluded.active, kind=excluded.kind""",
                (ws["slug"], ws["name"], ws.get("backlog_path"), int(ws.get("active", True)), kind),
            )
        conn.commit()
    finally:
        conn.close()


def upsert_item(
    source,
    external_id,
    title,
    body,
    url,
    author,
    raw_json,
    created_at,
    synced_at,
    saved_at=None,
    strict=False,
):
    """Insert or refresh one item; True if it is new.

    A failed write normally prints and returns False. Callers that move a sync checkpoint on the
    result pass strict=True, so a failed write cannot pass for an item that was already known.
    """
    conn = get_connection()
    try:
        existed = conn.execute(
            "SELECT 1 FROM items WHERE source=? AND external_id=?",
            (source, external_id),
        ).fetchone() is not None
        conn.execute(
            """INSERT INTO items
                   (source, external_id, title, body, url, author, raw_json, created_at, synced_at, saved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, external_id) DO UPDATE SET
                   title=excluded.title, body=excluded.body, url=excluded.url,
                   author=excluded.author, raw_json=excluded.raw_json,
                   synced_at=excluded.synced_at,
                   saved_at=COALESCE(excluded.saved_at, items.saved_at)""",
            (
                source,
                external_id,
                title,
                body,
                url,
                author,
                raw_json,
                created_at,
                synced_at,
                saved_at,
            ),
        )
        conn.commit()
        return not existed
    except Exception as e:
        if strict:
            raise
        print(f"  Error upserting {source}/{external_id}: {e}")
        return False
    finally:
        conn.close()


def get_unclassified_items(limit=50, since=None):
    """since: ISO date; skips items posted/saved earlier (non-ISO legacy dates count as old)."""
    conn = get_connection()
    date_filter = ("AND COALESCE(saved_at, created_at) GLOB '[0-9][0-9][0-9][0-9]-*' "
                   "AND COALESCE(saved_at, created_at) >= ?") if since else ""
    rows = conn.execute(
        f"""SELECT id, source, external_id, title, body, url, author, one_liner,
                  (SELECT group_concat(n.kind || ': ' || n.body, ' | ') FROM owner_notes n
                   WHERE n.item_id = items.id AND n.origin = 'owner') AS owner_notes FROM items
           WHERE classified_at IS NULL AND status='new'
             AND NOT EXISTS(SELECT 1 FROM intake_reviews r WHERE r.item_id=items.id)
             {date_filter}
           ORDER BY synced_at DESC,id LIMIT ?""",
        ((since, limit) if since else (limit,)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


_TCO = re.compile(r"\s*https://t\.co/\w+")


def item_preview(source, raw_json, body):
    """What the Triage card renders: cleaned text, media, quoted post, repo facts and a sortable date."""
    try:
        raw = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    preview = {"text": html.unescape(body or "") if source == "x" else body or ""}
    if source == "github":
        preview["repo"] = {"name": raw.get("full_name"), "language": raw.get("language"),
                           "stars": raw.get("stargazers_count"), "topics": raw.get("topics") or []}
        preview["date"] = raw.get("starred_at") or raw.get("created_at")
        return preview
    if source == "pinterest":
        preview["media"] = [{"type": "photo", "url": m.get("url"), "preview_url": None, "width": None, "height": None}
                            for m in raw.get("media") or [] if isinstance(m, dict)][:1]
        preview["date"] = raw.get("published") or raw.get("created_at")
        preview["board"] = raw.get("board")
        return preview
    if source not in ("x", "add"):
        return preview
    author = raw.get("author") or {}
    preview["author_name"] = author.get("name")
    preview["likes"] = raw.get("likeCount")
    preview["media"] = [{"type": m.get("type"), "url": m.get("url"), "preview_url": m.get("previewUrl"),
                         "width": m.get("width"), "height": m.get("height")} for m in raw.get("media") or []][:4]
    quoted = raw.get("quotedTweet")
    if isinstance(quoted, dict):
        quoted_author = quoted.get("author") or {}
        preview["quoted"] = {"author": f"@{quoted_author.get('username', '')}", "name": quoted_author.get("name"),
                             "text": html.unescape(_TCO.sub("", quoted.get("text") or "")).strip()}
    links = _TCO.findall(preview["text"])
    if preview["media"] or "quoted" in preview:
        # The trailing t.co link points at the media or quote we already render.
        preview["text"] = _TCO.sub("", preview["text"]).strip()
    elif links:
        preview["link"] = links[-1].strip()
    created = raw.get("createdAt")
    try:
        preview["date"] = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y").astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        preview["date"] = created
    return preview


def fts_query(query):
    """What you typed as an FTS5 query: every word must appear; quotes and operators are plain text."""
    return " ".join('"' + term.replace('"', '""') + '"' for term in query.split())


def get_library(offset=0, limit=200, area=None, query=None, ranked=None, noted=False, hide=()):
    """Mosaic's feed: every save with a picture, plus GitHub stars, newest first.

    Rejected items stay out. A video comes through as its poster image, since Mosaic stores
    pictures; text-only posts wait for Triage. Each save carries its area (shelf) when Jev placed it
    on one above the same 0.6 floor triage uses; `area` keeps only that shelf, and `query` keeps
    saves where every word (in any of its forms) is in the post or in what Beacon wrote about it
    (see LIBRARY_FTS_SCHEMA), best match first. `ranked` (item ids, best
    first, from a meaning or similarity search) keeps only those saves, in that order. `noted` also
    keeps saves outside the feed that carry the owner's own notes (a mymind card, a Readwise book), for agents.
    `hide` leaves out these save ids.
    """
    shelf = "a.area != 'none' AND a.probability >= 0.6"
    where = ("(i.in_library = 1 OR (i.status != 'rejected' AND EXISTS(SELECT 1 FROM owner_notes n WHERE n.item_id = i.id)))"
             if noted else "i.in_library = 1")
    params = tuple(hide)
    if hide:
        where += f" AND i.id NOT IN ({','.join('?' * len(params))})"
    if area:
        where += f" AND {shelf} AND a.area = ?"
        params = (*params, area)
    order = "i.id DESC"
    joined = "FROM items i LEFT JOIN area_reviews a ON a.item_id = i.id"
    if query and query.strip():
        joined += " JOIN library_fts f ON f.rowid = i.id"
        where += " AND library_fts MATCH ?"
        params = (*params, fts_query(query))
        order = "f.rank"
    if ranked is not None:
        ranked = list(dict.fromkeys(int(item_id) for item_id in ranked))
        if not ranked:
            return {"total": 0, "items": [], "next_offset": None}
        where += f" AND i.id IN ({','.join('?' * len(ranked))})"
        params = (*params, *ranked)
    joined += f" WHERE {where}"
    columns = f"SELECT i.id, i.source, i.title, i.body, i.url, i.author, i.raw_json, CASE WHEN {shelf} THEN a.area END AS area"
    with closing(get_connection()) as conn:
        if ranked is not None:
            # Put the search's order back in Python: ordering hundreds of ids in SQL took 50 ms.
            position = {item_id: index for index, item_id in enumerate(ranked)}
            found = sorted(conn.execute(f"{columns} {joined}", params).fetchall(), key=lambda row: position[row["id"]])
            total, rows = len(found), found[offset:offset + limit]
        else:
            total = conn.execute(f"SELECT COUNT(*) {joined}", params).fetchone()[0]
            rows = conn.execute(f"{columns} {joined} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        notes = owner_notes_for(conn, [row["id"] for row in rows])
    items = [library_entry(row, notes.get(row["id"])) for row in rows]
    next_offset = offset + len(rows) if offset + len(rows) < total else None
    return {"total": total, "items": items, "next_offset": next_offset}


# Items (aliased `i`) with no open intake review. A review that isn't approved is a hold:
# nothing automatic may act on the item until the owner clears it.
NOT_HELD_SQL = "NOT EXISTS(SELECT 1 FROM intake_reviews r WHERE r.item_id=i.id AND r.disposition!='approved')"


def is_held(item_id):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT 1 FROM items i WHERE i.id=? AND NOT " + NOT_HELD_SQL, (item_id,)).fetchone() is not None
    finally:
        conn.close()


def owner_notes_for(conn, ids):
    """{item id: [the owner's notes and tags on it]}, oldest first."""
    found = {}
    for start in range(0, len(ids), 500):
        chunk = list(ids[start:start + 500])
        for row in conn.execute(f"SELECT item_id, source, kind, origin, body FROM owner_notes WHERE item_id IN "
                                f"({','.join('?' * len(chunk))}) ORDER BY id", chunk):
            found.setdefault(row["item_id"], []).append(
                {"source": row["source"], "kind": row["kind"], "origin": row["origin"], "body": row["body"]})
    return found


def library_entry(row, owner_notes=None):
    """One save as the Library, the feed and the MCP tools show it; `row` has the get_library columns.

    `owner_notes` are the owner's own tags and notes on it (see owner_notes_for), labeled by origin.
    """
    preview = item_preview(row["source"], row["raw_json"], row["body"])
    return {
        "id": row["id"], "source": row["source"], "url": row["url"], "title": row["title"],
        "text": preview.get("text") or "", "author": row["author"], "author_name": preview.get("author_name"),
        "date": preview.get("date"), "repo": preview.get("repo"), "area": row["area"],
        "media": [{"url": m["preview_url"] if m["type"] in ("video", "animated_gif") else m["url"],
                   "width": m.get("width"), "height": m.get("height")}
                  for m in preview.get("media") or [] if m.get("url")],
        "owner_notes": owner_notes or [],
    }


def get_triage_queue():
    """Queued items waiting for a keep/done/reject decision, best project match first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT i.id, i.source, i.title, i.body, i.url, i.author, i.created_at, i.intent, i.raw_json,
                      w.slug, w.name, w.kind AS destination_kind, iw.confidence, s.theme, s.description AS theme_description, s.rank AS stack_rank, s.duplicate_of,
                      g.takeaway, g.kind, g.suggestion, g.reason
               FROM items i
               JOIN item_workstreams iw ON iw.item_id = i.id
               JOIN workstreams w ON w.id = iw.workstream_id AND w.active = 1
               LEFT JOIN item_stacks s ON s.item_id = i.id
               LEFT JOIN item_gists g ON g.item_id = i.id
               WHERE i.status = 'queued' AND """ + NOT_HELD_SQL
        ).fetchall()
    finally:
        conn.close()
    items = {}
    for row in rows:
        item = items.setdefault(row["id"], {key: row[key] for key in
                                            ("id", "source", "title", "body", "url", "author", "created_at", "intent")})
        item.setdefault("preview", item_preview(row["source"], row["raw_json"], row["body"]))
        if row["theme"] and "stack" not in item:
            item["stack"] = {"theme": row["theme"], "description": row["theme_description"],
                             "rank": row["stack_rank"], "duplicate_of": row["duplicate_of"]}
        if row["takeaway"] and "gist" not in item:
            item["gist"] = {key: row[key] for key in ("takeaway", "kind", "suggestion", "reason")}
        item.setdefault("projects", []).append({"slug": row["slug"], "name": row["name"], "fit": row["confidence"], "kind": row["destination_kind"]})
    for item in items.values():
        item["projects"].sort(key=lambda project: -(project["fit"] or 0))
        best = item["projects"][0]
        if best["kind"] == "area":
            # One stack per shelf: an area is already the theme.
            item["stack"] = {**item.get("stack", {"rank": None, "duplicate_of": None}),
                             "theme": best["name"], "description": f"Saves for your {best['name']} shelf."}
    return sorted(items.values(), key=lambda item: (-(item["projects"][0]["fit"] or 0), item["id"]))


def mark_classified(item_id, intent, tags, workstream_assignments, classified_at, human_read=False, one_liner=None):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE items SET intent=?, classified_at=?, human_read=?, one_liner=? WHERE id=?",
            (intent, classified_at, 1 if human_read else 0, one_liner, item_id),
        )

        for tag_name in tags:
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name=?", (tag_name,)).fetchone()["id"]
            conn.execute("INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))

        for ws in workstream_assignments:
            ws_row = conn.execute("SELECT id FROM workstreams WHERE slug=?", (ws["slug"],)).fetchone()
            if ws_row:
                conn.execute(
                    "INSERT OR REPLACE INTO item_workstreams (item_id, workstream_id, confidence) VALUES (?, ?, ?)",
                    (item_id, ws_row["id"], ws["confidence"]),
                )

        conn.commit()
    finally:
        conn.close()


def get_unrouted_items():
    conn = get_connection()
    rows = conn.execute(
        """SELECT i.id, i.source, i.external_id, i.title, i.body, i.url, i.author, i.intent, i.status,
                  w.slug as workstream_slug, w.backlog_path, iw.confidence
           FROM item_workstreams iw
           JOIN items i ON i.id = iw.item_id
           JOIN workstreams w ON w.id = iw.workstream_id
           WHERE iw.routed = 0 AND i.classified_at IS NOT NULL AND w.active=1
             AND i.status IN ('new','queued','reviewed','tried','adopted')
             AND NOT EXISTS(SELECT 1 FROM intake_reviews r WHERE r.item_id=i.id AND r.disposition!='approved')
           ORDER BY w.slug, i.synced_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_routed_batch(item_ids, workstream_slug, routed_at):
    conn = get_connection()
    try:
        ws_row = conn.execute("SELECT id FROM workstreams WHERE slug=?", (workstream_slug,)).fetchone()
        if not ws_row:
            return
        ws_id = ws_row["id"]
        for item_id in item_ids:
            conn.execute(
                "UPDATE item_workstreams SET routed=1, routed_at=? WHERE item_id=? AND workstream_id=?",
                (routed_at, item_id, ws_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_item_tags(item_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE it.item_id=?",
        (item_id,),
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def update_sync_state(source, last_id, last_sync, items_synced):
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO sync_state (source, last_id, last_sync, items_synced)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET last_id=excluded.last_id, last_sync=excluded.last_sync, items_synced=excluded.items_synced""",
            (source, last_id, last_sync, items_synced),
        )
        conn.commit()
    finally:
        conn.close()


def get_sync_state(source):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT source, last_id, last_sync, items_synced FROM sync_state WHERE source=?",
            (source,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_evaluation_candidates(statuses):
    """Return stable evaluation candidates without changing lifecycle state."""
    if not statuses:
        return []

    placeholders = ",".join("?" for _ in statuses)
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT i.id, i.source, i.external_id, i.title, i.url, i.author,
                       i.intent, i.one_liner, i.status, i.status_note, i.synced_at,
                       i.saved_at, GROUP_CONCAT(DISTINCT w.slug) AS workstreams
                FROM items i
                LEFT JOIN item_workstreams iw ON iw.item_id = i.id
                LEFT JOIN workstreams w ON w.id = iw.workstream_id
                WHERE i.status IN ({placeholders})
                GROUP BY i.id
                ORDER BY i.id""",
            tuple(statuses),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_unrouted_reading_items():
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, source, title, body, url, author, intent, one_liner, synced_at
           FROM items WHERE human_read = 1 AND reading_routed = 0
             AND classified_at IS NOT NULL AND status IN ('new','queued','reviewed','tried','adopted')
             AND NOT EXISTS(SELECT 1 FROM intake_reviews r WHERE r.item_id=items.id AND r.disposition!='approved')
           ORDER BY synced_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_reading_routed(item_ids):
    conn = get_connection()
    try:
        for item_id in item_ids:
            conn.execute("UPDATE items SET reading_routed = 1 WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()


def get_items(source=None, workstream=None, intent=None, unrouted=False, status=None, limit=20):
    conn = get_connection()
    query = (
        "SELECT DISTINCT i.id, i.source, i.title, i.body, i.url, i.author, i.intent, i.status, "
        "i.status_at, i.synced_at, json_extract(i.raw_json, '$.x_explanation') AS x_explanation FROM items i"
    )
    joins = []
    conditions = []
    params = []

    if workstream:
        joins.append("JOIN item_workstreams iw ON iw.item_id = i.id")
        joins.append("JOIN workstreams w ON w.id = iw.workstream_id")
        conditions.append("w.slug = ?")
        params.append(workstream)
    if unrouted:
        if not workstream:
            joins.append("JOIN item_workstreams iw ON iw.item_id = i.id")
        conditions.append("iw.routed = 0")
    if source:
        conditions.append("i.source = ?")
        params.append(source)
    if intent:
        conditions.append("i.intent = ?")
        params.append(intent)
    if status:
        conditions.append("i.status = ?")
        params.append(status)

    query += " " + " ".join(joins)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY i.synced_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) as c FROM items").fetchone()["c"]
    by_source = conn.execute("SELECT source, COUNT(*) as c FROM items GROUP BY source").fetchall()
    by_intent = conn.execute("SELECT intent, COUNT(*) as c FROM items WHERE intent IS NOT NULL GROUP BY intent").fetchall()
    by_ws = conn.execute(
        """SELECT w.slug, COUNT(*) as c FROM item_workstreams iw
           JOIN workstreams w ON w.id = iw.workstream_id GROUP BY w.slug"""
    ).fetchall()
    classified = conn.execute("SELECT COUNT(*) as c FROM items WHERE classified_at IS NOT NULL").fetchone()["c"]
    routed = conn.execute("SELECT COUNT(*) as c FROM item_workstreams WHERE routed = 1").fetchone()["c"]
    human_read = conn.execute("SELECT COUNT(*) as c FROM items WHERE human_read = 1").fetchone()["c"]
    conn.close()
    return {
        "total": total,
        "classified": classified,
        "routed": routed,
        "human_read": human_read,
        "by_source": {r["source"]: r["c"] for r in by_source},
        "by_intent": {r["intent"]: r["c"] for r in by_intent},
        "by_workstream": {r["slug"]: r["c"] for r in by_ws},
    }


def get_workstreams():
    conn = get_connection()
    # Projects only: areas have no backlog, scoring or agent. Triage reads areas through get_triage_queue.
    rows = conn.execute("SELECT slug, name, backlog_path, active FROM workstreams WHERE kind='project' ORDER BY slug").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_workstream(slug):
    conn = get_connection()
    row = conn.execute(
        "SELECT slug, name, backlog_path, active FROM workstreams WHERE slug = ?",
        (slug,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def add_workstream(slug, name, backlog_path):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO workstreams (slug, name, backlog_path) VALUES (?, ?, ?)",
            (slug, name, backlog_path),
        )
        conn.commit()
    finally:
        conn.close()


def assign_item_workstream(item_id, workstream_slug, confidence, routed=0, routed_at=None):
    """Assign an existing item to a workstream without touching item status."""
    conn = get_connection()
    try:
        ws_row = conn.execute("SELECT id FROM workstreams WHERE slug = ?", (workstream_slug,)).fetchone()
        if not ws_row:
            raise ValueError(f"Unknown workstream: {workstream_slug}")

        conn.execute(
            """INSERT INTO item_workstreams (item_id, workstream_id, confidence, routed, routed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(item_id, workstream_id) DO UPDATE SET
                   confidence=excluded.confidence,
                   routed=excluded.routed,
                   routed_at=excluded.routed_at""",
            (item_id, ws_row["id"], confidence, routed, routed_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_backfill_candidate_items(workstream_slug, limit=100):
    """Get already-classified non-terminal items not yet assigned to a workstream."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT i.id, i.source, i.external_id, i.title, i.body, i.url, i.author,
                  i.intent, i.one_liner, i.status, i.synced_at
           FROM items i
           JOIN workstreams w ON w.slug = ?
           LEFT JOIN item_workstreams iw
             ON iw.item_id = i.id AND iw.workstream_id = w.id
           WHERE i.classified_at IS NOT NULL
             AND i.status IN ('new', 'queued', 'reviewed', 'tried')
             AND iw.item_id IS NULL
           ORDER BY i.synced_at DESC
           LIMIT ?""",
        (workstream_slug, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_item_status(item_id, new_status, note=None):
    """Set status on an item. No transition validation — single user, just set it."""
    conn = get_connection()
    try:
        set_item_status(conn, item_id, new_status, note)
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def set_item_status(conn, item_id, new_status, note=None):
    """update_item_status inside the caller's transaction; the caller commits."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {new_status}. Must be one of {VALID_STATUSES}")
    now = datetime.now(timezone.utc).isoformat()
    previous = conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
    conn.execute(
        "UPDATE items SET status=?, status_at=?, status_note=? WHERE id=?",
        (new_status, now, note, item_id),
    )
    if new_status == 'adopted' and note not in FILED_NOTES and previous and previous['status'] != 'adopted':
        from .followups import PENDING
        best = conn.execute(
            "SELECT w.slug,w.kind FROM item_workstreams iw JOIN workstreams w ON w.id=iw.workstream_id "
            "WHERE iw.item_id=? AND w.active=1 ORDER BY iw.confidence DESC,w.id LIMIT 1", (item_id,)).fetchone()
        if best and best['kind'] == 'area':
            conn.execute("INSERT OR IGNORE INTO briefs(item_id,workstream,status,note,created_at) VALUES(?,?,'skipped',?,?)",
                         (item_id, best['slug'], PENDING, now))


def get_items_by_status(status, workstream=None, limit=50):
    """Get items filtered by status and optionally workstream."""
    conn = get_connection()
    query = """SELECT DISTINCT i.id, i.source, i.title, i.body, i.url, i.author,
                      i.intent, i.status, i.status_at, i.status_note, i.synced_at
               FROM items i"""
    joins = []
    conditions = ["i.status = ?"]
    params = [status]

    if workstream:
        joins.append("JOIN item_workstreams iw ON iw.item_id = i.id")
        joins.append("JOIN workstreams w ON w.id = iw.workstream_id")
        conditions.append("w.slug = ?")
        params.append(workstream)

    query += " " + " ".join(joins)
    query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY i.synced_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sweep_stale(queued_days=21, reviewed_days=45):
    """Auto-archive items that have been sitting untouched. Returns count."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """UPDATE items SET status='stale', status_at=?, status_note='auto-archived'
               WHERE (status='queued' AND julianday('now') - julianday(COALESCE(status_at, synced_at)) > ?)
                  OR (status='reviewed' AND julianday('now') - julianday(COALESCE(status_at, synced_at)) > ?)""",
            (now, queued_days, reviewed_days),
        )
        count = cursor.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def get_active_items_for_workstream(workstream_slug):
    """Get all active (queued/reviewed/tried) items for a workstream, grouped by intent."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT i.id, i.source, i.title, i.body, i.url, i.author,
                  i.intent, i.status, i.status_at, i.status_note
           FROM items i
           JOIN item_workstreams iw ON iw.item_id = i.id
           JOIN workstreams w ON w.id = iw.workstream_id
           WHERE w.slug = ? AND i.status IN ('queued', 'reviewed', 'tried')
             AND NOT EXISTS(SELECT 1 FROM intake_reviews r WHERE r.item_id=i.id AND r.disposition!='approved')
           ORDER BY i.intent, i.synced_at DESC""",
        (workstream_slug,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stale_items_for_workstream(workstream_slug):
    """Get rejected/stale items for a workstream (for archive file)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT i.id, i.source, i.title, i.body, i.url, i.author,
                  i.intent, i.status, i.status_at, i.status_note
           FROM items i
           JOIN item_workstreams iw ON iw.item_id = i.id
           JOIN workstreams w ON w.id = iw.workstream_id
           WHERE w.slug = ? AND i.status IN ('stale', 'rejected')
           ORDER BY i.status_at DESC""",
        (workstream_slug,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_processing_stats():
    """Get item counts by status + per-workstream active/total."""
    conn = get_connection()
    by_status = conn.execute(
        "SELECT status, COUNT(*) as c FROM items GROUP BY status"
    ).fetchall()
    by_ws = conn.execute(
        """SELECT w.slug,
                  SUM(CASE WHEN i.status IN ('queued', 'reviewed', 'tried') THEN 1 ELSE 0 END) as active,
                  COUNT(i.id) as total
           FROM workstreams w
           LEFT JOIN item_workstreams iw ON iw.workstream_id = w.id
           LEFT JOIN items i ON i.id = iw.item_id
           WHERE w.active = 1
           GROUP BY w.slug ORDER BY w.slug"""
    ).fetchall()
    approaching = conn.execute(
        """SELECT COUNT(*) as c FROM items
           WHERE status='queued'
             AND julianday('now') - julianday(COALESCE(status_at, synced_at)) > 14"""
    ).fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) as c FROM items").fetchone()["c"]
    conn.close()
    return {
        "total": total,
        "by_status": {r["status"]: r["c"] for r in by_status},
        "by_workstream": [{"slug": r["slug"], "active": r["active"], "total": r["total"]} for r in by_ws],
        "approaching_stale": approaching,
    }


SEARCH_TOOLS = ("search_library", "find_similar", "project_references")


def log_agent_call(session, client, tool, found, ids, ok, ms):
    """Record usage counts and result IDs without capturing task text or tool arguments."""
    conn = get_connection()
    try:
        conn.execute("INSERT INTO agent_calls(session, client, tool, found, ids, ok, ms, called_at) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (session, client, tool, found, json.dumps(ids) if ids is not None else None, int(ok), ms,
                      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        conn.commit()
    finally:
        conn.close()


def agent_usage(days=7):
    """What agents did over the last `days`: calls per tool, searches that found nothing, and how many
    searches were followed, in the same session, by opening one of their results with get_save."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_connection()
    try:
        rows = conn.execute("SELECT session, client, tool, found, ids, ok FROM agent_calls "
                            "WHERE called_at >= ? ORDER BY id", (since,)).fetchall()
    finally:
        conn.close()
    by_tool, clients = {}, {}
    searches = opened = empty = 0
    pending = []  # (session, ids) of searches not yet followed by an open
    for session, client, tool, found, ids, ok in rows:
        by_tool[tool] = by_tool.get(tool, 0) + 1
        clients[client or "unknown"] = clients.get(client or "unknown", 0) + 1
        ids = json.loads(ids) if ids else []
        if tool in SEARCH_TOOLS and ok:
            searches += 1
            if found:
                pending.append([session, set(ids)])
            else:
                empty += 1
        elif tool == "get_save" and ok and ids:
            for search in pending:
                if search[0] == session and ids[0] in search[1]:
                    opened += 1
                    search[1] = set()  # count each search once
                    break
            pending = [search for search in pending if search[1]]
    return {"days": days, "calls": len(rows), "sessions": len({row[0] for row in rows}), "clients": clients,
            "by_tool": by_tool, "searches": searches, "empty_searches": empty, "searches_opened": opened,
            "errors": sum(1 for row in rows if not row[5])}
