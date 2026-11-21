import csv
import io
import json
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

import beacon
from lib import db, mymind


class MymindImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "cards.csv"
        self.database = self.root / "isolated.db"

    def export(self, headers, rows, bom=False):
        with self.path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(rows)

    def rows(self):
        with closing(db.get_connection(self.database)) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]

    def test_bom_multiline_tags_dates_and_raw_fields(self):
        self.export(["Card ID", "Title", "Notes", "Source_URL", "Tags", "Created At", "Saved At", "Updated At", "File"],
                    [["card-1", "A bookmark", 'line one\n"line two"', "https://example.com", '["alpha", "a,b"]',
                      "2026-01-02T12:30:00+02:00", "2026-02-03", "2026-03-04T00:00:00Z", "files/local.png"]], bom=True)
        result = mymind.import_export(self.root, self.database)
        self.assertEqual(result["added"], 1)
        item = self.rows()[0]
        self.assertIsNone(item["body"], "a Notes column is the owner's note, not the post's text")
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT body FROM owner_notes").fetchone()[0], 'line one\n"line two"')
        self.assertEqual(item["created_at"], "2026-01-02T10:30:00+00:00")
        self.assertEqual(item["saved_at"], "2026-02-03T00:00:00+00:00")
        raw = json.loads(item["raw_json"])
        self.assertEqual(raw["record"]["File"], "files/local.png")
        self.assertEqual(raw["source_tags"], ["alpha", "a,b"])
        self.assertEqual(raw["updated_at"], "2026-03-04T00:00:00+00:00")
        with mock.patch.object(db, "DB_PATH", str(self.database)):
            self.assertEqual(set(db.get_item_tags(item["id"])), {"alpha", "a,b"})

    def test_repeated_and_edited_id_preserve_lifecycle_and_classifier_tags(self):
        self.export(["ID", "Content", "tags", "saved"], [["stable", "first", "source-old", "2026-01-02T00:00:00Z"]])
        mymind.import_export(self.path, self.database)
        item = self.rows()[0]
        with mock.patch.object(db, "DB_PATH", str(self.database)):
            db.mark_classified(item["id"], "reference", ["classifier"], [], "2026-01-05", one_liner="inferred")
            db.update_item_status(item["id"], "adopted", "keep")
        before = self.rows()[0]
        self.assertEqual(mymind.import_export(self.path, self.database)["added"], 0)
        self.export(["ID", "Content", "tags", "saved"], [["stable", "edited", "source-new", ""]])
        result = mymind.import_export(self.path, self.database)
        self.assertEqual(result["refreshed"], 1)
        refreshed = self.rows()[0]
        for field in ("id", "classified_at", "intent", "one_liner", "status", "status_at", "status_note", "saved_at"):
            self.assertEqual(refreshed[field], before[field])
        self.assertEqual(refreshed["body"], "edited")
        with mock.patch.object(db, "DB_PATH", str(self.database)):
            self.assertEqual(set(db.get_item_tags(item["id"])), {"classifier", "source-old", "source-new"})
        self.assertEqual(json.loads(refreshed["raw_json"])["source_tags"], ["source-new"])

    def test_fallback_identity_survives_reorder_and_move_and_notes_have_no_fake_dates(self):
        headers = ["title", "content", "url", "saved at"]
        rows = [["Link", "Text", "https://example.com", "2026-01-01T00:00:00Z"], ["", "local note", "", ""]]
        self.export(headers, rows)
        mymind.import_export(self.path, self.database)
        identities = {item["external_id"] for item in self.rows()}
        rows[0][1] = "changed content with URL/save identity"
        self.export(headers, list(reversed(rows)))
        moved = self.root / "moved.csv"
        self.path.rename(moved)
        self.assertEqual(mymind.import_export(moved, self.database)["added"], 0)
        self.assertEqual({item["external_id"] for item in self.rows()}, identities)
        note = next(item for item in self.rows() if not item["url"])
        self.assertIsNone(note["created_at"])
        self.assertIsNone(note["saved_at"])

    def test_an_export_never_replaces_a_card_the_mymind_sync_wrote(self):
        from lib import owner_notes
        card = {"id": "abc", "title": "Live title", "summary": "drawer joinery", "notes": [], "tags": [],
                "mainEntity": {"@type": "WebPage"}, "entityType": "WebPage", "created": "2026-11-01T00:00:00Z"}
        owner_notes.sync_mymind("/cli", db_path=str(self.database), run=lambda command, timeout: json.dumps(card))
        self.export(["id", "title", "note", "tags"], [["abc", "Old export title", "from the export", "csv-tag"]])
        mymind.import_export(self.path, self.database)
        row = self.rows()[0]
        raw = json.loads(row["raw_json"])
        self.assertEqual((row["title"], raw["import_format"], raw["listed"]), ("Live title", "mymind-cli-v1", 1))
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT body FROM owner_notes")], ["from the export"],
                             "the export's note still lands on the card")

    def test_invalid_exports_never_open_db(self):
        cases = [
            (["content", "saved"], [["good", ""], ["bad", "yesterday"]]),
            (["content", "saved"], [["bad", "2026-01-01T10:00:00"]]),
            (["id", "card id", "content"], [["a", "b", "c"]]),
            (["ID", "content"], [["same", "one"], ["same", "two"]]),
            (["content", "tags"], [["text", '[42]']]),
            (["content"], [["one", "extra"]]),
            (["content"], [[""]]),
            (["content", "author"], [["", "Writer"]]),
            (["Content", "content"], [["a", "b"]]),
        ]
        for headers, rows in cases:
            with self.subTest(headers=headers, rows=rows):
                self.export(headers, rows)
                with mock.patch.object(db, "init_db") as initialize:
                    with self.assertRaises(ValueError):
                        mymind.import_export(self.path, self.database)
                    initialize.assert_not_called()
                self.assertFalse(self.database.exists())
        self.path.write_text('content\n"unterminated', encoding="utf-8")
        with self.assertRaises(ValueError):
            mymind.import_export(self.path, self.database)
        self.assertFalse(self.database.exists())

    def test_transaction_failure_rolls_back_items_and_tags(self):
        db.init_db(self.database)
        with closing(db.get_connection(self.database)) as conn, conn:
            conn.execute("CREATE TRIGGER refuse_second BEFORE INSERT ON items WHEN NEW.external_id='bad' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        self.export(["ID", "content", "tags"], [["good", "one", "tag"], ["bad", "two", "tag"]])
        with self.assertRaises(sqlite3.IntegrityError):
            mymind.import_export(self.path, self.database)
        self.assertEqual(self.rows(), [])
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 0)

    def test_complementary_body_columns_and_author(self):
        self.export(["id", "Description", "Notes", "Highlight", "Creator", "Date"],
                    [["one", " description ", "  note\n", "highlight", "Writer", "2026-01-01"]])
        mymind.import_export(self.path, self.database)
        item = self.rows()[0]
        self.assertEqual(item["body"], "highlight\n\n description ")
        self.assertEqual(item["author"], "Writer")
        with closing(db.get_connection(self.database)) as conn:
            notes = [tuple(row) for row in conn.execute("SELECT item_id, kind, origin, body FROM owner_notes")]
        self.assertEqual(notes, [(item["id"], "note", "owner", "note")])
        self.assertEqual(item["saved_at"], "2026-01-01T00:00:00+00:00")
        self.export(["id", "content"], [["one", "edited"]])
        mymind.import_export(self.path, self.database)
        self.assertEqual(self.rows()[0]["author"], "Writer")
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM owner_notes").fetchone()[0], 0, "a removed note goes")

    def test_cli_preview_and_import_skip_config_seed_and_pipeline(self):
        self.export(["ID", "Content"], [["private-id", "PRIVATE CONTENT"]])
        with mock.patch.object(beacon, "load_config", side_effect=AssertionError("config access")), \
             mock.patch.object(db, "seed_workstreams", side_effect=AssertionError("seed access")):
            output = io.StringIO()
            with mock.patch("sys.argv", ["beacon", "import-mymind", str(self.path), "--db", str(self.database), "--preview"]), redirect_stdout(output):
                beacon.main()
            self.assertTrue(json.loads(output.getvalue())["preview"])
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertNotIn("private-id", output.getvalue())
            self.assertFalse(self.database.exists())
            with mock.patch("sys.argv", ["beacon", "import-mymind", str(self.path), "--db", str(self.database)]), redirect_stdout(io.StringIO()):
                beacon.main()
        self.assertEqual(len(self.rows()), 1)
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], 0)

    def test_cli_import_reimport_list_and_search(self):
        self.export(["ID", "Title", "Content", "URL"],
                    [["synthetic", "Synthetic compass", "Find the compass reference", "https://example.com/compass"],
                     ["note", "", "Compass note without title or URL", ""]])
        checkout = self.root / "checkout"
        checkout.mkdir()
        source = Path(beacon.__file__).parent
        shutil.copyfile(source / "beacon.py", checkout / "beacon.py")
        shutil.copytree(source / "lib", checkout / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        (checkout / "config.json").write_text(json.dumps({"sources": {"x": {}}, "workstreams": []}))
        def run(*arguments):
            result = subprocess.run([sys.executable, str(checkout / "beacon.py"), *arguments],
                                    cwd=checkout, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout
        self.assertIn("import-mymind", run("--help"))
        self.assertTrue(json.loads(run("import-mymind", str(self.path), "--preview"))["preview"])
        self.assertFalse((checkout / "beacon.db").exists())
        self.assertEqual(json.loads(run("import-mymind", str(self.path)))["added"], 2)
        self.assertEqual(json.loads(run("import-mymind", str(self.path)))["added"], 0)
        listed = run("list", "--source", "mymind")
        self.assertIn("Synthetic compass", listed)
        self.assertIn("Compass note without title or URL", listed)
        self.assertIn("Synthetic compass", run("search", "compass"))
        self.assertIn("Compass note without title or URL", run("search", "without"))
        rejected = subprocess.run([sys.executable, str(checkout / "beacon.py"), "sync", "--source", "mymind"],
                                  cwd=checkout, text=True, capture_output=True)
        self.assertEqual(rejected.returncode, 2)
        with closing(db.get_connection(checkout / "beacon.db")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
