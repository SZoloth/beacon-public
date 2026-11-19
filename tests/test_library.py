"""The library feed Mosaic reads: every visual save, newest first, in pages."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, server, vectors


def photo(name):
    return {"type": "photo", "url": f"https://pbs.twimg.com/media/{name}.jpg", "previewUrl": f"https://pbs.twimg.com/media/{name}.jpg:small"}


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        patcher = patch.object(db, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        rows = [
            (1, "x", "queued", {"text": "a", "media": [photo("one")], "author": {"username": "amy", "name": "Amy"},
                                "createdAt": "Tue Sep 29 18:38:50 +0000 2026"}),
            (2, "x", "adopted", {"text": "no picture"}),
            (3, "github", "new", {"full_name": "owner/repo", "language": "Swift", "topics": ["ui"], "starred_at": "2026-09-01T00:00:00Z"}),
            (4, "x", "rejected", {"text": "rejected", "media": [photo("four")]}),
            (5, "x", "stale", {"text": "e", "media": [photo("five"), {"type": "video", "url": "https://video.twimg.com/v.mp4", "previewUrl": "https://pbs.twimg.com/thumb.jpg"}]}),
            (6, "wiki", "queued", {}),
            (7, "x", "queued", None),
        ]
        with sqlite3.connect(self.path) as conn:
            for item_id, source, status, raw in rows:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,raw_json,synced_at) "
                             "VALUES(?,?,?,?,?,?,'@amy',?,?,'2026-10-01')",
                             (item_id, source, str(item_id), f"Title {item_id}", (raw or {}).get("text", f"Body {item_id}"),
                              f"https://example.com/{item_id}", status, json.dumps(raw) if raw is not None else "not json"))

    def test_lists_pictures_and_stars_newest_first_and_skips_rejected_and_text_only(self):
        page = db.get_library()
        self.assertEqual([item["id"] for item in page["items"]], [5, 3, 1])
        self.assertEqual((page["total"], page["next_offset"]), (3, None))
        five, star, one = page["items"]
        # A video comes in as its poster image; Mosaic stores pictures.
        self.assertEqual([m["url"] for m in five["media"]], ["https://pbs.twimg.com/media/five.jpg", "https://pbs.twimg.com/thumb.jpg"])
        self.assertEqual(star["repo"]["name"], "owner/repo")
        self.assertEqual((one["author_name"], one["author"], one["text"]), ("Amy", "@amy", "a"))
        self.assertEqual(one["date"], "2026-09-29T18:38:50+00:00")

    def test_pages_carry_an_offset_to_the_next_page(self):
        first = db.get_library(offset=0, limit=2)
        self.assertEqual(([i["id"] for i in first["items"]], first["next_offset"]), ([5, 3], 2))
        second = db.get_library(offset=2, limit=2)
        self.assertEqual(([i["id"] for i in second["items"]], second["next_offset"]), ([1], None))

    def test_saves_carry_their_area_and_filter_by_it(self):
        with sqlite3.connect(self.path) as conn:
            conn.executemany("INSERT INTO area_reviews VALUES(?,?,?,'2026-10-09')",
                             [(5, "style", 0.9), (1, "food", 0.4), (3, "none", 0.95)])
        page = db.get_library()
        # Below the 0.6 floor or "none" means no shelf, as in triage.
        self.assertEqual({item["id"]: item["area"] for item in page["items"]}, {5: "style", 3: None, 1: None})
        style = db.get_library(area="style")
        self.assertEqual(([item["id"] for item in style["items"]], style["total"]), ([5], 1))

    def test_word_search_keeps_matching_saves_only(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE items SET body='a tweed blazer' WHERE id=5")
        page = db.get_library(query="blazer")
        self.assertEqual(([item["id"] for item in page["items"]], page["total"]), ([5], 1))
        # Quotes and FTS operators in what you type are plain words, not syntax.
        self.assertEqual(db.get_library(query='blazer" OR "a')["total"], 0)
        self.assertEqual(db.get_library(query="  ")["total"], 3)

    def test_ranked_results_keep_their_order_and_still_hide_rejected_saves(self):
        # Meaning or similarity search hands over ids best first; 4 is rejected, 99 doesn't exist.
        page = db.get_library(ranked=[1, 4, 5, 99, 3], limit=2)
        self.assertEqual(([item["id"] for item in page["items"]], page["total"], page["next_offset"]), ([1, 5], 3, 2))
        self.assertEqual([item["id"] for item in db.get_library(ranked=[1, 4, 5, 99, 3], offset=2)["items"]], [3])
        self.assertEqual(db.get_library(ranked=[])["total"], 0)

    def test_server_answers_pages_itself_with_validated_paging(self):
        run = lambda args: self.fail("the library is answered in the server, not by a new process")
        status, page = server.route("GET", "/library?offset=0&limit=2", {}, run)
        self.assertEqual((status, [item["id"] for item in page["items"]]), (200, [5, 3]))
        with patch.object(vectors, "library_page", return_value={}) as library:
            server.route("GET", "/library?area=style&q=tweed%20blazer", {}, run)
            server.route("GET", "/library?meaning=cozy%20room&similar=12", {}, run)
        self.assertEqual(library.call_args_list[0].kwargs, {"offset": 0, "limit": 200, "area": "style", "words": "tweed blazer"})
        self.assertEqual(library.call_args_list[1].kwargs, {"offset": 0, "limit": 200, "about": "cozy room", "like": 12})
        for bad in ("/library?offset=-1", "/library?area=--help", "/library?q=--help", "/library?meaning=-x",
                    "/library?similar=1;2"):
            with self.assertRaises(ValueError):
                server.route("GET", bad, {}, run)
