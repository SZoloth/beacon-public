"""Generated Library layouts: only known saves, nothing goes missing, weak fits keep the plain grid."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import compose, db, server


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        patcher = patch.object(db, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        compose._cache.clear()
        with sqlite3.connect(self.path) as conn:
            for item_id in range(1, 9):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,raw_json,synced_at) "
                             "VALUES(?,'github',?,?,?,?,'new','{}','2026-11-12')",
                             (item_id, str(item_id), f"o/chair-{item_id}", f"Chair number {item_id}",
                              f"https://github.com/o/{item_id}"))
        self.ids = [8, 7, 6, 5, 4, 3, 2, 1]
        self.prompts = []

    def writer(self, answer):
        def write(text):
            self.prompts.append(text)
            return answer, 0.002
        return write

    def test_a_layout_uses_only_known_saves_once_and_keeps_the_rest_in_other_matches(self):
        answer = {"fit": "good", "pick": {"save": 1, "why": "the  exact chair"},
                  "groups": [{"title": "Wood", "saves": [1, 2, 3, 99]},  # 1 is the pick, 99 isn't a save
                             {"title": "Metal", "saves": [4, 4, 5]},
                             {"title": "wood", "saves": [6, 7]},  # a repeated title
                             {"title": "Lonely", "saves": [6]}],  # one save is not a group
                  "next": ["only red ones", "Metal", "only red ones", "Search 'armchair' for lounge seats",
                           "Filter to the ideas shelf", '"Windsor".'], "off": [8]}
        result = compose.compose("chairs", self.ids, writer=self.writer(answer))
        layout = result["layout"]
        self.assertEqual(layout["pick"], {"id": 8, "why": "the exact chair"})
        self.assertEqual(layout["groups"], [{"title": "Wood", "ids": [7, 6]}, {"title": "Metal", "ids": [5, 4]},
                                            {"title": "Other matches", "ids": [3, 2, 1]}])
        self.assertEqual(layout["next"], ["only red ones", "Windsor"])  # instructions and sentences dropped
        self.assertIn("1 | Chair number 8 | github, github | no shelf", self.prompts[0])  # not the repo's dict
        shown = {layout["pick"]["id"]} | {i for g in layout["groups"] for i in g["ids"]}
        self.assertEqual(shown, set(self.ids))  # nothing went missing
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT search, cost_usd FROM compose_log").fetchall(), [("chairs", 0.002)])

    def test_each_save_says_which_searched_words_it_contains_or_that_only_its_picture_matched(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE items SET title = 'o/lamp', body = 'A red lamp' WHERE id = 3")
        compose.compose("red chairs for the kitchen", self.ids, writer=self.writer({"fit": "weak"}))
        lines = {line.split(" | ")[1]: line for line in self.prompts[0].splitlines() if " | " in line}
        self.assertTrue(lines["Chair number 8"].endswith("| has: chairs"))  # "chair" matches "chairs"
        self.assertTrue(lines["A red lamp"].endswith("| has: red"))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE items SET title = 'o/else', body = 'Something else' WHERE id = 2")
        compose.compose("chairs", self.ids, writer=self.writer({"fit": "weak"}))
        self.assertIn("Something else | github, github | no shelf | none | has: none of the words", self.prompts[1])

    def test_weak_fits_and_failed_groupings_keep_the_plain_grid(self):
        weak = {"fit": "weak", "pick": None, "groups": [{"title": "A", "saves": [1, 2]}, {"title": "B", "saves": [3, 4]}],
                "next": [], "off": []}
        self.assertIsNone(compose.compose("crosshair", self.ids, writer=self.writer(weak))["layout"])
        one = {"fit": "good", "pick": None, "groups": [{"title": "All", "saves": [1, 2, 3]}], "next": [], "off": []}
        result = compose.compose("chairs", self.ids, writer=self.writer(one))
        self.assertIsNone(result["layout"])
        self.assertIn("didn't split", result["why"])
        self.assertEqual(compose.compose("chairs", [1, 2], writer=self.writer(one))["why"], "too few saves to group")

    def test_the_same_search_is_answered_from_the_cache_and_a_failure_is_not_cached(self):
        def down(text):
            raise RuntimeError("signed out")
        self.assertIn("didn't answer", compose.compose("chairs", self.ids, writer=down)["why"])
        answer = {"fit": "good", "pick": None, "groups": [{"title": "A", "saves": [1, 2]}, {"title": "B", "saves": [3, 4]}],
                  "next": [], "off": []}
        first = compose.compose("chairs", self.ids, writer=self.writer(answer))
        again = compose.compose("Chairs ", self.ids, writer=self.writer(answer))
        self.assertEqual((first["cached"], again["cached"], len(self.prompts)), (False, True, 1))

    def test_choices_are_recorded_and_summarised(self):
        answer = {"fit": "good", "pick": None, "groups": [{"title": "A", "saves": [1, 2]}, {"title": "B", "saves": [3, 4]}],
                  "next": [], "off": []}
        layout_id = compose.compose("chairs", self.ids, writer=self.writer(answer))["id"]
        status, _ = server.route("POST", "/library/compose/choice", {"id": layout_id, "choice": "kept"}, None)
        self.assertEqual(status, 200)
        with self.assertRaisesRegex(ValueError, "choice must be"):
            compose.choose(layout_id, "loved")
        self.assertEqual(server.route("GET", "/library/compose/summary", {}, None),
                         (200, {"searches": 1, "offered": 1, "kept": 1, "dismissed": 0, "ignored": 0, "cost_usd": 0.002}))

    def test_bad_requests_are_refused(self):
        with self.assertRaisesRegex(ValueError, "ids must be"):
            compose.compose("chairs", ["1"])
        with self.assertRaisesRegex(ValueError, "search is empty"):
            compose.compose("  ", self.ids)


if __name__ == "__main__":
    unittest.main()
