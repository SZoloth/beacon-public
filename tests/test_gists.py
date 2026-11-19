"""Gists: each queued item gets a plain takeaway and a suggestion, written once."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, gists


class GistsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "proj", "name": "Proj", "backlog_path": "b.md"}])
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id in (1, 2, 3):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) VALUES(?,'x',?,?,?,'u','queued','2026-09-30')",
                             (item_id, str(item_id), f"Post {item_id}", f"hook\n\nThe real trick {item_id}."))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT ?,id,0.8,1 FROM workstreams", (item_id,))
        self.calls = []

    def writer(self, items, projects):
        self.calls.append(([i["id"] for i in items], projects))
        # It answers for an item it wasn't sent and returns one invalid suggestion.
        return [{"item_id": i["id"], "takeaway": f"Trick {i['id']}", "kind": "tip",
                 "suggestion": "maybe" if i["id"] == 3 else "do", "reason": "Fits."} for i in items] + \
               [{"item_id": 99, "takeaway": "x", "kind": "tip", "suggestion": "do", "reason": "x"}], 0.01

    def test_queue_items_get_gists_once_with_full_text_and_project(self):
        result = gists.build([{"slug": "proj", "description": "A sync tool."}], writer=self.writer)
        self.assertEqual(result["written"], 2)
        self.assertEqual(self.calls[0][1], {"proj": "A sync tool."})
        queue = {item["id"]: item for item in db.get_triage_queue()}
        self.assertEqual(queue[1]["gist"], {"takeaway": "Trick 1", "kind": "tip", "suggestion": "do", "reason": "Fits."})
        self.assertNotIn("gist", queue[3])
        gists.build([], writer=self.writer)
        self.assertEqual(self.calls[1][0], [3])  # only the item without a valid gist is retried

    def test_parked_projects_are_not_offered(self):
        gists.build([{"slug": "proj", "description": "A sync tool."},
                     {"slug": "dead", "description": "Killed.", "active": False}], writer=self.writer)
        self.assertEqual(self.calls[0][1], {"proj": "A sync tool."})

    def test_a_reference_suggestion_is_offered_to_haiku_and_stored(self):
        item = db.get_triage_queue()[0]
        with patch.object(gists.briefs, "sonnet", return_value=({"items": []}, 0.0)) as call, \
                patch.object(gists.settings, "owner", return_value="Ada"):
            gists.write_gists([item], {"proj": "A sync tool."})
        prompt, schema = call.call_args.args[0], call.call_args.args[1]
        self.assertIn("reference = an idea", prompt)
        self.assertIn("reference", schema["properties"]["items"]["items"]["properties"]["suggestion"]["enum"])

        def writer(items, projects):
            return [{"item_id": i["id"], "takeaway": "Idea", "kind": "tip", "suggestion": "reference", "reason": "Later."}
                    for i in items], 0.0

        self.assertEqual(gists.build([], writer=writer)["written"], 3)
        self.assertEqual(db.get_triage_queue()[0]["gist"]["suggestion"], "reference")

    def test_text_sent_includes_the_body_past_the_hook(self):
        item = db.get_triage_queue()[0]
        self.assertIn("The real trick", gists._text(item))

    def test_a_timed_out_chunk_does_not_stop_the_next_one(self):
        import subprocess
        calls = []

        def flaky(items, projects):
            calls.append(len(calls))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("claude", 600)
            return self.writer(items, projects)

        with patch.object(gists, "CHUNK", 2):
            result = gists.build([], writer=flaky)
        self.assertEqual((result["failed_chunks"], len(calls)), (1, 2))


class TasteTest(unittest.TestCase):
    """Gists are written by Haiku shown the owner's own decisions on the most similar saves."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "proj", "name": "Proj", "backlog_path": "b.md"}])
        rows = {1: "queued", 10: "adopted", 11: "adopted", 12: "rejected", 13: "rejected", 14: "queued", 15: "queued"}
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, status in rows.items():
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,status_note,synced_at) "
                             "VALUES(?,'x',?,?,?,'u',?,?,'2026-10-13')",
                             (item_id, str(item_id), f"Save {item_id}", f"Body {item_id}", status, "read" if item_id == 11 else None))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT ?,id,0.8,1 FROM workstreams", (item_id,))
            conn.execute("INSERT INTO auto_actions(item_id,action,reason,acted_at) VALUES(11,'read','r','2026-10-13')")
            conn.execute("INSERT INTO auto_actions(item_id,action,reason,acted_at) VALUES(13,'pass','r','2026-10-13')")
            conn.execute("INSERT INTO auto_actions(item_id,action,reason,acted_at,undone_at) VALUES(14,'pass','r','2026-10-12','2026-10-13')")
            conn.execute("INSERT INTO choice_log(item_id,suggestion,choice,source,chosen_at) VALUES(15,'do','rejected','you','2026-10-13')")

    def test_examples_are_only_sams_own_decisions(self):
        got = {e["id"]: e["choice"] for e in gists.examples()}
        # 11 and 13 were Beacon's own actions; 14 is an autopilot pass the owner undid, so they kept it.
        self.assertEqual(got, {10: "kept", 12: "passed", 14: "kept", 15: "passed"})
        self.assertEqual(next(e for e in gists.examples() if e["id"] == 10)["where"], "Proj")

    def test_each_save_is_shown_its_nearest_decisions_but_never_itself(self):
        seen = {}

        def writer(items, projects):
            seen.update({i["id"]: [e["id"] for e in i["taste"]] for i in items})
            return [{"item_id": i["id"], "takeaway": "t", "kind": "tip", "suggestion": "do", "reason": "r"} for i in items], 0.0

        def nearest(saves, pool):
            return {s["id"]: [e for e in pool if e["id"] != s["id"]][:2] for s in saves}

        gists.build([], writer=writer, nearest=nearest)
        self.assertEqual(seen[1], [10, 12])
        self.assertEqual(seen[14], [10, 12])
        self.assertEqual(seen[15], [10, 12])

    def pick_rows(self, *picks):
        """Each pick is (left_id, right_id, choice); one finished round holds them all."""
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id in {i for p in picks for i in p[:2]} - {1, 10, 11, 12, 13, 14, 15}:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) "
                             "VALUES(?,'x',?,?,?,'u','new','2026-11-12')", (item_id, str(item_id), f"Save {item_id}", f"Body {item_id}"))
            round_id = conn.execute("INSERT INTO pick_rounds(axis,pairs_json,created_at) VALUES('working','[]','2026-11-12')").lastrowid
            ids = []
            for position, (left, right, choice) in enumerate(picks, 1):
                ids.append(conn.execute(
                    "INSERT INTO pick_log(round_id,position,left_id,right_id,axis,kind,choice,beacon_guess,chosen_at,origin) "
                    "VALUES(?,?,?,?,'working','matched',?,'left','2026-11-12','you-deliberate')",
                    (round_id, position, left, right, choice)).lastrowid)
        return ids

    def test_deliberate_picks_become_examples(self):
        self.pick_rows((20, 21, "left"), (22, 23, "neither"), (24, 25, "skip"))
        got = {e["id"]: e["choice"] for e in gists.examples()}
        self.assertEqual(got[20], "chose it over a similar save")
        self.assertEqual(got[21], "turned it down next to a similar save")
        self.assertEqual(got[22], "turned it down next to a similar save")  # neither: both are a pass
        self.assertEqual(got[23], "turned it down next to a similar save")
        self.assertNotIn(24, got)  # skip says nothing
        self.assertNotIn(25, got)
        self.assertEqual(next(e for e in gists.examples() if e["id"] == 20)["text"], "Save 20 Body 20")

    def test_a_save_picked_both_ways_is_no_example_and_a_decided_save_keeps_its_decision(self):
        self.pick_rows((20, 21, "left"), (21, 20, "left"), (10, 12, "right"))
        got = {e["id"]: e["choice"] for e in gists.examples()}
        self.assertNotIn(20, got)
        self.assertNotIn(21, got)
        self.assertEqual(got[10], "kept, and turned it down next to a similar save")
        self.assertEqual(got[12], "passed, and chose it over a similar save")

    def test_picks_can_be_left_out_or_held_out(self):
        first, second = self.pick_rows((20, 21, "left"), (22, 23, "right"))
        self.assertNotIn(20, {e["id"] for e in gists.examples(picks=False)})
        self.assertEqual({e["id"] for e in gists.examples()} - {10, 12, 14, 15}, {20, 21, 22, 23})
        self.assertEqual({e["id"] for e in gists.examples(held_out={first})} - {10, 12, 14, 15}, {22, 23})

    def test_the_prompt_shows_a_pick_as_a_side_by_side_choice(self):
        item = next(i for i in db.get_triage_queue() if i["id"] == 1)
        item["taste"] = [{"id": 20, "choice": "chose it over a similar save", "where": None, "text": "A worked example"}]
        with patch.object(gists.briefs, "sonnet", return_value=({"items": []}, 0.0)) as call, \
                patch.object(gists.settings, "owner", return_value="Ada"):
            gists.write_gists([item], {"proj": "A sync tool."})
        self.assertIn("Ada chose it over a similar save: A worked example", call.call_args.args[0])

    def test_the_prompt_carries_the_examples_and_runs_on_haiku(self):
        item = next(i for i in db.get_triage_queue() if i["id"] == 1)
        item["taste"] = [{"id": 12, "choice": "passed", "where": "Proj", "text": "Generic growth tips"}]
        with patch.object(gists.briefs, "sonnet", return_value=({"items": []}, 0.0)) as call, \
                patch.object(gists.settings, "owner", return_value="Ada"):
            gists.write_gists([item], {"proj": "A sync tool."})
        prompt, model = call.call_args.args[0], call.call_args.kwargs.get("model")
        self.assertIn("Ada passed (Proj): Generic growth tips", prompt)
        self.assertEqual(model, "haiku")
