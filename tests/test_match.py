import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lib import db, match, sources


FOLIO = {
    "slug": "folio",
    "name": "Folio",
    "active": True,
    "backlog_path": "/tmp/folio/docs/beacon-backlog.md",
    "description": "Local-first Tauri + React reading app. Helped by annotation/reading UX, Tauri, local-first sync, writing tools.",
}
BEACON = {
    "slug": "beacon",
    "name": "Beacon",
    "active": True,
    "description": "Beacon itself. Helped by bookmark/read-later tooling, classification, retrieval and personal knowledge triage.",
}
PARKED = {
    "slug": "parked",
    "name": "Parked Project",
    "active": False,
    "description": "Helped by parked phrase here.",
}
SKILL = {
    "slug": "skill-organization",
    "name": "Skill organization",
    "evidence": ["skill organization"],
    "application": "Compare the save with how skills are organized. Do not edit a bot profile from the save.",
    "disposition": "reference",
}
ROUTING = {
    "slug": "routing-correction-evals",
    "name": "Routing correction evals",
    "evidence": ["routing evaluation"],
    "application": "Add a frozen routing case. Do not change live routing from the save.",
    "disposition": "reference",
}


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = str(Path(self.temp.name) / "isolated.db")
        self.original_db = db.DB_PATH
        db.DB_PATH = self.database
        self.addCleanup(setattr, db, "DB_PATH", self.original_db)
        db.init_db(self.database)
        self.projects = [FOLIO, BEACON, PARKED]
        self.practices = [SKILL, ROUTING]

    def add(self, external_id, body, source="x", status="new", raw=None, url=None):
        raw = raw or {"media": []}
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute(
                """INSERT INTO items (source, external_id, title, body, url, author, raw_json, created_at, synced_at, status)
                   VALUES (?, ?, ?, ?, ?, 'Synthetic', ?, '2026-03-01T00:00:00+00:00', '2026-11-01T00:00:00+00:00', ?)""",
                (source, external_id, body[:100], body, url or f"https://x.com/synthetic_user/status/{external_id}",
                 json.dumps(raw), status),
            )
            conn.commit()
            return conn.execute("SELECT id FROM items WHERE external_id=?", (external_id,)).fetchone()[0]

    def decision(self, external_id):
        report = match.match_catalog(self.database, [FOLIO, BEACON], self.practices, write=False)
        for result in report["results"]:
            row = sqlite3.connect(self.database).execute("SELECT external_id FROM items WHERE id=?", (result["item_id"],)).fetchone()
            if row[0] == external_id:
                return result
        self.fail(external_id)

    def test_margin_match_cites_the_save_and_does_not_file_a_task(self):
        self.add("191", "Folio should use local-first sync for the reader")
        result = self.decision("191")
        self.assertEqual(result["outcome"], "matched")
        self.assertEqual(len(result["recommendations"]), 1)
        row = result["recommendations"][0]
        self.assertEqual((row["target_kind"], row["target_slug"], row["disposition"]), ("project", "folio", "apply"))
        self.assertGreaterEqual(row["confidence"], 0.9)
        self.assertIn("local-first sync", row["explanation"])
        self.assertIn("reading app", row["explanation"])
        self.assertIn('applying "local-first sync"', row["next_action"])
        self.assertIn("Stop after the note.", row["next_action"])
        self.assertIn("Recommendation only. Nothing is changed, opened, or sent.", row["next_action"])
        self.assertNotIn("Review against", row["next_action"])
        self.assertEqual(row["citation"]["external_id"], "191")
        self.assertTrue(row["citation"]["url"].startswith("https://"))
        self.assertIn("No task was filed", result["reason"])

    def test_workflow_only_match_stays_a_reference(self):
        self.add("192", "A note on skill organization for coding agents")
        result = self.decision("192")
        self.assertEqual(result["outcome"], "reference")
        self.assertEqual([row["target_slug"] for row in result["recommendations"]], ["skill-organization"])
        self.assertEqual(result["recommendations"][0]["disposition"], "reference")
        self.assertIn("bot profile", result["recommendations"][0]["next_action"])
        self.assertIn("skill organization", result["recommendations"][0]["next_action"])
        self.assertIn("Recommendation only", result["recommendations"][0]["next_action"])

    def test_no_fit_and_abstentions(self):
        self.add("193", "The team won the game in overtime")
        self.add("194", "", raw={"content_quality": "empty"})
        self.add("195", "Folio local-first sync", source="readwise", status="interest",
                 raw={"content_quality": "title_only", "readwise": {"reader_source": ""}})
        self.add("196", "Folio local-first sync", source="readwise", status="interest",
                 raw={"echo": True, "content_quality": "full", "readwise": {"reader_source": "Beacon"}})
        self.add("197", "Folio local-first sync", source="x-like", status="interest",
                 raw={"x_signals": {"like": {"rank": None, "retrieved_at": None, "acted_at": None}}})
        no_fit = self.decision("193")
        self.assertEqual(no_fit["outcome"], "no_fit")
        self.assertEqual(no_fit["recommendations"], [])
        self.assertEqual(no_fit["next_action"], "No useful match. Leave this save unmatched. Nothing was filed.")
        self.assertEqual(self.decision("194")["reason"], "No text. Not matched.")
        self.assertEqual(self.decision("195")["reason"], "Title only. Not enough text to match.")
        self.assertIn("Not a new save", self.decision("196")["reason"])
        self.assertIn("Liked only", self.decision("197")["reason"])

    def test_multiple_fits_are_deduped_and_idempotent(self):
        item_id = self.add("198", "Folio local-first sync and a routing evaluation for misses")
        first = match.match_catalog(self.database, [FOLIO, BEACON], self.practices, write=True)
        second = match.match_catalog(self.database, [FOLIO, BEACON], self.practices, write=True)
        slugs = sorted(row["target_slug"] for row in first["results"][0]["recommendations"])
        self.assertEqual(slugs, ["folio", "routing-correction-evals"])
        self.assertEqual(
            [row["explanation"] for row in first["results"][0]["recommendations"]],
            [row["explanation"] for row in second["results"][0]["recommendations"]],
        )
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM match_recommendations").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()[0], "new")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_workstreams").fetchone()[0], 0)
        self.assertFalse(second["executed"])

    def test_missing_catalog_is_insufficient_context(self):
        self.add("199", "Folio local-first sync")
        result = match.match_catalog(self.database, [], [], write=False)["results"][0]
        self.assertEqual(result["outcome"], "insufficient_context")

    def test_a_project_doc_phrase_applies_only_with_the_name(self):
        self.add("201", "Beacon keeps a unified item stream")
        loaded = match.match_catalog(self.database, [BEACON], [], repo_root=str(Path(".").resolve()))
        self.assertTrue(loaded["context"][0]["loaded"])
        row = loaded["results"][0]["recommendations"][0]
        self.assertEqual(row["target_slug"], "beacon")
        self.assertEqual(row["disposition"], "apply")
        self.assertIn("unified item stream", row["explanation"])
        bare = match.match_item(
            {"source": "x", "status": "new", "title": "", "body": "Beacon keeps a unified item stream",
             "url": "https://x.com/synthetic_user/status/201", "external_id": "201", "raw_json": "{}"},
            [BEACON], [],
        )
        self.assertEqual(bare["recommendations"][0]["disposition"], "reference")
        _, missing = match.context_report([FOLIO], None)
        self.assertFalse(missing[0]["loaded"])
        self.assertTrue(missing[0]["path"].endswith("beacon-context.md"))

    def test_pending_text_is_not_matched(self):
        self.add("202", "Folio local-first sync", source="readwise", status="pending",
                 raw={"content_quality": "title_only"})
        self.assertEqual(self.decision("202")["reason"], "Post text is not here yet. Not matched.")

    def test_github_repo_match_uses_the_configured_path(self):
        self.add("synthetic/folio", "Unrelated repository description", source="github",
                 url="https://github.com/synthetic/folio")
        result = self.decision("synthetic/folio")
        row = result["recommendations"][0]
        self.assertEqual(row["target_slug"], "folio")
        self.assertEqual(row["confidence"], 0.95)
        self.assertEqual(row["disposition"], "apply")
        self.assertIn("write down one difference", row["next_action"])
        self.assertIn("Do not open a pull request.", row["next_action"])
        self.assertIn("Recommendation only", row["next_action"])

    def test_phrase_without_the_project_name_stays_reference(self):
        self.add("203", "A local-first sync design for a personal reader")
        result = self.decision("203")
        self.assertEqual(result["outcome"], "reference")
        row = result["recommendations"][0]
        self.assertEqual((row["target_kind"], row["target_slug"], row["disposition"]), ("project", "folio", "reference"))
        self.assertIn("Folio", row["explanation"])
        self.assertIn("local-first sync", row["explanation"])
        self.assertIn('as a Folio reference for "local-first sync"', row["next_action"])
        self.assertIn("Do not open a task.", row["next_action"])
        self.assertNotIn("write one note on applying", row["next_action"])
        self.assertIn("No task was filed", result["reason"])
