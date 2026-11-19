import gc
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib import autopilot, db, reader, route


class FakeResponse:
    def read(self, *_):
        return b'{"url": "https://read.readwise.io/new/read/doc1"}'

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class LiveOnlyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch.object(db, "DB_PATH", str(self.root / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backlog = self.root / "docs" / "beacon-backlog.md"
        self.archive = self.root / "docs" / "beacon-backlog-archive.md"
        self.inbox = self.root / "reading-inbox.md"
        self.backlog.parent.mkdir()
        for path in (self.backlog, self.archive, self.inbox):
            path.write_text("hand-written\n")
        db.init_db()
        db.seed_workstreams([{"slug": "proj", "name": "Proj", "backlog_path": str(self.backlog)}])
        # closing(): `with sqlite3.connect()` alone commits but leaves the connection open, and its
        # ResourceWarning lands in the stderr a test below compares.
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            for item_id, status, human_read in ((1, "queued", 0), (2, "stale", 0), (3, "queued", 1)):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,human_read,classified_at,synced_at) "
                             "VALUES(?,'x',?,?,'Body',?,?,?,'2026-10-10','2026-10-10')",
                             (item_id, str(item_id), f"Item {item_id}", f"https://x.com/a/{item_id}", status, human_read))
            conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) "
                         "SELECT i, id, 0.8, 0 FROM workstreams, (SELECT 1 AS i UNION SELECT 2)")
            conn.execute("INSERT INTO item_gists VALUES(1,'About 1','post','read','Because',  '2026-10-10')")

    def quiet(self, call, *args):
        # Earlier tests leave unclosed connections; collect them now so their ResourceWarnings aren't read as this call's errors.
        gc.collect()
        errors = io.StringIO()
        with redirect_stderr(errors), redirect_stdout(io.StringIO()):
            result = call(*args)
        return result, errors.getvalue()

    def skipped_line(self):
        return f"skipped: not the live database ({os.path.realpath(db.DB_PATH)})\n"

    def test_only_the_canonical_database_is_live(self):
        live = self.root / "checkout" / "beacon.db"
        live.parent.mkdir()
        (self.root / "link.db").symlink_to(live)
        with patch.object(db, "LIVE_DB_PATH", str(live)):
            for path, expected in ((live, True), (self.root / "link.db", True),
                                   (live.parent / ".claude" / "worktrees" / "agent" / "beacon.db", False),
                                   (self.root / "copy" / "beacon.db", False)):
                with patch.object(db, "DB_PATH", str(path)):
                    self.assertIs(db.is_live(), expected, path)

    def test_a_copy_leaves_the_backlog_file_untouched(self):
        count, errors = self.quiet(route.regenerate_backlog, "proj")
        self.assertEqual((self.backlog.read_text(), errors), ("hand-written\n", self.skipped_line()))
        self.assertEqual(count, 1)

    def test_a_copy_leaves_the_archive_file_untouched(self):
        _, errors = self.quiet(route.regenerate_archive, "proj")
        self.assertEqual((self.archive.read_text(), errors), ("hand-written\n", self.skipped_line()))

    def test_regenerating_every_backlog_on_a_copy_says_so_once(self):
        _, errors = self.quiet(route.regenerate_all_backlogs)
        self.assertEqual(errors, self.skipped_line())
        self.assertEqual([p.read_text() for p in (self.backlog, self.archive)], ["hand-written\n"] * 2)

    def test_routing_on_a_copy_writes_neither_backlog_nor_reading_inbox(self):
        with patch.object(route, "READING_INBOX", str(self.inbox)):
            routed, errors = self.quiet(route.route_items)
        self.assertEqual(routed, 1)
        self.assertEqual([p.read_text() for p in (self.backlog, self.inbox)], ["hand-written\n"] * 2)
        self.assertIn(self.skipped_line(), errors)

    def test_the_live_database_still_writes_its_files(self):
        with patch.object(db, "is_live", return_value=True), patch.object(route, "READING_INBOX", str(self.inbox)):
            _, errors = self.quiet(route.route_items)
            self.quiet(route.regenerate_archive, "proj")
        self.assertEqual(errors, "")
        self.assertIn("### [Item 1](https://x.com/a/1)", self.backlog.read_text())
        self.assertIn("Item 3", self.inbox.read_text())
        self.assertIn("Item 2", self.archive.read_text())

    def test_a_copy_sends_nothing_to_reader(self):
        with patch.object(reader, "urlopen", side_effect=AssertionError("request sent")) as urlopen, \
             patch.object(reader, "_token", return_value="tok"):
            result, errors = self.quiet(reader.read_item, 1)
        urlopen.assert_not_called()
        self.assertEqual((result["reader_url"], errors), (None, self.skipped_line()))

    def test_a_copy_needs_no_reader_token(self):
        with patch.object(reader, "_token", return_value=None):
            result, _ = self.quiet(reader.read_item, 1)
        self.assertEqual(result["item_id"], 1)

    def test_autopilot_on_a_copy_sends_no_reads(self):
        with patch.object(reader, "urlopen", side_effect=AssertionError("request sent")) as urlopen, \
             patch.object(reader, "_token", return_value="tok"):
            result, _ = self.quiet(lambda: autopilot.run())
        urlopen.assert_not_called()
        self.assertEqual(result["read"], 1)

    def test_the_live_database_sends_to_reader(self):
        with patch.object(db, "is_live", return_value=True), patch.object(reader, "_token", return_value="tok"), \
             patch.object(reader, "urlopen", return_value=FakeResponse()) as urlopen:
            result, errors = self.quiet(reader.read_item, 1)
        urlopen.assert_called_once()
        self.assertEqual((result["reader_url"], errors), ("https://read.readwise.io/new/read/doc1", ""))


if __name__ == "__main__":
    unittest.main()
