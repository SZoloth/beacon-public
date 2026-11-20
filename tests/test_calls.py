import io
import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import beacon
from lib import calls, db, gists, pick, server

KEY = "k" * 40
LONG = "A readable post about building a sync tool with real numbers and steps."


class CallsBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([{"slug": "proj-a", "name": "Proj A", "backlog_path": "a.md"}])
        db.seed_workstreams([{"slug": "food", "name": "Food", "backlog_path": None}], kind="area")

    def save(self, item_id, outcome, body=LONG, status="new", note=None, destination=None):
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,status_note,status_at,synced_at) "
                         "VALUES(?,'x',?,?,?,?,'@someone',?,?,'2026-10-13',  '2026-10-12')",
                         (item_id, str(item_id), "", body, f"https://x.com/a/{item_id}", status, note))
            conn.execute("INSERT INTO pile_decisions VALUES(?,?,?,?,'2026-10-13')",
                         (item_id, destination, outcome, f"Takeaway {item_id}"))

    def pile(self, readable=20, empty=10, keeps=10):
        for i in range(1, readable + 1):
            self.save(i, "pass")
        for i in range(101, 101 + empty):
            self.save(i, "pass", body="t.co/x")
        for i in range(201, 201 + keeps):
            self.save(i, "reference", status="adopted", note="reference", destination="proj-a")

    def rows(self, sql, *params):
        with closing(sqlite3.connect(db.DB_PATH)) as conn:
            return conn.execute(sql, params).fetchall()

    def kinds(self, session_id):
        return {c["card_id"]: c for c in json.loads(self.rows("SELECT cards_json FROM call_sessions WHERE id=?", session_id)[0][0])}


class WilsonTest(unittest.TestCase):
    def test_wilson_ninety_percent_interval(self):
        low, high = calls.wilson(2, 30)
        # Worked by hand: p = 2/30, z = 1.6449 gives 2.2% to 18.3%.
        self.assertAlmostEqual(low, 0.0223, places=4)
        self.assertAlmostEqual(high, 0.1827, places=4)
        self.assertEqual(calls.wilson(0, 0), (None, None))
        low, high = calls.wilson(0, 60)
        self.assertEqual(low, 0.0)
        self.assertLess(high, calls.CLOSE_BELOW)


class SessionTest(CallsBase):
    def test_a_session_holds_five_random_cards_by_group_quota_and_fills_suspects(self):
        self.pile()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.executemany("INSERT INTO call_suspects VALUES(?,?,8,NULL,'2026-11-19')",
                             [(i, 1 - i / 100) for i in range(1, 21)])
        result = calls.session(rng=random.Random(1))
        self.assertEqual(len(result["cards"]), 10)
        cards = list(self.kinds(result["session_id"]).values())
        randoms = [c for c in cards if c["kind"] == "random"]
        self.assertEqual(sorted(c["stratum"] for c in randoms), ["keeps", "near-empty", "readable", "readable", "readable"])
        suspects = [c["item_id"] for c in cards if c["kind"] == "suspect"]
        self.assertEqual(len(suspects), 5)
        self.assertEqual(len({c["item_id"] for c in cards}), 10)
        # The highest suspect scores are chosen, among those no random card took.
        best = [i for i in range(1, 21) if i not in {c["item_id"] for c in randoms}][:5]
        self.assertEqual(sorted(suspects), sorted(best))

    def test_cards_are_blind_to_beacons_call_group_and_kind(self):
        self.pile()
        result = calls.session(rng=random.Random(2))
        for card in result["cards"]:
            self.assertEqual(set(card) - {"answer"}, {"card_id", "position", "item"})
            self.assertNotIn("takeaway", card["item"])
            self.assertNotIn("Takeaway", json.dumps(card))
            for word in ("outcome", "kind", "stratum", "suspect", "group"):
                self.assertNotIn(word, card["item"])

    def test_without_a_ranking_random_cards_fill_the_session(self):
        self.pile()
        cards = list(self.kinds(calls.session(rng=random.Random(3))["session_id"]).values())
        self.assertEqual(len(cards), 10)
        self.assertTrue(all(c["kind"] == "random" for c in cards))

    def test_the_open_session_comes_back_until_new_is_asked_for(self):
        self.pile()
        first = calls.session(rng=random.Random(4))["session_id"]
        self.assertEqual(calls.session()["session_id"], first)
        self.assertNotEqual(calls.session(new=True)["session_id"], first)

    def test_a_closed_group_gives_its_random_slots_to_suspects(self):
        self.pile(readable=80)
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO call_sessions VALUES('old', 10, '[]', '2026-11-01')")
            conn.executemany("INSERT INTO call_checks(session_id, card_id, item_id, stratum, kind, beacon_outcome, "
                             "your_outcome, checked_at) VALUES('old', ?, ?, 'readable', 'random', 'pass', 'pass', '2026-11-01')",
                             [(str(i), i) for i in range(1, 61)])
            conn.executemany("INSERT INTO call_suspects VALUES(?,0.9,8,NULL,'2026-11-19')", [(i,) for i in range(61, 81)])
        groups = {g["name"]: g for g in calls.summary()["groups"]}
        self.assertTrue(groups["readable"]["closed"])
        cards = list(self.kinds(calls.session(new=True, rng=random.Random(5))["session_id"]).values())
        self.assertFalse([c for c in cards if c["kind"] == "random" and c["stratum"] == "readable"])
        # 2 random (near-empty, keeps), 1 repeat (the checks are weeks old), the rest suspects.
        self.assertEqual(sum(c["kind"] == "repeat" for c in cards), 1)
        self.assertEqual(sum(c["kind"] == "suspect" for c in cards), 7)

    def test_flagged_neighbours_lead_the_suspects_and_are_used_once(self):
        self.pile()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO call_overrides(item_id, source, old_outcome, new_outcome, at) "
                         "VALUES(1, 'check', 'pass', 'reference', '2026-11-18')")
            conn.execute("INSERT INTO call_flags VALUES(7, 1, '2026-11-19', NULL)")
            conn.executemany("INSERT INTO call_suspects VALUES(?,0.9,8,NULL,'2026-11-19')", [(i,) for i in range(10, 21)])
        self.assertEqual(calls.summary()["flagged_next"], 1)
        session = calls.session(rng=random.Random(6))["session_id"]
        cards = self.kinds(session).values()
        if 7 not in {c["item_id"] for c in cards if c["kind"] == "random"}:
            self.assertIn(7, {c["item_id"] for c in cards if c["kind"] == "suspect"})
        self.assertEqual(self.rows("SELECT used_session FROM call_flags")[0][0], session)
        self.assertEqual(calls.summary()["flagged_next"], 0)

    def test_a_check_a_week_old_comes_back_once_as_a_repeat(self):
        self.pile()
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO call_sessions VALUES('old', 10, '[]', ?)", (old,))
            conn.execute("INSERT INTO call_checks(session_id, card_id, item_id, stratum, kind, beacon_outcome, your_outcome, "
                         "checked_at) VALUES('old', 'a', 3, 'readable', 'random', 'pass', 'pass', ?)", (old,))
        cards = list(self.kinds(calls.session(rng=random.Random(7))["session_id"]).values())
        repeats = [c for c in cards if c["kind"] == "repeat"]
        self.assertEqual([(c["item_id"], c["repeat_of"]) for c in repeats], [(3, 1)])
        self.assertEqual(len(cards), 10)

    def test_no_repeat_before_a_week(self):
        self.pile()
        recent = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO call_sessions VALUES('old', 10, '[]', ?)", (recent,))
            conn.execute("INSERT INTO call_checks(session_id, card_id, item_id, stratum, kind, beacon_outcome, your_outcome, "
                         "checked_at) VALUES('old', 'a', 3, 'readable', 'random', 'pass', 'pass', ?)", (recent,))
        cards = self.kinds(calls.session(new=True, rng=random.Random(8))["session_id"]).values()
        self.assertFalse([c for c in cards if c["kind"] == "repeat"])

    def test_saves_you_already_decided_are_never_cards(self):
        self.pile(readable=3, empty=0, keeps=0)
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE items SET status = 'rejected' WHERE id = 1")  # rejected by you, not autopilot
            conn.execute("INSERT INTO choice_log(item_id, choice, source, chosen_at) VALUES(2, 'adopted', 'you', 'x')")
        cards = self.kinds(calls.session(rng=random.Random(9))["session_id"]).values()
        self.assertEqual([c["item_id"] for c in cards], [3])


class CheckTest(CallsBase):
    def card(self, item_id, kind="random"):
        session = calls.session(rng=random.Random(0))
        for card in session["cards"]:
            if card["item"]["id"] == item_id:
                return session["session_id"], card["card_id"]
        raise AssertionError(f"{item_id} not in the session")

    def test_agreeing_writes_a_check_and_changes_nothing(self):
        self.save(1, "pass")
        session, card = self.card(1)
        result = calls.check(session, card, "pass")
        self.assertTrue(result["agreed"])
        self.assertIsNone(result["change_id"])
        self.assertEqual(result["beacon_outcome"], "pass")
        self.assertEqual(self.rows("SELECT your_outcome, stratum, kind FROM call_checks")[0], ("pass", "readable", "random"))
        self.assertEqual(self.rows("SELECT outcome FROM pile_decisions")[0][0], "pass")

    def test_keeping_a_pass_files_it_routes_it_and_undo_puts_everything_back(self):
        self.save(1, "pass", destination="proj-a")
        session, card = self.card(1)
        result = calls.check(session, card, "keep", chip="real numbers")
        self.assertFalse(result["agreed"])
        self.assertEqual(result["suggested_destination"], "proj-a")
        self.assertEqual(result["beacon_reason"], "Beacon passed this: Takeaway 1")
        self.assertEqual(self.rows("SELECT outcome, destination FROM pile_decisions")[0], ("reference", "proj-a"))
        self.assertEqual(self.rows("SELECT status, status_note FROM items")[0], ("adopted", "reference"))
        self.assertEqual(len(self.rows("SELECT * FROM item_workstreams")), 1)
        self.assertEqual(calls.summary(session)["axes"][0]["said"], 1)  # "real numbers" is a working chip
        self.assertEqual(calls.undo(result["change_id"]), {"restored": True})
        self.assertEqual(self.rows("SELECT outcome, destination FROM pile_decisions")[0], ("pass", "proj-a"))
        self.assertEqual(self.rows("SELECT status, status_note, status_at FROM items")[0], ("new", None, "2026-10-13"))
        self.assertEqual(self.rows("SELECT * FROM item_workstreams"), [])
        self.assertIsNotNone(self.rows("SELECT voided_at FROM call_checks")[0][0])
        self.assertEqual(calls.undo(result["change_id"]), {"restored": False})

    def test_a_chosen_destination_wins_over_the_suggestion(self):
        self.save(1, "pass")
        session, card = self.card(1)
        result = calls.check(session, card, "keep", destination="food")
        self.assertIsNone(result["suggested_destination"])
        self.assertEqual(self.rows("SELECT outcome, destination FROM pile_decisions")[0], ("reference", "food"))
        with self.assertRaises(ValueError):
            calls.check(session, card, "keep", destination="nowhere")

    def test_keeping_with_nowhere_to_go_is_a_read(self):
        self.save(1, "pass")
        session, card = self.card(1)
        calls.check(session, card, "keep")
        self.assertEqual(self.rows("SELECT outcome, destination FROM pile_decisions")[0], ("read", None))
        self.assertEqual(self.rows("SELECT status FROM items")[0][0], "new")

    def test_passing_a_filed_keep_rejects_it_and_undo_refiles_it(self):
        self.save(1, "reference", status="adopted", note="reference", destination="proj-a")
        db.assign_item_workstream(1, "proj-a", 0.9, routed=1, routed_at="2026-10-13")
        session, card = self.card(1)
        result = calls.check(session, card, "pass")
        self.assertEqual(self.rows("SELECT status, status_note FROM items")[0], ("rejected", calls.CHECK_NOTE))
        self.assertEqual(self.rows("SELECT outcome FROM pile_decisions")[0][0], "pass")
        calls.undo(result["change_id"])
        self.assertEqual(self.rows("SELECT status, status_note FROM items")[0], ("adopted", "reference"))
        self.assertEqual(self.rows("SELECT outcome, destination FROM pile_decisions")[0], ("reference", "proj-a"))
        self.assertEqual(self.rows("SELECT confidence, routed_at FROM item_workstreams")[0], (0.9, "2026-10-13"))

    def test_answering_a_card_again_replaces_the_answer_and_its_change(self):
        self.save(1, "pass")
        session, card = self.card(1)
        calls.check(session, card, "keep")
        calls.check(session, card, "pass")
        self.assertEqual(self.rows("SELECT outcome FROM pile_decisions")[0][0], "pass")
        self.assertEqual(self.rows("SELECT your_outcome FROM call_checks WHERE voided_at IS NULL"), [("pass",)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM call_overrides WHERE undone_at IS NULL")[0][0], 0)
        summary = calls.summary(session)
        self.assertEqual(summary["session"], {"checked": 1, "changed": 0, "unsure": 0})

    def test_cant_tell_counts_as_unsure_and_never_in_the_estimate(self):
        self.save(1, "pass")
        session, card = self.card(1)
        result = calls.check(session, card, "unsure")
        self.assertTrue(result["unsure"])
        self.assertIsNone(result["change_id"])
        self.assertEqual(calls.summary(session)["session"]["unsure"], 1)
        self.assertEqual(calls.summary()["groups"][0]["checked_random"], 0)

    def test_bad_answers_and_chips_and_cards_are_refused(self):
        self.save(1, "pass")
        session, card = self.card(1)
        for args, kwargs in (((session, card, "maybe"), {}), ((session, card, "keep"), {"chip": "made up"}),
                             ((session, "nope", "keep"), {}), (("nope", card, "keep"), {})):
            with self.assertRaises(ValueError):
                calls.check(*args, **kwargs)

    def test_the_owners_name_in_a_takeaway_becomes_you(self):
        self.save(1, "pass")
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE pile_decisions SET takeaway = 'Joke ranking; Pat passes on these quips'")
        session, card = self.card(1)
        with patch("lib.settings.owner", return_value="Pat"):
            result = calls.check(session, card, "pass")
        self.assertEqual(result["beacon_reason"], "Beacon passed this: Joke ranking; you passes on these quips")


class ExamplesTest(CallsBase):
    def test_every_check_is_your_example_confirmations_too_and_undo_removes_it(self):
        for i in (1, 2, 3):
            self.save(i, "pass")
        session = calls.session(rng=random.Random(0))
        by_item = {c["item"]["id"]: c["card_id"] for c in session["cards"]}
        calls.check(session["session_id"], by_item[1], "pass")
        change = calls.check(session["session_id"], by_item[2], "keep")["change_id"]
        calls.check(session["session_id"], by_item[3], "unsure")
        examples = {e["id"]: e["choice"] for e in gists.examples()}
        self.assertEqual(examples, {1: "passed", 2: "kept"})
        calls.undo(change)
        self.assertEqual({e["id"] for e in gists.examples()}, {1})


class EstimateTest(CallsBase):
    def checks(self, stratum, changed, total, beacon="pass"):
        yours = "keep" if beacon == "pass" else "pass"
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO call_sessions VALUES('s', 10, '[]', '2026-11-01')")
            conn.executemany("INSERT INTO call_checks(session_id, card_id, item_id, stratum, kind, beacon_outcome, "
                             "your_outcome, checked_at) VALUES('s', ?, ?, ?, 'random', ?, ?, '2026-11-01')",
                             [(f"{stratum}{n}", 1, stratum, beacon, yours if n < changed else side)
                              for n, side in ((n, "pass" if beacon == "pass" else "keep") for n in range(total))])

    def test_groups_are_estimated_separately_and_need_ten_checks(self):
        self.save(1, "pass")
        self.checks("readable", 2, 30)
        self.checks("near-empty", 1, 9)
        groups = {g["name"]: g for g in calls.summary()["groups"]}
        self.assertEqual((groups["readable"]["checked_random"], groups["readable"]["changed_random"]), (30, 2))
        self.assertEqual((groups["readable"]["low"], groups["readable"]["high"]), (0.022, 0.183))
        self.assertFalse(groups["near-empty"]["enough"])
        self.assertIsNone(groups["near-empty"]["low"])
        self.assertEqual(groups["keeps"]["checked_random"], 0)

    def test_suspect_and_repeat_checks_never_feed_the_estimate(self):
        self.save(1, "pass")
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO call_sessions VALUES('s', 10, '[]', '2026-11-01')")
            conn.execute("INSERT INTO call_checks(session_id, card_id, item_id, stratum, kind, beacon_outcome, your_outcome, "
                         "checked_at) VALUES('s', 'a', 1, 'readable', 'suspect', 'pass', 'keep', '2026-11-01')")
        self.assertEqual(calls.summary()["groups"][0]["checked_random"], 0)

    def test_a_bad_group_gets_the_redecide_offer_until_one_runs(self):
        for i in range(1, 6):
            self.save(i, "pass")
        self.checks("readable", 6, 20)
        offer = calls.summary()["offer"]
        self.assertEqual(offer["group"], "readable")
        self.assertEqual(offer["items"], 4)  # save 1 is checked, so a re-decide leaves it alone
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO call_redecides(stratum, started_at) VALUES('readable', '2026-11-02')")
        self.assertIsNone(calls.summary()["offer"])

    def test_axes_need_eight_decisive_picks_for_a_lean(self):
        axes = {a["axis"]: a for a in calls.summary()["axes"]}
        self.assertEqual(axes["working"]["needed"], 8)
        self.assertIsNone(axes["working"]["lean"])
        self.assertEqual(axes["working"]["state"], "inferred")


class RankTest(CallsBase):
    def test_suspect_score_is_the_share_of_neighbours_that_went_the_other_way(self):
        kept = {"choice": "kept"}
        passed = {"choice": "passed"}
        self.assertEqual(calls.suspect_score("pass", [kept, kept, passed, passed]), 0.5)
        self.assertEqual(calls.suspect_score("reference", [kept, passed, passed, passed]), 0.75)
        self.assertEqual(calls.suspect_score("pass", [{"choice": gists.CHOSE}, {"choice": "kept, and " + gists.TURNED_DOWN}]), 1)
        self.assertIsNone(calls.suspect_score("pass", []))

    def test_rank_stores_scores_and_flags_the_nearest_passes_of_a_change(self):
        for i in range(1, 6):
            self.save(i, "pass")
        self.save(50, "pass", status="adopted")  # you kept it: an example
        self.save(51, "pass", status="rejected")  # you passed it

        def nearest(saves, pool, k=8):
            return {s["id"]: [p for p in pool if p["id"] != s["id"]][:k] for s in saves}

        session = calls.session(rng=random.Random(0))
        card = next(c for c in session["cards"] if c["item"]["id"] == 1)
        calls.check(session["session_id"], card["card_id"], "keep")
        result = calls.rank(nearest=nearest)
        self.assertEqual(result["flagged"], 4)
        flagged = {r[0] for r in self.rows("SELECT item_id FROM call_flags")}
        self.assertEqual(flagged, {2, 3, 4, 5})
        scores = dict(self.rows("SELECT item_id, score FROM call_suspects"))
        self.assertEqual(set(scores), {2, 3, 4, 5})  # 1 is checked; 50 and 51 are yours
        self.assertAlmostEqual(scores[2], 2 / 3)  # neighbours: 1 kept (your check), 50 kept, 51 passed
        self.assertEqual(calls.rank(nearest=nearest)["flagged"], 0)  # a change is flagged once

    def test_rank_keeps_the_last_ranking_when_the_model_fails(self):
        self.save(1, "pass")
        self.save(50, "pass", status="adopted")
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("INSERT INTO call_suspects VALUES(1, 0.5, 8, NULL, 'x')")
        with self.assertRaises(RuntimeError):
            calls.rank(nearest=lambda saves, pool, k=8: {})
        self.assertEqual(len(self.rows("SELECT * FROM call_suspects")), 1)


class RedecideTest(CallsBase):
    def test_redecide_changes_only_flipped_calls_and_each_change_is_undoable(self):
        for i in (1, 2):
            self.save(i, "pass")

        def judge(items, dests):
            return [{"item_id": 1, "destination": "proj-a", "outcome": "reference", "takeaway": ""},
                    {"item_id": 2, "destination": "none", "outcome": "pass", "takeaway": ""}], 0.01

        result = calls.redecide("readable", [], judge=judge, nearest=lambda saves, pool, k=8: {})
        self.assertEqual((result["changed"], result["same"]), (1, 1))
        self.assertEqual(self.rows("SELECT outcome, destination FROM pile_decisions WHERE item_id=1")[0], ("reference", "proj-a"))
        change = self.rows("SELECT id, source FROM call_overrides")[0]
        self.assertEqual(change[1], "redecide")
        calls.undo(change[0])
        self.assertEqual(self.rows("SELECT outcome FROM pile_decisions WHERE item_id=1")[0][0], "pass")
        self.assertEqual(self.rows("SELECT status FROM items WHERE id=1")[0][0], "new")


class RoutesTest(CallsBase):
    def call(self, method, path, body=None, key=KEY):
        headers = {"Host": "100.100.1.1:8765", "Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        from email.message import Message
        message = Message()
        for name, value in headers.items():
            message[name] = value
        refused = server.authorize("100.100.1.2", method, path, message, KEY)
        if refused:
            return refused[0], {"error": refused[1]}
        return server.route(method, path, body or {})

    def test_every_calls_route_needs_the_access_key(self):
        for method, path in (("GET", "/calls/session"), ("GET", "/calls/summary"), ("POST", "/calls/check"),
                             ("POST", "/calls/undo"), ("POST", "/calls/redecide")):
            self.assertEqual(self.call(method, path, key=None)[0], 401, path)
            self.assertEqual(self.call(method, path, key="w" * 40)[0], 401, path)

    def test_a_session_checked_and_undone_over_http(self):
        self.save(1, "pass", destination="proj-a")
        status, session = self.call("GET", "/calls/session?size=10")
        self.assertEqual(status, 200)
        card = session["cards"][0]
        status, result = self.call("POST", "/calls/check", {"session_id": session["session_id"], "card_id": card["card_id"],
                                                            "answer": "keep", "chip": "real numbers"})
        self.assertEqual(status, 200)
        self.assertFalse(result["agreed"])
        status, summary = self.call("GET", f"/calls/summary?session={session['session_id']}")
        self.assertEqual(summary["session"], {"checked": 1, "changed": 1, "unsure": 0})
        self.assertEqual(self.call("POST", "/calls/undo", {"change_id": result["change_id"]}), (200, {"restored": True}))

    def test_bad_bodies_are_bad_requests(self):
        for body in ({"session_id": 1, "card_id": "a", "answer": "keep"}, {"session_id": "a", "card_id": "b", "answer": "no"}):
            with self.assertRaises(ValueError):
                self.call("POST", "/calls/check", body)
        with self.assertRaises(ValueError):
            self.call("POST", "/calls/undo", {"change_id": "1"})
        with self.assertRaises(ValueError):
            self.call("GET", "/calls/session?size=0")
        with self.assertRaises(ValueError):
            self.call("POST", "/calls/redecide", {"group": "everything"})


class CliTest(CallsBase):
    def run_cli(self, *args):
        out = io.StringIO()
        with patch("sys.argv", ["beacon.py", *args]), redirect_stdout(out):
            beacon.main()
        return json.loads(out.getvalue())

    def test_session_check_summary_and_undo_from_the_command_line(self):
        self.save(1, "pass")
        session = self.run_cli("calls", "session", "--size", "10")
        card = session["cards"][0]["card_id"]
        result = self.run_cli("calls", "check", "--session", session["session_id"], "--card", card, "--answer", "keep")
        self.assertIsNotNone(result["change_id"])
        self.assertEqual(self.run_cli("calls", "summary")["session"]["changed"], 1)
        self.assertEqual(self.run_cli("calls", "undo", "--change", str(result["change_id"])), {"restored": True})


class PairsTest(CallsBase):
    def test_sessions_serve_pairs_only_from_a_round_built_after_the_scorer_fix(self):
        self.save(1, "pass")
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            for i in (60, 61, 62, 63):
                conn.execute("INSERT INTO items(id,source,external_id,body,status,synced_at) VALUES(?,'x',?,'b','new','x')",
                             (i, str(i)))
            pairs = [{"kind": "matched", "left_id": 60, "right_id": 61, "beacon_guess": "left", "position": 1},
                     {"kind": "repeat", "left_id": 62, "right_id": 63, "beacon_guess": "none", "position": 2, "repeat_of": 1},
                     {"kind": "matched", "left_id": 61, "right_id": 62, "beacon_guess": "right", "position": 3}]
            conn.execute("INSERT INTO pick_rounds(axis, pairs_json, created_at) VALUES('working', ?, 'x')", (json.dumps(pairs),))
        self.assertEqual(calls.session()["pairs"], [])
        with closing(sqlite3.connect(db.DB_PATH)) as conn, conn:
            conn.execute("UPDATE pick_rounds SET scorer = ?", (pick.SCORER,))
        served = calls.session(new=True)["pairs"]
        self.assertEqual([(p["kind"], p["position"], p["round_id"]) for p in served], [("matched", 1, 1), ("repeat", 2, 1)])
        self.assertEqual(served[0]["question"], pick.AXES["working"]["question"])


if __name__ == "__main__":
    unittest.main()
