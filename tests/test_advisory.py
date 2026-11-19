import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import beacon
from lib import advisory, db


class AdvisoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "beacon.db"
        with patch.object(db, "DB_PATH", str(self.db_path)):
            db.init_db()
        now = datetime(2026, 9, 20, tzinfo=timezone.utc)
        self.now = now
        self.packet = Path(self.temp.name) / "advisories.json"
        rows = [
            {
                "source_owner": "Synthetic safety owner", "action_owner": "Synthetic action owner",
                "source_ref": "synthetic:safety/1", "current_version": "v1",
                "advisory_reason": "Pause before changing the invented safety control.",
                "observed_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
                "severity": "urgent", "risk_type": "safety",
            },
            {
                "source_owner": "Synthetic safety owner", "action_owner": "Synthetic action owner",
                "source_ref": "synthetic:safety/1", "current_version": "v1",
                "advisory_reason": "Pause before changing the invented safety control.",
                "observed_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
                "severity": "urgent", "risk_type": "safety",
            },
            {
                "source_owner": "Synthetic finance owner", "action_owner": "Synthetic action owner",
                "source_ref": "synthetic:finance/2", "current_version": "v2",
                "advisory_reason": "Verify the invented financial assumption before action.",
                "observed_at": (now - timedelta(days=2)).isoformat(), "expires_at": (now - timedelta(hours=1)).isoformat(),
                "severity": "material", "risk_type": "financial",
            },
        ]
        self.packet.write_text(json.dumps({"version": 1, "advisories": rows}))

    def test_ingest_deduplicates_and_keeps_expired_material_hold_visible(self):
        result = advisory.ingest_packet(self.packet, self.db_path, now=self.now)
        self.assertEqual(result["events_received"], 3)
        self.assertEqual(result["unique_events"], 2)
        self.assertEqual(result["deduplicated"], 1)
        self.assertEqual(result["open_queue_count"], 2)
        self.assertEqual(result["material_or_urgent_open"], 2)
        replay = advisory.ingest_packet(self.packet, self.db_path, now=self.now)
        self.assertEqual(replay["updated"], 0)
        self.assertEqual(replay["inserted"], 0)
        queue = advisory.review_queue(self.db_path, now=self.now)
        self.assertEqual([row["severity"] for row in queue], ["urgent", "material"])
        self.assertTrue(queue[1]["expired"])
        self.assertTrue(all(row["hold"] for row in queue))
        self.assertEqual(queue[0]["source_ref"], "synthetic:safety/1")
        self.assertEqual(queue[0]["action_owner"], "Synthetic action owner")

    def test_cli_ingest_and_existing_held_queue_output_are_local(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["beacon", "advisory", "ingest", str(self.packet), "--db", str(self.db_path)]), redirect_stdout(output):
            beacon.main()
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["unique_events"], 2)
        output = io.StringIO()
        with patch.object(sys, "argv", ["beacon", "triage", "--held", "--db", str(self.db_path)]), redirect_stdout(output):
            beacon.main()
        held = json.loads(output.getvalue())
        self.assertEqual(len(held["advisories"]), 2)
        self.assertEqual(held["advisories"][0]["source_owner"], "Synthetic safety owner")
        self.assertEqual(held["advisories"][1]["advisory_reason"], "Verify the invented financial assumption before action.")

    def test_manual_resolution_removes_only_selected_hold_and_reingest_does_not_resurrect(self):
        advisory.ingest_packet(self.packet, self.db_path, now=self.now)
        first = advisory.review_queue(self.db_path, now=self.now)[0]
        result = advisory.resolve(self.db_path, first["id"], "Reviewed by the human owner.")
        self.assertFalse(result["already_resolved"])
        self.assertEqual(len(advisory.review_queue(self.db_path, now=self.now)), 1)
        advisory.ingest_packet(self.packet, self.db_path, now=self.now + timedelta(hours=1))
        self.assertEqual(len(advisory.review_queue(self.db_path, now=self.now)), 1)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM advisory_reviews WHERE id=?", (first["id"],)).fetchone()[0], "resolved")

    def test_packet_validation_fails_closed(self):
        raw = json.loads(self.packet.read_text())
        raw["advisories"][0]["extra"] = "not accepted"
        with self.assertRaisesRegex(ValueError, "fields"):
            advisory.validate_packet(raw, now=self.now)
        raw = json.loads(self.packet.read_text())
        raw["advisories"][0]["severity"] = "critical"
        with self.assertRaisesRegex(ValueError, "severity"):
            advisory.validate_packet(raw, now=self.now)

    def test_same_version_conflicts_reject_atomically_and_identical_replay_stays_resolved(self):
        advisory.ingest_packet(self.packet, self.db_path, now=self.now)
        urgent = advisory.review_queue(self.db_path, now=self.now)[0]
        advisory.resolve(self.db_path, urgent["id"], "Human checked the synthetic hold.")

        conflicting = json.loads(self.packet.read_text())
        conflicting["advisories"] = [conflicting["advisories"][0], conflicting["advisories"][2]]
        conflicting["advisories"][0]["severity"] = "warning"
        conflicting["advisories"][0]["advisory_reason"] = "Changed reason must not overwrite the hold."
        conflicting["advisories"][0]["expires_at"] = (self.now + timedelta(days=3)).isoformat()
        conflicting["advisories"][1]["source_ref"] = "synthetic:finance/new"
        conflicting["advisories"].reverse()
        conflict_path = Path(self.temp.name) / "conflict.json"
        conflict_path.write_text(json.dumps(conflicting))
        with self.assertRaisesRegex(ValueError, "use a new version"):
            advisory.ingest_packet(conflict_path, self.db_path, now=self.now)

        # A failed packet is atomic: the new financial row was not inserted.
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM advisory_reviews").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT severity FROM advisory_reviews WHERE id=?", (urgent["id"],)).fetchone()[0], "urgent")

        replay = json.loads(self.packet.read_text())
        replay_path = Path(self.temp.name) / "replay.json"
        replay_path.write_text(json.dumps(replay))
        advisory.ingest_packet(replay_path, self.db_path, now=self.now + timedelta(hours=1))
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM advisory_reviews WHERE id=?", (urgent["id"],)).fetchone()[0], "resolved")

    def test_identity_read_and_insert_share_a_write_transaction(self):
        checks = []

        class ObservedConnection(sqlite3.Connection):
            def execute(connection, sql, parameters=()):
                if "WHERE dedup_key=?" in sql:
                    checks.append(connection.in_transaction)
                return super().execute(sql, parameters)

        connect = sqlite3.connect
        with patch.object(advisory.sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(*args, **kwargs, factory=ObservedConnection)):
            advisory.ingest_packet(self.packet, self.db_path, now=self.now)
        self.assertTrue(checks)
        self.assertTrue(all(checks), "Identity checks must hold the SQLite write lock through insertion.")

    def test_malformed_advisory_schema_is_not_hidden(self):
        path = Path(self.temp.name) / "malformed.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE advisory_reviews (id INTEGER)")
        with self.assertRaises(sqlite3.OperationalError):
            advisory.review_queue(path, now=self.now)

    def test_version_and_observation_time_are_strict(self):
        raw = json.loads(self.packet.read_text())
        raw["version"] = True
        with self.assertRaisesRegex(ValueError, "version"):
            advisory.validate_packet(raw, now=self.now)
        raw = json.loads(self.packet.read_text())
        raw["advisories"][0]["observed_at"] = (self.now + timedelta(minutes=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "future"):
            advisory.validate_packet(raw, now=self.now)

    def test_oversized_packet_is_rejected_before_full_read(self):
        oversized = Path(self.temp.name) / "oversized.json"
        oversized.write_bytes(b"{" + b" " * 24000)
        with self.assertRaisesRegex(ValueError, "24 KB"):
            advisory.ingest_packet(oversized, self.db_path, now=self.now)


if __name__ == "__main__":
    unittest.main()
