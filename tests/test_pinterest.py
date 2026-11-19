"""Pinterest board RSS: parsed and stored from a fixture; no network."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, pinterest

FIXTURE = Path(__file__).parent / "fixtures" / "pinterest-board.rss"
FEED = "https://www.pinterest.com/testuser/kitchen-ideas.rss"


class BoardUrlTest(unittest.TestCase):
    def test_accepts_the_common_ways_to_name_a_board(self):
        for board in ("testuser/kitchen-ideas", "/testuser/kitchen-ideas/",
                      "https://www.pinterest.com/testuser/kitchen-ideas/",
                      "https://pinterest.com/testuser/kitchen-ideas.rss",
                      "https://uk.pinterest.com/testuser/kitchen-ideas"):
            self.assertEqual(pinterest.board_feed_url(board), FEED, board)

    def test_rejects_other_hosts_schemes_and_shapes(self):
        for board in ("", "http://www.pinterest.com/testuser/kitchen-ideas", "https://example.com/testuser/kitchen-ideas",
                      "https://pinterest.com.evil.example/testuser/kitchen-ideas", "file:///etc/passwd",
                      "testuser", "testuser/board/section"):
            with self.assertRaises(ValueError, msg=board):
                pinterest.board_feed_url(board)


class ParseFeedTest(unittest.TestCase):
    def setUp(self):
        self.pins = pinterest.parse_feed(FIXTURE.read_bytes(), FEED)

    def test_reads_pins_and_skips_items_with_no_pin_id(self):
        self.assertEqual([pin["pin_id"] for pin in self.pins], ["1111111111111111", "2222222222222222", "3333333333333333"])

    def test_pin_fields(self):
        pin = self.pins[0]
        self.assertEqual(pin["title"], "Walnut open shelving")
        self.assertEqual(pin["text"], "Warm walnut shelves & brass brackets above a butcher-block counter.")
        self.assertEqual(pin["url"], "https://www.pinterest.com/pin/1111111111111111/")
        self.assertEqual(pin["board"], "Kitchen ideas")
        self.assertEqual(pin["owner"], "testuser")
        self.assertEqual(pin["published"], "2026-10-04T12:30:00+00:00")

    def test_image_is_the_larger_rendition(self):
        self.assertEqual(self.pins[0]["thumbnail"], "https://i.pinimg.com/236x/aa/bb/cc/aabbcc.jpg")
        self.assertEqual(self.pins[0]["image"], "https://i.pinimg.com/736x/aa/bb/cc/aabbcc.jpg")

    def test_untitled_pin_gets_a_fallback_title_and_dates_convert_to_utc(self):
        pin = self.pins[1]
        self.assertEqual(pin["title"], "Pin 2222222222222222")
        self.assertEqual(pin["published"], "2026-10-03T14:00:00+00:00")

    def test_text_only_pin_has_no_image_or_date(self):
        pin = self.pins[2]
        self.assertIsNone(pin["image"])
        self.assertIsNone(pin["published"])
        self.assertEqual(pin["text"], "Text only description.")

    def test_refuses_entity_declarations_and_non_feeds(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
        with self.assertRaisesRegex(ValueError, "DOCTYPE"):
            pinterest.parse_feed(bomb, FEED)
        with self.assertRaisesRegex(ValueError, "valid XML"):
            pinterest.parse_feed(b"<html>sign in</html", FEED)
        with self.assertRaisesRegex(ValueError, "channel"):
            pinterest.parse_feed(b"<html><body>sign in</body></html>", FEED)


class FetchBoardsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", os.path.join(self.temp.name, "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def rows(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM items WHERE source='pinterest' ORDER BY external_id").fetchall()

    def test_stores_pins_as_items_and_a_second_run_adds_nothing(self):
        calls = []
        def download(url):
            calls.append(url)
            return FIXTURE.read_bytes()
        self.assertEqual(pinterest.fetch_pinterest_boards(["testuser/kitchen-ideas"], download=download), 3)
        self.assertEqual(pinterest.fetch_pinterest_boards(["testuser/kitchen-ideas"], download=download), 0)
        self.assertEqual(calls, [FEED, FEED])
        rows = self.rows()
        self.assertEqual([row["external_id"] for row in rows], ["1111111111111111", "2222222222222222", "3333333333333333"])
        first = rows[0]
        self.assertEqual(first["url"], "https://www.pinterest.com/pin/1111111111111111/")
        self.assertEqual(first["author"], "testuser")
        self.assertEqual(first["created_at"], "2026-10-04T12:30:00+00:00")
        self.assertEqual(first["saved_at"], "2026-10-04T12:30:00+00:00")
        self.assertIn("Pinterest board: Kitchen ideas", first["body"])
        raw = json.loads(first["raw_json"])
        self.assertEqual(raw["media"], [{"type": "photo", "url": "https://i.pinimg.com/736x/aa/bb/cc/aabbcc.jpg"}])
        self.assertEqual(json.loads(rows[2]["raw_json"])["media"], [])
        self.assertEqual(db.get_sync_state("pinterest")["items_synced"], 0)

    def test_one_failing_board_does_not_stop_the_others(self):
        def download(url):
            if "broken" in url:
                raise OSError("offline")
            return FIXTURE.read_bytes()
        count = pinterest.fetch_pinterest_boards(["testuser/broken", "not a board", "testuser/kitchen-ideas"], download=download)
        self.assertEqual(count, 3)

    def test_no_boards_is_a_quiet_skip(self):
        self.assertEqual(pinterest.fetch_pinterest_boards([]), 0)
        self.assertIsNone(db.get_sync_state("pinterest"))

    def test_every_board_failing_fails_the_sync_and_leaves_sync_state_alone(self):
        # The weekly job and the app's Sync report a failed source only when it raises.
        def download(url):
            raise OSError("offline")
        with self.assertRaisesRegex(RuntimeError, "no Pinterest board synced"):
            pinterest.fetch_pinterest_boards(["testuser/kitchen-ideas", "testuser/travel"], download=download)
        self.assertIsNone(db.get_sync_state("pinterest"))

    def test_pins_show_in_the_library_and_render_as_pictures(self):
        pinterest.fetch_pinterest_boards(["testuser/kitchen-ideas"], download=lambda url: FIXTURE.read_bytes())
        library = db.get_library()
        by_id = {item["url"]: item for item in library["items"]}
        shelving = by_id["https://www.pinterest.com/pin/1111111111111111/"]
        self.assertEqual(shelving["source"], "pinterest")
        self.assertEqual(shelving["media"], [{"url": "https://i.pinimg.com/736x/aa/bb/cc/aabbcc.jpg", "width": None, "height": None}])
        self.assertEqual(len(library["items"]), 2, "a pin with no picture waits for Triage like a text-only post")

    def test_a_rejected_pin_leaves_the_library(self):
        pinterest.fetch_pinterest_boards(["testuser/kitchen-ideas"], download=lambda url: FIXTURE.read_bytes())
        item_id = self.rows()[0]["id"]
        db.update_item_status(item_id, "rejected")
        self.assertNotIn(item_id, [item["id"] for item in db.get_library()["items"]])


class LibraryColumnMigrationTest(unittest.TestCase):
    def test_a_database_from_before_pinterest_gets_the_new_library_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "old.db")
            db.init_db(path)
            old_rule = ("(status != 'rejected' AND (source IN ('github', 'add') OR (source = 'x' AND CASE WHEN json_valid(raw_json) "
                        "THEN json_array_length(json_extract(raw_json, '$.media')) > 0 ELSE 0 END)))")
            conn = sqlite3.connect(path)
            conn.execute("DROP INDEX items_in_library")
            conn.execute("ALTER TABLE items DROP COLUMN in_library")
            conn.execute(f"ALTER TABLE items ADD COLUMN in_library INTEGER GENERATED ALWAYS AS {old_rule} VIRTUAL")
            conn.execute("CREATE INDEX items_in_library ON items(in_library, id)")
            media = json.dumps({"media": [{"type": "photo", "url": "https://i.pinimg.com/736x/a.jpg"}]})
            conn.executemany(
                "INSERT INTO items(source, external_id, title, body, raw_json, synced_at) VALUES(?,?,?,?,?,?)",
                [("pinterest", "1", "Pin", "", media, "2026-10-01"), ("x", "2", "Post", "", media, "2026-10-01"),
                 ("x", "3", "Text only", "", "{}", "2026-10-01"), ("mymind", "4", "Card", "", media, "2026-10-01")])
            conn.commit()
            self.assertEqual(conn.execute("SELECT external_id FROM items WHERE in_library=1").fetchall(), [("2",)])
            conn.close()

            db.init_db(path)
            db.init_db(path)  # a second start leaves it alone
            conn = sqlite3.connect(path)
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("SELECT external_id FROM items WHERE in_library=1 ORDER BY id").fetchall(),
                             [("1",), ("2",)])
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='items_in_library'").fetchone())


if __name__ == "__main__":
    unittest.main()
