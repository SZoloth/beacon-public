import os
import tempfile
import unittest

from lib import db


class ProcessingStatsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmpdir.name, "test.db")
        db.init_db()
        db.seed_workstreams(
            [
                {
                    "slug": "folio",
                    "name": "Folio",
                    "backlog_path": "~/Projects/folio/docs/beacon-backlog.md",
                },
                {
                    "slug": "detour",
                    "name": "Detour",
                    "backlog_path": "~/Projects/detour/docs/beacon-backlog.md",
                },
            ]
        )
        self.addCleanup(self._restore_db_path)

    def _restore_db_path(self):
        db.DB_PATH = self.original_db_path

    def test_processing_stats_include_active_workstreams_with_zero_items(self):
        created = "2026-03-12T00:00:00+00:00"
        db.upsert_item(
            source="github",
            external_id="folio-item",
            title="Folio item",
            body="Body",
            url="https://example.com/folio-item",
            author="ada",
            raw_json="{}",
            created_at=created,
            synced_at=created,
        )

        conn = db.get_connection()
        try:
            item_id = conn.execute(
                "SELECT id FROM items WHERE external_id='folio-item'"
            ).fetchone()["id"]
        finally:
            conn.close()

        db.mark_classified(
            item_id=item_id,
            intent="build-with",
            tags=["agents"],
            workstream_assignments=[{"slug": "folio", "confidence": 0.8}],
            classified_at=created,
            human_read=False,
            one_liner="summary",
        )
        db.update_item_status(item_id, "queued")

        stats = db.get_processing_stats()
        by_slug = {row["slug"]: row for row in stats["by_workstream"]}

        self.assertIn("detour", by_slug)
        self.assertEqual(by_slug["detour"]["active"], 0)
        self.assertEqual(by_slug["detour"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
