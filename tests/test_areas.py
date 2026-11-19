"""Areas: lifestyle shelves (Style, Food, ...) that saves can land on without a project or an agent."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import areas, db, useful

AREAS = [{"slug": "food", "name": "Food", "description": "Recipes and restaurants."},
         {"slug": "style", "name": "Style", "description": "Clothes and personal style."}]


def answers(choices):
    """Jev's reply: item id -> chosen area, with that choice at 0.9."""
    def call(request, **_):
        out = {}
        for key, question in request["questions"].items():
            choice = choices[int(key.split("_")[1])]
            probabilities = {criterion: (0.9 if criterion == choice else 0.1 / (len(question["criteria"]) - 1))
                             for criterion in question["criteria"]}
            out[key] = {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": 0.8}
        return {"model": useful.MODEL, "answers": out, "usage": {"input_tokens": 50, "output_tokens": 5}}
    return call


class AreasTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        patcher = patch.object(db, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "proj", "name": "Proj", "backlog_path": str(Path(self.temp.name) / "b.md")}])
        db.seed_workstreams(AREAS, kind="area")
        with sqlite3.connect(self.path) as conn:
            for item_id, status in [(1, "new"), (2, "new"), (3, "new"), (4, "rejected"), (5, "queued")]:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) VALUES(?,'x',?,?,?,?,?,'2026-10-09')",
                             (item_id, str(item_id), f"Post {item_id}", f"Body {item_id}", f"https://x.com/a/{item_id}", status))

    def test_held_saves_land_on_their_area_and_show_up_in_triage_as_areas(self):
        with patch.object(useful, "call_typesafe", side_effect=answers({1: "food", 2: "none", 3: "style"})) as call:
            result = areas.assign(AREAS, limit=10, allow_upload=True)
        self.assertEqual((result["reviewed"], result["assigned"]), (3, 2))
        # Only held saves are asked about; rejected and already-queued ones are not.
        asked = {int(key.split("_")[1]) for key in call.call_args.args[0]["questions"]}
        self.assertEqual(asked, {1, 2, 3})
        queue = {item["id"]: item for item in db.get_triage_queue()}
        self.assertEqual(set(queue), {1, 3})
        self.assertEqual(queue[1]["projects"][0], {"slug": "food", "name": "Food", "fit": 0.9, "kind": "area"})
        self.assertEqual(queue[1]["stack"]["theme"], "Food")  # one stack per shelf
        # Areas are not projects: no backlog, no project scoring, no agent.
        self.assertEqual([ws["slug"] for ws in db.get_workstreams()], ["proj"])

    def test_each_area_fills_to_its_cap_and_reviewed_saves_are_not_asked_again(self):
        with patch.object(useful, "call_typesafe", side_effect=answers({1: "food", 2: "food", 3: "food"})):
            first = areas.assign(AREAS, limit=10, allow_upload=True, k=2)
        self.assertEqual(first["assigned"], 2)
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE items SET status='adopted' WHERE id=1")  # Keep clears a slot
        with patch.object(useful, "call_typesafe") as call:
            second = areas.assign(AREAS, limit=10, allow_upload=True, k=2)
        call.assert_not_called()
        self.assertEqual((second["reviewed"], second["assigned"]), (0, 1))
