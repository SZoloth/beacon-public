"""Offline state-machine tests for authored future-use conditions."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import conditions, useful


class ConditionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "conditions.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        conditions.init_db(self.conn)
        self.addCleanup(self.conn.close)
        conditions.save_condition(
            self.conn,
            condition_id="condition-1",
            project_slug="demo",
            title="Keep this for later",
            definition={"when": "a future project event makes this useful"},
            authored_by="user",
            authored_at="2026-09-20T12:00:00+00:00",
        )

    def evaluate(self, item, event, evaluator):
        event = {"project_slug": "demo", **event}
        return conditions.evaluate_event(
            self.conn,
            "condition-1",
            item,
            event,
            evaluator,
            evaluated_at="2026-09-20T12:01:00+00:00",
        )

    def test_conditions_are_explicitly_authored_and_hash_all_fields(self):
        condition = conditions.get_condition(self.conn, "condition-1")
        self.assertEqual(condition["authored_by"], "user")
        self.assertEqual(condition["definition"], {"when": "a future project event makes this useful"})
        self.assertEqual(len(condition["condition_hash"]), 64)
        with self.assertRaisesRegex(ValueError, "authored_by"):
            conditions.save_condition(
                self.conn,
                condition_id="bad",
                project_slug="demo",
                title="Not authored",
                definition={"when": "never"},
                authored_by=" ",
            )

    def test_unchanged_event_skips_callback_but_any_full_hash_change_rechecks(self):
        calls = []

        def evaluator(condition, item, event):
            calls.append((condition, item, event))
            return "unknown"

        item = {"id": 7, "status": "queued", "title": "A", "body": "first"}
        event = {"id": "event-1", "kind": "opened", "payload": {"x": 1}}
        first = self.evaluate(item, event, evaluator)
        second = self.evaluate(item, event, evaluator)
        self.assertEqual(first["state"], "unknown")
        self.assertTrue(second["skipped"])
        self.assertEqual(len(calls), 1)

        changed_item = {**item, "body": "changed"}
        self.evaluate(changed_item, event, evaluator)
        changed_event = {**event, "payload": {"x": 2}}
        changed = self.evaluate(changed_item, changed_event, evaluator)
        self.assertFalse(changed["skipped"])
        self.assertEqual(len(calls), 3)
        self.assertNotEqual(first["item_hash"], changed["item_hash"])
        self.assertNotEqual(first["event_hash"], changed["event_hash"])

        conditions.save_condition(
            self.conn,
            condition_id="condition-1",
            project_slug="demo",
            title="Keep this for later",
            definition={"when": "a different future project event makes this useful"},
            authored_by="user",
            authored_at="2026-09-20T12:00:00+00:00",
        )
        condition_changed = self.evaluate(changed_item, {"id": "e3"}, evaluator)
        self.assertEqual(len(calls), 4)
        self.assertNotEqual(first["condition_hash"], condition_changed["condition_hash"])

    def test_met_transitions_emit_one_local_suggestion_each(self):
        answers = iter(["unknown", "met", "met", "not-met", "met"])
        calls = []

        def evaluator(condition, item, event):
            calls.append(event["id"])
            return next(answers)

        item = {"id": 7, "status": "queued", "title": "A"}
        first = self.evaluate(item, {"id": "e1"}, evaluator)
        met = self.evaluate(item, {"id": "e2"}, evaluator)
        same = self.evaluate(item, {"id": "e2"}, evaluator)
        still_met = self.evaluate(item, {"id": "e3"}, evaluator)
        reverted = self.evaluate(item, {"id": "e4"}, evaluator)
        met_again = self.evaluate(item, {"id": "e5"}, evaluator)

        self.assertEqual(first["state"], "unknown")
        self.assertEqual(met["state"], "met")
        self.assertEqual(len(met["suggestions"]), 1)
        self.assertTrue(same["skipped"])
        self.assertEqual(len(still_met["suggestions"]), 0)
        self.assertEqual(reverted["state"], "reverted")
        self.assertEqual(met_again["state"], "met")
        self.assertEqual(len(met_again["suggestions"]), 1)
        self.assertEqual(calls, ["e1", "e2", "e3", "e4", "e5"])
        self.assertEqual(len(conditions.list_suggestions(self.conn)), 2)
        self.assertEqual(len(conditions.list_suggestions(self.conn, active_only=True)), 1)

    def test_stale_or_rejected_items_never_resurrect_or_suggest(self):
        calls = []

        def evaluator(condition, item, event):
            calls.append(event["id"])
            return "met"

        item = {"id": 9, "status": "queued", "title": "A"}
        self.assertEqual(len(self.evaluate(item, {"id": "e1"}, evaluator)["suggestions"]), 1)
        stale = {**item, "status": "stale"}
        reverted = self.evaluate(stale, {"id": "e2"}, evaluator)
        self.assertEqual(reverted["state"], "reverted")
        self.assertEqual(reverted["suggestions"], [])

        active_again = {**item, "status": "queued", "title": "changed"}
        blocked = self.evaluate(active_again, {"id": "e3"}, evaluator)
        self.assertTrue(blocked["skipped"])
        self.assertEqual(blocked["reason"], "terminal_item")
        self.assertEqual(blocked["suggestions"], [])
        self.assertEqual(calls, ["e1"])
        self.assertEqual(conditions.list_suggestions(self.conn, active_only=True), [])

    def test_inactive_and_cross_project_conditions_fail_closed(self):
        event = {"id": "e1", "project_slug": "other"}
        with self.assertRaisesRegex(ValueError, "project_event.project_slug"):
            conditions.evaluate_event(self.conn, "condition-1", {"id": 1, "status": "queued"}, event, lambda *_: "met")
        with self.assertRaisesRegex(ValueError, "project_event"):
            conditions.evaluate_event(self.conn, "condition-1", {"id": 1, "status": "queued"}, {"project_slug": "demo"}, lambda *_: "met")
        with self.conn:
            self.conn.execute("UPDATE future_conditions SET active=0 WHERE condition_id='condition-1'")
        with self.assertRaisesRegex(ValueError, "inactive"):
            self.evaluate({"id": 1, "status": "queued"}, {"id": "e1"}, lambda *_: "met")

    def test_condition_edit_discards_old_state_and_suggestions(self):
        item = {"id": 10, "status": "queued"}
        self.assertEqual(len(self.evaluate(item, {"id": "e1"}, lambda *_: "met")["suggestions"]), 1)
        self.assertEqual(len(conditions.list_suggestions(self.conn, active_only=True)), 1)
        conditions.save_condition(
            self.conn,
            condition_id="condition-1",
            project_slug="demo",
            title="Edited condition",
            definition={"when": "edited"},
            authored_by="user",
            authored_at="2026-09-20T12:02:00+00:00",
        )
        self.assertEqual(conditions.list_suggestions(self.conn), [])
        state_count = self.conn.execute("SELECT COUNT(*) FROM future_condition_states").fetchone()[0]
        self.assertEqual(state_count, 0)

    def test_current_beacon_status_hides_old_active_suggestion(self):
        self.conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, status TEXT)")
        self.conn.execute("INSERT INTO items (id, status) VALUES (11, 'queued')")
        self.conn.commit()
        self.evaluate({"id": 11, "status": "queued"}, {"id": "e1"}, lambda *_: "met")
        self.assertEqual(len(conditions.list_suggestions(self.conn, active_only=True)), 1)
        self.conn.execute("UPDATE items SET status='stale' WHERE id=11")
        self.conn.commit()
        self.assertEqual(conditions.list_suggestions(self.conn, active_only=True), [])

    def test_condition_or_item_change_during_callback_is_discarded(self):
        item = {"id": 12, "status": "queued"}

        def changes_condition(*_):
            conditions.save_condition(
                self.conn,
                condition_id="condition-1",
                project_slug="demo",
                title="Changed while waiting",
                definition={"when": "changed"},
                authored_by="user",
            )
            return "met"

        with self.assertRaisesRegex(ValueError, "changed during evaluation"):
            self.evaluate(item, {"id": "e1"}, changes_condition)
        self.assertEqual(conditions.list_suggestions(self.conn), [])

    def test_typesafe_request_and_explicit_evaluator_are_bounded(self):
        condition = conditions.get_condition(self.conn, "condition-1")
        item = {"id": 13, "title": "Synthetic item", "body": "met evidence", "secret": "omit"}
        event = {"id": "synthetic-met", "project_slug": "demo", "source_path": "/synthetic/event.json"}
        request = conditions.build_typesafe_request(condition, item, event)
        self.assertEqual(request["model"], useful.MODEL)
        self.assertEqual(set(request["questions"]), {"condition_state"})
        self.assertNotIn("secret", request["state"]["item"])
        self.assertEqual(request["state"]["project_event"]["source_path"], "/synthetic/event.json")
        answer = {"type": "choice", "choice": "met", "confidence": 0.9,
                  "probabilities": {key: float(key == "met") for key in conditions.CONDITION_CRITERIA}}
        response = {"model": useful.MODEL, "answers": {"condition_state": answer},
                    "usage": {"input_tokens": 10, "output_tokens": 4}}
        with patch.object(useful, "call_typesafe", return_value=response) as call:
            evaluation = conditions.make_typesafe_evaluator(allow_upload=True)(condition, item, event)
        call.assert_called_once()
        self.assertEqual(evaluation["state"], "met")
        self.assertEqual(evaluation["usage"], {"input_tokens": 10, "output_tokens": 4})
        self.assertEqual(len(evaluation["request_sha256"]), 64)

    def test_older_inflight_event_cannot_overwrite_newer_condition_state(self):
        item = {"id": 14, "status": "queued"}
        def newer_event(*_):
            self.evaluate(item, {"id": "newer"}, lambda *_: "met")
            return "not-met"
        with self.assertRaisesRegex(ValueError, "State changed during evaluation"):
            self.evaluate(item, {"id": "older"}, newer_event)
        self.assertEqual(len(conditions.list_suggestions(self.conn, active_only=True)), 1)

    def test_invalid_evaluation_does_not_leave_a_write_transaction_open(self):
        with self.assertRaises(ValueError):
            self.evaluate({"id": 15, "status": "queued"}, {"id": "e1"}, lambda *_: "bogus")
        self.assertFalse(self.conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
