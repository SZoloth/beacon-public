import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import beacon
from lib import curator, db


class CuratorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.db"
        self.context = Path(self.temp.name) / "context.json"
        self.inbox = Path(self.temp.name) / "advisories.json"
        with patch.object(db, "DB_PATH", str(self.path)):
            db.init_db()
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) VALUES (1,'manual','1','Backup','Local SQLite backup workflow','https://example.com/1','new',?)", (now.isoformat(),))
        self.context.write_text(json.dumps({
            "version": 1, "task": "Build a local SQLite backup workflow", "conditions": [], "efforts": []
        }))
        self.inbox.write_text(json.dumps({"version": 1, "advisories": [{
            "source_owner": "Synthetic owner", "action_owner": "Synthetic owner",
            "source_ref": "synthetic:hold", "current_version": "v1",
            "advisory_reason": "Pause before the invented operation.",
            "observed_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
            "severity": "urgent", "risk_type": "safety"
        }]}))
        self.config = {"curator": {"context_path": str(self.context), "advisory_inbox": str(self.inbox),
                                    "task": "Build a local SQLite backup workflow",
                                    "window": {"since": "2026-09-19T00:00:00Z", "until": "2026-09-21T00:00:00Z"},
                                    "max_save_reviews": 2}}

    def test_once_imports_advisory_and_reviews_new_revision_without_model(self):
        result = curator.run_once(self.path, self.config, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["phases"]["save_reviews"]["examined"], 1)
        self.assertEqual(result["phases"]["advisory"]["inserted"], 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM save_reviews").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM advisory_reviews").fetchone()[0], 1)
        again = curator.run_once(self.path, self.config, now=datetime(2026, 9, 20, 1, tzinfo=timezone.utc))
        self.assertTrue(again["reused"])
        self.assertEqual(again["phases"]["save_reviews"]["examined"], 1)
        self.assertEqual(again["network_calls"], 0)

    def test_cli_requires_explicit_once_and_keeps_bad_inbox_visible(self):
        bad = Path(self.temp.name) / "bad.json"
        bad.write_text("not json")
        config = {**self.config, "curator": {**self.config["curator"], "advisory_inbox": str(bad)}}
        with patch.object(beacon, "load_config", return_value=config), patch.object(beacon, "db", db), patch("sys.argv", ["beacon", "curate", "--once", "--db", str(self.path)]):
            beacon.main()
        with sqlite3.connect(self.path) as conn:
            payload = conn.execute("SELECT result_json FROM curator_runs").fetchone()[0]
        self.assertIn('"hold": true', payload)
        output = __import__("io").StringIO()
        with patch.object(beacon, "db", db), patch("sys.argv", ["beacon", "triage", "--held", "--db", str(self.path)]), patch("sys.stdout", output):
            beacon.main()
        self.assertIn('"save_reviews"', output.getvalue())

    def test_local_zero_budget_is_valid_and_source_change_invalidates_cache(self):
        config = {**self.config, "curator": {**self.config["curator"], "max_calls": 0}}
        result = curator.run_once(self.path, config, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "complete")
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE items SET body='Changed source' WHERE id=1")
        changed = curator.run_once(self.path, config, now=datetime(2026, 9, 20, 1, tzinfo=timezone.utc))
        self.assertFalse(changed.get("reused", False))
        self.assertEqual(changed["phases"]["save_reviews"]["examined"], 1)

    def test_failed_review_is_not_retried_without_identity_change(self):
        config = {**self.config, "curator": {**self.config["curator"], "context_path": str(self.context) + ".missing"}}
        first = curator.run_once(self.path, config, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.assertEqual(first["phases"]["save_reviews"]["failures"][0]["item_id"], 1)
        second = curator.run_once(self.path, config, now=datetime(2026, 9, 20, 1, tzinfo=timezone.utc))
        self.assertEqual(second["phases"]["save_reviews"]["examined"], 1)
        self.assertTrue(second["reused"])
        self.assertEqual(second["phases"]["save_reviews"]["failures"][0]["item_id"], 1)

    def test_interrupted_same_window_is_held_without_replaying(self):
        first = curator.run_once(self.path, self.config, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
        interrupted = dict(first)
        interrupted["status"] = "running"
        interrupted["network_calls"] = 1
        interrupted["phases"] = {"save_reviews": {"status": "running", "failures": []}}
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE curator_runs SET status='running', phase='save_reviews', result_json=?, finished_at=NULL",
                         (json.dumps(interrupted),))

        held = curator.run_once(self.path, self.config, now=datetime(2026, 9, 20, 1, tzinfo=timezone.utc))

        self.assertTrue(held["reused"])
        self.assertEqual(held["status"], "failed")
        self.assertTrue(held["interrupted"])
        self.assertEqual(held["network_calls"], 0)
        self.assertEqual(held["prior_network_calls"], 1)
        self.assertEqual(held["phases"]["save_reviews"]["reason"], "interrupted_unknown_completion")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM save_reviews").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM curator_runs").fetchone()[0], "failed")

    def test_interrupted_old_window_does_not_block_new_identity(self):
        first = curator.run_once(self.path, self.config, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
        interrupted = dict(first)
        interrupted["status"] = "running"
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE curator_runs SET status='running', phase='deck', result_json=?, finished_at=NULL",
                         (json.dumps(interrupted),))

        changed = {**self.config, "curator": {**self.config["curator"], "task": "A changed synthetic task"}}
        fresh = curator.run_once(self.path, changed, now=datetime(2026, 9, 20, 1, tzinfo=timezone.utc))

        self.assertFalse(fresh.get("reused", False))
        self.assertNotEqual(fresh["window_key"], first["window_key"])
        with sqlite3.connect(self.path) as conn:
            statuses = [row[0] for row in conn.execute("SELECT status FROM curator_runs ORDER BY started_at")]
        self.assertIn("failed", statuses)
        self.assertIn(fresh["status"], {"complete", "failed"})

    def test_remote_budget_covers_both_phases_and_failed_call_counts(self):
        config = {**self.config, "curator": {**self.config["curator"], "provider": "typesafe", "allow_upload": True, "max_calls": 0}}
        with patch.object(curator.save_review, "generate", side_effect=AssertionError("no remote call")):
            zero = curator.run_once(self.path, config, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.assertEqual(zero["network_calls"], 0)
        self.assertEqual(zero["phases"]["deck"]["status"], "budget_exhausted")

        config["curator"]["max_calls"] = 1
        packet = json.loads(self.context.read_text())
        packet["conditions"] = [{"id": "backup", "project": "beacon", "owner": "Fixture", "source_ref": "synthetic:backup", "when": "Need backups", "observed_at": "2026-09-20T00:00:00Z", "expires_at": "2026-09-23T00:00:00Z"}]
        self.context.write_text(json.dumps(packet))
        with patch.object(curator.save_review.useful, "call_typesafe", side_effect=ValueError("synthetic provider failure")) as call:
            failed = curator.run_once(self.path, config, now=datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(failed["network_calls"], 1)
        self.assertEqual(failed["phases"]["deck"]["status"], "budget_exhausted")

    def test_active_run_lock_rejects_second_claim(self):
        with curator._run_lock(self.path):
            with self.assertRaisesRegex(ValueError, "another curator run"):
                curator.run_once(self.path, self.config, now=datetime(2026, 9, 20, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
