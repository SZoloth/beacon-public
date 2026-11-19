"""One agent run per project plans all of its Do items; Jev checks each brief's claims."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import autopilot, briefs, db, useful


def jev(supported=0.9):
    def answer(request, **_):
        answers = {key: {"type": "noul", "noul": supported if key == "claim_0" else 0.95} for key in request["questions"]}
        return {"model": useful.MODEL, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 5}}
    return answer


def entry(item_id, verdict="try it"):
    return {"item_id": item_id, "verdict": verdict, "summary": f"Item {item_id}.", "why_here": "Fits.",
            "next_step": f"Step for {item_id}.", "steps": ["a"], "files": ["lib/x.py"], "effort": "under 1 hour",
            "risks": "None.", "task_prompt": f"Do {item_id}.", "item_claims": ["It is a CLI.", "It is fast."]}


class BriefsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "proj"
        (self.repo / "docs").mkdir(parents=True)
        patcher = patch.object(db, "DB_PATH", str(root / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        db.seed_workstreams([
            {"slug": "proj", "name": "Proj", "backlog_path": str(self.repo / "docs" / "beacon-backlog.md")},
            {"slug": "job-search", "name": "Job Search", "backlog_path": str(root / "js" / "beacon-backlog.md")},
        ])
        self.config = [{"slug": "proj", "name": "Proj", "description": "A sync tool."},
                       {"slug": "job-search", "name": "Job Search", "description": "Ada's search.", "career": True}]
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, status, slug in [(1, "reviewed", "proj"), (2, "reviewed", "proj"), (3, "reviewed", "job-search"),
                                          (4, "queued", "proj")]:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,synced_at) VALUES(?,'x',?,?,?,?,?,'2026-09-30')",
                             (item_id, str(item_id), f"Tool {item_id}", f"Body {item_id}", f"https://x.com/a/{item_id}", status))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT ?,id,0.8,1 FROM workstreams WHERE slug=?",
                             (item_id, slug))
        self.calls = []

    def agent(self, prompt, cwd):
        self.calls.append((prompt, cwd))
        ids = [int(line.split()[1].rstrip(":")) for line in prompt.splitlines() if line.startswith("ITEM ")]
        # The agent ranks the later item first, and invents one it was never sent.
        return {"summary": "Start with the second one.", "overlaps": ["1 and 2 overlap."],
                "items": [entry(i) for i in reversed(ids)] + [entry(99)]}, 0.02

    def test_one_agent_run_per_project_ranks_all_its_items(self):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            result = briefs.generate(self.config, allow_upload=True, agent=self.agent)
        self.assertEqual(result["ready"], 3)
        self.assertEqual(len(self.calls), 2)
        proj_prompt, cwd = next(call for call in self.calls if "Proj" in call[0])
        self.assertEqual(Path(cwd), self.repo)
        self.assertIn("Tool 1", proj_prompt)
        self.assertIn("Tool 2", proj_prompt)
        self.assertNotIn("Tool 4", proj_prompt)
        [plan] = [p for p in briefs.list_plans() if p["workstream"] == "proj"]
        self.assertEqual(plan["summary"], "Start with the second one.")
        self.assertEqual(plan["overlaps"], ["1 and 2 overlap."])
        self.assertEqual([b["item_id"] for b in plan["briefs"]], [2, 1])
        self.assertEqual(Path(plan["project_path"]), self.repo)
        self.assertNotIn(99, {b["item_id"] for p in briefs.list_plans() for b in p["briefs"]})

    def test_briefs_from_before_plans_sort_after_ranked_ones(self):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            briefs.generate(self.config, allow_upload=True, agent=self.agent)
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE briefs SET plan_rank = NULL WHERE item_id = 1")
        [plan] = [p for p in briefs.list_plans() if p["workstream"] == "proj"]
        self.assertEqual([b["item_id"] for b in plan["briefs"]], [2, 1])
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE briefs SET plan_rank = NULL WHERE item_id = 2")
            conn.execute("UPDATE briefs SET plan_rank = 5 WHERE item_id = 1")
        [plan] = [p for p in briefs.list_plans() if p["workstream"] == "proj"]
        self.assertEqual([b["item_id"] for b in plan["briefs"]], [1, 2])

    def test_unverified_claims_are_flagged_per_item(self):
        with patch.object(useful, "call_typesafe", side_effect=jev(supported=0.2)):
            briefs.generate(self.config, project="proj", allow_upload=True, agent=self.agent)
        [plan] = briefs.list_plans()
        self.assertEqual(plan["briefs"][0]["unsupported_claims"], ["It is a CLI."])

    def test_items_the_agent_skipped_are_marked_failed_and_retried(self):
        def partial(prompt, cwd):
            return {"summary": "Only one.", "overlaps": [], "items": [entry(1)]}, 0.01
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            first = briefs.generate(self.config, project="proj", allow_upload=True, agent=partial)
            self.assertEqual((first["ready"], first["failed"]), (1, 1))
            retry = briefs.generate(self.config, project="proj", allow_upload=True, agent=self.agent)
        self.assertEqual(retry["ready"], 1)

    def test_career_projects_are_framed_as_preparation_only(self):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            briefs.generate(self.config, project="job-search", allow_upload=True, agent=self.agent)
        self.assertIn("This is career work. Recommend preparation or practice only", self.calls[0][0])

    def test_agent_failure_marks_the_batch_failed_and_retryable(self):
        def broken(prompt, cwd):
            raise RuntimeError("timeout")
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            result = briefs.generate(self.config, project="proj", allow_upload=True, agent=broken)
            self.assertEqual(result["failed"], 2)
            retry = briefs.generate(self.config, project="proj", allow_upload=True, agent=self.agent)
        self.assertEqual(retry["ready"], 2)

    def test_never_twice_and_dismiss_hides(self):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            briefs.generate(self.config, allow_upload=True, agent=self.agent)
            again = briefs.generate(self.config, allow_upload=True, agent=self.agent)
        self.assertEqual(again["ready"] + again["failed"], 0)
        briefs.set_status(2, "proj", "dismissed")
        [plan] = [p for p in briefs.list_plans() if p["workstream"] == "proj"]
        self.assertEqual([b["item_id"] for b in plan["briefs"]], [1])

    def test_reference_only_plans_are_filed_as_references_and_leave_briefs(self):
        def agent(prompt, cwd):
            return {"summary": "One to build.", "overlaps": [],
                    "items": [entry(1), {**entry(2, "reference only"), "why_here": "Worth remembering for motion work."}]}, 0.01
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            briefs.generate(self.config, project="proj", allow_upload=True, agent=agent)
        for _ in range(2):
            [plan] = briefs.list_plans()
            self.assertEqual([b["item_id"] for b in plan["briefs"]], [1])
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT status, status_note FROM items WHERE id=2").fetchone(), ("adopted", "reference"))
            self.assertEqual(conn.execute("SELECT status FROM briefs WHERE item_id=2").fetchone()[0], "dismissed")
            self.assertEqual(conn.execute("SELECT action, reason FROM auto_actions").fetchall(),
                             [("reference", "Worth remembering for motion work.")])
        autopilot.unhandle(2)
        db.update_item_status(2, "reviewed")
        self.assertEqual([i["id"] for i in briefs.pending("proj")["proj"]], [2])

    def test_phone_can_put_a_brief_on_the_macs_list(self):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            briefs.generate(self.config, allow_upload=True, agent=self.agent)
        briefs.set_for_mac(1, "proj", True)
        [plan] = [p for p in briefs.list_plans() if p["workstream"] == "proj"]
        self.assertEqual({b["item_id"]: b["for_mac"] for b in plan["briefs"]}, {1: True, 2: False})
        briefs.set_for_mac(1, "proj", False)
        [plan] = [p for p in briefs.list_plans() if p["workstream"] == "proj"]
        self.assertFalse(any(b["for_mac"] for b in plan["briefs"]))

    def test_replanning_a_brief_keeps_its_start_on_mac_flag(self):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            briefs.generate(self.config, allow_upload=True, agent=self.agent)
        briefs.set_for_mac(1, "proj", True)
        briefs._save(1, "proj", "ready", brief=entry(1), rank=0)
        [plan] = [p for p in briefs.list_plans() if p["workstream"] == "proj"]
        self.assertTrue(next(b for b in plan["briefs"] if b["item_id"] == 1)["for_mac"])

    def test_github_claims_are_checked_against_the_readme_too(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE items SET url='https://github.com/octo/tool' WHERE id=1")
        seen = []
        def capture(request, **kwargs):
            seen.append(request)
            return jev()(request)
        with patch.object(useful, "call_typesafe", side_effect=capture), \
             patch.object(briefs, "_github_readme", return_value="Tool: a CLI with SQLite support.") as readme:
            briefs.generate(self.config, project="proj", allow_upload=True, agent=self.agent)
        readme.assert_called_once_with("octo/tool")
        self.assertTrue(any("a CLI with SQLite support" in r["state"]["source"] for r in seen))

    def test_prompt_tells_agent_not_to_webfetch_x_links(self):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            briefs.generate(self.config, project="proj", allow_upload=True, agent=self.agent)
        self.assertIn("Do not WebFetch x.com, twitter.com, or t.co URLs", self.calls[0][0])

    # Audit B06: Complete and Send change the brief and its save together, or neither.

    def ready_brief(self, item_id=1):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT INTO briefs(item_id,workstream,status,created_at) VALUES(?,'proj','ready','2026-11-19')", (item_id,))

    def brief_and_item(self, item_id=1):
        with sqlite3.connect(db.DB_PATH) as conn:
            return (conn.execute("SELECT status FROM briefs WHERE item_id=?", (item_id,)).fetchone()[0],
                    conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()[0])

    def test_finish_marks_the_brief_dispatched_and_its_save_used(self):
        self.ready_brief()
        briefs.finish(1, "proj", "dispatched", "adopted")
        self.assertEqual(self.brief_and_item(), ("dispatched", "adopted"))

    def test_finish_changes_nothing_when_the_save_cannot_be_updated(self):
        self.ready_brief()
        with patch.object(db, "set_item_status", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                briefs.finish(1, "proj", "dispatched", "tried")
        self.assertEqual(self.brief_and_item(), ("ready", "reviewed"))

    def test_finish_refuses_a_brief_that_does_not_exist(self):
        with self.assertRaisesRegex(LookupError, "no brief"):
            briefs.finish(1, "proj", "dispatched", "adopted")
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT status FROM items WHERE id=1").fetchone()[0], "reviewed")

    def choices(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            return [row[0] for row in conn.execute("SELECT choice FROM choice_log ORDER BY id")]

    def test_finish_records_the_choice_with_the_save(self):
        self.ready_brief()
        self.assertTrue(briefs.finish(1, "proj", "dispatched", "adopted"))
        self.assertEqual(self.choices(), ["adopted"])

    def test_a_second_finish_from_a_stale_screen_changes_nothing(self):
        # Complete on the phone, then Send on a Mac that still showed the brief.
        self.ready_brief()
        briefs.finish(1, "proj", "dispatched", "adopted")
        self.assertFalse(briefs.finish(1, "proj", "dispatched", "tried"))
        self.assertEqual(self.brief_and_item(), ("dispatched", "adopted"))
        self.assertEqual(self.choices(), ["adopted"])

    def test_brief_done_reports_success_when_only_the_backlog_files_fail(self):
        # The save is committed; a backlog file that can't be written mustn't make the app put it back.
        import beacon
        from types import SimpleNamespace
        self.ready_brief()
        args = SimpleNamespace(item_id=1, workstream="proj", status="dispatched", item_status="adopted")
        with patch.object(beacon, "regenerate_all_backlogs", side_effect=PermissionError("read-only")):
            beacon.cmd_brief_done(args)
        self.assertEqual(self.brief_and_item(), ("dispatched", "adopted"))


if __name__ == "__main__":
    unittest.main()


class PlanClaimTest(BriefsTest):
    """Audit B09: a plan is claimed before the agent runs and saved only if nothing changed meanwhile."""

    def brief_rows(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute("SELECT item_id, status FROM briefs WHERE workstream='proj' ORDER BY item_id").fetchall()

    def generate(self, agent):
        with patch.object(useful, "call_typesafe", side_effect=jev()):
            return briefs.generate(self.config, project="proj", allow_upload=True, agent=agent)

    def test_a_second_run_started_meanwhile_does_not_plan_the_same_items(self):
        inner = {}

        def agent(prompt, cwd):
            if not inner:
                inner["result"] = self.generate(self.agent)  # a second `beacon brief` while the first is running
            return self.agent(prompt, cwd)

        first = self.generate(agent)
        self.assertEqual(first["ready"], 2)
        self.assertEqual((inner["result"]["ready"], inner["result"]["failed"]), (0, 0))
        self.assertEqual(len(self.calls), 1)  # one agent run, not two

    def test_undo_while_planning_discards_the_plan(self):
        def agent(prompt, cwd):
            db.update_item_status(1, "queued")  # Undo in Triage while the agent runs
            return self.agent(prompt, cwd)

        result = self.generate(agent)
        self.assertEqual((result["ready"], result["stale"]), (1, 1))
        self.assertEqual(self.brief_rows(), [(2, "ready")])

    def test_a_changed_save_discards_the_plan_and_plans_it_again_next_run(self):
        def agent(prompt, cwd):
            with sqlite3.connect(db.DB_PATH) as conn:
                conn.execute("UPDATE items SET body='A different post' WHERE id=2")
            return self.agent(prompt, cwd)

        self.assertEqual(self.generate(agent)["stale"], 1)
        self.assertEqual(self.brief_rows(), [(1, "ready")])
        self.assertEqual(self.generate(self.agent)["ready"], 1)
        self.assertEqual(self.brief_rows(), [(1, "ready"), (2, "ready")])

    def test_a_failed_run_frees_its_claims_and_an_abandoned_claim_expires(self):
        def broken(prompt, cwd):
            raise RuntimeError("timeout")

        self.generate(broken)
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM brief_claims").fetchone()[0], 0)
            # A run that crashed an hour ago left a claim behind.
            conn.execute("INSERT INTO brief_claims VALUES(1,'proj','dead','x','2026-01-01T00:00:00+00:00')")
        self.assertEqual(self.generate(self.agent)["ready"], 2)
