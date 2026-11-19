"""A backlog can live inside a hand-written roadmap: Beacon rewrites only the marked block."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, route

ROADMAP = """# Roadmap

Hand-written plans stay.

<!-- beacon:backlog:start -->
old generated text
<!-- beacon:backlog:end -->

## Idea bank

Also kept.
"""


class RoadmapTest(unittest.TestCase):
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
        self.roadmap = Path(self.temp.name) / "docs" / "ROADMAP.md"
        self.roadmap.parent.mkdir()
        self.roadmap.write_text(ROADMAP)
        db.seed_workstreams([{"slug": "beacon", "name": "Beacon", "backlog_path": str(self.roadmap)}])
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, title, status in [(1, "Bird every minute", "queued"), (2, "An old idea", "stale")]:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,intent,status,status_at,synced_at) "
                             "VALUES(?,'x',?,?,?,?, 'reference', ?, '2026-10-12', '2026-10-12')",
                             (item_id, str(item_id), title, title, f"https://x.com/a/{item_id}", status))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence) "
                             "SELECT ?, id, 0.9 FROM workstreams WHERE slug='beacon'", (item_id,))

    def test_only_the_marked_block_is_rewritten(self):
        self.assertEqual(route.regenerate_backlog("beacon"), 1)
        text = self.roadmap.read_text()
        self.assertTrue(text.startswith("# Roadmap\n\nHand-written plans stay.\n"))
        self.assertIn("Bird every minute", text)
        self.assertNotIn("old generated text", text)
        self.assertTrue(text.endswith("## Idea bank\n\nAlso kept.\n"))
        self.assertIn('Keep "Bird every minute" as a Beacon reference.', text)
        self.assertIn("Do not open a task.", text)
        self.assertIn("Recommendation only. Nothing is changed, opened, or sent.", text)
        self.assertNotIn("Review against", text)

    def test_the_archive_goes_beside_the_roadmap_not_over_it(self):
        route.regenerate_archive("beacon")
        self.assertIn("An old idea", (self.roadmap.parent / "beacon-backlog-archive.md").read_text())
        self.assertEqual(self.roadmap.read_text(), ROADMAP)


if __name__ == "__main__":
    unittest.main()
