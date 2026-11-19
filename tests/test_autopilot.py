"""Autopilot: Beacon passes the noise and sends reading to Reader itself, logs it, and can undo it."""
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import beacon
from lib import autopilot, db

# id: (title, Beacon's suggestion)
QUEUE = {1: ("Old meme", "skip"), 2: ("A long essay on agents", "read"), 3: ("A tool to adopt", "do"),
         4: ("Another essay", "read"), 5: ("More noise", "skip")}


class AutopilotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, (title, suggestion) in QUEUE.items():
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) "
                             "VALUES(?,'x',?,?,?,?,'queued','2026-10-10')",
                             (item_id, str(item_id), title, title, f"https://x.com/a/{item_id}"))
                conn.execute("INSERT INTO item_gists VALUES(?,?,'post',?,?,'2026-10-10')",
                             (item_id, f"About {title}", suggestion, f"Because {title}"))
        self.sent = []

    def send(self, item_id):
        self.sent.append(item_id)
        db.update_item_status(item_id, "adopted", note="read")

    def status(self, item_id):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()[0]

    def test_noise_is_passed_reading_goes_to_reader_and_picks_stay(self):
        result = autopilot.run(read_limit=1, send=self.send)
        self.assertEqual((result["passed"], result["read"], result["read_waiting"]), (2, 1, 1))
        self.assertEqual([self.status(i) for i in (1, 2, 3, 4, 5)], ["rejected", "adopted", "queued", "queued", "rejected"])
        self.assertEqual(self.sent, [2])
        handled = autopilot.handled()
        self.assertEqual([(h["id"], h["action"]) for h in handled], [(2, "read"), (5, "pass"), (1, "pass")])
        self.assertEqual(handled[1]["title"], "More noise")
        self.assertEqual(handled[1]["reason"], "Because More noise")

    def test_a_reference_is_filed_logged_and_never_a_pick(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) "
                         "VALUES(6,'x','6','Animation easing notes','Easing curves for UI motion','https://x.com/a/6','queued','2026-10-10')")
            conn.execute("INSERT INTO item_gists VALUES(6,'Easing curves for UI motion','post','reference','Inspiration for Folio motion','2026-10-10')")
            conn.execute("INSERT INTO workstreams(slug,name,kind) VALUES('style','Style','area')")
            conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT 6,id,0.9,1 FROM workstreams")
        result = autopilot.run(read_limit=1, send=self.send)
        self.assertEqual((result["passed"], result["read"], result["referenced"]), (2, 1, 1))
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT status, status_note FROM items WHERE id=6").fetchone(), ("adopted", "reference"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM briefs WHERE item_id=6").fetchone()[0], 0)
        self.assertEqual(self.status(3), "queued")
        self.assertNotIn(6, [item["id"] for item in db.get_triage_queue()])
        self.assertIn((6, "reference", "Inspiration for Folio motion"),
                      [(h["id"], h["action"], h["reason"]) for h in autopilot.handled()])
        autopilot.unhandle(6)
        self.assertEqual(self.status(6), "queued")
        autopilot.run(send=self.send)
        self.assertEqual(self.status(6), "queued")

    def test_a_held_reference_is_not_filed(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) "
                         "VALUES(6,'x','6','Notes','Body','https://x.com/a/6','queued','2026-10-10')")
            conn.execute("INSERT INTO item_gists VALUES(6,'Notes','post','reference','Later','2026-10-10')")
        self.hold_by_changing_source(6)
        result = autopilot.run(send=self.send)
        self.assertEqual((result["referenced"], self.status(6)), (0, "queued"))

    def test_a_dry_run_changes_nothing(self):
        result = autopilot.run(dry_run=True, send=self.send)
        self.assertEqual((result["passed"], result["read"], result["referenced"]), (2, 2, 0))
        self.assertEqual([self.status(i) for i in QUEUE], ["queued"] * 5)
        self.assertEqual((self.sent, autopilot.handled()), ([], []))

    def test_reader_trouble_stops_sending_but_passes_still_happen(self):
        def broken(item_id):
            raise RuntimeError("READWISE_ACCESS_TOKEN is not set")

        result = autopilot.run(send=broken)
        self.assertEqual((result["passed"], result["read"]), (2, 0))
        self.assertIn("READWISE_ACCESS_TOKEN", result["read_error"])
        self.assertEqual([self.status(i) for i in (2, 4)], ["queued", "queued"])

    def hold_by_changing_source(self, *item_ids):
        """An approved review, then the post's text changes upstream: the review is held again."""
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id in item_ids:
                conn.execute("INSERT INTO intake_reviews VALUES(?,?,?,?,?)",
                             (item_id, "hash", "approved", json.dumps({"assessment": "approved", "reasons": []}), "2026-10-10"))
                conn.execute("UPDATE items SET classified_at='2026-10-10' WHERE id=?", (item_id,))
                conn.execute("UPDATE items SET body='Rewritten after the owner approved it', "
                             "url='https://example.com/now-something-else' WHERE id=?", (item_id,))

    def test_a_source_change_hold_stops_autopilot_from_passing_or_sending_the_item(self):
        self.hold_by_changing_source(1, 2)  # 1 was suggested skip, 2 was suggested read
        with sqlite3.connect(db.DB_PATH) as conn:
            held = {row[0]: row[1] for row in conn.execute("SELECT item_id, disposition FROM intake_reviews")}
        self.assertEqual(held, {1: "review", 2: "review"})

        dry = autopilot.run(dry_run=True, send=self.send)
        self.assertEqual((dry["passed"], dry["read"]), (1, 1))  # only 5 and 4 are still eligible

        autopilot.run(send=self.send)
        self.assertEqual([self.status(i) for i in QUEUE], ["queued", "queued", "queued", "adopted", "rejected"])
        self.assertEqual(self.sent, [4])
        self.assertEqual({h["id"] for h in autopilot.handled()}, {4, 5})

    def test_a_hold_applied_after_the_list_was_made_is_still_respected(self):
        def send_then_hold_the_next(item_id):
            self.send(item_id)
            if item_id == 2:
                self.hold_by_changing_source(4)

        autopilot.run(send=send_then_hold_the_next)
        self.assertEqual(self.sent, [2])
        self.assertEqual(self.status(4), "queued")

    def test_a_suggestion_for_old_text_is_not_acted_on_after_the_hold_is_approved(self):
        self.hold_by_changing_source(1)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE intake_reviews SET disposition='approved' WHERE item_id=1")
        autopilot.run(send=self.send)
        # The owner approved the item, but "skip" was written about text that no longer exists.
        self.assertEqual(self.status(1), "queued")

    def test_undo_puts_it_back_and_records_that_beacon_was_wrong(self):
        autopilot.run(send=self.send)
        autopilot.unhandle(5)
        self.assertEqual(self.status(5), "queued")
        self.assertNotIn(5, [h["id"] for h in autopilot.handled()])
        self.assertEqual(autopilot.choices(), [{"item_id": 5, "suggestion": "skip", "choice": "undo", "source": "you"}])
        # Run again: an item you brought back isn't passed a second time.
        autopilot.run(send=self.send)
        self.assertEqual(self.status(5), "queued")

    def test_your_choices_in_triage_are_kept_with_the_suggestion_you_saw(self):
        argv = ["beacon.py", "mark", "reviewed", "3"]
        with patch.object(sys, "argv", argv), patch.object(beacon, "regenerate_all_backlogs"), redirect_stdout(io.StringIO()):
            beacon.main()
        self.assertEqual(autopilot.choices(), [{"item_id": 3, "suggestion": "do", "choice": "reviewed", "source": "you"}])


class OldSchemaTest(unittest.TestCase):
    def test_a_database_made_before_references_learns_to_hold_them(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "beacon.db")
            db.init_db(path)
            with sqlite3.connect(path) as conn:
                conn.executescript("""
                    DROP TABLE item_gists; DROP TABLE auto_actions;
                    CREATE TABLE item_gists (item_id INTEGER PRIMARY KEY REFERENCES items(id), takeaway TEXT NOT NULL,
                        kind TEXT NOT NULL, suggestion TEXT NOT NULL CHECK(suggestion IN ('do','read','skip')),
                        reason TEXT NOT NULL, built_at TEXT NOT NULL);
                    CREATE TABLE auto_actions (item_id INTEGER PRIMARY KEY REFERENCES items(id),
                        action TEXT NOT NULL CHECK(action IN ('pass', 'read')), reason TEXT, acted_at TEXT NOT NULL, undone_at TEXT);
                    INSERT INTO items(id,source,external_id,synced_at) VALUES(1,'x','1','2026-10-10'),(2,'x','2','2026-10-10');
                    INSERT INTO item_gists VALUES(1,'t','post','do','r','2026-10-10');
                    INSERT INTO auto_actions VALUES(1,'pass','r','2026-10-10',NULL);""")
            for _ in range(2):
                db.init_db(path)
            with sqlite3.connect(path) as conn:
                conn.execute("INSERT INTO item_gists VALUES(2,'t','post','reference','r','2026-10-10')")
                conn.execute("INSERT INTO auto_actions VALUES(2,'reference','r','2026-10-10',NULL)")
                self.assertEqual(conn.execute("SELECT suggestion FROM item_gists ORDER BY item_id").fetchall(), [("do",), ("reference",)])
                self.assertEqual(conn.execute("SELECT action FROM auto_actions ORDER BY item_id").fetchall(), [("pass",), ("reference",)])
                conn.execute("UPDATE items SET body='rewritten' WHERE id=2")  # the gist trigger still reaches the rebuilt table
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_gists WHERE item_id=2").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
