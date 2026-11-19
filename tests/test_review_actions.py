import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lib import db, intake, review_actions


class HumanReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        self.db_patch = patch.object(db, "DB_PATH", str(self.path))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "reader", "name": "Reader", "backlog_path": str(self.path.parent / "backlog.md")}])
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,synced_at,one_liner,human_read) VALUES(1,'manual','1','Synthetic source','Exact saved words','https://example.com/1','Example','new','2026-09-21','Keep this summary',0)")
            item = dict(conn.execute("SELECT * FROM items WHERE id=1").fetchone())
            conn.execute("INSERT INTO intake_reviews VALUES(1,?,'no_fit',?,'2026-09-21')", (intake.source_hash(item), json.dumps({"assessment": "no_fit", "reasons": ["Uncertain fit"], "model": "jev-1.13.0"})))

    def test_classification_preserves_source_and_reaches_existing_router_without_delivery(self):
        snapshot = review_actions.inspect(self.path, "intake:1")
        result = review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="classify", note="Use for offline reader", intent="reference", workstreams=["reader"])
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["backlog_writes"], 0)
        self.assertEqual(intake.review_queue(self.path), [])
        self.assertEqual([row["id"] for row in db.get_unrouted_items()], [1])
        with closing(db.get_connection()) as conn:
            row = conn.execute("SELECT * FROM items WHERE id=1").fetchone()
            self.assertEqual((row["body"], row["one_liner"], row["human_read"], row["status"]), ("Exact saved words", "Keep this summary", 0, "new"))
            history = json.loads(conn.execute("SELECT snapshot_json FROM review_resolutions").fetchone()[0])
            self.assertEqual(json.loads(history["hold"]["result_json"])["assessment"], "no_fit")
        self.assertFalse((self.path.parent / "backlog.md").exists())
        self.assertTrue(review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="classify", note="Use for offline reader", intent="reference", workstreams=["reader"])["already_applied"])

    def test_changed_inputs_and_terminal_state_cannot_be_approved(self):
        for sql in ("UPDATE items SET body=body||' changed' WHERE id=1", "UPDATE items SET status='rejected' WHERE id=1", "UPDATE workstreams SET backlog_path='changed' WHERE slug='reader'", "UPDATE intake_reviews SET reviewed_at='changed' WHERE item_id=1"):
            with self.subTest(sql=sql):
                snapshot = review_actions.inspect(self.path, "intake:1")
                with sqlite3.connect(self.path) as conn: conn.execute(sql)
                with self.assertRaises(ValueError):
                    review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="classify", note="Explicit review", intent="reference", workstreams=["reader"])
                with sqlite3.connect(self.path) as conn:
                    conn.execute("UPDATE items SET status='new'")
                    conn.execute("INSERT OR IGNORE INTO intake_reviews VALUES(1,'old','review','{}','2026-09-21')")

    def test_save_filing_preserves_failure_and_new_source_reopens_it(self):
        from lib import curator
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO save_reviews(item_id,source_hash,status,result_json,reviewed_at) VALUES(1,'old','failed',?, '2026-09-21')", (json.dumps({"error": "Provider unavailable"}),))
        snapshot = review_actions.inspect(self.path, "save:1")
        review_actions.apply(self.path, "save:1", expected=snapshot["digest"], action="file", note="Read the failure; no action")
        self.assertEqual(curator.review_queue(self.path), [])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT status,result_json FROM save_reviews").fetchone(), ("failed", '{"error": "Provider unavailable"}'))
            conn.execute("UPDATE items SET body='New source version'")
        self.assertEqual(len(curator.review_queue(self.path)), 1)

    def test_changed_queued_source_can_be_reconfirmed_and_corrected_without_resetting_lifecycle(self):
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE items SET status='queued',classified_at='yesterday',intent='reference'")
            conn.execute("INSERT INTO item_workstreams VALUES(1,1,.9,1,'yesterday')")
            conn.execute("UPDATE items SET body='Updated source evidence'")
        snapshot = review_actions.inspect(self.path, "intake:1")
        review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="classify", note="Confirm updated source", intent="reference", workstreams=["reader"])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT routed FROM item_workstreams").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM items").fetchone()[0], "queued")
        correction = review_actions.inspect(self.path, "intake:1")
        self.assertIn("classify", correction["actions"])
        review_actions.apply(self.path, "intake:1", expected=correction["digest"], action="classify", note="Keep in library only", intent="reference", workstreams=[])
        self.assertEqual(db.get_unrouted_items(), [])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_workstreams").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_resolutions").fetchone()[0], 2)

    def test_fresh_terminal_approval_and_invalid_destinations_roll_back(self):
        for status, destination in (("new", "missing"), ("rejected", "reader"), ("stale", "reader")):
            with sqlite3.connect(self.path) as conn: conn.execute("UPDATE items SET status=?", (status,))
            snapshot = review_actions.inspect(self.path, "intake:1")
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="classify", note="Test rejection", intent="reference", workstreams=[destination])
            self.assertEqual(self.path.read_bytes(), before)

    def test_unclear_intent_stays_held_and_duplicate_interaction_reports_the_receipt(self):
        snapshot = review_actions.inspect(self.path, "intake:1")
        with self.assertRaises(ValueError):
            review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="classify", note="Still uncertain", intent="unclear", workstreams=["reader"])
        replies = iter(["classify", "reference", "reader", "Explicit choice", "yes"])
        output = []
        def ask(prompt):
            reply = next(replies)
            if reply == "yes":
                review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="classify", note="Explicit choice", intent="reference", workstreams=["reader"])
            return reply
        review_actions.interactive(self.path, "intake:1", ask=ask, emit=output.append)
        self.assertIn("Earlier decision", output[-1])

    def test_filed_intake_can_be_reopened_for_a_later_explicit_classification(self):
        snapshot = review_actions.inspect(self.path, "intake:1")
        review_actions.apply(self.path, "intake:1", expected=snapshot["digest"], action="file", note="Keep for later")
        self.assertEqual(intake.review_queue(self.path), [])
        reopened = review_actions.inspect(self.path, "intake:1")
        self.assertIn("classify", reopened["actions"])
        review_actions.apply(self.path, "intake:1", expected=reopened["digest"], action="classify", note="Now useful for reader", intent="reference", workstreams=["reader"])
        self.assertEqual([row["id"] for row in db.get_unrouted_items()], [1])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_resolutions").fetchone()[0], 2)

    def test_fresh_snapshot_cannot_refile_unchanged_intake_or_save_evidence(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO save_reviews(item_id,source_hash,status,result_json,reviewed_at) VALUES(1,'old','failed','{}','2026-09-21')")
        for target in ("intake:1", "save:1"):
            with self.subTest(target=target):
                snapshot = review_actions.inspect(self.path, target)
                review_actions.apply(self.path, target, expected=snapshot["digest"], action="file", note="Read and filed")
                fresh = review_actions.inspect(self.path, target)
                self.assertNotIn("file", fresh["actions"])
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "already closed"):
                    review_actions.apply(self.path, target, expected=fresh["digest"], action="file", note="Read and filed")
                self.assertEqual(self.path.read_bytes(), before)

    def test_interrupt_after_commit_never_claims_nothing_changed(self):
        replies = iter(["classify", "reference", "none", "Keep locally", "yes"])
        output = []
        def emit(message):
            if message.startswith("Saved locally"):
                raise KeyboardInterrupt
            output.append(message)
        with self.assertRaises(KeyboardInterrupt):
            review_actions.interactive(self.path, "intake:1", ask=lambda _: next(replies), emit=emit)
        self.assertEqual(intake.review_queue(self.path), [])
        self.assertFalse(any("Nothing changed" in message for message in output))

    def test_advisory_resolution_preserves_exact_version_and_queue_reads_are_read_only(self):
        from lib import advisory, curator
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO advisory_reviews(dedup_key,source_owner,action_owner,source_ref,current_version,advisory_reason,observed_at,expires_at,severity,risk_type,status,received_at) VALUES('one','mosaic','Ada','local:example','v1','Review this tile','2026-09-20T00:00:00Z','2026-09-21T00:00:00Z','info','general','open','2026-09-20T00:00:00Z')")
        before = self.path.read_bytes()
        snapshot = review_actions.inspect(self.path, "advisory:1")
        curator.review_queue(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        review_actions.apply(self.path, "advisory:1", expected=snapshot["digest"], action="file", note="Read this old version only")
        self.assertEqual(advisory.review_queue(self.path), [])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT current_version,expires_at FROM advisory_reviews").fetchone(), ("v1", "2026-09-21T00:00:00Z"))

    def test_interactive_cancel_has_no_database_writes_and_confirmed_review_survives_reopen(self):
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "beacon.py"), "review", "intake:1", "--db", str(self.path)]
        before = self.path.read_bytes()
        cancelled = subprocess.run(command, input="\n", text=True, capture_output=True)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertIn("Left held", cancelled.stdout)
        self.assertEqual(self.path.read_bytes(), before)
        saved = subprocess.run(command, input="classify\nreference\nreader\nUseful offline evidence\nyes\n", text=True, capture_output=True)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        self.assertIn("Backlog files unchanged", saved.stdout)
        self.assertEqual(intake.review_queue(self.path), [])


if __name__ == "__main__":
    unittest.main()
