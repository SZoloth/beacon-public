import json
import os
import tempfile
import unittest
from pathlib import Path

from lib import db
from lib.evaluate import write_baseline_bundle


class EvaluationBaselineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmpdir.name, "beacon.db")
        db.init_db()
        db.seed_workstreams(
            [
                {
                    "slug": "beacon",
                    "name": "Beacon",
                    "backlog_path": "~/Projects/beacon/docs/beacon-backlog.md",
                }
            ]
        )
        self.addCleanup(self._restore_db_path)

        for status in ("adopted", "queued", "stale"):
            for index in range(4):
                self._create_item(status, index)

    def _restore_db_path(self):
        db.DB_PATH = self.original_db_path

    def _create_item(self, status, index):
        stamp = "2026-07-13T00:00:00+00:00"
        external_id = f"{status}-{index}"
        db.upsert_item(
            source="github",
            external_id=external_id,
            title=external_id,
            body=f"{external_id} body",
            url=f"https://github.com/example/{external_id}",
            author="example",
            raw_json="{}",
            created_at=stamp,
            synced_at=stamp,
        )
        conn = db.get_connection()
        try:
            item_id = conn.execute(
                "SELECT id FROM items WHERE source='github' AND external_id=?",
                (external_id,),
            ).fetchone()["id"]
        finally:
            conn.close()
        db.mark_classified(
            item_id=item_id,
            intent="build-with",
            tags=["agents"],
            workstream_assignments=[{"slug": "beacon", "confidence": 0.8}],
            classified_at=stamp,
            one_liner=f"Use {external_id}",
        )
        db.update_item_status(item_id, status, note=f"{status} label")

    def test_write_baseline_bundle_is_stratified_read_only_and_emits_receipt(self):
        output_dir = os.path.join(self.tmpdir.name, "runs")

        result = write_baseline_bundle(output_dir=output_dir, size=9, seed=7)

        baseline = json.loads(Path(result["baseline_path"]).read_text(encoding="utf-8"))
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))

        self.assertEqual(len(baseline["items"]), 9)
        self.assertEqual(
            baseline["status_distribution"],
            {"adopted": 3, "queued": 3, "stale": 3},
        )
        self.assertTrue(all(item["label_state"] == "pending_sam" for item in baseline["items"]))
        self.assertTrue(
            all(
                item["labels"]
                == {
                    "destination_fit": None,
                    "intended_use": None,
                    "action_quality": None,
                    "confidence": None,
                    "approval_required": None,
                }
                for item in baseline["items"]
            )
        )
        self.assertEqual(receipt["routing_decisions"], [])
        self.assertIn("reviewed", receipt["unresolved"]["missing_statuses"])
        self.assertIn("tried", receipt["unresolved"]["missing_statuses"])
        self.assertEqual(len(receipt["changed_files"]), 2)

        conn = db.get_connection()
        try:
            counts = {
                row["status"]: row["c"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS c FROM items GROUP BY status"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(counts, {"adopted": 4, "queued": 4, "stale": 4})


if __name__ == "__main__":
    unittest.main()
