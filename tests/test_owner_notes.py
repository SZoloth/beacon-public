"""The owner's own tags and notes: which count, where they attach, and how a sync mirrors them."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lib import db, owner_notes


def mymind_object(object_id, *, notes=(), tags=(), url=None, title="A card"):
    """A synthetic mymind object in the CLI's JSONL shape. tags are (name, flags) pairs; flags None leaves it out."""
    return {
        "id": object_id, "title": title, "summary": "What mymind says the card shows", "entityType": "WebPage",
        "mainEntity": {"@type": "WebPage", "author": {"name": "A writer", "url": "https://example.com"}},
        "notes": [{"id": f"n{i}", "content": {"type": "text/markdown", "body": body}, "created": "2026-11-01T00:00:00Z"}
                  for i, body in enumerate(notes)],
        "tags": [{"id": f"t{i}", "name": name, **({} if flags is None else {"flags": flags})}
                 for i, (name, flags) in enumerate(tags)],
        **({"source": {"url": url}} if url else {}),
        "created": "2026-11-01T00:00:00Z", "modified": "2026-11-02T00:00:00Z", "bumped": "2026-11-02T00:00:00Z",
    }


class OwnerNotesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "beacon.db")
        patcher = patch.object(db, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def notes(self):
        with closing(db.get_connection()) as conn:
            return sorted((row["item_id"], row["kind"], row["origin"], row["body"]) for row in
                          conn.execute("SELECT * FROM owner_notes"))

    def item(self, source, external_id):
        with closing(db.get_connection()) as conn:
            row = conn.execute("SELECT id FROM items WHERE source=? AND external_id=?", (source, external_id)).fetchone()
        return row and row[0]

    def sync(self, *objects):
        lines = "\n".join(json.dumps(obj) for obj in objects)
        return owner_notes.sync_mymind("/cli", run=lambda command, timeout: lines)

    def test_only_tags_the_owner_added_count_whatever_other_bits_are_set(self):
        self.sync(mymind_object("a", tags=[("pencil review", 79882), ("Artificial Intelligence", 4098),
                                           ("old tag", None), ("Pencil Review", 8), ("design", 2)]),
                  mymind_object("b", tags=[("design", 2)]))
        card = self.item("mymind", "a")
        self.assertEqual(self.notes(), [(card, "tag", "owner", "pencil review")],
                         "AI tags, tags without flags and a second spelling of the same tag are left out")
        self.assertIsNone(self.item("mymind", "b"), "a card with no owner note or tag is not imported")

    def test_a_note_on_an_x_post_beacon_has_attaches_to_that_post(self):
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO items(source, external_id, title, url, synced_at) "
                         "VALUES('x', '123', 'post', 'https://x.com/someone/status/123', 'now')")
        result = self.sync(mymind_object("a", notes=["for the onboarding flow\n\n"], url="https://twitter.com/someone/status/123"),
                           mymind_object("b", notes=["elsewhere"], url="https://x.com/other/status/999"))
        self.assertEqual((result["attached_to_x"], result["own_items"]), (1, 1))
        self.assertEqual(self.notes(), sorted([(self.item("x", "123"), "note", "owner", "for the onboarding flow"),
                                               (self.item("mymind", "b"), "note", "owner", "elsewhere")]))
        self.assertIsNone(self.item("mymind", "a"), "no second copy of the post")

    def test_notes_kept_before_beacon_had_the_post_move_to_it_on_the_next_sync(self):
        card = mymind_object("a", notes=["walnut sideboard"], url="https://x.com/someone/status/55")
        self.sync(card)
        own = self.item("mymind", "a")
        with closing(db.get_connection()) as conn, conn:
            post = conn.execute("INSERT INTO items(source, external_id, url, synced_at) "
                                "VALUES('x', '55', 'https://x.com/someone/status/55', 'now')").lastrowid
        self.sync(card)
        self.sync(card)
        self.assertEqual(self.notes(), [(post, "note", "owner", "walnut sideboard")])
        self.assertEqual(self.item("mymind", "a"), own, "the card's own save stays")
        with closing(db.get_connection()) as conn:
            found = conn.execute("SELECT rowid FROM library_fts WHERE library_fts MATCH 'sideboard'").fetchall()
        self.assertEqual([row[0] for row in found], [post], "word search finds the post, and no longer the card")

    def test_a_full_sync_mirrors_removed_notes_and_tags_and_keeps_items(self):
        self.sync(mymind_object("a", notes=["first", "second"], tags=[("beacon", 8)]), mymind_object("b", tags=[("cos", 8)]))
        first = self.notes()
        self.assertEqual(self.sync(mymind_object("a", notes=["first"], tags=[("beacon", 8)]),
                                   mymind_object("b", tags=[("cos", 8)]))["removed"], 1)
        self.sync(mymind_object("a", notes=["first"]))
        self.assertEqual(self.notes(), [(self.item("mymind", "a"), "note", "owner", "first")])
        self.assertTrue(self.item("mymind", "b"), "an item whose notes went stays in the library")
        self.assertEqual(len(first), 4)

    def test_a_sync_never_removes_a_note_the_mymind_export_import_wrote(self):
        from lib import mymind
        export = Path(self.temp.name) / "cards.csv"
        export.write_text("Card ID,Title,Notes\na,A card,from the export\n")
        mymind.import_export(export, self.path)
        self.sync(mymind_object("a", tags=[("kiln", 8)]), mymind_object("b", notes=["other"]))
        self.sync(mymind_object("b", notes=["other"]))
        self.assertIn((self.item("mymind", "a"), "note", "owner", "from the export"), self.notes())

    def test_a_line_separator_inside_a_note_stays_in_the_note(self):
        self.sync(mymind_object("a", notes=["first line\u2028second line"]))
        self.assertEqual(self.notes()[0][3], "first line\u2028second line")

    def test_syncing_twice_changes_nothing(self):
        objects = [mymind_object("a", notes=["one"], tags=[("beacon", 8)]), mymind_object("b", tags=[("cos", 8)])]
        self.sync(*objects)
        before = self.notes()
        again = self.sync(*objects)
        self.assertEqual((self.notes(), again["removed"]), (before, 0))
        with closing(db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)

    def test_an_empty_listing_changes_nothing(self):
        self.sync(mymind_object("a", notes=["keep me"]))
        with self.assertRaises(RuntimeError):
            owner_notes.sync_mymind("/cli", run=lambda command, timeout: "")
        self.assertEqual(len(self.notes()), 1)

    def test_a_save_made_for_notes_starts_as_interest_and_a_post_they_attach_to_keeps_its_status(self):
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO items(source, external_id, title, url, synced_at) "
                         "VALUES('x', '123', 'post', 'https://x.com/someone/status/123', 'now')")
        self.sync(mymind_object("a", notes=["one"], url="https://x.com/someone/status/123"), mymind_object("b", notes=["two"]))
        self.sync(mymind_object("a", notes=["one"], url="https://x.com/someone/status/123"), mymind_object("b", notes=["two"]))
        with closing(db.get_connection()) as conn:
            statuses = dict(conn.execute("SELECT source, status FROM items").fetchall())
        self.assertEqual(statuses, {"x": "new", "mymind": "interest"})
        self.assertEqual([item["source"] for item in db.get_unclassified_items()], ["x"],
                         "a save made for notes is never sent to the classifier")

    def test_the_classifier_reads_the_owners_notes_as_their_own_escaped_field(self):
        from lib import classify
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO items(source, external_id, title, url, synced_at) "
                         "VALUES('x', '123', 'post', 'https://x.com/someone/status/123', 'now')")
        self.sync(mymind_object("a", notes=["for <b>beacon</b> & the deck"], tags=[("kiln", 8), ("ai", 2)],
                                url="https://x.com/someone/status/123"))
        item = next(item for item in db.get_unclassified_items() if item["source"] == "x")
        prompt = classify.build_classification_prompt([item], [])
        self.assertIn("own notes and tags (strongest signal of intent): note: for &lt;b&gt;beacon&lt;/b&gt; &amp; the deck | tag: kiln\n</item>", prompt)
        self.assertNotIn("tag: ai", prompt)
        self.assertNotIn("strongest signal", classify.build_classification_prompt([{**item, "owner_notes": None}], []))

    def test_word_search_index_carries_notes_on_a_database_made_before_them(self):
        old_path = str(Path(self.temp.name) / "old.db")
        db.init_db(old_path)
        with closing(sqlite3.connect(old_path)) as conn, conn:
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'library_fts_%'").fetchall():
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute("CREATE TRIGGER library_fts_items_insert AFTER INSERT ON items BEGIN "
                         "INSERT INTO library_fts(rowid, words, about) VALUES (NEW.id, COALESCE(NEW.title, ''), ''); END")
            item = conn.execute("INSERT INTO items(source, external_id, title, synced_at) VALUES('mymind', 'a', 'A card', 'now')").lastrowid
            conn.execute("INSERT INTO owner_notes(item_id, source, external_id, kind, origin, body, synced_at) "
                         "VALUES(?, 'mymind', 'a:note:1', 'note', 'owner', 'walnut sideboard', 'now')", (item,))

        def found():
            with closing(sqlite3.connect(old_path)) as conn:
                return [row[0] for row in conn.execute("SELECT rowid FROM library_fts WHERE library_fts MATCH 'sideboard'")]
        self.assertEqual(found(), [], "the old index never saw the note")
        db.init_db(old_path)
        self.assertEqual(found(), [item])
        with closing(sqlite3.connect(old_path)) as conn, conn:
            conn.execute("UPDATE items SET title = 'renamed'")
        self.assertEqual(found(), [item], "an edit to the save keeps its notes in the index")


if __name__ == "__main__":
    unittest.main()


CONFIG = {"workstreams": [{"slug": "beacon", "name": "Beacon"}, {"slug": "cos", "name": "COS"},
                          {"slug": "client-app", "name": "Client app"}],
          "client_work": {"slugs": ["client-app"], "names": "(?i:megacorp)", "words": ["megacorp"]}}


class NotedSavesTest(unittest.TestCase):
    """Saves the owner's notes point at reach an agent working on that project, matched when it asks."""

    def setUp(self):
        from lib import activate, references
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = json.loads(json.dumps(CONFIG))

        def config():
            activate._load_clients(self.config)
            return self.config
        for target, name, value in ((db, "DB_PATH", str(Path(self.temp.name) / "beacon.db")),
                                    (activate, "_config", config), (references.settings, "load", lambda: self.config)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(activate._load_clients, {})
        self.references = references
        db.init_db()
        db.seed_workstreams(self.config["workstreams"])

    def card(self, object_id, *, notes=(), tags=()):
        return mymind_object(object_id, notes=notes, tags=[(tag, 8) for tag in tags], title=f"card {object_id}")

    def sync(self, *objects):
        lines = "\n".join(json.dumps(obj) for obj in objects)
        owner_notes.sync_mymind("/cli", run=lambda command, timeout: lines)
        with closing(db.get_connection()) as conn:
            return {row["external_id"]: row["id"] for row in conn.execute("SELECT id, external_id FROM items")}

    def found(self, project, task=None, **paging):
        return [(save["id"], save["filed"], save["reason"]) for save in
                self.references.for_project(project, task, **paging)["saves"]]

    def test_a_tag_or_note_names_a_project_by_whole_words_in_any_spelling(self):
        ids = self.sync(self.card("a", tags=["cos"]), self.card("b", notes=["the cosmos is big"]),
                        self.card("c", notes=["maybe for Pencil_Review later"]), self.card("d", tags=["pencil-review"]))
        self.assertEqual(self.found("cos"), [(ids["a"], "noted", 'Your mymind tag "cos" names this project')])
        self.assertEqual(self.found("pencil-review"),
                         [(ids["d"], "noted", 'Your mymind tag "pencil-review" names this project'),
                          (ids["c"], "noted", 'Your mymind note mentions "pencil review"')],
                         "a slug no project has still finds the saves that name it")
        save = self.references.for_project("pencil-review")["saves"][1]
        self.assertEqual(save["owner_notes"], [{"source": "mymind", "kind": "note", "origin": "owner",
                                                "body": "maybe for Pencil_Review later"}])
        self.assertEqual(self.found(None), [], "no project, no noted saves")

    def test_a_project_made_after_the_note_finds_it_without_a_new_sync(self):
        ids = self.sync(self.card("a", notes=["use this in the glaze tracker"]))
        self.assertEqual(self.found("glaze-log"), [])
        db.seed_workstreams([{"slug": "glaze-log", "name": "Glaze tracker"}])
        self.assertEqual(self.found("glaze-log"), [(ids["a"], "noted", 'Your mymind note mentions "glaze tracker"')])
        self.assertEqual(self.references.for_project("glaze-log")["name"], "Glaze tracker")
        self.config["workstreams"].append({"slug": "kiln", "name": "Kiln", "aliases": ["glaze"]})
        db.seed_workstreams([{"slug": "kiln", "name": "Kiln"}])
        self.assertEqual([found[0] for found in self.found("kiln")], [ids["a"]], "a configured alias counts")

    def test_with_a_task_every_noted_save_shares_its_words_and_the_ones_naming_the_project_come_first(self):
        ids = self.sync(self.card("a", notes=["great onboarding example"]), self.card("b", tags=["beacon"]),
                        self.card("c", notes=["nothing related"]), self.card("d", notes=["beacon onboarding idea"]),
                        mymind_object("e", tags=[("beacon", 8)], title="Onboarding flows that work"))
        self.assertEqual(self.found("beacon", "rework the onboarding"),
                         [(ids["e"], "noted", 'Your mymind tag "beacon" names this project · matched words: "onboarding"'),
                          (ids["d"], "noted", 'Your mymind note mentions "beacon" · matched words: "onboarding"'),
                          (ids["a"], "noted", 'Your mymind note mentions "onboarding"')],
                         "a save naming the project but sharing no task word stays out; its title's words count")
        self.assertEqual([save["matches_task"] for save in self.references.for_project("beacon", "rework the onboarding")["saves"]],
                         [True, True, True])
        self.assertEqual([found[0] for found in self.found("beacon")], [ids["e"], ids["d"], ids["b"]],
                         "without a task, every save naming the project comes back")

    def test_filed_saves_come_first_once_each_and_paging_crosses_into_noted_ones(self):
        ids = self.sync(self.card("a", tags=["beacon"]), self.card("b", tags=["beacon"]))
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE items SET status = 'adopted', status_note = 'reference' WHERE id = ?", (ids["a"],))
            conn.execute("INSERT INTO item_workstreams(item_id, workstream_id, confidence, routed) "
                         "SELECT ?, id, 0.8, 1 FROM workstreams WHERE slug = 'beacon'", (ids["a"],))
        self.assertEqual([(found[0], found[1]) for found in self.found("beacon")],
                         [(ids["a"], "reference"), (ids["b"], "noted")])
        page = self.references.for_project("beacon", limit=1, offset=1)
        self.assertEqual(([save["id"] for save in page["saves"]], page["total"], page["next_offset"]), ([ids["b"]], 2, None))
        self.assertEqual(self.references.for_project("beacon", limit=1)["next_offset"], 1)
        self.assertEqual([save["id"] for save in self.references.for_project("beacon", item_id=ids["b"])["saves"]], [ids["b"]])

    def test_client_work_reaches_only_the_client_project(self):
        ids = self.sync(self.card("a", notes=["beacon could use this for Megacorp"], tags=["client-app"]),
                        self.card("b", tags=["beacon", "client-app"]), self.card("c", tags=["beacon"]))
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO item_workstreams(item_id, workstream_id, confidence, routed) "
                         "SELECT ?, id, 0.8, 1 FROM workstreams WHERE slug = 'client-app'", (ids["c"],))
        self.assertEqual(self.found("beacon"), [], "a client name in a note, or filing under client work, keeps it out")
        self.assertEqual(sorted(found[0] for found in self.found("client-app")), [ids["a"], ids["b"]])
        owner_notes.store("granola", [{
            "source": "granola", "external_id": "doc", "title": "A meeting", "body": None, "url": None, "author": None,
            "created_at": None, "raw_json": None, "x_ids": [], "scopes": ["doc"],
            "notes": [owner_notes._note("doc:idea:1", "note", "an idea", origin="owner_digest",
                                        context=json.dumps(["beacon", "client-app"]))]}], complete=True)
        self.assertEqual(self.found("beacon"), [], "a digest filed under a client project stays out too")

    def test_a_term_the_owner_files_under_client_work_keeps_its_saves_out_of_other_projects(self):
        from lib import mcp, vocabulary
        ids = self.sync(self.card("a", tags=["beacon", "side gig"], notes=["walnut sideboard"]),
                        self.card("b", tags=["beacon"], notes=["walnut sideboard"]))
        vocabulary.set_term("Side_Gig", "client-app")
        self.assertEqual([found[0] for found in self.found("beacon")], [ids["b"]])
        self.assertEqual(sorted(found[0] for found in self.found("client-app")), [ids["a"]])
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "search_library", "arguments": {"query": "walnut sideboard"}}})
        self.assertEqual([save["id"] for save in reply["result"]["structuredContent"]["saves"]], [ids["b"]])

    def test_a_granola_meeting_titled_for_a_client_stays_out(self):
        owner_notes.store("granola", [{
            "source": "granola", "external_id": "doc", "title": "Megacorp roadmap sync", "body": None, "url": None,
            "author": None, "created_at": None, "raw_json": None, "x_ids": [], "scopes": ["doc"],
            "notes": [owner_notes._note("doc:idea:1", "note", "an idea", origin="owner_digest",
                                        context=json.dumps(["beacon"]))]}], complete=True)
        self.assertEqual(self.found("beacon"), [])

    def test_an_area_is_named_by_a_tag_never_by_a_word_in_a_note(self):
        self.config["areas"] = [{"slug": "home", "name": "Home"}]
        db.seed_workstreams(self.config["areas"], kind="area")
        ids = self.sync(self.card("a", notes=["a home page idea"]), self.card("b", tags=["home"]),
                        self.card("c", notes=["beacon home page"]))
        self.assertEqual(self.found("home"), [(ids["b"], "noted", 'Your mymind tag "home" names this project')])
        self.assertEqual([found[0] for found in self.found("beacon")], [ids["c"]], "a project still matches note text")

    def test_a_rejected_save_stays_out_and_a_stale_one_still_comes_back(self):
        ids = self.sync(self.card("a", tags=["beacon"]), self.card("b", tags=["beacon"]))
        db.update_item_status(ids["a"], "rejected")
        db.update_item_status(ids["b"], "stale")
        self.assertEqual([found[0] for found in self.found("beacon")], [ids["b"]])

    def test_a_granola_digest_says_a_model_wrote_it(self):
        owner_notes.store("granola", [{
            "source": "granola", "external_id": "doc", "title": "A meeting", "body": None, "url": None, "author": None,
            "created_at": None, "raw_json": None, "x_ids": [], "scopes": ["doc"],
            "notes": [owner_notes._note("doc:idea:1", "note", "a way to sort saves", origin="owner_digest",
                                        context=json.dumps(["beacon"]))]}], complete=True)
        save = self.references.for_project("beacon")["saves"][0]
        self.assertEqual(save["reason"], 'A Granola digest of your notes (summarized by a model) files this under "beacon"')
        self.assertEqual(save["owner_notes"][0]["origin"], "owner_digest")

    def test_agents_read_and_search_noted_saves_outside_the_picture_feed(self):
        from lib import mcp
        ids = self.sync(self.card("a", notes=["walnut sideboard for the hallway"], tags=["beacon"]))

        def call(name, **arguments):
            reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            return reply["result"]["structuredContent"]
        notes = [{"source": "mymind", "kind": "note", "origin": "owner", "body": "walnut sideboard for the hallway"},
                 {"source": "mymind", "kind": "tag", "origin": "owner", "body": "beacon"}]
        self.assertEqual(call("get_save", id=ids["a"])["owner_notes"], notes)
        self.assertEqual(call("get_save", id=ids["a"], project="beacon")["owner_notes"], notes)
        self.assertEqual([save["id"] for save in call("search_library", query="sideboards")["saves"]], [ids["a"]])
        found = call("project_references", project="beacon")["references"]
        self.assertEqual([(save["id"], save["filed"], save["owner_notes"]) for save in found], [(ids["a"], "noted", notes)])

    def test_agents_never_read_or_find_a_noted_save_the_owner_rejected(self):
        from lib import mcp
        ids = self.sync(self.card("a", notes=["walnut sideboard"]), self.card("b", notes=["walnut sideboard"]))
        db.update_item_status(ids["a"], "rejected")

        def call(name, **arguments):
            return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]
        self.assertEqual([save["id"] for save in call("search_library", query="sideboard")["structuredContent"]["saves"]], [ids["b"]])
        self.assertTrue(call("get_save", id=ids["a"])["isError"])
        self.assertEqual(call("get_save", id=ids["b"])["structuredContent"]["id"], ids["b"])

    def test_agents_outside_a_project_never_find_a_save_whose_notes_name_client_work(self):
        from lib import mcp
        with closing(db.get_connection()) as conn, conn:
            for status in ("55", "56"):
                conn.execute("INSERT INTO items(source, external_id, url, raw_json, synced_at) VALUES('x', ?, ?, ?, 'now')",
                             (status, f"https://x.com/a/status/{status}", json.dumps({"media": [{"type": "photo", "url": "p.jpg"}]})))
        ids = self.sync(self.card("a", notes=["walnut sideboard for Megacorp"]), self.card("b", notes=["walnut sideboard"]),
                        self.card("c", tags=["client-app"], notes=["walnut sideboard"]),
                        mymind_object("d", notes=["walnut sideboard, megacorp pitch"], url="https://x.com/a/status/55"))

        def call(name, **arguments):
            return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]
        found = call("search_library", query="walnut sideboard")["structuredContent"]
        self.assertEqual(([save["id"] for save in found["saves"]], found["total"]), ([ids["b"]], 1),
                         "a mymind card or an X post whose notes name a client or a client project stays out of search")
        with patch.object(mcp.vectors, "meaning", return_value=[(ids["55"], 0.9), (ids["56"], 0.8)]):
            self.assertEqual([save["id"] for save in call("search_library", query="a sideboard", mode="meaning")
                              ["structuredContent"]["saves"]], [ids["56"]], "a post in the feed whose note names a client too")
        self.assertEqual(call("get_save", id=ids["a"])["structuredContent"]["owner_notes"][0]["body"],
                         "walnut sideboard for Megacorp", "asked for by id, the save comes back")
        self.assertTrue(call("get_save", id=ids["a"], project="beacon")["isError"], "never inside a project that isn't client work")

    def test_an_agent_reads_five_notes_per_save_in_a_list_and_all_of_them_in_get_save(self):
        from lib import mcp
        ids = self.sync(self.card("a", tags=["beacon"] + [f"tag {n}" for n in range(7)]))

        def call(name, **arguments):
            return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})["result"]["structuredContent"]
        listed = call("project_references", project="beacon")["references"][0]
        self.assertEqual((len(listed["owner_notes"]), listed["owner_notes_more"]), (5, 3))
        self.assertEqual(len(call("search_library", query="beacon")["saves"][0]["owner_notes"]), 5)
        whole = call("get_save", id=ids["a"])
        self.assertEqual((len(whole["owner_notes"]), len(whole["tag_targets"]), whole.get("owner_notes_more")), (8, 8, None))


def highlight(highlight_id, *, note="", tags=(), book_tags=(), url="https://example.com/post", book_id=7, updated="2026-11-01T00:00:00Z"):
    return {"id": highlight_id, "text": f"passage {highlight_id}", "note": note, "tags": [{"id": 1, "name": t} for t in tags],
            "url": url, "highlighted_at": updated, "updated": updated, "book_id": book_id, "book_title": "A book",
            "book_author": "A writer", "book_source_url": "https://example.com/book", "book_category": "articles",
            "book_tags": [{"id": 2, "name": t} for t in book_tags], "book_document_note": ""}


class ReadwiseNotesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        self.calls = []

    def readwise(self, pages, fail_on=None):
        """A fake readwise CLI serving `pages` (lists of highlights) by --page."""
        def run(command, timeout):
            page = int(command[command.index("--page") + 1])
            self.calls.append((command[command.index("--updated-gt") + 1], page))
            if page == fail_on and self.calls.count(self.calls[-1]) <= 3:
                raise RuntimeError("readwise exited 1")
            return json.dumps({"count": 0, "next": "more" if page < len(pages) else None, "results": pages[page - 1]})
        return run

    def notes(self):
        with closing(db.get_connection()) as conn:
            return sorted((row["external_id"], row["body"], row["context"]) for row in conn.execute("SELECT * FROM owner_notes"))

    def test_notes_and_tags_keep_their_passage_and_attach_to_a_post_beacon_has(self):
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO items(source, external_id, url, synced_at) VALUES('x', '55', 'https://x.com/a/status/55', 'now')")
        result = owner_notes.sync_readwise("readwise", run=self.readwise([[
            highlight(1, note="try this for onboarding", tags=["Key point"], url="https://x.com/a/status/55"),
            highlight(2, book_tags=["evals"], book_id=8), highlight(3, book_id=9)]]))
        self.assertEqual((result["annotated"], result["attached_to_x"], result["own_items"]), (2, 1, 1))
        self.assertEqual(self.notes(), [("book:8:tag:evals", "evals", None),
                                        ("highlight:1:note", "try this for onboarding", "passage 1"),
                                        ("highlight:1:tag:key point", "Key point", "passage 1")])

    def test_a_books_tags_and_note_sit_on_the_book_whichever_highlight_comes_last(self):
        with closing(db.get_connection()) as conn, conn:
            post = conn.execute("INSERT INTO items(source, external_id, url, synced_at) "
                                "VALUES('x', '55', 'https://x.com/a/status/55', 'now')").lastrowid
        quoted = dict(highlight(1, note="from the post", book_tags=["evals"], url="https://x.com/a/status/55"),
                      book_document_note="the whole book")
        plain = dict(highlight(2, book_tags=["evals"]), book_document_note="the whole book")
        for page in ([quoted, plain], [plain, quoted]):
            owner_notes.sync_readwise("readwise", full=True, run=self.readwise([page]))
            with closing(db.get_connection()) as conn:
                book = conn.execute("SELECT id FROM items WHERE source='readwise' AND external_id='book:7'").fetchone()[0]
                placed = dict(conn.execute("SELECT external_id, item_id FROM owner_notes").fetchall())
            self.assertEqual(placed, {"highlight:1:note": post, "book:7:tag:evals": book, "book:7:note": book})

    def test_an_incremental_run_moves_a_books_notes_to_the_post_beacon_got_since(self):
        thread = dict(highlight(1, note="keep this"), book_source_url="https://x.com/a/status/77", url=None)
        owner_notes.sync_readwise("readwise", run=self.readwise([[thread]]))
        with closing(db.get_connection()) as conn, conn:
            post = conn.execute("INSERT INTO items(source, external_id, url, synced_at) "
                                "VALUES('x', '77', 'https://x.com/a/status/77', 'now')").lastrowid
        owner_notes.sync_readwise("readwise", run=self.readwise([[highlight(2, note="other", book_id=9)]]))
        with closing(db.get_connection()) as conn:
            self.assertEqual(conn.execute("SELECT item_id FROM owner_notes WHERE body = 'keep this'").fetchone()[0], post)

    def test_a_highlight_that_comes_back_without_its_tag_loses_it_and_a_run_starts_after_the_newest(self):
        owner_notes.sync_readwise("readwise", run=self.readwise([[highlight(1, tags=["a", "b"]), highlight(2, note="keep")]]))
        owner_notes.sync_readwise("readwise", run=self.readwise([[highlight(1, tags=["a"], updated="2026-11-05T00:00:00Z")]]))
        self.assertEqual([note[0] for note in self.notes()], ["highlight:1:tag:a", "highlight:2:note"])
        self.assertEqual(self.calls[-1], ("2026-11-01T00:00:00Z", 1))

    def test_a_stopped_run_resumes_at_its_page_and_a_full_run_drops_what_is_gone(self):
        pages = [[highlight(1, note="one")], [highlight(2, note="two")], [highlight(3, note="three")]]
        with self.assertRaises(RuntimeError):
            owner_notes.sync_readwise("readwise", run=self.readwise(pages, fail_on=2), pause=lambda seconds: None)
        owner_notes.sync_readwise("readwise", run=self.readwise(pages))
        self.assertEqual([call[1] for call in self.calls], [1, 2, 2, 2, 2, 3], "a page is tried three times, then resumed")
        self.assertEqual(len(self.notes()), 3)
        owner_notes.sync_readwise("readwise", full=True, run=self.readwise([[highlight(1, note="one")]]))
        self.assertEqual(self.calls[-1], (owner_notes.READWISE_START, 1))
        self.assertEqual([note[0] for note in self.notes()], ["highlight:1:note"])

    def test_an_unset_path_skips_the_sync(self):
        with patch.object(owner_notes.settings, "load", return_value={"paths": {"readwise_cli": ""}}), \
                patch.object(owner_notes, "sync_readwise", side_effect=AssertionError("ran")):
            self.assertIsNone(owner_notes.sync("readwise-notes"))


DIGEST = """# Granola ideas, Nov 18, 2026

## Needs you

[Open in Granola](https://notes.granola.ai/d/00000000-0000-0000-0000-00000000000a)

| Idea | Belongs to | Already there | Gap | Next step |
| --- | --- | --- | --- | --- |
| Call the plumber | beacon | nothing | none | call |

## Kiln planning

[Open in Granola](https://notes.granola.ai/d/00000000-0000-0000-0000-00000000000b)

| Idea | Belongs to | Already there | Gap | Next step |
| --- | --- | --- | --- | --- |
| Sort glaze tests by cone \\| temperature | kiln, beacon | nothing found | no tracker | draft one |
| Buy more clay | none | nothing found | errand | buy |
"""


class GranolaDigestsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "digests"
        self.folder.mkdir()
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def notes(self):
        with closing(db.get_connection()) as conn:
            return [(row["kind"], row["origin"], row["body"], json.loads(row["context"])) for row in
                    conn.execute("SELECT * FROM owner_notes ORDER BY id")]

    def test_ideas_filed_under_a_project_become_model_digest_notes_once(self):
        (self.folder / "2026-11-18.md").write_text(DIGEST)
        (self.folder / "2026-11-19.md").write_text(DIGEST.replace("Sort glaze", "Sort  GLAZE"))
        (self.folder / "2026-11-19-handoff-prompts.md").write_text("| Idea | Belongs to |\n| x | beacon |\n")
        result = owner_notes.sync_granola(self.folder)
        self.assertEqual((result["annotated"], result["own_items"]), (1, 1))
        self.assertEqual(self.notes(), [("note", "owner_digest", "Sort glaze tests by cone | temperature", ["kiln", "beacon"])],
                         "Needs you, rows filed under none, other files and the same idea on another day are left out")
        with closing(db.get_connection()) as conn:
            item = conn.execute("SELECT source, title, url FROM items").fetchone()
        self.assertEqual(tuple(item), ("granola", "Kiln planning", "https://notes.granola.ai/d/00000000-0000-0000-0000-00000000000b"))

    def test_a_sync_that_succeeds_records_when_so_the_doctor_can_tell_a_stopped_one(self):
        (self.folder / "2026-11-18.md").write_text(DIGEST)
        owner_notes.sync_granola(self.folder)
        owner_notes.sync_mymind("/cli", run=lambda command, timeout: json.dumps(mymind_object("a", notes=["x"])))
        with closing(db.get_connection()) as conn:
            recorded = {row[0] for row in conn.execute("SELECT source FROM sync_state WHERE last_sync IS NOT NULL")}
        self.assertLessEqual({"granola-digests", "mymind"}, recorded)

    def test_an_idea_gone_from_the_digests_goes_and_an_empty_folder_changes_nothing(self):
        (self.folder / "2026-11-18.md").write_text(DIGEST)
        owner_notes.sync_granola(self.folder)
        (self.folder / "2026-11-18.md").write_text(DIGEST.replace("| kiln, beacon |", "| none |"))
        self.assertEqual(owner_notes.sync_granola(self.folder)["removed"], 1)
        (self.folder / "2026-11-18.md").write_text(DIGEST)
        owner_notes.sync_granola(self.folder)
        (self.folder / "2026-11-18.md").unlink()
        with self.assertRaises(RuntimeError):
            owner_notes.sync_granola(self.folder)
        self.assertEqual(len(self.notes()), 1)


class SyncCommandTest(unittest.TestCase):
    def test_a_full_sync_reads_x_before_the_notes_that_attach_to_its_posts(self):
        import beacon
        ran = []
        config = {"sources": {name: {} for name in ("granola-digests", "mymind", "readwise-notes", "github", "x", "readwise")}}
        with patch.object(beacon, "load_config", return_value=config), patch.object(beacon, "_after_ingest"), \
                patch.object(beacon, "_sync_source", side_effect=lambda name, *_, **__: ran.append(name)):
            beacon.cmd_sync(type("Args", (), {"source": None, "full": False})())
        self.assertLess(ran.index("x"), min(ran.index("mymind"), ran.index("readwise-notes")))
        self.assertEqual(sorted(ran), sorted(config["sources"]))

    def test_beacon_sync_runs_an_owner_notes_source_only_when_its_path_is_set(self):
        import beacon
        with patch.object(owner_notes.settings, "load", return_value={"paths": {"granola_digests": "/digests"}}), \
                patch.object(owner_notes, "sync_granola", return_value={"source": "granola"}) as granola, \
                patch.object(owner_notes, "sync_mymind", side_effect=AssertionError("mymind has no path")):
            self.assertEqual(beacon._sync_source("granola-digests", {"method": "digest_folder"}), {"source": "granola"})
            self.assertIsNone(beacon._sync_source("mymind", {"method": "mymind_cli"}))
        granola.assert_called_once_with("/digests")
