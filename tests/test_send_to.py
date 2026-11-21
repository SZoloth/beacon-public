"""Share-sheet saves: ranked projects, routed at once with the owner's why, copied to mymind, never routed twice."""
import json
import unittest
from contextlib import closing
from unittest.mock import patch

from lib import db, owner_notes, send_to, tag_routes
from tests.test_owner_notes import mymind_object
from tests import test_tag_routes


class SendToTest(unittest.TestCase):
    setUp = test_tag_routes.TagRoutesTest.setUp
    sync = test_tag_routes.TagRoutesTest.sync
    status = test_tag_routes.TagRoutesTest.status

    def add(self, url="https://example.com/post", title="A grid layout case study"):
        db.upsert_item("add", url, title, "", url, "example.com", "{}", None, "2026-11-21T00:00:00+00:00")
        with closing(db.get_connection()) as conn:
            item_id = conn.execute("SELECT id FROM items WHERE source='add' AND external_id=?", (url,)).fetchone()[0]
        db.update_item_status(item_id, "adopted")  # /add keeps every link
        return item_id

    def notes(self, item_id):
        with closing(db.get_connection()) as conn:
            return [(row["source"], row["kind"], row["body"]) for row in
                    conn.execute("SELECT source, kind, body FROM owner_notes WHERE item_id=?", (item_id,))]

    def test_projects_rank_by_shared_words_then_recent_use(self):
        self.assertEqual([row["slug"] for row in send_to.ranked("the owner's site redesign")][:1], ["site"])
        item_id = self.add()
        send_to.route_save(item_id, ["beacon"])
        self.assertEqual(send_to.ranked("")[0]["slug"], "beacon", "with no words to go on, the project used lately leads")
        self.assertNotIn("parked", [row["slug"] for row in send_to.ranked("")])

    def test_a_save_is_queued_in_each_project_with_the_why_as_the_owners_note(self):
        item_id = self.add()
        result = send_to.route_save(item_id, ["site", "beacon"], "steal the scroll layout")
        self.assertEqual(result, {"routed": ["site", "beacon"], "kept_out": []})
        self.assertEqual(self.status(item_id), ("queued", "share sheet: site, beacon"))
        self.assertEqual(self.notes(item_id), [("share", "note", "steal the scroll layout")])
        self.assertIn("A grid layout case study", self.backlog.read_text())
        with closing(db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_workstreams WHERE routed=1 AND item_id=?",
                                          (item_id,)).fetchone()[0], 2)

    def test_an_unknown_or_parked_project_is_refused(self):
        item_id = self.add()
        for slug in ("nope", "parked", "home"):
            with self.assertRaises(ValueError):
                send_to.route_save(item_id, [slug])
        self.assertEqual(self.status(item_id)[0], "adopted")

    def test_a_why_naming_client_work_keeps_the_save_out_of_personal_projects(self):
        item_id = self.add()
        result = send_to.route_save(item_id, ["site", "client-app"], "for the Megacorp pitch")
        self.assertEqual(result, {"routed": ["client-app"], "kept_out": ["site"]})

    def test_the_mymind_card_carries_the_projects_as_tags_and_the_why_as_a_note(self):
        calls = []

        def run(cli_dir, *args, stdin=None):
            calls.append((args, stdin))
            return {"object": {"id": "mm1"}} if args[:2] == ("objects", "create") else {}
        with patch.object(send_to.settings, "path", return_value="/cli"):
            self.assertEqual(send_to.mirror_to_mymind("https://example.com/post", ["site", "beacon"], "why", run=run), "mm1")
        self.assertEqual(calls, [(("objects", "create", "--url", "https://example.com/post", "--tag", "site", "--tag", "beacon"), None),
                                 (("objects", "notes", "create", "mm1"), "why")])
        with patch.object(send_to.settings, "path", return_value=None):
            self.assertIsNone(send_to.mirror_to_mymind("https://example.com/post", ["site"], run=run))

    def test_the_mymind_copy_coming_back_with_its_tags_is_not_routed_again(self):
        item_id = self.add(url="https://example.com/grid")
        send_to.route_save(item_id, ["site"])
        obj = mymind_object("mm1", tags=[("Personal Site", 8)], url="https://example.com/grid")
        owner_notes.sync_mymind("/cli", run=lambda command, timeout: json.dumps(obj))
        self.assertEqual(tag_routes.route_tagged()["routed"], {}, "the same link is already in that project")


if __name__ == "__main__":
    unittest.main()
