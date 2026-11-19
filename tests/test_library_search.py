"""Word search in the Library finds a save by what it shows, not only by the words its poster typed.

Design posts on X often say little ("this is so clean"), so the words Beacon wrote about a save (its
takeaway, its tags, its one-liner, a pile decision) are searched too, and words match their other
forms (drag, dragging, dragged).
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db

PHOTO = [{"type": "photo", "url": "https://pbs.twimg.com/media/p.jpg"}]


class LibrarySearchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        patcher = patch.object(db, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def save(self, item_id, text, raw=None, one_liner=None):
        raw = {"text": text, "media": PHOTO, **(raw or {})}
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,raw_json,one_liner,synced_at) "
                         "VALUES(?,'x',?,?,?,?,'@amy','adopted',?,?,'2026-10-01')",
                         (item_id, str(item_id), text, text, f"https://x.com/amy/status/{item_id}", json.dumps(raw), one_liner))

    def found(self, query):
        return [item["id"] for item in db.get_library(query=query)["items"]]

    def test_words_match_their_other_forms(self):
        self.save(1, "Scroll animation for a pricing page")
        self.save(2, "Dragging cards between columns feels great")
        self.assertEqual(self.found("scroll animations"), [1])
        self.assertEqual(self.found("animated"), [1])
        self.assertEqual(self.found("drag"), [2])
        self.assertEqual(self.found("card"), [2])

    def test_finds_a_save_by_its_takeaway_tags_one_liner_and_pile_decision(self):
        self.save(1, "this is so clean 🔥")
        self.save(2, "ok wow")
        self.save(3, "obsessed")
        self.save(4, "need this", one_liner="Empty state with an illustrated first step")
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO item_gists VALUES(1,'Drag to reorder a list, with spring physics','ui pattern','reference','r','2026-10-10')")
            conn.execute("INSERT INTO pile_decisions VALUES(2,'folio','reference','Date picker that makes choosing a range effortless','2026-10-10')")
        db.mark_classified(3, "reference", ["hover-effects", "cursor"], [], "2026-10-10")
        self.assertEqual(self.found("drag"), [1])
        self.assertEqual(self.found("reorder list"), [1])
        self.assertEqual(self.found("pickers"), [2])
        self.assertEqual(self.found("hover"), [3])
        self.assertEqual(self.found("empty state"), [4])
        self.assertEqual(self.found("wow"), [2])  # the poster's own words still count

    def test_finds_the_quoted_post(self):
        self.save(1, "this 👇", raw={"quotedTweet": {"text": "Command menu with fuzzy search and previews"}})
        self.assertEqual(self.found("command menu"), [1])

    def test_index_follows_later_changes(self):
        self.save(1, "so good")
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO item_gists VALUES(1,'Tooltip that follows the cursor','ui','reference','r','2026-10-10')")
        self.assertEqual(self.found("tooltip"), [1])
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT OR REPLACE INTO item_gists VALUES(1,'Toast that stacks','ui','reference','r','2026-10-11')")
        self.assertEqual((self.found("tooltip"), self.found("toast")), ([], [1]))
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM item_gists WHERE item_id=1")
        self.assertEqual(self.found("toast"), [])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE items SET body='Segmented control', title='Segmented control' WHERE id=1")
        self.assertEqual(self.found("segmented"), [1])
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM items WHERE id=1")
            self.assertEqual(conn.execute("SELECT count(*) FROM library_fts").fetchone()[0], 0)

    def test_a_save_with_unreadable_raw_json_is_still_kept_and_found(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,raw_json,synced_at) "
                         "VALUES(9,'github','9','owner/carousel','A carousel','https://github.com/owner/carousel','@o','new','not json','2026-10-01')")
        self.assertEqual(self.found("carousel"), [9])

    def test_an_existing_library_gets_its_index_built_once(self):
        self.save(1, "Bento grid layout")
        with sqlite3.connect(self.path) as conn:  # a database from before this index existed
            conn.execute("DROP TABLE library_fts")
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'library_fts_%'").fetchall():
                conn.execute(f"DROP TRIGGER {name}")
        with patch.object(db, "_LIBRARY_ROW", "SELECT id, title, body FROM unavailable_source"):
            with self.assertRaises(sqlite3.OperationalError):
                db.init_db()
        db.init_db()
        db.init_db()  # a second start must not add the save twice
        self.assertEqual(self.found("bento"), [1])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM library_fts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
