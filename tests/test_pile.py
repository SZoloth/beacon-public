import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import beacon
from lib import autopilot, briefs, db, gists, pile

NO_TASTE = lambda saves, pool: {}
DESCRIBED = [{"slug": "proj-a", "description": "A sync tool."}, {"slug": "food", "description": "Recipes."}]


class PileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "proj-a", "name": "Proj A", "backlog_path": "a.md"},
                             {"slug": "proj-b", "name": "Proj B", "backlog_path": "b.md"},
                             {"slug": "parked", "name": "Parked", "backlog_path": "p.md", "active": False}])
        db.seed_workstreams([{"slug": "food", "name": "Food", "backlog_path": None}], kind="area")
        self.judged = []

    def save(self, item_id, status="new", note=None):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,status_note,synced_at) "
                         "VALUES(?,'x',?,?,?,?,?,?,'2026-10-12')",
                         (item_id, str(item_id), f"Save {item_id}", f"Body of save {item_id}", f"https://x.com/a/{item_id}",
                          status, note))

    def rows(self, sql, *params):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            return conn.execute(sql, params).fetchall()

    def status(self, item_id):
        return self.rows("SELECT status, status_note FROM items WHERE id=?", item_id)[0]

    def judge(self, script):
        def judge(items, destinations):
            self.judged.append([item["id"] for item in items])
            answers = []
            for item in items:
                outcome, destination = script.get(item["id"], ("pass", "none"))
                answers.append({"item_id": item["id"], "outcome": outcome, "destination": destination,
                                "takeaway": f"About {item['id']}"})
            return answers, 0.01
        return judge

    def decide(self, script, limit=100, nearest=None):
        return pile.decide(limit, DESCRIBED, judge=self.judge(script), nearest=nearest or NO_TASTE)

    def hold(self, *item_ids, disposition="review"):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.executemany("INSERT INTO intake_reviews VALUES(?,?,?,?,?)",
                             [(i, "hash", disposition, json.dumps({"assessment": disposition, "reasons": []}), "2026-10-12")
                              for i in item_ids])

    def decisions(self):
        return {r[0]: r[1:] for r in self.rows("SELECT item_id, outcome, destination, takeaway FROM pile_decisions")}


class HaikuCallTest(unittest.TestCase):
    def test_the_call_skips_the_prompt_cache_and_runs_outside_the_repo(self):
        reply = json.dumps({"structured_output": {"items": []}, "total_cost_usd": 0.01})
        with patch.object(briefs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, reply, "")) as run:
            self.assertEqual(pile.haiku_judge([], []), ([], 0.01))
        self.assertEqual(run.call_args.kwargs["env"]["DISABLE_PROMPT_CACHING"], "1")
        self.assertEqual(Path(run.call_args.kwargs["cwd"]), Path(tempfile.gettempdir()))
        self.assertIn("haiku", run.call_args.args[0])


class DecideTest(PileTest):
    def test_new_saves_are_decided_newest_first_and_a_rerun_skips_decided_ones(self):
        for item_id in range(1, 8):
            self.save(item_id)
        self.save(8, status="queued")
        self.save(9, status="stale")
        script = {7: ("do", "proj-a"), 6: ("reference", "food"), 5: ("read", "proj-b"), 4: ("pass", "none")}
        result = self.decide(script, limit=4)
        self.assertEqual(self.judged, [[7, 6, 5, 4]])
        self.assertEqual(self.decisions(), {7: ("do", "proj-a", "About 7"), 6: ("reference", "food", "About 6"),
                                            5: ("read", "proj-b", "About 5"), 4: ("pass", None, "About 4")})
        self.assertEqual((result["decided"], result["by_outcome"]), (4, {"do": 1, "reference": 1, "read": 1, "pass": 1}))
        self.assertEqual(result["by_destination"], {"proj-a": {"do": 1}, "food": {"reference": 1}, "proj-b": {"read": 1}})
        self.assertAlmostEqual(result["cost_usd"], 0.01)

        self.decide(script, limit=100)
        self.assertEqual(self.judged[1], [3, 2, 1])
        self.assertEqual(sorted(self.decisions()), [1, 2, 3, 4, 5, 6, 7])
        self.decide(script)
        self.assertEqual(len(self.judged), 2)

    def test_saves_held_by_intake_review_are_not_decided_until_approved(self):
        for item_id in range(1, 7):
            self.save(item_id)
        self.hold(6, 5, disposition="no_fit")
        self.hold(4, disposition="review")
        self.hold(3, disposition="approved")

        result = self.decide({}, limit=3)
        self.assertEqual(self.judged, [[3, 2, 1]])
        self.assertEqual(result["pending"], 3)

        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE intake_reviews SET disposition='approved' WHERE item_id=4")
        self.decide({})
        self.assertEqual(self.judged[1], [4])
        self.assertEqual(sorted(self.decisions()), [1, 2, 3, 4])

    def test_a_crash_loses_at_most_one_chunk(self):
        for item_id in range(1, 7):
            self.save(item_id)
        calls = []

        def crashing(items, destinations):
            calls.append([item["id"] for item in items])
            if len(calls) == 3:
                raise ValueError("boom")
            return [{"item_id": i["id"], "outcome": "pass", "destination": "none", "takeaway": "x"} for i in items], 0.0

        with patch.object(pile, "CHUNK", 2), self.assertRaises(ValueError):
            pile.decide(100, DESCRIBED, judge=crashing, nearest=NO_TASTE)
        self.assertEqual(sorted(self.decisions()), [3, 4, 5, 6])
        self.decide({})
        self.assertEqual(self.judged, [[2, 1]])

    def test_a_failed_chunk_is_retried_next_run_and_repeated_failures_stop_the_run(self):
        for item_id in range(1, 11):
            self.save(item_id)
        calls = []

        def failing(items, destinations):
            calls.append(len(calls))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("claude", 600)
            raise RuntimeError("Haiku failed")

        with patch.object(pile, "CHUNK", 2):
            result = pile.decide(100, DESCRIBED, judge=failing, nearest=NO_TASTE)
        self.assertEqual((result["decided"], result["failed_chunks"], len(calls)), (0, pile.MAX_FAILURES, pile.MAX_FAILURES))
        self.assertEqual(self.decisions(), {})

    def test_invalid_answers_are_skipped_and_retried(self):
        for item_id in (1, 2, 3, 4):
            self.save(item_id)

        def sloppy(items, destinations):
            good = {"item_id": 4, "outcome": "do", "destination": "proj-a", "takeaway": "ok"}
            return [good,
                    {"item_id": 3, "outcome": "maybe", "destination": "proj-a", "takeaway": "x"},
                    {"item_id": 2, "outcome": "do", "destination": "nowhere", "takeaway": "x"},
                    {"item_id": 99, "outcome": "do", "destination": "proj-a", "takeaway": "x"}], 0.0

        pile.decide(100, DESCRIBED, judge=sloppy, nearest=NO_TASTE)
        self.assertEqual(self.decisions(), {4: ("do", "proj-a", "ok")})
        self.decide({})
        self.assertEqual(self.judged, [[3, 2, 1]])

    def test_the_judge_sees_the_destinations_and_sams_nearest_decisions(self):
        self.save(1)
        seen = {}

        def judge(items, destinations):
            seen["taste"] = items[0]["taste"]
            seen["destinations"] = destinations
            return [{"item_id": 1, "outcome": "pass", "destination": "none", "takeaway": "x"}], 0.0

        taste = [{"id": 50, "choice": "passed", "where": "Proj A", "text": "Something like it"}]
        pile.decide(100, DESCRIBED, judge=judge, nearest=lambda saves, pool: {1: taste})
        self.assertEqual(seen["taste"], taste)
        self.assertEqual({d["slug"]: (d["kind"], d["about"]) for d in seen["destinations"]},
                         {"proj-a": ("project", "A sync tool."), "proj-b": ("project", ""), "food": ("area", "Recipes.")})

    def test_a_run_stops_before_deciding_when_no_taste_comes_back(self):
        self.save(1)
        self.save(2, status="adopted")
        with self.assertRaises(RuntimeError):
            self.decide({})
        self.assertEqual((self.decisions(), self.judged), ({}, []))

    def test_the_prompt_carries_the_save_text_taste_and_every_destination(self):
        self.save(1)
        item = {"id": 1, "title": "Save 1", "body": "A trick for CI", "url": "https://x.com/a/1", "author": "a",
                "taste": [{"choice": "passed", "where": "Proj A", "text": "A similar promo"}]}
        dests = [{"slug": "proj-a", "name": "Proj A", "kind": "project", "about": "A sync tool."}]
        prompt, schema = pile.prompt([item], dests)
        for fragment in ("SAVE 1", "A trick for CI", "A similar promo", "proj-a: Proj A (project). A sync tool."):
            self.assertIn(fragment, prompt)
        answer = schema["properties"]["items"]["items"]["properties"]
        self.assertEqual(answer["destination"]["enum"], ["proj-a", "none"])
        self.assertEqual(answer["outcome"]["enum"], ["do", "read", "reference", "pass"])


class ReleaseTest(PileTest):
    def test_release_queues_at_most_n_do_saves_per_destination_newest_first(self):
        for item_id in range(1, 8):
            self.save(item_id)
        script = {i: ("do", "proj-a") for i in (1, 2, 3, 4, 5)} | {6: ("do", "proj-b"), 7: ("do", "proj-b")}
        self.decide(script)

        result = pile.release(3)
        self.assertEqual(result["queued"], {"proj-a": 3, "proj-b": 2})
        self.assertEqual([self.status(i)[0] for i in range(1, 8)], ["new", "new", "queued", "queued", "queued", "queued", "queued"])
        routed = self.rows("SELECT item_id, w.slug, confidence, routed FROM item_workstreams iw "
                           "JOIN workstreams w ON w.id = iw.workstream_id ORDER BY item_id")
        self.assertEqual(routed, [(3, "proj-a", 0.9, 1), (4, "proj-a", 0.9, 1), (5, "proj-a", 0.9, 1),
                                  (6, "proj-b", 0.9, 1), (7, "proj-b", 0.9, 1)])
        self.assertEqual([item["id"] for item in db.get_triage_queue()], [3, 4, 5, 6, 7])

        result = pile.release(3)
        self.assertEqual(result["queued"], {"proj-a": 2})
        self.assertEqual([self.status(i)[0] for i in (1, 2)], ["queued", "queued"])
        self.assertEqual(pile.release(3)["queued"], {})

    def test_references_are_filed_at_once_without_a_follow_up_or_a_pick(self):
        for item_id in (1, 2, 3):
            self.save(item_id)
        self.decide({1: ("reference", "proj-a"), 2: ("reference", "food"), 3: ("reference", "food")})

        result = pile.release(1)
        self.assertEqual(result["referenced"], 3)
        self.assertEqual([self.status(i) for i in (1, 2, 3)], [("adopted", "reference")] * 3)
        filed = self.rows("SELECT item_id, w.slug, routed FROM item_workstreams iw JOIN workstreams w ON w.id = iw.workstream_id "
                          "ORDER BY item_id")
        self.assertEqual(filed, [(1, "proj-a", 1), (2, "food", 1), (3, "food", 1)])
        self.assertEqual(db.get_triage_queue(), [])
        self.assertEqual(self.rows("SELECT * FROM briefs"), [])
        self.assertEqual(pile.release(1)["referenced"], 0)

    def test_a_released_reference_is_logged_as_beacons_action_and_can_be_undone(self):
        self.save(1)
        self.decide({1: ("reference", "proj-b")})
        pile.release(3)
        self.assertEqual(self.rows("SELECT action, reason FROM auto_actions"), [("reference", "About 1")])
        self.assertEqual([h["id"] for h in autopilot.handled()], [1])

        autopilot.unhandle(1)
        self.assertEqual(self.status(1)[0], "queued")
        self.assertEqual(self.rows("SELECT w.slug, iw.routed FROM item_workstreams iw JOIN workstreams w ON w.id = iw.workstream_id "
                                   "WHERE item_id = 1"), [("proj-b", 1)])

    def test_held_saves_are_neither_queued_nor_filed_until_approved(self):
        for item_id in (1, 2):
            self.save(item_id)
        self.decide({1: ("do", "proj-a"), 2: ("reference", "proj-a")})
        self.hold(1, 2)

        result = pile.release(3)
        self.assertEqual((result["queued"], result["referenced"]), ({}, 0))
        self.assertEqual(result["waiting"], {"do": 1, "reference": 1})
        self.assertEqual([self.status(i) for i in (1, 2)], [("new", None)] * 2)
        self.assertEqual(self.rows("SELECT * FROM item_workstreams"), [])
        self.assertEqual(self.rows("SELECT * FROM auto_actions"), [])

        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE intake_reviews SET disposition='approved'")
        result = pile.release(3)
        self.assertEqual((result["queued"], result["referenced"]), ({"proj-a": 1}, 1))

    def test_release_counts_what_waits_and_what_stays_out_of_triage(self):
        for item_id in range(1, 8):
            self.save(item_id)
        script = {1: ("do", "proj-a"), 2: ("do", "proj-a"), 3: ("do", "proj-a"), 4: ("reference", "proj-a"),
                  5: ("read", "proj-a"), 6: ("pass", "none"), 7: ("pass", "none")}
        self.decide(script)
        self.hold(4)

        result = pile.release(2)
        self.assertEqual(result["queued"], {"proj-a": 2})
        self.assertEqual(result["waiting"], {"do": 1, "reference": 1})
        self.assertEqual(result["kept_out"], {"read": 1, "pass": 2})
        self.assertNotIn("held", result)

    def test_reads_and_passes_are_held_with_their_decision_and_never_sent_anywhere(self):
        for item_id in (1, 2, 3):
            self.save(item_id)
        self.decide({1: ("read", "proj-a"), 2: ("pass", "none"), 3: ("read", "none")})
        with patch("lib.reader.read_item", side_effect=AssertionError("Reader was called")):
            result = pile.release(3)
        self.assertEqual((result["queued"], result["referenced"]), ({}, 0))
        self.assertEqual([self.status(i) for i in (1, 2, 3)], [("new", None)] * 3)
        self.assertEqual({i: d[0] for i, d in self.decisions().items()}, {1: "read", 2: "pass", 3: "read"})
        self.assertEqual(self.rows("SELECT * FROM item_workstreams"), [])
        self.decide({})
        self.assertEqual(self.judged, [[3, 2, 1]])

    def test_release_leaves_alone_saves_without_a_usable_destination_or_already_handled_by_sam(self):
        for item_id in (1, 2, 3, 4):
            self.save(item_id)
        self.decide({1: ("do", "none"), 2: ("do", "parked"), 3: ("do", "proj-a"), 4: ("reference", "proj-a")})
        db.update_item_status(3, "rejected")
        db.update_item_status(4, "adopted")
        result = pile.release(3)
        self.assertEqual((result["queued"], result["referenced"]), ({}, 0))
        self.assertEqual([self.status(i)[0] for i in (1, 2, 3, 4)], ["new", "new", "rejected", "adopted"])
        self.assertEqual(self.rows("SELECT * FROM item_workstreams"), [])

    def test_pile_references_are_not_taken_as_sams_own_decisions(self):
        self.save(1)
        self.save(2)
        self.decide({1: ("reference", "proj-a")})
        pile.release(3)
        db.update_item_status(2, "adopted")
        self.assertEqual([e["id"] for e in gists.examples()], [2])


class WeeklyJobTest(unittest.TestCase):
    def test_release_runs_before_gist_so_released_saves_get_their_takeaway_the_same_week(self):
        steps = [line.split("beacon.py ")[1].split(" ||")[0] for line in Path("scripts/weekly.sh").read_text().splitlines()
                 if line.startswith("python3 beacon.py ")]
        self.assertLess(steps.index("pile release --per 3"), steps.index("gist"))


class CommandTest(PileTest):
    def run_cli(self, *argv):
        out = io.StringIO()
        config = {"workstreams": [{"slug": "folio", "name": "Folio", "backlog_path": "f.md"}],
                  "areas": [{"slug": "food", "name": "Food", "description": "Recipes."}]}
        with patch.object(sys, "argv", ["beacon.py", "pile", *argv]), redirect_stdout(out), \
                patch.object(beacon, "_config", config):
            beacon.main()
        return json.loads(out.getvalue())

    def test_decide_then_release_through_the_command_line(self):
        for item_id in range(1, 6):
            self.save(item_id)
        script = {i: ("do", "folio") for i in (1, 2, 3, 4)} | {5: ("reference", "food")}
        with patch.object(pile, "haiku_judge", self.judge(script)), patch.object(gists, "nearest", lambda saves, pool, k=8: {}):
            decided = self.run_cli("decide", "--limit", "5")
        self.assertEqual((decided["decided"], decided["by_outcome"]), (5, {"do": 4, "reference": 1}))
        released = self.run_cli("release", "--per", "2")
        self.assertEqual((released["queued"], released["referenced"]), ({"folio": 2}, 1))


if __name__ == "__main__":
    unittest.main()
