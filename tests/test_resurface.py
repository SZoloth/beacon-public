import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from lib import conditions, db, resurface, review, useful


class ResurfaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "event.db"
        with patch.object(db, "DB_PATH", str(self.path)):
            db.init_db()
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        conditions.init_db(self.conn)
        self.conn.execute("INSERT INTO items(id,source,external_id,title,body,status,synced_at) VALUES(1,'manual','one','Offline guide','Use cached text.','new','2026-09-21')")
        self.conn.commit()
        conditions.save_condition(self.conn, condition_id="offline", project_slug="reader", title="Offline launch", definition={"when": "Reader project ships offline mode"}, authored_by="Fixture")
        self.now = datetime.now(timezone.utc)
        self.event = {"id": "launch", "project_slug": "reader", "summary": "Offline mode shipped.",
                      "observed_at": (self.now - timedelta(minutes=1)).isoformat(), "expires_at": (self.now + timedelta(days=1)).isoformat()}
        resurface.bind(self.conn, "offline", 1, "Fixture", (self.now + timedelta(days=2)).isoformat(), now=self.now)

    def answer(self, event, choice="met"):
        preview = resurface.run(self.conn, event, preview=True, now=self.now)
        return [{"request_sha256": review.request_hash(r), "response": {"model": useful.MODEL,
            "usage": {"input_tokens": 20, "output_tokens": 10}, "answers": {k: {"type": "choice", "choice": choice,
            "confidence": 1.0, "probabilities": {name: float(name == choice) for name in q["criteria"]}}
            for k, q in r["questions"].items()}}} for r in preview["requests"]]

    def test_event_transitions_duplicates_ordering_reversion(self):
        result = resurface.run(self.conn, self.event, replay=self.answer(self.event), apply=True, now=self.now)
        self.assertEqual(len(result["suggestions"]), 1)
        duplicate = resurface.run(self.conn, self.event, now=self.now)
        self.assertEqual(duplicate["skipped"][0]["reason"], "duplicate_event")
        old = {**self.event, "id": "old", "observed_at": (self.now - timedelta(hours=1)).isoformat()}
        self.assertEqual(resurface.run(self.conn, old, now=self.now)["skipped"][0]["reason"], "out_of_order_event")
        reverted = {**self.event, "id": "revert", "summary": "Offline launch rolled back.", "observed_at": self.now.isoformat()}
        result = resurface.run(self.conn, reverted, replay=self.answer(reverted, "not-met"), apply=True, now=self.now)
        self.assertEqual(result["transitions"][0]["state"], "reverted")
        self.assertEqual(conditions.list_suggestions(self.conn, active_only=True), [])
        self.assertEqual(self.conn.execute("SELECT status FROM items").fetchone()[0], "new")

    def test_approval_freshness_expiry_and_concurrent_edit(self):
        with self.assertRaisesRegex(ValueError, "expired"):
            resurface.run(self.conn, {**self.event, "expires_at": self.now.isoformat()}, now=self.now)
        def changed(request, **kwargs):
            response = self.answer(self.event)[0]["response"]
            self.conn.execute("UPDATE items SET body='changed' WHERE id=1")
            self.conn.commit()
            return response
        with patch.object(useful, "call_typesafe", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                resurface.run(self.conn, self.event, allow_upload=True, apply=True, now=self.now)
        self.assertEqual(conditions.list_suggestions(self.conn), [])
        result = resurface.run(self.conn, self.event, now=self.now)
        self.assertEqual(result["skipped"][0]["reason"], "changed_source_or_condition_requires_approval")

    def test_expired_event_suggestion_not_active(self):
        resurface.run(self.conn, self.event, replay=self.answer(self.event), apply=True, now=self.now)
        self.conn.execute("UPDATE condition_bindings SET last_event_expires_at='2000-01-01T00:00:00+00:00'")
        self.conn.commit()
        self.assertEqual(conditions.list_suggestions(self.conn, active_only=True), [])

    def test_removed_approval_hides_event_suggestion(self):
        resurface.run(self.conn, self.event, replay=self.answer(self.event), apply=True, now=self.now)
        self.conn.execute("DELETE FROM condition_bindings")
        self.conn.commit()
        self.assertEqual(conditions.list_suggestions(self.conn, active_only=True), [])

    def test_default_replay_is_read_only(self):
        before = self.path.read_bytes()
        result = resurface.run(self.conn, self.event, replay=self.answer(self.event), now=self.now)
        self.assertEqual(result["database_writes"], 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_provider_time_terminal_edit_cannot_write_stale_transition(self):
        def rejected(request, **kwargs):
            response = self.answer(self.event)[0]["response"]
            self.conn.execute("UPDATE items SET status='rejected' WHERE id=1")
            self.conn.commit()
            return response
        with patch.object(useful, "call_typesafe", side_effect=rejected):
            with self.assertRaisesRegex(ValueError, "changed"):
                resurface.run(self.conn, self.event, allow_upload=True, apply=True, now=self.now)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM future_condition_states").fetchone()[0], 0)

    def test_terminal_change_between_core_snapshot_and_status_check_is_rejected(self):
        replay = self.answer(self.event)
        original = conditions._db_item_status
        def race(conn, item_id):
            conn.execute("UPDATE items SET status='rejected' WHERE id=?", (item_id,))
            conn.commit()
            return original(conn, item_id)
        with patch.object(conditions, "_db_item_status", side_effect=race):
            with self.assertRaisesRegex(ValueError, "changed"):
                resurface.run(self.conn, self.event, replay=replay, apply=True, now=self.now)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM future_condition_states").fetchone()[0], 0)

    def test_cli_replay_and_preview_are_discoverable(self):
        event_path = Path(self.temp.name) / "event.json"
        event_path.write_text(json.dumps(self.event))
        command = [sys.executable, "beacon.py", "conditions", "event", "--db", str(self.path), "--event", str(event_path)]
        before = self.path.read_bytes()
        preview = subprocess.run(command + ["--preview", "--json"], text=True, capture_output=True, check=True)
        self.assertEqual(len(json.loads(preview.stdout)["requests"]), 1)
        self.assertEqual(self.path.read_bytes(), before)
        answers_path = Path(self.temp.name) / "answers.json"
        answers_path.write_text(json.dumps(self.answer(self.event)))
        assessed = subprocess.run(command + ["--answers", str(answers_path), "--json"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(assessed.stdout)["database_writes"], 0)
        self.assertEqual(self.path.read_bytes(), before)
        replay = subprocess.run(command + ["--answers", str(answers_path), "--apply", "--json"], text=True, capture_output=True, check=True)
        self.assertEqual(len(json.loads(replay.stdout)["suggestions"]), 1)


if __name__ == "__main__":
    unittest.main()
