"""Stacks: group queued items by theme so one decision can cover several; flag near-duplicates."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, stacks, useful

THEMES = [{"label": "Agent skills", "description": "Writing or using skills for coding agents."},
          {"label": "Hiring", "description": "Getting hired or hiring."}]
ITEM_THEME = {1: "Agent skills", 2: "Agent skills", 3: "Agent skills", 4: "Hiring", 5: "Hiring", 6: "none"}


def jev(duplicate_pair=(1, 2), unsure_item=None):
    def answer(request, **_):
        answers = {}
        for key, question in request["questions"].items():
            if question["type"] == "choice":
                item_id = int(key.split("_")[1])
                choice = ITEM_THEME[item_id]
                p = 0.4 if item_id == unsure_item else 0.95
                probabilities = {k: (p if k == choice else (1 - p) / (len(question["criteria"]) - 1)) for k in question["criteria"]}
                answers[key] = {"type": "choice", "choice": choice, "confidence": p, "probabilities": probabilities}
            else:
                a, b = (int(x) for x in key.split("_")[1:])
                answers[key] = {"type": "noul", "noul": 0.9 if {a, b} == set(duplicate_pair) else 0.05}
        return {"model": useful.MODEL, "answers": answers, "usage": {"input_tokens": 50, "output_tokens": 5}}
    return answer


class StacksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "proj", "name": "Proj", "backlog_path": "b.md"}])
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, fit in [(1, 0.7), (2, 0.9), (3, 0.6), (4, 0.8), (5, 0.5), (6, 0.9)]:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) VALUES(?,'x',?,?,?,'u','queued','2026-09-30')",
                             (item_id, str(item_id), f"Post {item_id}", f"Body {item_id}"))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT ?,id,?,1 FROM workstreams", (item_id, fit))
        self.namer_calls = []

    def namer(self, snippets):
        self.namer_calls.append(snippets)
        return THEMES

    def stack_of(self, item_id):
        return next(item.get("stack") for item in db.get_triage_queue() if item["id"] == item_id)

    def test_three_or_more_items_form_a_stack_ranked_by_fit(self):
        with patch.object(useful, "call_typesafe", side_effect=jev(duplicate_pair=())):
            result = stacks.build(allow_upload=True, namer=self.namer)
        self.assertEqual(result["stacks"], 1)
        self.assertEqual(len(self.namer_calls[0]), 6)
        self.assertEqual([self.stack_of(i)["rank"] for i in (2, 1, 3)], [0, 1, 2])
        self.assertEqual(self.stack_of(2)["theme"], "Agent skills")
        self.assertEqual(self.stack_of(2)["description"], "Writing or using skills for coding agents.")
        self.assertIsNone(self.stack_of(4))  # only two hiring posts: no stack
        self.assertIsNone(self.stack_of(6))  # no theme fits

    def test_near_duplicates_point_at_the_better_ranked_item(self):
        with patch.object(useful, "call_typesafe", side_effect=jev(duplicate_pair=(1, 2))):
            stacks.build(allow_upload=True, namer=self.namer)
        self.assertEqual(self.stack_of(1)["duplicate_of"], 2)
        self.assertIsNone(self.stack_of(2)["duplicate_of"])

    def test_unsure_theme_assignment_stays_single(self):
        with patch.object(useful, "call_typesafe", side_effect=jev(duplicate_pair=(), unsure_item=3)):
            result = stacks.build(allow_upload=True, namer=self.namer)
        self.assertEqual(result["stacks"], 0)
        self.assertIsNone(self.stack_of(1))

    def test_rebuild_replaces_old_stacks(self):
        with patch.object(useful, "call_typesafe", side_effect=jev(duplicate_pair=())):
            stacks.build(allow_upload=True, namer=self.namer)
            with sqlite3.connect(db.DB_PATH) as conn:
                conn.execute("UPDATE items SET status='rejected' WHERE id IN (1,2)")
            stacks.build(allow_upload=True, namer=self.namer)
        self.assertIsNone(self.stack_of(3))


if __name__ == "__main__":
    unittest.main()
