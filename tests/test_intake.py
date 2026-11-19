"""Automatic routing safety and bounded typed-intake integration."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib import db, route


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        self.addCleanup(patch.stopall)
        patch.object(db, "DB_PATH", str(self.path)).start()
        patch.object(db, "is_live", return_value=True).start()
        db.init_db()
        self.workstreams = [{"slug": "reader", "name": "Offline reading", "keywords": ["offline", "notes"],
                             "backlog_path": str(Path(self.temp.name) / "beacon-backlog.md")}]
        db.seed_workstreams(self.workstreams)

    def item(self, item_id, *, status="new", body="Search notes offline", source="manual", title="Notes"):
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,synced_at) VALUES(?,?,?,?,?,?,'',?,'2026-09-20')",
                         (item_id, source, str(item_id), title, body, f"https://example.com/{item_id}", status))

    def test_route_never_resurrects_terminal_items_or_inactive_destinations(self):
        for item_id, status in [(1, "stale"), (2, "rejected"), (3, "new")]:
            self.item(item_id, status=status)
            db.mark_classified(item_id, "build-with", [], [{"slug": "reader", "confidence": 1}], "2026-09-20")
        self.assertEqual([item["id"] for item in db.get_unrouted_items()], [3])
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE workstreams SET active=0")
        with redirect_stdout(io.StringIO()):
            count = route.route_items()
        self.assertEqual(count, 0)
        with closing(db.get_connection()) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT status FROM items ORDER BY id")], ["stale", "rejected", "new"])
        self.assertFalse(Path(self.workstreams[0]["backlog_path"]).exists())

    def test_routing_preserves_reviewed_tried_and_adopted_lifecycle(self):
        for item_id, status in [(1, "reviewed"), (2, "tried"), (3, "adopted")]:
            self.item(item_id, status=status)
            db.mark_classified(item_id, "build-with", [], [{"slug": "reader", "confidence": 1}], "2026-09-20")
        with redirect_stdout(io.StringIO()):
            route.route_items()
        with closing(db.get_connection()) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT status FROM items ORDER BY id")], ["reviewed", "tried", "adopted"])

    def test_failed_backlog_write_stays_pending_and_can_be_retried(self):
        self.item(1)
        db.mark_classified(1, "build-with", [], [{"slug": "reader", "confidence": 1}], "2026-09-20")
        with patch.object(route, "atomic_write", side_effect=OSError("disk full")), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                route.route_items()
        self.assertEqual([item["id"] for item in db.get_unrouted_items()], [1])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(route.route_items(), 1)
        self.assertIn("Notes", Path(self.workstreams[0]["backlog_path"]).read_text())

    def test_typed_intake_routes_only_strong_results_and_parks_others(self):
        from lib import intake, useful
        for item_id in range(1, 4):
            self.item(item_id)
        def answer(request, **kwargs):
            answers = {}
            for key, question in request["questions"].items():
                if question["type"] == "noul":
                    item_id = int(key.split("_")[1])
                    answers[key] = {"type": "noul", "noul": {1: 0.999, 2: 0.5, 3: 0.001}[item_id]}
                else:
                    answers[key] = {"type": "choice", "choice": "build-with", "confidence": 0.99,
                                    "probabilities": {k: float(k == "build-with") for k in question["criteria"]}}
            return {"model": useful.MODEL, "answers": answers, "usage": {"input_tokens": 20, "output_tokens": 10}}
        with patch.object(useful, "call_typesafe", side_effect=answer) as network:
            result = intake.classify_pending(self.workstreams, limit=3, allow_upload=True, semantic_routing=True)
            again = intake.classify_pending(self.workstreams, limit=3, allow_upload=True, semantic_routing=True)
        self.assertEqual(result["classified"], 1)
        self.assertEqual(result["held"], 2)
        self.assertEqual(again["live_model_calls"], 0)
        self.assertEqual(network.call_count, 3)
        with redirect_stdout(io.StringIO()):
            route.route_items()
        self.assertIn("Notes", Path(self.workstreams[0]["backlog_path"]).read_text())
        with closing(db.get_connection()) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT status FROM items ORDER BY id")], ["queued", "new", "new"])
        self.assertEqual({r["id"] for r in intake.review_queue(self.path)}, {2, 3})
        self.assertEqual(db.get_unclassified_items(), [])

    def test_intake_failure_stale_source_and_terminal_item_fail_closed(self):
        from lib import intake, useful
        self.item(1)
        with patch.object(useful, "call_typesafe", side_effect=ValueError("service unavailable")):
            with self.assertRaises(ValueError):
                intake.classify_pending(self.workstreams, allow_upload=True)
        with closing(db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM intake_reviews").fetchone()[0], 0)
        before = intake.source_hash(db.get_unclassified_items()[0])
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE items SET body='Changed' WHERE id=1")
        decision = {"id": 1, "assessment": "review", "reasons": ["Unclear"]}
        with self.assertRaisesRegex(ValueError, "changed"):
            intake.persist(decision, before, self.workstreams)
        db.update_item_status(1, "rejected")
        self.assertEqual(db.get_unclassified_items(), [])

    def test_content_change_releases_held_item_but_unchanged_sync_does_not(self):
        from lib import intake
        self.item(1)
        item = db.get_unclassified_items()[0]
        intake.persist({"id": 1, "assessment": "review", "reasons": ["Unclear"]}, intake.source_hash(item), self.workstreams)
        self.assertEqual(db.get_unclassified_items(), [])
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE items SET body=body,synced_at='2026-09-21' WHERE id=1")
        self.assertEqual(db.get_unclassified_items(), [])
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE items SET body='A changed source' WHERE id=1")
        self.assertEqual([row["id"] for row in db.get_unclassified_items()], [1])

    def test_policy_never_treats_advisory_confidence_as_auto_route_authority(self):
        from lib import intake
        row = {"id": 1, "method": "typesafe", "assessment": "proposed", "workstreams": ["reader"],
               "intent": "build-with", "intent_judgment": {"confidence": 1, "choice": "build-with", "probabilities": {"build-with": 1}},
               "destination_probabilities": {"reader": 0.999}, "reasons": []}
        self.assertEqual(intake.routing_decision(row, semantic_routing=False)["assessment"], "review")
        self.assertEqual(intake.routing_decision(row, semantic_routing=True)["assessment"], "approved")
        row["destination_probabilities"]["reader"] = 0.84
        self.assertEqual(intake.routing_decision(row, semantic_routing=True)["assessment"], "review")

    def test_changed_approved_source_stays_visible_for_review_not_active_routing(self):
        from lib import intake
        self.item(1)
        item = db.get_unclassified_items()[0]
        intake.persist({"id": 1, "assessment": "approved", "intent": "build-with", "workstreams": ["reader"]},
                       intake.source_hash(item), self.workstreams)
        db.update_item_status(1, "queued")
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE items SET body='Changed after routing' WHERE id=1")
        self.assertEqual([row["id"] for row in intake.review_queue(self.path)], [1])
        self.assertEqual(db.get_active_items_for_workstream("reader"), [])
        self.assertEqual(db.get_unrouted_items(), [])
        self.assertEqual(db.get_unclassified_items(), [])
        with closing(db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT status FROM items").fetchone()[0], "queued")

    def test_recorded_live_holdout_replays_through_real_database_and_backlog_router(self):
        from lib import intake, intake_eval, review, useful
        report_path = Path(__file__).resolve().parents[1] / "runs/2026-09-20-routing/live-v2.json"
        if not report_path.exists():
            self.skipTest("Optional retained live evaluation is not present; offline safety tests still run.")
        report = intake.load_routing_evaluation(report_path)
        fixture = json.loads(intake_eval.FIXTURE.read_text())
        workstreams = [{**ws, "backlog_path": str(Path(self.temp.name) / ws["slug"] / "beacon-backlog.md")} for ws in fixture["workstreams"]]
        db.seed_workstreams(workstreams)
        expected = {}
        for index, case in enumerate(fixture["cases"]):
            if case["split"] != "held_out":
                continue
            item_id = index + 1
            self.item(item_id, title=case["title"], body=case["body"])
            item = db.get_unclassified_items(limit=20)
            item = next(row for row in item if row["id"] == item_id)
            snapshot = {**item, "excerpt": case["body"]}
            plan = review.prepare_triage([snapshot], workstreams)
            recorded = next(row for row in report["cases"] if row["id"] == case["id"])
            decision = intake.routing_decision(review.triage_result(plan, recorded["response"])["decisions"][0], semantic_routing=True)
            intake.persist(decision, intake.source_hash(item), workstreams, summary={"human_read": False})
            if decision["assessment"] == "approved":
                expected[item_id] = decision["workstreams"]
        with patch.object(useful, "call_typesafe") as network, redirect_stdout(io.StringIO()):
            route.route_items()
        network.assert_not_called()
        self.assertEqual(len(expected), 5)
        with closing(db.get_connection()) as conn:
            routed = list(conn.execute("SELECT i.id,w.slug FROM items i JOIN item_workstreams iw ON iw.item_id=i.id JOIN workstreams w ON w.id=iw.workstream_id WHERE iw.routed=1"))
        self.assertEqual({(r[0], r[1]) for r in routed}, {(i, slug) for i, slugs in expected.items() for slug in slugs})
        for item_id, slugs in expected.items():
            title = fixture["cases"][item_id-1]["title"]
            for slug in slugs:
                self.assertIn(title, (Path(self.temp.name) / slug / "beacon-backlog.md").read_text())


if __name__ == "__main__":
    unittest.main()
