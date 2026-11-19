import io
import json
import random
import sys
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import beacon
from lib import db, gists, pick, server


def topic_neighbours(saves, pool, k=8):
    """Stand-in for EmbeddingGemma: saves whose ids share a tens digit are on the same topic."""
    return {s["id"]: [p for p in pool if p["id"] // 10 == s["id"] // 10 and p["id"] != s["id"]][:k] for s in saves}


class PickBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def save(self, item_id, source="x", status="new", body=None):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,synced_at) "
                         "VALUES(?,?,?,?,?,?,?,?,'2026-11-12')",
                         (item_id, source, str(item_id), f"Save {item_id}", body or f"Body of save {item_id}",
                          f"https://x.com/a/{item_id}", "@someone", status))

    def scores(self, axis, scores):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.executemany("INSERT INTO pick_scores(item_id, axis, score, scored_at) VALUES(?,?,?,'2026-11-12')",
                             [(i, axis, s) for i, s in scores.items()])

    def rows(self, sql, *params):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            return conn.execute(sql, params).fetchall()

    def topics(self, axis="working", count=14):
        """`count` topics, each with a high-scoring and a low-scoring save (ids 10/11, 20/21, ...)."""
        scores = {}
        for t in range(1, count + 1):
            for i, s in ((t * 10, 0.9), (t * 10 + 1, 0.2)):
                self.save(i)
                scores[i] = s
        self.scores(axis, scores)

class PickTest(PickBase):
    # scoring

    def test_scoring_sends_unscored_saves_in_batches_of_fifteen_and_stores_scores(self):
        for i in range(1, 32):
            self.save(i)
        self.save(99, status="pending")  # title-only tweet: no text to judge yet
        seen = []

        def judge(items, axis):
            seen.append([item["id"] for item in items])
            return [{"item_id": item["id"], "applies": item["id"] != 5, "score": 0.7} for item in items], 0.01

        result = pick.score("working", limit=100, judge=judge)
        self.assertEqual([len(b) for b in seen], [15, 15, 1])
        self.assertNotIn(99, sum(seen, []))
        self.assertEqual(result["scored"], 31)
        self.assertEqual(self.rows("SELECT score FROM pick_scores WHERE item_id = 5"), [(None,)])
        self.assertEqual(self.rows("SELECT score FROM pick_scores WHERE item_id = 6"), [(0.7,)])
        self.assertEqual(pick.score("working", limit=100, judge=judge)["scored"], 0)  # nothing left to score

    def test_scoring_ignores_answers_for_other_saves_and_out_of_range_scores(self):
        self.save(1)
        self.save(2)
        judge = lambda items, axis: ([{"item_id": 1, "applies": True, "score": 1.4},
                                      {"item_id": 77, "applies": True, "score": 0.5},
                                      {"item_id": 2, "applies": True, "score": 0.4}], 0.0)
        pick.score("working", limit=10, judge=judge)
        self.assertEqual(self.rows("SELECT item_id, score FROM pick_scores ORDER BY item_id"), [(2, 0.4)])

    def test_unknown_axis_is_refused(self):
        with self.assertRaises(ValueError):
            pick.score("vibes", limit=1, judge=lambda items, axis: ([], 0))

    def test_saves_held_by_intake_review_are_not_scored(self):
        self.save(1)
        self.save(2)
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO intake_reviews(item_id, source_hash, disposition, result_json, reviewed_at) VALUES(2, 'h', 'review', '{}', '2026-11-12')")
        seen = []
        pick.score("working", limit=10, judge=lambda items, axis: (seen.extend(i["id"] for i in items) or [], 0))
        self.assertEqual(seen, [1])

    # rounds

    def test_a_round_pairs_same_topic_saves_that_differ_on_the_axis(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(1))
        matched = [p for p in round_["pairs"] if p["kind"] == "matched"]
        self.assertEqual(len(round_["pairs"]), 14)  # no likes, no destinations, no history: matched pairs fill in
        for p in matched:
            left, right = p["left"]["id"], p["right"]["id"]
            self.assertEqual(left // 10, right // 10)
            self.assertIn(p["beacon_guess"], ("left", "right"))
            keep = left if p["beacon_guess"] == "left" else right
            self.assertEqual(keep % 10, 0)  # the higher-scoring save is Beacon's guess
        self.assertEqual(sorted(p["position"] for p in round_["pairs"]), list(range(1, 15)))
        self.assertEqual(len({i for p in round_["pairs"] for i in (p["left"]["id"], p["right"]["id"])}), 28)

    def test_a_round_is_capped_at_twenty_and_mixes_in_foils_from_likes(self):
        self.topics(count=30)
        for t in range(1, 31):
            self.save(t * 10 + 5, source="x-like", status="interest")
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(2))
        kinds = [p["kind"] for p in round_["pairs"]]
        self.assertEqual(len(kinds), 20)
        self.assertEqual(kinds.count("foil"), 4)
        for p in round_["pairs"]:
            if p["kind"] == "foil":
                ids = {p["left"]["id"], p["right"]["id"]}
                like = [i for i in ids if i % 10 == 5]
                self.assertEqual(len(like), 1)
                saved = (ids - set(like)).pop()
                self.assertEqual(saved // 10, like[0] // 10)
                self.assertEqual(p["left" if p["beacon_guess"] == "left" else "right"]["id"], saved)

    def test_too_few_scored_saves_is_an_error_not_a_thin_round(self):
        self.topics(count=3)
        with self.assertRaises(RuntimeError):
            pick.prepare("working", nearest=topic_neighbours)

    def test_no_topic_neighbours_means_no_round(self):
        self.topics()
        with self.assertRaises(RuntimeError):
            pick.prepare("working", nearest=lambda saves, pool, k=8: {})

    def test_the_current_round_resumes_where_you_left_off(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(3))
        pick.record(round_["round_id"], 1, "left")
        pick.record(round_["round_id"], 2, "neither")
        current = pick.current()
        self.assertEqual(current["round_id"], round_["round_id"])
        self.assertEqual(current["next_position"], 3)
        self.assertEqual(current["total"], 14)
        self.assertEqual(current["question"], pick.AXES["working"]["question"])
        self.assertEqual(current["chips"], pick.AXES["working"]["chips"])
        self.assertEqual([p["choice"] for p in current["picks"]], ["left", "neither"])
        card = current["pairs"][0]["left"]
        self.assertTrue({"id", "author", "text", "takeaway", "url", "has_media"} <= set(card))

    def test_no_prepared_round_says_so(self):
        self.assertEqual(pick.current(), {"round_id": None, "preparing": False,
                                          "reason": "No round prepared. Run `beacon pick prepare`."})

    def test_a_finished_round_is_not_current(self):
        self.topics(count=10)
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(4))
        for position in range(1, round_["total"] + 1):
            pick.record(round_["round_id"], position, "right")
        self.assertIsNone(pick.current()["round_id"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pick_rounds WHERE finished_at IS NOT NULL"), [(1,)])

    # picks

    def test_a_pick_is_recorded_as_your_deliberate_choice_and_can_be_changed(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(5))
        pick.record(round_["round_id"], 1, "left", why=pick.AXES["working"]["chips"][0], shown_at="2026-11-12T20:00:00+00:00")
        pick.record(round_["round_id"], 1, "right")
        rows = self.rows("SELECT position, choice, why, origin, axis, kind FROM pick_log")
        self.assertEqual(rows, [(1, "right", None, "you-deliberate", "working", round_["pairs"][0]["kind"])])

    def test_bad_picks_are_refused(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(6))
        for args, kwargs in [((round_["round_id"], 1, "both"), {}),
                             ((round_["round_id"], 99, "left"), {}),
                             ((round_["round_id"] + 1, 1, "left"), {}),
                             ((round_["round_id"], 1, "left"), {"why": "because I said so"})]:
            with self.assertRaises(ValueError):
                pick.record(*args, **kwargs)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM pick_log"), [(0,)])

    def test_back_removes_the_last_pick(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(7))
        pick.record(round_["round_id"], 1, "left")
        pick.record(round_["round_id"], 2, "right")
        self.assertEqual(pick.undo(round_["round_id"]), {"position": 2})
        self.assertEqual(self.rows("SELECT position FROM pick_log"), [(1,)])
        pick.undo(round_["round_id"])
        self.assertEqual(pick.undo(round_["round_id"]), {"position": None})

    def test_repeats_come_back_with_sides_swapped(self):
        self.topics(count=30)
        first = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(8))
        for p in first["pairs"]:
            pick.record(first["round_id"], p["position"], "left")
        second = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(9))
        repeats = [p for p in second["pairs"] if p["kind"] == "repeat"]
        self.assertEqual(len(repeats), 2)
        earlier = {(p["left"]["id"], p["right"]["id"]) for p in first["pairs"]}
        for p in repeats:
            self.assertIn((p["right"]["id"], p["left"]["id"]), earlier)

    # summary

    def test_summary_states_follow_your_picks_toward_beacons_guess(self):
        self.topics(count=30)
        for _ in range(2):
            round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(10))
            for p in round_["pairs"]:
                pick.record(round_["round_id"], p["position"], p["beacon_guess"] if p["kind"] != "foil" else "neither",
                            why=pick.AXES["working"]["chips"][1])
        working = pick.summary()["axes"]["working"]
        self.assertEqual(working["state"], "confirmed")
        self.assertEqual(working["toward_keep"], working["decisive"])
        self.assertEqual(working["top_why"][0], [pick.AXES["working"]["chips"][1], working["decisive"]])
        self.assertEqual(pick.summary()["axes"]["why"]["state"], "inferred")

    def test_summary_can_overturn_a_map_line(self):
        self.topics(count=30)
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(11))
        for p in round_["pairs"]:
            against = "right" if p["beacon_guess"] == "left" else "left"
            pick.record(round_["round_id"], p["position"], against)
        state = pick.summary()["axes"]["working"]
        self.assertGreaterEqual(state["decisive"], 15)
        self.assertEqual(state["state"], "overturned")

    def test_the_next_axis_is_the_least_settled_one(self):
        self.assertEqual(pick.next_axis(), list(pick.AXES)[0])
        self.topics(count=30)
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(12))
        for p in round_["pairs"]:
            pick.record(round_["round_id"], p["position"], "left")
        self.assertNotEqual(pick.next_axis(), "working")


class PrepareInBackgroundTest(PickBase):
    """The phone can ask for the next round; building one takes minutes, so it runs on its own thread."""

    def setUp(self):
        super().setUp()
        self.addCleanup(pick._background.update, running=False, error=None)

    def test_a_round_is_prepared_in_the_background_and_shows_up_as_current(self):
        self.topics()
        threads = []

        class Later:
            def __init__(self, target, daemon):
                threads.append(target)

            def start(self):
                pass

        prepare = lambda axis: pick.prepare(axis, nearest=topic_neighbours, rng=random.Random(14))
        self.assertEqual(pick.start_prepare("working", build=prepare, thread=Later), {"started": True, "preparing": True})
        self.assertEqual(pick.current(), {"round_id": None, "reason": "Preparing the next round.", "preparing": True})
        self.assertEqual(pick.start_prepare(build=prepare, thread=Later), {"started": False, "preparing": True})
        threads[0]()
        current = pick.current()
        self.assertEqual((current["axis"], current["preparing"]), ("working", False))
        self.assertEqual(len(threads), 1)

    def test_a_failed_preparation_is_the_reason_there_is_no_round(self):
        def fails(axis):
            raise RuntimeError("Only 3 saves are scored on why. Run `beacon pick score --axis why` first.")

        pick.start_prepare("why", build=fails, thread=lambda target, daemon: type("T", (), {"start": lambda self: target()})())
        self.assertEqual(pick.current(), {"round_id": None, "preparing": False,
                                          "reason": "Only 3 saves are scored on why. Run `beacon pick score --axis why` first."})

    def test_an_unknown_axis_is_refused_before_anything_starts(self):
        with self.assertRaises(ValueError):
            pick.start_prepare("vibes", thread=lambda **kw: self.fail("started"))


class HoldoutTest(PickBase):
    """Does showing Haiku your picks as examples make it agree more with picks it never saw?"""

    def picked(self, count, axis="working"):
        """`count` decisive picks over distinct pairs, plus one skip and one neither; you always pick the higher id."""
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            round_id = conn.execute("INSERT INTO pick_rounds(axis,pairs_json,created_at) VALUES(?, '[]', '2026-11-19')",
                                    (axis,)).lastrowid
        rows = []
        for n in range(count + 2):
            left, right = 1000 + 2 * n, 1001 + 2 * n
            self.save(left)
            self.save(right)
            choice = "right" if n < count else ("skip" if n == count else "neither")
            rows.append((round_id, n + 1, left, right, axis, "matched", choice, "left" if n % 3 else "right"))
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.executemany("INSERT INTO pick_log(round_id,position,left_id,right_id,axis,kind,choice,beacon_guess,"
                             "chosen_at,origin) VALUES(?,?,?,?,?,?,?,?,'2026-11-19','you-deliberate')", rows)

    def everyone(self, saves, pool, k=8):
        return {s["id"]: [p for p in pool if p["id"] != s["id"]][:k] for s in saves}

    def test_too_few_picks_says_how_many_and_makes_no_calls(self):
        self.picked(5)
        result = pick.holdout(judge=lambda *a: self.fail("model called"), nearest=lambda *a, **k: self.fail("embedded"))
        self.assertEqual((result["enough"], result["decisive"], result["picks"]), (False, 5, 7))
        self.assertIn("at least", result["reason"])

    def test_preview_splits_without_calling_anything(self):
        self.picked(40)
        result = pick.holdout(preview=True, judge=lambda *a: self.fail("model called"),
                              nearest=lambda *a, **k: self.fail("embedded"))
        self.assertEqual((result["decisive"], result["held_out"] + result["train"]), (40, 40))
        self.assertEqual(result["held_out"], 12)

    def test_both_arms_judge_the_same_held_out_picks_and_only_one_sees_training_picks(self):
        self.picked(40)
        seen = []

        def judge(pairs):
            seen.append(pairs)
            # Answers "right" only when a training pick is among the examples, so with-picks agrees and without doesn't.
            helped = lambda p: any("chose it over" in e["choice"] for side in ("left", "right") for e in p[side]["taste"])
            return {p["key"]: ("right" if helped(p) else "left") for p in pairs}, 0.01, {"input_tokens": 100, "output_tokens": 10}

        result = pick.holdout(judge=judge, nearest=self.everyone, seed=3)
        self.assertTrue(result["enough"])
        self.assertEqual(result["with_picks"], {"agree": 12, "of": 12, "rate": 1.0})
        self.assertEqual(result["without_picks"], {"agree": 0, "of": 12, "rate": 0.0})
        self.assertEqual((result["only_with_right"], result["only_without_right"]), (12, 0))
        held = {p["key"] for p in seen[0]}
        self.assertEqual(held, {p["key"] for p in seen[1]})  # the same held-out picks in both arms
        # A held-out pick is never an example: its saves carry no pick line from that very choice.
        for pairs in seen:
            for p in pairs:
                for side in ("left", "right"):
                    self.assertNotIn(p[side]["id"], {e["id"] for e in p[side]["taste"]})
        self.assertEqual(result["tokens"], {"input_tokens": 400, "output_tokens": 40})  # 2 batches x 2 arms
        self.assertAlmostEqual(result["cost_usd"], 0.04)
        self.assertEqual(result["beacon_guess"]["of"], 12)

    def test_held_out_choices_never_reach_the_examples(self):
        self.picked(40)
        pools = []

        def nearest(saves, pool, k=8):
            pools.append({e["id"]: e["choice"] for e in pool})
            return self.everyone(saves, pool, k)

        result = pick.holdout(judge=lambda pairs: ({p["key"]: "left" for p in pairs}, 0.0, {}), nearest=nearest, seed=3)
        held_items = {i for i in result["held_out_items"]}
        without, with_ = pools
        self.assertFalse(any("Pick" in c or "similar save" in c for c in without.values()))
        self.assertFalse(held_items & set(with_))  # pairs are distinct here, so held-out saves have no other pick
        self.assertEqual(len(with_), 2 * 28 + 2)  # 28 training picks and the neither pick

    def test_the_default_judge_asks_haiku_which_save_you_picked_and_counts_tokens(self):
        pairs = [{"key": 7, "question": "Shows the working?", "left": {"id": 1, "text": "Left post", "taste": [
                     {"id": 9, "choice": "chose it over a similar save", "where": None, "text": "A worked example"}]},
                  "right": {"id": 2, "text": "Right post", "taste": []}}]
        payload = ({"items": [{"pair": 7, "choice": "left"}, {"pair": 99, "choice": "right"}]}, 0.003)

        def fake(prompt, schema, system, **kwargs):
            kwargs["usage"].update(input_tokens=1200, output_tokens=40)
            return payload

        with patch.object(pick.briefs, "sonnet", side_effect=fake) as call, \
                patch.object(gists.settings, "owner", return_value="Ada"):
            answers, cost, usage = pick.haiku_holdout(pairs)
        prompt = call.call_args.args[0]
        self.assertIn("Ada chose it over a similar save: A worked example", prompt)
        self.assertIn("Left post", prompt)
        self.assertEqual(call.call_args.kwargs["model"], "haiku")
        self.assertEqual((answers, cost, usage), ({7: "left"}, 0.003, {"input_tokens": 1200, "output_tokens": 40}))

    def test_spending_stops_at_the_cap(self):
        self.picked(80)
        calls = []

        def judge(pairs):
            calls.append(len(pairs))
            return {p["key"]: "right" for p in pairs}, 0.4, {"input_tokens": 1, "output_tokens": 1}

        result = pick.holdout(judge=judge, nearest=self.everyone, max_cost=1.0, seed=1)
        self.assertTrue(result["stopped_at_cap"])
        self.assertLessEqual(result["cost_usd"], 1.2)
        self.assertEqual(result["with_picks"]["of"], result["without_picks"]["of"])  # arms stay paired


class PickServerTest(PickBase):
    def route(self, method, path, body=None):
        return server.route(method, path, body or {}, run=lambda args: self.fail(f"CLI started: {args}"))

    def test_the_phone_reads_the_round_and_records_picks_without_the_cli(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(13))
        status, payload = self.route("GET", "/pick/round")
        self.assertEqual((status, payload["round_id"]), (200, round_["round_id"]))
        self.assertEqual(self.route("POST", "/pick", {"round_id": round_["round_id"], "position": 1, "choice": "skip"}),
                         (200, {"ok": True, "next_position": 2}))
        self.assertEqual(self.route("POST", "/pick/undo", {"round_id": round_["round_id"]}), (200, {"position": 1}))
        status, summary = self.route("GET", "/pick/summary")
        self.assertEqual(status, 200)
        self.assertIn("working", summary["axes"])

    def test_the_phone_can_ask_for_the_next_round(self):
        with patch.object(pick, "start_prepare", return_value={"started": True, "preparing": True}) as start:
            self.assertEqual(self.route("POST", "/pick/prepare", {}), (200, {"started": True, "preparing": True}))
            self.route("POST", "/pick/prepare", {"axis": "why"})
        self.assertEqual([c.args for c in start.call_args_list], [(None,), ("why",)])
        with self.assertRaises(ValueError):
            self.route("POST", "/pick/prepare", {"axis": 3})

    def test_bad_pick_requests_are_refused(self):
        for body in [{"round_id": "1", "position": 1, "choice": "left"},
                     {"round_id": 1, "position": 1, "choice": "left", "why": 3},
                     {"round_id": 1, "choice": "left"},
                     {"round_id": 1, "position": 1, "choice": "left", "shown_at": "yesterday"}]:
            with self.assertRaises(ValueError):
                self.route("POST", "/pick", body)


class PickCommandTest(PickBase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["beacon.py", "pick", *argv]), redirect_stdout(out), redirect_stderr(err):
            beacon.main()
        return json.loads(out.getvalue())

    def test_score_prepare_and_summary_through_the_command_line(self):
        self.topics()
        for i in range(1, 6):
            self.save(200 + i)
        judge = lambda items, axis: ([{"item_id": i["id"], "applies": True, "score": 0.5} for i in items], 0.0)
        with patch.object(pick, "haiku_score", judge):
            self.assertEqual(self.run_cli("score", "--axis", "working", "--limit", "5")["scored"], 5)
        with patch.object(gists, "nearest", topic_neighbours):
            prepared = self.run_cli("prepare", "--axis", "working")
        self.assertEqual(prepared["axis"], "working")
        self.assertGreaterEqual(prepared["pairs"], 10)
        self.assertEqual(self.run_cli("round")["round_id"], prepared["round_id"])
        self.assertEqual(self.run_cli("summary")["picks"], 0)

    def test_the_mac_records_and_takes_back_a_pick_through_the_command_line(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(13))
        rid = str(round_["round_id"])
        self.assertEqual(self.run_cli("record", "--round", rid, "--position", "1", "--choice", "left",
                                      "--why", "real numbers", "--shown-at", "2026-11-19T10:00:00+00:00"),
                         {"ok": True, "next_position": 2})
        self.assertEqual([tuple(r) for r in self.rows("SELECT position, choice, why, shown_at FROM pick_log")],
                         [(1, "left", "real numbers", "2026-11-19T10:00:00+00:00")])
        self.assertEqual(self.run_cli("undo", "--round", rid), {"position": 1})
        self.assertEqual(self.rows("SELECT * FROM pick_log"), [])

    def test_a_bad_pick_exits_2_with_the_reason(self):
        self.topics()
        round_ = pick.prepare("working", nearest=topic_neighbours, rng=random.Random(13))
        err = io.StringIO()
        with patch.object(sys, "argv", ["beacon.py", "pick", "record", "--round", str(round_["round_id"]), "--position", "1",
                                        "--choice", "left", "--why", "made up"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            beacon.main()
        self.assertEqual((caught.exception.code, err.getvalue()), (2, "pick: why must be one of the round's chips\n"))

    def test_holdout_preview_through_the_command_line(self):
        result = self.run_cli("holdout", "--preview")
        self.assertEqual((result["enough"], result["decisive"]), (False, 0))

    def test_prepare_without_scores_exits_with_the_reason(self):
        err = io.StringIO()
        with patch.object(sys, "argv", ["beacon.py", "pick", "prepare", "--axis", "why"]), \
                redirect_stdout(io.StringIO()), redirect_stderr(err), self.assertRaises(SystemExit):
            beacon.main()
        self.assertIn("beacon pick score --axis why", err.getvalue())


if __name__ == "__main__":
    unittest.main()
