"""An owner tag that names a project routes the save to that project's backlog, with no model call."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lib import db, owner_notes, tag_routes, vocabulary
from tests.test_owner_notes import mymind_object


class TagRoutesTest(unittest.TestCase):
    def setUp(self):
        from lib import activate, references
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.backlog = root / "site" / "docs" / "beacon-backlog.md"
        self.config = {
            "workstreams": [{"slug": "site", "name": "Personal Site", "description": "The owner's site.",
                             "backlog_path": str(self.backlog)},
                            {"slug": "beacon", "name": "Beacon", "description": "Saves.",
                             "backlog_path": str(root / "beacon" / "beacon-backlog.md")},
                            {"slug": "client-app", "name": "Client app", "description": "Work for a client.",
                             "backlog_path": str(root / "client" / "beacon-backlog.md")},
                            {"slug": "parked", "name": "Parked", "description": "Old.", "active": False,
                             "backlog_path": str(root / "parked" / "beacon-backlog.md")}],
            "client_work": {"slugs": ["client-app"], "names": "(?i:megacorp)"},
            "areas": [{"slug": "home", "name": "Home", "description": "Interiors."}]}

        def config():
            activate._load_clients(self.config)
            return self.config
        for target, name, value in ((db, "DB_PATH", str(root / "beacon.db")),
                                    (activate, "_config", config), (references.settings, "load", config)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(activate._load_clients, {})
        config()
        live = patch.object(db, "is_live", return_value=True)  # backlog files are only written for the live database
        live.start()
        self.addCleanup(live.stop)
        db.init_db()
        db.seed_workstreams(self.config["workstreams"])
        db.seed_workstreams(self.config["areas"], kind="area")

    def sync(self, *cards):
        objects = [mymind_object(card_id, tags=[(tag, flags) for tag, flags in tags], title=f"Card {card_id}")
                   for card_id, tags in cards]
        owner_notes.sync_mymind("/cli", run=lambda command, timeout: "\n".join(json.dumps(obj) for obj in objects))
        with closing(db.get_connection()) as conn:
            return {row["external_id"]: row["id"] for row in conn.execute("SELECT id, external_id FROM items")}

    def status(self, item_id):
        with closing(db.get_connection()) as conn:
            row = conn.execute("SELECT status, status_note FROM items WHERE id = ?", (item_id,)).fetchone()
            return row["status"], row["status_note"]

    def test_a_tag_naming_a_project_queues_the_save_in_its_backlog(self):
        ids = self.sync(("a", [("Personal Site", 8)]), ("b", [("nothing here", 8)]))
        self.assertEqual(self.status(ids["a"])[0], "interest", "synced mymind saves start as interest")
        result = tag_routes.route_tagged()
        self.assertEqual(result["routed"], {"site": 1})
        self.assertEqual(self.status(ids["a"]), ("queued", "owner tag: Personal Site"))
        self.assertEqual(self.status(ids["b"])[0], "interest")
        self.assertIn("Card a", self.backlog.read_text())

    def test_an_owner_vocabulary_row_routes_but_a_model_row_waits(self):
        ids = self.sync(("a", [("portfolio", 8)]), ("b", [("showcase", 8)]))
        vocabulary.set_term("portfolio", "site")
        vocabulary.set_term("showcase", "site", origin="model_proposed")
        self.assertEqual(tag_routes.route_tagged()["routed"], {"site": 1})
        self.assertEqual(self.status(ids["a"])[0], "queued")
        self.assertEqual(self.status(ids["b"])[0], "interest", "a model's guess is not the owner's word")

    def test_areas_parked_projects_and_mymind_ai_tags_do_not_route(self):
        ids = self.sync(("a", [("home", 8)]), ("b", [("parked", 8)]), ("c", [("Beacon", 2), ("later", 8)]))
        self.assertEqual(tag_routes.route_tagged()["routed"], {}, "mymind's own AI tags (flag 2) are not the owner's")
        for card in "abc":
            self.assertEqual(self.status(ids[card])[0], "interest")

    def test_a_dry_run_changes_nothing_and_a_second_run_routes_nothing_new(self):
        ids = self.sync(("a", [("Beacon", 8), ("Personal Site", 8)]))
        preview = tag_routes.route_tagged(dry_run=True)
        self.assertEqual(preview["routed"], {"beacon": 1, "site": 1})
        self.assertEqual(self.status(ids["a"])[0], "interest")
        self.assertFalse(self.backlog.exists())
        self.assertEqual(tag_routes.route_tagged()["routed"], {"beacon": 1, "site": 1})
        self.assertEqual(tag_routes.route_tagged()["routed"], {}, "a queued save is no longer open")

    def test_a_save_naming_client_work_reaches_only_the_client_project(self):
        ids = self.sync(("a", [("Personal Site", 8), ("Megacorp", 8)]), ("b", [("Client app", 8)]))
        self.assertEqual(tag_routes.route_tagged()["routed"], {"client-app": 1})
        self.assertEqual(self.status(ids["a"])[0], "interest", "a client name keeps the save out of personal projects")
        self.assertEqual(self.status(ids["b"])[0], "queued")

    def test_a_save_the_owner_already_triaged_keeps_its_status(self):
        ids = self.sync(("a", [("Personal Site", 8)]))
        db.update_item_status(ids["a"], "rejected")
        self.assertEqual(tag_routes.route_tagged()["routed"], {})
        self.assertEqual(self.status(ids["a"])[0], "rejected")


if __name__ == "__main__":
    unittest.main()
