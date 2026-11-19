"""The triage queue the Beacon Triage app reads."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        patcher = patch.object(db, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([
            {"slug": "agents", "name": "Agent Workflows", "backlog_path": "a.md"},
            {"slug": "site", "name": "Personal Site", "backlog_path": "s.md"},
        ])
        with sqlite3.connect(self.path) as conn:
            for item_id, status, fits in [(1, "queued", {"agents": 0.6}), (2, "queued", {"agents": 0.9, "site": 0.7}),
                                          (3, "rejected", {"agents": 0.99}), (4, "reviewed", {"site": 0.8})]:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,created_at,status,intent,synced_at) "
                             "VALUES(?,'x',?,?,?,?,'@a','2026-09-01T00:00:00+00:00',?,'reference','2026-09-29')",
                             (item_id, str(item_id), f"Title {item_id}", f"Body {item_id}", f"https://x.com/a/{item_id}", status))
                for slug, fit in fits.items():
                    conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) "
                                 "SELECT ?,id,?,1 FROM workstreams WHERE slug=?", (item_id, fit, slug))

    def test_queue_lists_untriaged_items_best_match_first_with_projects(self):
        queue = db.get_triage_queue()
        self.assertEqual([item["id"] for item in queue], [2, 1])
        self.assertEqual(queue[0]["projects"], [{"slug": "agents", "name": "Agent Workflows", "fit": 0.9, "kind": "project"},
                                                {"slug": "site", "name": "Personal Site", "fit": 0.7, "kind": "project"}])
        self.assertEqual(queue[0]["body"], "Body 2")
        self.assertEqual(queue[0]["url"], "https://x.com/a/2")

    def test_an_item_whose_source_changed_leaves_the_queue_until_its_review_is_approved(self):
        with sqlite3.connect(self.path) as conn:
            for item_id in (1, 2):
                conn.execute("INSERT INTO intake_reviews VALUES(?,?,?,?,?)",
                             (item_id, "hash", "approved", json.dumps({"assessment": "approved", "reasons": []}), "2026-09-29"))
            conn.execute("UPDATE items SET classified_at='2026-09-29' WHERE id IN (1,2)")
            conn.execute("UPDATE items SET body='Edited upstream' WHERE id=2")
            held = conn.execute("SELECT disposition FROM intake_reviews WHERE item_id=2").fetchone()[0]
        self.assertEqual(held, "review")
        self.assertEqual([item["id"] for item in db.get_triage_queue()], [1])

        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE intake_reviews SET disposition='approved' WHERE item_id=2")
        self.assertEqual([item["id"] for item in db.get_triage_queue()], [2, 1])

    def test_a_suggestion_written_for_old_content_is_dropped_when_the_source_changes(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO item_gists VALUES(2,'About the old text','post','skip','Old reason','2026-09-29')")
            conn.execute("UPDATE items SET synced_at='2026-10-01', raw_json='{}' WHERE id=2")  # same text: kept
            kept = conn.execute("SELECT COUNT(*) FROM item_gists WHERE item_id=2").fetchone()[0]
            conn.execute("UPDATE items SET url='https://x.com/a/other' WHERE id=2")
            dropped = conn.execute("SELECT COUNT(*) FROM item_gists WHERE item_id=2").fetchone()[0]
        self.assertEqual((kept, dropped), (1, 0))


class PreviewTest(unittest.TestCase):
    def test_x_preview_keeps_media_quote_and_counts_and_drops_media_link(self):
        raw = {"text": "Look at this https://t.co/abc", "createdAt": "Tue Sep 29 13:40:09 +0000 2026", "likeCount": 6414,
               "author": {"username": "DKThomp", "name": "Derek Thompson"},
               "media": [{"type": "photo", "url": "https://pbs/x.png", "previewUrl": "https://pbs/x.png:small", "width": 655, "height": 543}],
               "quotedTweet": {"text": "Original", "author": {"username": "orig", "name": "Orig Name"}}}
        preview = db.item_preview("x", json.dumps(raw), "Look at this https://t.co/abc")
        self.assertEqual(preview["author_name"], "Derek Thompson")
        self.assertEqual(preview["likes"], 6414)
        self.assertEqual(preview["media"], [{"type": "photo", "url": "https://pbs/x.png", "preview_url": "https://pbs/x.png:small", "width": 655, "height": 543}])
        self.assertEqual(preview["quoted"], {"author": "@orig", "name": "Orig Name", "text": "Original"})
        self.assertEqual(preview["text"], "Look at this")
        self.assertEqual(preview["date"], "2026-09-29T13:40:09+00:00")

    def test_x_preview_keeps_a_lone_link_for_link_cards(self):
        raw = {"text": "Read https://t.co/zzz", "createdAt": "2026-09-01T00:00:00+00:00", "author": {"username": "a", "name": "A"}}
        preview = db.item_preview("x", json.dumps(raw), "Read https://t.co/zzz")
        self.assertEqual(preview["text"], "Read https://t.co/zzz")
        self.assertEqual(preview["link"], "https://t.co/zzz")

    def test_github_preview(self):
        raw = {"full_name": "0xSero/ai-data-extraction", "description": "extract data", "language": "Python",
               "stargazers_count": 1287, "topics": ["ai"], "starred_at": "2026-08-15T00:28:56Z"}
        preview = db.item_preview("github", json.dumps(raw), "extract data")
        self.assertEqual(preview["repo"], {"name": "0xSero/ai-data-extraction", "language": "Python", "stars": 1287, "topics": ["ai"]})
        self.assertEqual(preview["date"], "2026-08-15T00:28:56Z")

    def test_html_entities_are_decoded(self):
        raw = {"text": "Slack -&gt; PRs &amp; more", "author": {"username": "a"},
               "quotedTweet": {"text": "a &lt; b", "author": {"username": "q"}}}
        preview = db.item_preview("x", json.dumps(raw), "Slack -&gt; PRs &amp; more")
        self.assertEqual(preview["text"], "Slack -> PRs & more")
        self.assertEqual(preview["quoted"]["text"], "a < b")

    def test_bad_raw_json_falls_back_to_body(self):
        self.assertEqual(db.item_preview("wiki", "not json", "Body")["text"], "Body")


if __name__ == "__main__":
    unittest.main()
