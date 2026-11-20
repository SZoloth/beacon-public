"""Read: save an item to Readwise Reader, then mark it adopted with a "read" note."""
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

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
        with patch.object(reader, "_token", return_value=None), patch.object(reader, "_cli", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "READWISE_ACCESS_TOKEN"):
                reader.read_item(1)

    def test_without_a_working_token_the_signed_in_readwise_cli_saves_it(self):
        ran = []

        def fake_run(command, **_):
            ran.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps({"id": "doc2", "url": "https://read.readwise.io/read/doc2"}), "")
        rejected = HTTPError("https://readwise.io/api/v3/save/", 401, "Unauthorized", {}, None)
        for token, opened in ((None, None), ("stale", rejected)):
            ran.clear()
            with patch.object(reader, "_token", return_value=token), patch.object(reader, "_cli", return_value="/bin/readwise"), \
                 patch.object(reader, "urlopen", side_effect=opened), patch.object(reader.subprocess, "run", side_effect=fake_run):
                result = reader._save("https://x.com/a/status/1", ["beacon", "proj"])
            self.assertEqual(result["url"], "https://read.readwise.io/read/doc2")
            self.assertEqual(ran, [["/bin/readwise", "--json", "reader-create-document", "--url", "https://x.com/a/status/1",
                                    "--tags", '["beacon", "proj"]']])


class KeychainTokenTest(unittest.TestCase):
    """launchd and the app don't load shell files, so the token comes from Keychain through with-secret."""

    def fake_with_secret(self, folder, body):
        script = Path(folder) / "with-secret"
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        return script

    def test_token_comes_from_keychain_when_the_environment_has_none(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = self.fake_with_secret(folder, 'shift 2\nREADWISE_ACCESS_TOKEN=from-keychain exec "$@"\n')
            with patch.dict(os.environ, {}, clear=False), patch.object(reader, "WITH_SECRET", fake), \
                    patch.object(reader, "SECRETS", Path(folder) / "none.zsh"):
                os.environ.pop("READWISE_ACCESS_TOKEN", None)
                self.assertEqual(reader._token(), "from-keychain")

    def test_a_failed_keychain_lookup_gives_no_token(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = self.fake_with_secret(folder, 'echo partial; exit 1\n')
            with patch.dict(os.environ, {}, clear=False), patch.object(reader, "WITH_SECRET", fake), \
                    patch.object(reader, "SECRETS", Path(folder) / "none.zsh"):
                os.environ.pop("READWISE_ACCESS_TOKEN", None)
                self.assertIsNone(reader._token())


if __name__ == "__main__":
    unittest.main()
