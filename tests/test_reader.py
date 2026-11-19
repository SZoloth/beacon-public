"""Read: save an item to Readwise Reader, then mark it adopted with a "read" note."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, reader


class FakeResponse:
    def __init__(self, status, body):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        live = patch.object(db, "is_live", return_value=True)
        live.start()
        self.addCleanup(live.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "proj", "name": "Proj", "backlog_path": str(Path(self.temp.name) / "b.md")}])
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) "
                         "VALUES(1,'x','1','Post','Body','https://x.com/a/status/1','queued','2026-09-30')")
            conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT 1,id,0.8,1 FROM workstreams")

    def test_read_saves_to_reader_with_project_tag_then_marks_adopted(self):
        sent = []
        def fake_open(request, timeout):
            sent.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)))
            return FakeResponse(201, {"id": "doc1", "url": "https://read.readwise.io/new/read/doc1"})
        with patch.object(reader, "_token", return_value="tok"), patch.object(reader, "urlopen", side_effect=fake_open):
            result = reader.read_item(1)
        url, auth, payload = sent[0]
        self.assertEqual(url, "https://readwise.io/api/v3/save/")
        self.assertEqual(auth, "Token tok")
        self.assertEqual(payload["url"], "https://x.com/a/status/1")
        self.assertEqual(payload["tags"], ["beacon", "proj"])
        self.assertEqual(result["reader_url"], "https://read.readwise.io/new/read/doc1")
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT status, status_note FROM items WHERE id=1").fetchone(), ("adopted", "read"))

    def test_failed_save_leaves_the_item_untouched(self):
        with patch.object(reader, "_token", return_value="tok"), \
             patch.object(reader, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(RuntimeError):
                reader.read_item(1)
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT status FROM items WHERE id=1").fetchone()[0], "queued")

    def test_missing_token_is_an_explicit_error(self):
        with patch.object(reader, "_token", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "READWISE_ACCESS_TOKEN"):
                reader.read_item(1)


if __name__ == "__main__":
    unittest.main()
