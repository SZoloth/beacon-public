"""Top-K routing from stored Jev fit scores; no model calls."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, intake


class RouteTopTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        patcher = patch.object(db, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([
            {"slug": "agents", "name": "Agents", "backlog_path": str(Path(self.temp.name) / "a.md")},
            {"slug": "site", "name": "Site", "backlog_path": str(Path(self.temp.name) / "s.md")},
        ])

    def held(self, item_id, fits, intent="reference", disposition="review", status="new"):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,status,synced_at) VALUES(?,'x',?,?,'body',?,'2026-09-29')",
                         (item_id, str(item_id), f"Item {item_id}", status))
            conn.execute("INSERT INTO intake_reviews VALUES(?,?,?,?,?)",
                         (item_id, "hash", disposition, json.dumps({"intent": intent, "destination_probabilities": fits}), "2026-09-29"))

    def test_takes_top_k_per_project_above_floor_and_skips_memes_and_terminal_items(self):
        for item_id, fit in enumerate([0.9, 0.8, 0.7, 0.6, 0.4], start=1):
            self.held(item_id, {"agents": fit, "site": 0.05})
        self.held(6, {"agents": 0.95, "site": 0.9}, intent="non-technical")
        self.held(7, {"agents": 0.99, "site": 0.0}, status="rejected")
        self.held(8, {"agents": 0.05, "site": 0.55}, disposition="no_fit")

        plan = intake.route_top(k=3, floor=0.5, dry_run=True)
        self.assertEqual(plan, {"agents": [(1, 0.9), (2, 0.8), (3, 0.7)], "site": [(8, 0.55)]})
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_workstreams").fetchone()[0], 0)

        intake.route_top(k=3, floor=0.5)
        routable = {(row["id"], row["workstream_slug"]) for row in db.get_unrouted_items()}
        self.assertEqual(routable, {(1, "agents"), (2, "agents"), (3, "agents"), (8, "site")})
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT disposition FROM intake_reviews WHERE item_id=1").fetchone()[0], "approved")
            self.assertEqual(conn.execute("SELECT one_liner FROM items WHERE id=1").fetchone()[0], "Item 1")

    def test_rerun_counts_already_routed_items_toward_k(self):
        for item_id, fit in enumerate([0.9, 0.8, 0.7], start=1):
            self.held(item_id, {"agents": fit, "site": 0.0})
        intake.route_top(k=2, floor=0.5)
        self.assertEqual(intake.route_top(k=2, floor=0.5, dry_run=True), {})


if __name__ == "__main__":
    unittest.main()


class ParkedProjectTest(RouteTopTest):
    def test_a_project_switched_off_in_config_gets_nothing_and_the_default_cap_is_25(self):
        db.seed_workstreams([{"slug": "site", "name": "Site", "backlog_path": "s.md", "active": False}])
        for item_id in range(1, 31):
            self.held(item_id, {"agents": 0.9, "site": 0.9})
        plan = intake.route_top(dry_run=True)
        self.assertEqual(list(plan), ["agents"])
        self.assertEqual(len(plan["agents"]), 25)
        db.seed_workstreams([{"slug": "site", "name": "Site", "backlog_path": "s.md"}])
        self.assertIn("site", intake.route_top(dry_run=True))
