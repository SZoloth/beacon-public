import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

import beacon
from lib import db, saves_handoff, x_handoff


READER = "2024-01-02T03:04:05+00:00"


def _readwise(reader_id="reader-synthetic", saved_at=READER, source="Reader Share Sheet iOS"):
    return {"reader_id": reader_id, "reader_saved_at": saved_at, "reader_source": source}


class SavesHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "saves.jsonl"
        self.database = self.root / "isolated.db"
        self.original_db = db.DB_PATH
        db.DB_PATH = str(self.database)
        self.addCleanup(setattr, db, "DB_PATH", self.original_db)

    def post(self, post_id, text, source_types, created_at="2026-03-01T12:00:00Z", **extra):
        item = {
            "post_id": post_id,
            "url": f"https://x.com/synthetic_user/status/{post_id}",
            "text": text,
            "created_at": created_at,
            "source_types": source_types,
            "author": {"username": "synthetic_user", "name": "Synthetic Name"},
        }
        item.update(extra)
        return item

    def write(self, posts):
        self.path.write_text("".join(json.dumps(post) + "\n" for post in posts), encoding="utf-8")
        return self.path

    def rows(self):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT id, source, external_id, title, body, status, saved_at, created_at, raw_json FROM items ORDER BY external_id"
            )]

    def test_preview_prints_counts_without_a_database_or_text(self):
        self.write([
            self.post("1900000000000000101", "SYNTHETIC bookmark text", ["bookmark"], fetched_at="2026-11-01T00:00:00Z"),
            self.post("1900000000000000102", "SYNTHETIC title", ["readwise_tweet"], created_at=None,
                      text_is_title_only=True, readwise=_readwise(source="Beacon")),
        ])
        with mock.patch.object(db, "init_db", wraps=db.init_db) as init_db:
            preview = saves_handoff.import_saves(self.path, self.database, preview=True)
        init_db.assert_not_called()
        self.assertFalse(self.database.exists())
        self.assertFalse(preview["live"])
        self.assertEqual(preview["mode"], "handoff")
        self.assertEqual(preview["posts"], 2)
        self.assertEqual(preview["bookmarks"], 1)
        self.assertEqual(preview["readwise_only"], 1)
        self.assertEqual(preview["echo"], 1)
        self.assertEqual(preview["actionable"], 1)
        self.assertEqual(preview["abstained"], 1)
        blob = json.dumps(preview)
        self.assertNotIn("SYNTHETIC", blob)
        self.assertNotIn("1900000000000000101", blob)

    def test_roles_timestamps_and_repeat_import(self):
        self.write([
            self.post("1900000000000000201", "SYNTHETIC fresh bookmark", ["bookmark"], fetched_at="2026-11-01T00:00:00Z",
                      saved_at=None, readwise=_readwise(reader_id="overlap", saved_at="2024-05-01T00:00:00Z")),
            self.post("1900000000000000202", "SYNTHETIC title", ["readwise_tweet"], created_at=None,
                      text_is_title_only=True, readwise=_readwise()),
            self.post("1900000000000000203", "", ["readwise_tweet"], created_at=None, text_is_title_only=True,
                      readwise=_readwise(reader_id="empty")),
            self.post("1900000000000000204", "SYNTHETIC already sent", ["readwise_tweet"], created_at=None,
                      text_is_title_only=False, readwise=_readwise(reader_id="echo", source="Beacon")),
            self.post("1900000000000000205", "SYNTHETIC readwise post text about local-first sync", ["readwise_tweet"],
                      created_at=None, readwise=_readwise(reader_id="full", saved_at="2024-06-01T00:00:00Z")),
            self.post("1900000000000000206", "SYNTHETIC like", ["like"], fetched_at="2026-11-01T00:00:00Z"),
        ])
        db.init_db(self.database)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("INSERT INTO sync_state (source, last_id, last_sync, items_synced) VALUES ('x', 'keep-me', '2020-01-01T00:00:00+00:00', 4)")
            conn.commit()
        first = saves_handoff.import_saves(self.path, self.database)
        self.assertEqual(first["added"], 6)
        self.assertEqual(first["readwise_checkpoint"], "2024-06-01T00:00:00+00:00")
        self.assertFalse(first["live"])
        stored = {row["external_id"]: row for row in self.rows()}
        overlap = stored["1900000000000000201"]
        self.assertEqual(overlap["source"], "x")
        self.assertEqual(overlap["status"], "new")
        self.assertIsNone(overlap["saved_at"])
        raw = json.loads(overlap["raw_json"])
        self.assertEqual(raw["readwise"]["reader_saved_at"], "2024-05-01T00:00:00+00:00")
        self.assertIn("One save", raw["x_explanation"])
        title = stored["1900000000000000202"]
        self.assertEqual((title["source"], title["status"]), ("readwise", "pending"))
        self.assertIsNone(title["saved_at"])
        self.assertEqual(title["created_at"], "")
        self.assertEqual(json.loads(title["raw_json"])["readwise"]["reader_saved_at"], READER)
        self.assertEqual(stored["1900000000000000203"]["status"], "pending")
        self.assertEqual(stored["1900000000000000204"]["status"], "interest")
        self.assertIn("Not a new save", json.loads(stored["1900000000000000204"]["raw_json"])["x_explanation"])
        full = stored["1900000000000000205"]
        self.assertEqual((full["source"], full["status"]), ("readwise", "new"))
        self.assertIsNone(full["saved_at"])
        self.assertEqual(stored["1900000000000000206"]["source"], "x-like")
        with closing(sqlite3.connect(self.database)) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(conn.execute("SELECT last_id FROM sync_state WHERE source='x'").fetchone()["last_id"], "keep-me")
            unclassified = db.get_unclassified_items(limit=20)
        self.assertEqual(sorted(item["source"] for item in unclassified), ["readwise", "x"])
        second = saves_handoff.import_saves(self.path, self.database)
        self.assertEqual((second["added"], second["refreshed"]), (0, 6))
        self.assertEqual([row["id"] for row in self.rows()], [stored[key]["id"] for key in sorted(stored)])

    def test_title_then_full_text_promotes_the_same_row(self):
        post_id = "1900000000000000301"
        self.write([self.post(post_id, "SYNTHETIC title", ["readwise_tweet"], created_at=None,
                              text_is_title_only=True, readwise=_readwise(saved_at="2024-01-01T00:00:00Z"))])
        saves_handoff.import_saves(self.path, self.database)
        self.write([self.post(post_id, "SYNTHETIC full post text for the same id", ["readwise_tweet"], created_at=None,
                              readwise=_readwise(saved_at="2024-02-01T00:00:00Z"))])
        second = saves_handoff.import_saves(self.path, self.database)
        self.assertEqual(second["promoted"], 1)
        self.assertEqual(second["added"], 0)
        row = self.rows()[0]
        self.assertEqual(row["status"], "new")
        self.assertIn("full post text", row["body"])
        self.assertEqual(second["readwise_checkpoint"], "2024-02-01T00:00:00+00:00")

    def test_older_incremental_file_does_not_move_the_checkpoint_backward(self):
        post_id = "1900000000000000302"
        self.write([self.post(post_id, "SYNTHETIC newer history", ["readwise_tweet"], created_at=None,
                              readwise=_readwise(saved_at="2025-01-01T00:00:00Z"))])
        saves_handoff.import_saves(self.path, self.database)
        self.write([self.post("1900000000000000303", "SYNTHETIC older history", ["readwise_tweet"], created_at=None,
                              readwise=_readwise(reader_id="older", saved_at="2023-01-01T00:00:00Z"))])
        incremental = saves_handoff.import_saves(self.path, self.database)
        self.assertEqual(incremental["added"], 1)
        self.assertEqual(incremental["readwise_checkpoint"], "2025-01-01T00:00:00+00:00")
        self.assertEqual(len(self.rows()), 2)

    def test_bookmark_then_title_keeps_one_queued_save(self):
        post_id = "1900000000000000401"
        self.write([self.post(post_id, "SYNTHETIC bookmark body that is long enough", ["bookmark"],
                              fetched_at="2026-11-01T00:00:00Z")])
        saves_handoff.import_saves(self.path, self.database)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("UPDATE items SET status='queued'")
            conn.commit()
        self.write([self.post(post_id, "SYNTHETIC title", ["readwise_tweet"], created_at=None, text_is_title_only=True,
                              readwise=_readwise())])
        saves_handoff.import_saves(self.path, self.database)
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertEqual(row["source"], "x")
        self.assertEqual(row["status"], "queued")
        self.assertIn("long enough", row["body"])

    def test_conflicting_duplicate_rejects_before_the_database_opens(self):
        post_id = "1900000000000000501"
        with mock.patch.object(db, "init_db", wraps=db.init_db) as init_db:
            with self.assertRaises(ValueError):
                saves_handoff.parse_saves(self.write([
                    self.post(post_id, "SYNTHETIC one", ["bookmark"], fetched_at="2026-11-01T00:00:00Z"),
                    self.post(post_id, "SYNTHETIC two", ["bookmark"], fetched_at="2026-11-01T00:00:00Z"),
                ]))
        init_db.assert_not_called()

    def test_bird_resync_keeps_readwise_provenance(self):
        post_id = "1900000000000000601"
        self.write([self.post(post_id, "SYNTHETIC title", ["readwise_tweet"], created_at=None, text_is_title_only=True,
                              readwise=_readwise())])
        saves_handoff.import_saves(self.path, self.database)
        tweet = {"id": post_id, "text": "SYNTHETIC bird text", "author": {"username": "synthetic_user", "name": "Synthetic Name"},
                 "createdAt": "Sat Mar 01 18:00:00 +0000 2026"}
        self.assertTrue(x_handoff.record_bird_bookmark(tweet, "2026-11-02T00:00:00+00:00"))
        row = self.rows()[0]
        self.assertEqual(row["source"], "x")
        self.assertEqual(row["status"], "new")
        self.assertIn("bird text", row["body"])
        self.assertEqual(json.loads(row["raw_json"])["readwise"]["reader_id"], "reader-synthetic")

    def test_route_dry_run_skips_abstained_rows(self):
        self.write([
            self.post("1900000000000000701", "SYNTHETIC actionable save", ["bookmark"], fetched_at="2026-11-01T00:00:00Z"),
            self.post("1900000000000000702", "SYNTHETIC title", ["readwise_tweet"], created_at=None, text_is_title_only=True,
                      readwise=_readwise()),
        ])
        saves_handoff.import_saves(self.path, self.database)
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute("INSERT INTO workstreams (slug, name, backlog_path, active) VALUES ('synth', 'Synthetic', ?, 1)",
                         (str(self.root / "backlog.md"),))
            item_id = conn.execute("SELECT id FROM items WHERE source='x'").fetchone()[0]
            conn.execute(
                "INSERT INTO item_workstreams (item_id, workstream_id, confidence) "
                "SELECT ?, id, 0.9 FROM workstreams WHERE slug='synth'",
                (item_id,),
            )
            conn.execute("UPDATE items SET classified_at='2026-11-02T00:00:00+00:00', intent='reference' WHERE id=?", (item_id,))
            conn.commit()
        from lib.route import route_items
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            route_items(dry_run=True)
        text = stdout.getvalue()
        self.assertIn("SYNTHETIC actionable save", text)
        self.assertNotIn("SYNTHETIC title", text)
        self.assertFalse((self.root / "backlog.md").exists())
        self.assertEqual(self.rows()[1]["status"], "pending")


class SavesCliTests(unittest.TestCase):
    def test_cli_preview_and_rejected_live_sync(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "saves.jsonl"
        path.write_text(json.dumps({
            "post_id": "1900000000000000801",
            "url": "https://x.com/synthetic_user/status/1900000000000000801",
            "text": "SYNTHETIC cli title",
            "created_at": None,
            "source_types": ["readwise_tweet"],
            "text_is_title_only": True,
            "readwise": {"reader_id": "cli", "reader_saved_at": "2024-01-02T03:04:05+00:00", "reader_source": ""},
        }) + "\n", encoding="utf-8")
        checkout = root / "checkout"
        checkout.mkdir()
        source = Path(beacon.__file__).parent
        shutil.copyfile(source / "beacon.py", checkout / "beacon.py")
        shutil.copytree(source / "lib", checkout / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        (checkout / "config.json").write_text(json.dumps({"sources": {"x": {"method": "bird"}, "github": {"method": "gh_api"}}, "workstreams": []}))

        def run(*arguments):
            return subprocess.run([sys.executable, str(checkout / "beacon.py"), *arguments], cwd=checkout, text=True, capture_output=True)

        preview = run("import-saves", str(path), "--preview")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        payload = json.loads(preview.stdout)
        self.assertTrue(payload["preview"])
        self.assertFalse(payload["live"])
        self.assertNotIn("SYNTHETIC", preview.stdout)
        self.assertFalse((checkout / "beacon.db").exists())
        imported = run("import-saves", str(path))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(json.loads(imported.stdout)["added"], 1)
        rejected = run("sync", "--source", "readwise")
        self.assertEqual(rejected.returncode, 2)
        listed = run("list", "--source", "readwise")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("Title only from Readwise", listed.stdout)
