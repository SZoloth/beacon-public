"""The owner's vocabulary: owner tags reach the projects and areas they point at, though they never name them."""
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lib import db, owner_notes, vocabulary
from tests.test_owner_notes import mymind_object

CONFIG = {"workstreams": [{"slug": "beacon", "name": "Beacon", "description": "Saves and where they go."},
                          {"slug": "site", "name": "Personal Site", "description": "The owner's showcase site."},
                          {"slug": "client-app", "name": "Client app", "description": "Work for Megacorp."}],
          "areas": [{"slug": "food", "name": "Food", "description": "Stews and cooking."},
                    {"slug": "home", "name": "Home", "description": "Interiors and furniture."}],
          "client_work": {"slugs": ["client-app"], "names": "(?i:megacorp)|ZQX"}}
SAVE_TEXT = "What mymind says the card shows"


class VocabularyTest(unittest.TestCase):
    def setUp(self):
        from lib import activate, references
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = json.loads(json.dumps(CONFIG))

        def config():
            activate._load_clients(self.config)
            return self.config
        for target, name, value in ((db, "DB_PATH", str(Path(self.temp.name) / "beacon.db")),
                                    (activate, "_config", config), (references.settings, "load", config)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(activate._load_clients, {})
        config()
        self.references = references
        db.init_db()
        db.seed_workstreams(self.config["workstreams"])
        db.seed_workstreams(self.config["areas"], kind="area")
        self.calls = []

    def sync(self, *cards):
        objects = [mymind_object(card_id, notes=notes, tags=[(tag, 8) for tag in tags]) for card_id, tags, notes in cards]
        owner_notes.sync_mymind("/cli", run=lambda command, timeout: "\n".join(json.dumps(obj) for obj in objects))
        with closing(db.get_connection()) as conn:
            return {row["external_id"]: row["id"] for row in conn.execute("SELECT id, external_id FROM items")}

    def found(self, project):
        return [(save["id"], save["reason"]) for save in self.references.for_project(project)["saves"]]

    def model(self, answer):
        def run(prompt, timeout, runners):
            self.assertEqual(runners, ["claude"], "the owner's terms go only to Claude")
            self.calls.append(prompt)
            return {"ok": True, "results": answer, "errors": [], "runner": "claude"}
        return run

    def listing(self):
        with closing(db.get_connection()) as conn:
            return {row["term"]: (row["how"], row["targets"], row["saves"]) for row in vocabulary.listing(conn)}

    def test_a_tag_filed_under_a_project_reaches_it_and_says_who_filed_it(self):
        ids = self.sync(("a", ["Stews"], []), ("b", [], ["stews"]), ("c", [], ["some stews for later"]))
        vocabulary.set_term("stews", "food", origin="model_proposed")
        self.assertEqual(self.found("food"), [
            (ids["b"], 'Your mymind note "stews" (Beacon files it under Food; a model proposed this)'),
            (ids["a"], 'Your mymind tag "Stews" (Beacon files it under Food; a model proposed this)')],
            "a note that is just the term counts; a longer note does not")

    def test_an_owner_row_hides_the_model_rows_for_its_term(self):
        ids = self.sync(("a", ["showcase"], []))
        vocabulary.set_term("showcase", "beacon", origin="model_proposed")
        vocabulary.set_term("Showcase", "site")
        self.assertEqual(self.found("beacon"), [])
        self.assertEqual(self.found("site"), [(ids["a"], 'Your mymind tag "showcase" (you filed this term under Personal Site)')])
        self.assertEqual(self.listing()["showcase"], ("owner", ["site"], 1))

    def test_none_unlinks_a_term_even_one_that_names_a_project(self):
        ids = self.sync(("a", ["stews", "beacon"], []), ("b", ["beacon"], []))
        vocabulary.set_term("stews", "food", origin="model_proposed")
        vocabulary.set_term("stews", "none")
        vocabulary.set_term("beacon", "none")
        self.assertEqual((self.found("food"), self.found("beacon")), ([], []))
        self.assertEqual(self.listing()["beacon"], ("none", [], 2))
        vocabulary.unset_term("beacon", "none")
        self.assertEqual([found[0] for found in self.found("beacon")], [ids["b"], ids["a"]])

    def test_a_term_can_point_at_several_places(self):
        ids = self.sync(("a", ["pantry"], []))
        vocabulary.set_term("pantry", "home")
        vocabulary.set_term("pantry", "food")
        self.assertEqual([found[0] for found in self.found("home")], [ids["a"]])
        self.assertEqual([found[0] for found in self.found("food")], [ids["a"]])

    def test_unresolved_terms_are_the_ones_no_name_or_row_resolves_largest_first(self):
        self.sync(("a", ["beacon ideas", "someday list", "stews"], []), ("b", ["someday list"], []), ("c", ["Megacorp deck"], []))
        vocabulary.set_term("stews", "food", origin="model_proposed")
        listing = self.listing()
        self.assertEqual(listing["beacon ideas"], ("literal", ["beacon"], 1))
        self.assertEqual([term for term, (how, _, _) in listing.items() if how == "unresolved"], ["someday list", "megacorp deck"])

    def test_propose_sends_only_terms_and_descriptions_and_never_offers_client_work(self):
        self.sync(("a", ["someday list", "megacorp deck", "beacon", "ZQX board"], ["a private thought"]))
        preview = vocabulary.propose(preview=True)
        self.assertEqual(preview["would_send"], 3)
        for sent in ("- someday list (1 save; 1 also filed under beacon)", "- megacorp deck (1 save; 1 also filed under beacon)",
                     "- zqx board (1 save; 1 also filed under beacon)",
                     "- food (area): Food. Stews and cooking."):
            self.assertIn(sent, preview["prompt"], "a term naming a client may be sent, as a word")
        for absent in (SAVE_TEXT, "A card", "a private thought", "client-app", "Work for", "- beacon (1"):
            self.assertNotIn(absent.lower(), preview["prompt"].lower(), "a client project is never offered")
        self.assertEqual(self.listing()["someday list"][0], "unresolved", "a preview stores nothing")

    def test_propose_shows_where_the_other_tags_on_a_terms_saves_already_point(self):
        cards = [mymind_object("a", title="TITLE-SECRET one", notes=["a private thought"], tags=[("north star", 8), ("stews", 8)]),
                 mymind_object("b", title="TITLE-SECRET two", tags=[("North Star", 8), ("stews", 8), ("beacon", 8)]),
                 mymind_object("c", tags=[("north star", 8), ("beacon ideas", 8), ("megacorp deck", 8)]),
                 mymind_object("d", tags=[("north star", 8)]), mymind_object("e", tags=[("someday list", 8)])]
        owner_notes.sync_mymind("/cli", run=lambda command, timeout: "\n".join(json.dumps(card) for card in cards))
        vocabulary.set_term("stews", "food")
        vocabulary.set_term("megacorp deck", "client-app")
        prompt = vocabulary.propose(preview=True)["prompt"]
        self.assertIn("- north star (4 saves; 2 also filed under beacon, 2 under food)\n", prompt)
        self.assertIn("- someday list (1 save)\n", prompt)
        for absent in ("TITLE-SECRET", "a private thought", SAVE_TEXT, "client-app"):
            self.assertNotIn(absent, prompt)

    def test_a_term_asked_before_its_saves_had_evidence_is_asked_again_once(self):
        self.sync(("a", ["someday list"], []))
        vocabulary.propose(run=self.model([]))
        self.assertEqual(vocabulary.propose(preview=True)["sent"], 0)
        self.sync(("a", ["someday list"], []), ("b", ["someday list", "stews"], []))
        vocabulary.set_term("stews", "food")
        self.assertEqual(vocabulary.propose(preview=True)["would_send"], 1, "co-tags now point somewhere")
        result = vocabulary.propose(run=self.model([{"term": "someday list (2 saves; 1 also filed under food)",
                                                     "targets": [{"slug": "food", "confidence": 0.8}]}]))
        self.assertEqual(result["stored"], 1, "a term echoed with its evidence still counts")
        self.assertEqual(vocabulary.propose(preview=True)["sent"], 0)

    def test_only_confident_answers_for_listed_targets_are_kept(self):
        ids = self.sync(("a", ["someday list"], []), ("b", ["spark file"], []), ("c", ["presents"], []))
        result = vocabulary.propose(run=self.model([
            {"term": "someday list", "targets": [{"slug": "home", "confidence": 0.9}, {"slug": "client-app", "confidence": 0.95}]},
            {"term": "spark file", "targets": [{"slug": "site", "confidence": 0.5}]},
            {"term": "not asked", "targets": [{"slug": "food", "confidence": 0.9}]},
            {"term": "presents", "targets": [{"slug": "nowhere", "confidence": 0.9}]}]))
        self.assertEqual((result["sent"], result["stored"]), (3, 1))
        self.assertEqual([found[0] for found in self.found("home")], [ids["a"]])
        self.assertEqual((self.found("client-app"), self.found("site")), ([], []))
        self.assertEqual(vocabulary.propose(run=self.model([]))["sent"], 0)
        self.assertEqual(len(self.calls), 1, "terms already asked about against these projects are not sent again")
        self.sync(("a", ["someday list"], []), ("b", ["fresh term"], []))
        with self.assertRaises(RuntimeError):
            vocabulary.propose(run=self.model({"fresh term": "home"}))
        self.assertEqual(vocabulary.propose(preview=True)["would_send"], 1, "an unreadable answer leaves its terms to ask again")

    def test_a_project_added_later_reopens_only_the_still_unresolved_terms(self):
        ids = self.sync(("a", ["someday list", "pottery", "stews"], []))
        vocabulary.propose(run=self.model([{"term": "stews", "targets": [{"slug": "food", "confidence": 0.9}]}]))
        self.config["workstreams"].append({"slug": "kiln", "name": "Kiln", "description": "A glaze log for the studio."})
        db.seed_workstreams([self.config["workstreams"][-1]])
        result = vocabulary.propose(run=self.model([{"term": "pottery", "targets": [{"slug": "kiln", "confidence": 0.8}]}]))
        self.assertIn("- kiln (project): Kiln. A glaze log", self.calls[1])
        self.assertIn("- pottery (1 save; 1 also filed under food)", self.calls[1])
        self.assertIn("- someday list (1 save; 1 also filed under food)", self.calls[1])
        self.assertNotIn("stews", self.calls[1])
        self.assertEqual(result["stored"], 1)
        self.assertEqual(self.found("kiln"), [(ids["a"], 'Your mymind tag "pottery" (Beacon files it under Kiln; a model proposed this)')])

    def test_get_save_shows_where_each_owner_tag_points(self):
        from lib import mcp
        ids = self.sync(("a", ["stews", "Beacon", "someday list"], ["a note"]))
        vocabulary.set_term("stews", "food", origin="model_proposed")
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "get_save", "arguments": {"id": ids["a"]}}})
        self.assertEqual(reply["result"]["structuredContent"]["tag_targets"], {
            "stews": [{"target": "food", "how": "model"}], "Beacon": [{"target": "beacon", "how": "literal"}], "someday list": []})


if __name__ == "__main__":
    unittest.main()
