import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lib import curator, db, intake, review, save_review, useful


def answer(request):
    answers = {}
    for key, question in request["questions"].items():
        if question["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.95 if key.endswith("_0") else 0.01}
        else:
            choice = "reference" if "reference" in question["criteria"] else "distinct"
            answers[key] = {"type": "choice", "choice": choice, "confidence": 1.0,
                            "probabilities": {k: float(k == choice) for k in question["criteria"]}}
    return {"model": useful.MODEL, "answers": answers, "usage": {"input_tokens": 50, "output_tokens": 20}}


class TypedBatchesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "batch.db"
        self.context = Path(self.temp.name) / "context.json"
        self.now = datetime.now(timezone.utc)
        self.workstreams = [{"slug": f"project-{i}", "name": f"Project {i}", "keywords": ["some", "words"], "backlog_path": str(Path(self.temp.name) / f"backlog-{i}.md")} for i in range(31)]
        self.db_patch = patch.object(db, "DB_PATH", str(self.path))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()
        db.seed_workstreams(self.workstreams)
        with sqlite3.connect(self.path) as conn:
            conn.executemany("INSERT INTO items(id,source,external_id,title,body,status,synced_at) VALUES(?,'manual',?,'Notes','Read cached notes offline.','new',?)", [(i, str(i), self.now.isoformat()) for i in range(1, 5)])
        self.context.write_text(json.dumps({"version": 1, "task": "Read offline notes", "efforts": [], "conditions": [
            {"id": "travel", "project": "reader", "owner": "Fixture", "source_ref": "synthetic:travel",
             "when": "Need offline reading", "observed_at": (self.now - timedelta(hours=1)).isoformat(),
             "expires_at": (self.now + timedelta(days=1)).isoformat()}]}))

    def test_31_destinations_size_split_preserves_all_questions_and_singleton_results(self):
        items = review.read_items(self.path, item_ids=[1, 2, 3, 4])
        plans = review.prepare_triage_batches(items, self.workstreams)
        self.assertGreater(len(plans), 1)
        # Per-question project context makes a 31-project item ~12 KB, so the 24 KB cap holds one item.
        self.assertLessEqual(len(plans), 4)
        actual = []
        for plan in plans:
            request = plan["request"]
            self.assertLessEqual(len(useful.encode_request(request)), 24000)
            self.assertEqual(len({q["instructions"]["project"]["slug"] for q in request["questions"].values() if q["type"] == "noul"}), 31)
            for item in request["state"]["items"]:
                self.assertEqual(sum(k.startswith(f"fit_{item['id']}_") for k in request["questions"]), 31)
            actual.extend(review.triage_result(plan, answer(request))["decisions"])
        expected = []
        for plan in review.prepare_triage_batches(items, self.workstreams, batch_size=1):
            expected.extend(review.triage_result(plan, answer(plan["request"]))["decisions"])
        self.assertEqual(actual, expected)

    def test_intake_batches_and_checks_each_item_before_persisting(self):
        with patch.object(useful, "call_typesafe", side_effect=lambda r, **_: answer(r)) as call:
            result = intake.classify_pending(self.workstreams, limit=4, allow_upload=True)
        self.assertEqual(result["held"], 4)
        self.assertLessEqual(call.call_count, 4)
        self.assertEqual(result["summary_generation_calls"], 0)
        self.assertEqual(len(intake.review_queue(self.path)), 4)

    def test_intake_processes_more_than_twenty_items_in_one_run(self):
        with sqlite3.connect(self.path) as conn:
            conn.executemany("INSERT INTO items(id,source,external_id,title,body,status,synced_at) VALUES(?,'manual',?,'Notes','Read cached notes offline.','new',?)", [(i, str(i), self.now.isoformat()) for i in range(5, 46)])
        with patch.object(useful, "call_typesafe", side_effect=lambda r, **_: answer(r)):
            result = intake.classify_pending(self.workstreams, limit=45, allow_upload=True)
        self.assertEqual(result["held"], 45)

    def test_since_skips_old_posts(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE items SET created_at='2017-06-27T20:48:18+00:00' WHERE id IN (1,2)")
            conn.execute("UPDATE items SET created_at='Wed Nov 17 23:16:10 +0000 2021' WHERE id=3")
            conn.execute("UPDATE items SET created_at='2026-09-29T00:00:00+00:00' WHERE id=4")
        with patch.object(useful, "call_typesafe", side_effect=lambda r, **_: answer(r)):
            result = intake.classify_pending(self.workstreams, limit=4, allow_upload=True, since="2026-07-01")
        self.assertEqual(result["held"], 1)

    def test_one_malformed_response_skips_that_item_and_continues(self):
        def flaky(request, **_):
            response = answer(request)
            if any(key == "intent_2" for key in request["questions"]):
                response["answers"]["intent_2"]["probabilities"] = {k: 0.0 for k in response["answers"]["intent_2"]["probabilities"]}
            return response
        with patch.object(useful, "call_typesafe", side_effect=flaky):
            result = intake.classify_pending(self.workstreams, limit=4, allow_upload=True, batch_size=1)
        self.assertEqual(result["held"], 3)
        self.assertEqual(result["errors"], 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM intake_reviews WHERE item_id=2").fetchone()[0], 0)

    def test_no_fit_items_keep_their_intent_for_later_search(self):
        def nothing_fits(request, **_):
            response = answer(request)
            for key, value in response["answers"].items():
                if value["type"] == "noul":
                    value["noul"] = 0.01
            return response
        with patch.object(useful, "call_typesafe", side_effect=nothing_fits):
            intake.classify_pending(self.workstreams, limit=1, allow_upload=True)
        with sqlite3.connect(self.path) as conn:
            disposition, intent = conn.execute("SELECT r.disposition, i.intent FROM intake_reviews r JOIN items i ON i.id=r.item_id").fetchone()
        self.assertEqual((disposition, intent), ("no_fit", "reference"))

    def test_intake_discards_stale_item_without_overwriting_it(self):
        def changed(request, **kwargs):
            with sqlite3.connect(self.path) as conn:
                conn.execute("UPDATE items SET body='changed' WHERE id=2")
            return answer(request)
        with patch.object(useful, "call_typesafe", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                intake.classify_pending(self.workstreams, limit=4, allow_upload=True)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM intake_reviews WHERE item_id=2").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT body FROM items WHERE id=2").fetchone()[0], "changed")

    def test_batched_triage_actual_cli_preview_replay(self):
        command = [sys.executable, "beacon.py", "triage", "1", "2", "3", "4", "--batched", "--db", str(self.path)]
        preview = subprocess.run(command + ["--preview", "--json"], capture_output=True, text=True, check=True)
        requests = json.loads(preview.stdout)["requests"]
        self.assertTrue(all(len({q["instructions"]["project"]["slug"] for q in r["questions"].values() if q["type"] == "noul"}) == 31 for r in requests))
        replay = [{"request_sha256": review.request_hash(r), "response": answer(r)} for r in requests]
        answers = Path(self.temp.name) / "triage-answers.json"
        answers.write_text(json.dumps(replay))
        before = self.path.read_bytes()
        result = subprocess.run(command + ["--answers", str(answers), "--json"], capture_output=True, text=True, check=True)
        self.assertEqual(len(json.loads(result.stdout)["decisions"]), 4)
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_batches_match_singletons_and_cli_replay(self):
        preview = save_review.generate_many([1, 2, 3, 4], self.path, self.context, preview=True)
        self.assertEqual(len(preview["requests"]), 1)
        replay = [{"request_sha256": review.request_hash(r), "response": answer(r)} for r in preview["requests"]]
        result = save_review.generate_many([1, 2, 3, 4], self.path, self.context, replay=replay)
        self.assertEqual(result["replayed_calls"], 1)
        self.assertEqual(result["usage"]["input_tokens"], 50)
        self.assertTrue(all(row["future_use"]["state"] == "proposed" for row in result["reviews"]))
        for row in result["reviews"]:
            with patch.object(useful, "call_typesafe", side_effect=lambda r, **_: answer(r)):
                single = save_review.generate(row["item"]["id"], self.path, self.context, provider="typesafe", allow_upload=True)
            self.assertEqual(row["future_use"], single["future_use"])
        answers = Path(self.temp.name) / "answers.json"
        answers.write_text(json.dumps(replay))
        command = [sys.executable, "beacon.py", "consider-batch", "1", "2", "3", "4", "--db", str(self.path), "--context", str(self.context), "--answers", str(answers)]
        before = self.path.read_bytes()
        output = subprocess.run(command + ["--json"], text=True, capture_output=True, check=True)
        self.assertEqual(len(json.loads(output.stdout)["reviews"]), 4)
        output = subprocess.run(command, text=True, capture_output=True, check=True)
        self.assertIn("You will want this when Need offline reading", output.stdout)
        self.assertEqual(before, self.path.read_bytes())

    def test_curator_uses_one_batch_and_local_transactional_holds(self):
        with patch.object(useful, "call_typesafe", side_effect=lambda r, **_: answer(r)) as call:
            result = curator.review_new_revisions(self.path, self.context, limit=4, provider="typesafe", allow_upload=True)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["database_writes"], 4)
        self.assertEqual(result["usage"]["input_tokens"], 50)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM save_reviews WHERE status='held'").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items WHERE status='new'").fetchone()[0], 4)

    def test_batch_rejects_changed_context_and_preflights_budget(self):
        def changed(request, **kwargs):
            self.context.write_text(self.context.read_text() + " ")
            return answer(request)
        with patch.object(useful, "call_typesafe", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                save_review.generate_many([1, 2], self.path, self.context, allow_upload=True)
        with patch.object(useful, "call_typesafe") as network:
            with self.assertRaises(ValueError):
                save_review.generate_many([1], self.path, self.context, max_calls=0, allow_upload=True)
        network.assert_not_called()

    def test_curator_rechecks_context_expiry_after_provider(self):
        with patch.object(curator, "datetime") as time_source:
            time_source.now.return_value = self.now
            def expires(request, **kwargs):
                time_source.now.return_value = self.now + timedelta(days=2)
                return answer(request)
            with patch.object(useful, "call_typesafe", side_effect=expires):
                result = curator.review_new_revisions(self.path, self.context, limit=2, provider="typesafe", allow_upload=True)
        self.assertEqual(result["network_calls"], 1)
        self.assertEqual(len(result["failures"]), 2)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM save_reviews WHERE status='held'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
