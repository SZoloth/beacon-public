import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import db, readwise_sync


def _doc(doc_id, url, title, html_content="", source="", updated="2024-06-01T00:00:00+00:00", saved="2024-06-01T00:00:00+00:00"):
    return {
        "id": doc_id,
        "source_url": url,
        "title": title,
        "html_content": html_content,
        "source": source,
        "category": "tweet",
        "saved_at": saved,
        "created_at": saved,
        "updated_at": updated,
        "parent_id": None,
    }


class ReadwiseSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "isolated.db"
        self.original = db.DB_PATH
        db.DB_PATH = str(self.database)
        self.addCleanup(setattr, db, "DB_PATH", self.original)
        db.init_db(str(self.database))

    def test_missing_token_imports_a_dropped_file_and_does_not_call_reader(self):
        folder = self.root / "handoffs"
        folder.mkdir()
        (folder / "history.jsonl").write_text(json.dumps({
            "post_id": "1900000000000000901",
            "url": "https://x.com/synthetic_user/status/1900000000000000901",
            "text": "SYNTHETIC dropped title",
            "created_at": None,
            "source_types": ["readwise_tweet"],
            "text_is_title_only": True,
            "readwise": {"reader_id": "dropped", "reader_saved_at": "2024-01-01T00:00:00Z", "reader_source": ""},
        }) + "\n", encoding="utf-8")
        opener = mock.Mock(side_effect=AssertionError("Reader was called"))
        with self.assertRaises(RuntimeError) as raised:
            readwise_sync.fetch_readwise(count=10, handoff_dir=str(folder), db_path=str(self.database), token="", opener=opener)
        self.assertIn("Auth was not tested", str(raised.exception))
        opener.assert_not_called()
        conn = db.get_connection(str(self.database))
        try:
            row = conn.execute("SELECT status FROM items WHERE external_id='1900000000000000901'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "pending")

    def test_list_pages_checkpoint_and_hydration_promotes_pending(self):
        calls = []

        def opener(url, token):
            self.assertEqual(token, "synthetic-token")
            calls.append(url)
            if "pageCursor=page-2" in url:
                return {"results": [_doc("full", "https://x.com/synthetic_user/status/1900000000000000902",
                                          "Short title", "<p>SYNTHETIC full reader body that is longer than the title</p>",
                                          updated="2024-07-01T00:00:00+00:00")], "nextPageCursor": None}
            if "id=title-id" in url:
                return {"results": [_doc("title-id", "https://x.com/synthetic_user/status/1900000000000000903",
                                          "Title", "<p>SYNTHETIC hydrated body that is longer than the title</p>")]}
            return {"results": [
                _doc("title-id", "https://x.com/synthetic_user/status/1900000000000000903", "Title only synthetic"),
                _doc("echo-id", "https://x.com/synthetic_user/status/1900000000000000904", "Already sent", source="Beacon"),
                {"id": "article", "source_url": "https://example.com/not-a-status", "title": "Skip", "category": "tweet", "parent_id": None},
            ], "nextPageCursor": "page-2"}

        result = readwise_sync.fetch_readwise(count=10, db_path=str(self.database), token="synthetic-token", opener=opener)
        self.assertTrue(result["auth_tested"])
        self.assertTrue(result["list_ok"])
        self.assertGreaterEqual(result["hydrated"], 1)
        conn = db.get_connection(str(self.database))
        try:
            rows = {row["external_id"]: row["status"] for row in conn.execute("SELECT external_id, status FROM items")}
            state = json.loads(conn.execute("SELECT last_id FROM sync_state WHERE source='readwise-api'").fetchone()["last_id"])
        finally:
            conn.close()
        self.assertEqual(rows["1900000000000000902"], "new")
        self.assertEqual(rows["1900000000000000903"], "new")
        self.assertEqual(rows["1900000000000000904"], "interest")
        self.assertNotIn("article", rows)
        self.assertEqual(state["page_cursor"], "")
        self.assertTrue(state["list_ok"])
        self.assertGreaterEqual(len(calls), 2)

    def test_a_failed_second_page_keeps_the_cursor(self):
        def opener(url, token):
            if "pageCursor=next" in url:
                raise RuntimeError("Readwise Reader list failed: timed out")
            return {"results": [_doc("one", "https://x.com/synthetic_user/status/1900000000000000905", "Title only synthetic")],
                    "nextPageCursor": "next"}

        with self.assertRaises(RuntimeError):
            readwise_sync.fetch_readwise(count=10, db_path=str(self.database), token="synthetic-token", opener=opener)
        conn = db.get_connection(str(self.database))
        try:
            state = json.loads(conn.execute("SELECT last_id FROM sync_state WHERE source='readwise-api'").fetchone()["last_id"])
            status = conn.execute("SELECT status FROM items WHERE external_id='1900000000000000905'").fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(state["page_cursor"], "next")
        self.assertEqual(status, "pending")


class WeeklyOrchestratorTests(unittest.TestCase):
    def test_weekly_script_syncs_every_enabled_source_once(self):
        text = Path("scripts/weekly.sh").read_text()
        self.assertEqual(text.count("python3 beacon.py sync"), 1)
        self.assertNotIn("sync --source", text)
        self.assertIn("sync_ok", text)

    @mock.patch("beacon.fetch_readwise")
    @mock.patch("beacon.fetch_last30days")
    @mock.patch("beacon.fetch_markdown_directory")
    @mock.patch("beacon.fetch_keep_local")
    @mock.patch("beacon.fetch_github_stars")
    @mock.patch("beacon.fetch_x_bookmarks")
    @mock.patch("beacon.load_config")
    def test_sync_includes_readwise_when_another_source_fails(
        self, mock_load_config, mock_x, mock_github, mock_keep, mock_markdown, mock_last, mock_readwise,
    ):
        import beacon
        from types import SimpleNamespace
        mock_load_config.return_value = {"sources": {
            "x": {}, "github": {}, "keep-local": {}, "wiki": {}, "omnisearch": {}, "last30days": {}, "readwise": {"default_count": 20},
        }}
        mock_x.side_effect = RuntimeError("bird signed out")
        with self.assertRaises(SystemExit) as raised:
            beacon.cmd_sync(SimpleNamespace(source=None))
        self.assertEqual(raised.exception.code, 1)
        mock_x.assert_called_once()
        mock_github.assert_called_once()
        mock_keep.assert_called_once()
        self.assertEqual(mock_markdown.call_count, 2)
        mock_last.assert_called_once()
        mock_readwise.assert_called_once()
