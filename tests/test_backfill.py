import os
import tempfile
import unittest
from unittest import mock

from lib import db
from lib.classify import backfill_workstream


class BackfillTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmpdir.name, "test.db")
        db.init_db()
        db.seed_workstreams(
            [
                {
                    "slug": "agent-foundry",
                    "name": "Agent Foundry",
                    "backlog_path": "~/Projects/agent-foundry/docs/beacon-backlog.md",
                },
                {
                    "slug": "beacon",
                    "name": "Beacon",
                    "backlog_path": "~/Projects/beacon/docs/beacon-backlog.md",
                },
            ]
        )
        self.addCleanup(self._restore_db_path)

    def _restore_db_path(self):
        db.DB_PATH = self.original_db_path

    def _create_item(self, external_id, title, *, status="queued", classified=True):
        created = "2026-03-12T00:00:00+00:00"
        db.upsert_item(
            source="github",
            external_id=external_id,
            title=title,
            body=f"{title} body",
            url=f"https://example.com/{external_id}",
            author="ada",
            raw_json="{}",
            created_at=created,
            synced_at=created,
        )

        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM items WHERE source='github' AND external_id=?",
                (external_id,),
            ).fetchone()
            item_id = row["id"]
        finally:
            conn.close()

        if classified:
            db.mark_classified(
                item_id=item_id,
                intent="build-with",
                tags=["agents"],
                workstream_assignments=[{"slug": "beacon", "confidence": 0.8}],
                classified_at=created,
                human_read=False,
                one_liner=f"{title} summary",
            )

        db.update_item_status(item_id, status)
        return item_id

    def test_get_backfill_candidate_items_excludes_terminal_and_existing_assignments(self):
        active_id = self._create_item("active", "Active item", status="queued")
        stale_id = self._create_item("stale", "Stale item", status="stale")
        unclassified_id = self._create_item("new", "New item", status="new", classified=False)
        already_assigned_id = self._create_item("assigned", "Assigned item", status="queued")

        db.assign_item_workstream(already_assigned_id, "agent-foundry", 0.9)

        candidates = db.get_backfill_candidate_items("agent-foundry", limit=20)
        candidate_ids = {item["id"] for item in candidates}

        self.assertIn(active_id, candidate_ids)
        self.assertNotIn(stale_id, candidate_ids)
        self.assertNotIn(unclassified_id, candidate_ids)
        self.assertNotIn(already_assigned_id, candidate_ids)

    @mock.patch("lib.classify.classify_backfill_batch")
    def test_backfill_workstream_assigns_recommended_items_only(self, mock_classify_batch):
        keep_id = self._create_item("keep", "Keep item", status="queued")
        skip_id = self._create_item("skip", "Skip item", status="queued")
        stale_id = self._create_item("stale-2", "Stale item", status="stale")

        mock_classify_batch.return_value = [
            {"id": keep_id, "relevant": True, "confidence": 0.92},
            {"id": skip_id, "relevant": False, "confidence": 0.2},
            {"id": stale_id, "relevant": True, "confidence": 0.95},
        ]

        assigned = backfill_workstream("agent-foundry", batch_size=10, limit=10, dry_run=False)

        self.assertEqual(assigned, 1)

        candidates = db.get_backfill_candidate_items("agent-foundry", limit=20)
        candidate_ids = {item["id"] for item in candidates}
        self.assertIn(skip_id, candidate_ids)
        self.assertNotIn(keep_id, candidate_ids)

        conn = db.get_connection()
        try:
            assigned_rows = conn.execute(
                """SELECT i.external_id
                   FROM item_workstreams iw
                   JOIN workstreams w ON w.id = iw.workstream_id
                   JOIN items i ON i.id = iw.item_id
                   WHERE w.slug = 'agent-foundry'"""
            ).fetchall()
        finally:
            conn.close()

        external_ids = {row["external_id"] for row in assigned_rows}
        self.assertEqual(external_ids, {"keep"})

    @mock.patch("lib.classify.classify_backfill_batch")
    def test_backfill_promotes_new_items_to_queued_when_assigned(self, mock_classify_batch):
        created = "2026-03-12T00:00:00+00:00"
        db.upsert_item(
            source="github",
            external_id="newly-classified",
            title="Swift helper",
            body="Swift helper body",
            url="https://example.com/swift-helper",
            author="ada",
            raw_json="{}",
            created_at=created,
            synced_at=created,
        )

        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT id FROM items WHERE source='github' AND external_id='newly-classified'"
            ).fetchone()
            item_id = row["id"]
        finally:
            conn.close()

        db.mark_classified(
            item_id=item_id,
            intent="build-with",
            tags=["swift"],
            workstream_assignments=[{"slug": "beacon", "confidence": 0.8}],
            classified_at=created,
            human_read=False,
            one_liner="Swift helper summary",
        )

        mock_classify_batch.return_value = [
            {"id": item_id, "relevant": True, "confidence": 0.9},
        ]

        backfill_workstream("agent-foundry", batch_size=10, limit=10, dry_run=False)

        conn = db.get_connection()
        try:
            status = conn.execute(
                "SELECT status FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()["status"]
        finally:
            conn.close()

        self.assertEqual(status, "queued")


if __name__ == "__main__":
    unittest.main()
