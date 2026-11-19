"""Agents use the library through `beacon mcp` (Model Context Protocol over stdin/stdout)."""
import base64
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import adds, db, mcp, vectors


def call(name, **arguments):
    return mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


class McpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value, replacement in ((db, "DB_PATH", str(root / "beacon.db")), (adds, "MEDIA_DIR", root / "media")):
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        photo = {"type": "photo", "url": "https://pbs.twimg.com/media/chair.jpg", "width": 800, "height": 1000}
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, text, media, status in ((1, "a walnut lounge chair", [photo], "adopted"),
                                                 (2, "a pasta recipe", [{**photo, "url": "/media/abc.jpg"}], "new"),
                                                 (3, "rejected chair", [photo], "rejected")):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,raw_json,synced_at) "
                             "VALUES(?,'x',?,?,?,?,'@amy',?,?,'2026-10-09')",
                             (item_id, str(item_id), text, text, f"https://x.com/amy/status/{item_id}", status,
                              json.dumps({"text": text, "media": media, "author": {"name": "Amy"}})))

    def test_handshake_and_tool_list(self):
        hello = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
        self.assertEqual((hello["id"], hello["result"]["serverInfo"]["name"], hello["result"]["protocolVersion"]),
                         (1, "beacon", "2025-06-18"))
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = {tool["name"] for tool in mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(names, {"search_library", "find_similar", "get_save", "list_shelves", "review_references",
                                 "project_references", "keep_link", "keep_picture"})

    def test_word_search_returns_saves_with_their_pictures(self):
        result = call("search_library", query="chair")["result"]
        saves = result["structuredContent"]["saves"]
        self.assertEqual([save["id"] for save in saves], [1])  # the rejected chair stays out
        self.assertEqual(saves[0]["pictures"], ["https://pbs.twimg.com/media/chair.jpg"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

    def test_meaning_search_and_similar_use_the_on_device_index(self):
        with patch.object(vectors, "_run", return_value=json.dumps([[2, 0.4], [1, 0.3]])):
            saves = call("search_library", query="dinner", mode="meaning")["result"]["structuredContent"]["saves"]
            similar = call("find_similar", id=1)["result"]["structuredContent"]["saves"]
        self.assertEqual([save["id"] for save in saves], [2, 1])
        # A picture you added is a file on this Mac; agents get its path so they can open it.
        self.assertEqual(saves[0]["pictures"], [str(adds.MEDIA_DIR / "abc.jpg")])
        self.assertEqual([save["id"] for save in similar], [2, 1])

    def test_explicit_combined_search_fuses_words_and_meaning_with_pagination(self):
        answer = {"pictures": [[2, 0.4]], "words": [[2, 0.5], [1, 0.3]]}
        with patch.object(vectors, "_run", return_value=json.dumps(answer)):
            default = call("search_library", query="chair")["result"]["structuredContent"]
            found = call("search_library", query="chair", mode="both", limit=1)["result"]["structuredContent"]
            next_page = call("search_library", query="chair", mode="both", offset=found["next_offset"])["result"]["structuredContent"]
        self.assertEqual([save["id"] for save in default["saves"]], [1])
        # 2 tops both meaning lists; 1 has the word but is only second by meaning.
        self.assertEqual([save["id"] for save in found["saves"]], [2])
        self.assertEqual([save["id"] for save in next_page["saves"]], [1])
        self.assertEqual(found["searched"], ["words", "pictures", "meaning"])

    def test_review_references_keeps_text_on_this_mac_unless_asked(self):
        with patch.object(vectors, "meaning", return_value=[(1, 0.3), (2, 0.2)]), \
             patch.object(mcp.useful, "call_typesafe", side_effect=AssertionError("no upload")):
            saves = call("review_references", brief="a lounge chair", ids=[2, 1])["result"]["structuredContent"]["saves"]
        self.assertEqual([(save["id"], save["fit"]) for save in saves], [(1, "off"), (2, "off")])  # best first
        self.assertTrue(call("review_references", brief="chair", ids="1,2")["result"]["isError"])

    def test_bad_calls_are_tool_errors_not_crashes(self):
        self.assertTrue(call("get_save", id=999)["result"]["isError"])
        self.assertTrue(call("search_library", query="x", mode="vibes")["result"]["isError"])
        self.assertEqual(call("nope")["error"]["code"], -32602)
        self.assertEqual(mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})["error"]["code"], -32601)

    def test_a_failing_tool_that_prints_still_leaves_clean_replies_and_a_running_server(self):
        def locked(*_):
            print("  Error upserting add/x: database is locked")
            raise sqlite3.OperationalError("database is locked")

        output = io.StringIO()
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "keep_link", "arguments": {"url": "https://a.com"}}}
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        stdout = sys.stdout
        self.addCleanup(setattr, sys, "stdout", stdout)
        with patch.object(adds, "add_link", side_effect=locked):
            mcp.serve(io.StringIO(json.dumps(request) + "\n" + json.dumps(ping) + "\n"), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertIn("database is locked", replies[0]["result"]["content"][0]["text"])

    def test_project_references_are_the_projects_filed_and_kept_saves_ranked_by_the_task(self):
        db.seed_workstreams([{"slug": "folio", "name": "Folio", "backlog_path": "m.md"},
                             {"slug": "beacon", "name": "Beacon", "backlog_path": "b.md"}])
        db.seed_workstreams([{"slug": "travel", "name": "Travel"}], kind="area")
        saves = [
            (10, "Spring animations and easing curves for UI motion", "adopted", "reference", "folio"),
            (11, "Onboarding copy teardown: what first-run screens say", "adopted", "reference", "folio"),
            (12, "A tiny markdown parser worth reading. " + "It handles nesting. " * 30, "adopted", None, "folio"),
            (13, "Animation article sent to Reader", "adopted", "read", "folio"),
            (14, "Animation idea still in Triage", "queued", None, "folio"),
            (15, "Animation noise Ada passed", "rejected", None, "folio"),
            (16, "Animation reference for another project", "adopted", "reference", "beacon"),
            (17, "Lisbon in October: the quiet neighbourhoods", "adopted", None, "travel")]
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, text, status, note, slug in saves:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,status_note,raw_json,synced_at) "
                             "VALUES(?,'x',?,?,?,?,'@amy',?,?,?,'2026-10-09')",
                             (item_id, str(item_id), text, text, f"https://x.com/amy/status/{item_id}", status, note,
                              json.dumps({"text": text, "media": [], "author": {"name": "Amy"}})))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT ?,id,0.8,1 FROM workstreams WHERE slug=?",
                             (item_id, slug))
            conn.execute("INSERT INTO item_gists VALUES(10,'Springs make UI motion feel alive','tip','reference','Folio motion','2026-10-10')")

        def refs(**arguments):
            return call("project_references", **arguments)["result"]["structuredContent"]

        found = refs(project="folio", task="animations")
        self.assertEqual([(r["id"], r["filed"], r["matches_task"]) for r in found["references"]],
                         [(10, "reference", True)])
        self.assertEqual((found["project"], found["name"], found["total"]), ("folio", "Folio", 3))
        self.assertEqual(found["references"][0]["takeaway"], "Springs make UI motion feel alive")
        self.assertEqual(found["references"][0]["url"], "https://x.com/amy/status/10")
        self.assertTrue(found["references"][0]["reason"])
        self.assertEqual(refs(project="folio", task="onboarding copy")["references"][0]["id"], 11)
        reopened = call("get_save", id=12, project="folio")["result"]["structuredContent"]
        self.assertGreater(len(reopened["text"]), 500)
        self.assertEqual(reopened["url"], "https://x.com/amy/status/12")
        self.assertTrue(call("get_save", id=16, project="folio")["result"]["isError"])
        self.assertEqual(refs(project="folio", task="Find a recipe for baked lemon ricotta gnocchi")["references"], [])
        self.assertEqual(refs(project="folio", task="Compare risotto with baked pasta screens")["references"], [])
        self.assertEqual([r["id"] for r in refs(project="folio")["references"]], [12, 11, 10])
        self.assertEqual([r["id"] for r in refs(project="folio", limit=1)["references"]], [12])
        self.assertEqual([r["id"] for r in refs(project="travel", task="Lisbon trip")["references"]], [17])
        self.assertEqual(refs(project="nowhere", task="animations"), {"project": "nowhere", "name": None, "task": "animations", "total": 0, "references": [], "next_offset": None})
        self.assertTrue(call("project_references", project="folio", task="the and")["result"]["isError"])
        with sqlite3.connect(db.DB_PATH) as conn:  # a held save (its review was not approved) never reaches an agent
            conn.execute("INSERT INTO intake_reviews VALUES(11,'hash','review','{}','2026-10-10')")
        self.assertEqual([r["id"] for r in refs(project="folio")["references"]], [12, 10])
        self.assertTrue(call("get_save", id=11, project="folio")["result"]["isError"])

    def test_project_references_find_the_project_from_the_agents_working_folder(self):
        home = Path(self.temp.name) / "home"
        db.seed_workstreams([
            {"slug": "folio", "name": "Folio", "backlog_path": "~/Projects/folio/docs/beacon-backlog.md"},
            {"slug": "folio-ios", "name": "Folio iOS", "backlog_path": "~/Projects/folio/ios/docs/beacon-backlog.md"},
            {"slug": "folio-stats", "name": "Folio stats", "backlog_path": str(home / "Projects/folio-stats/docs/beacon-backlog.md")},
            {"slug": "notes", "name": "Notes", "backlog_path": "~/Notes/notes/beacon-backlog.md"},
            {"slug": "retired", "name": "Retired", "backlog_path": "~/Projects/retired/docs/beacon-backlog.md", "active": False},
            {"slug": "loose", "name": "Loose", "backlog_path": "loose.md"}])
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, slug in ((20, "folio"), (21, "folio-ios"), (22, "folio-stats"), (23, "notes"), (24, "loose")):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,status_note,raw_json,synced_at) "
                             "VALUES(?,'x',?,'a reference','a reference',?,'@amy','adopted','reference',?,'2026-10-09')",
                             (item_id, str(item_id), f"https://x.com/amy/status/{item_id}", json.dumps({"text": "a reference", "media": []})))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT ?,id,0.8,1 FROM workstreams WHERE slug=?",
                             (item_id, slug))

        def ask(**arguments):
            with patch.dict(os.environ, {"HOME": str(home)}):
                return call("project_references", **arguments)["result"]

        def ids(**arguments):
            return [r["id"] for r in ask(**arguments)["structuredContent"]["references"]]

        for folder, expected in ((f"{home}/Projects/folio", [20]),
                                 (f"{home}/Projects/folio/src/components", [20]),
                                 (f"{home}/Projects/folio/ios", [21]),
                                 (f"{home}/Projects/folio/ios/App/Views", [21]),
                                 (f"{home}/Projects/folio-stats/src", [22]),
                                 (f"{home}/Notes/notes", [23]),
                                 (f"{home}/Notes/notes/2026/october", [23]),
                                 ("~/Projects/folio/src", [20]),
                                 ("~/Projects/folio/ios/", [21]),
                                 (f"{home}/Projects/marginalia", []),
                                 (f"{home}/Projects/retired", []),
                                 (f"{home}/Projects", []),
                                 (str(home), []),
                                 ("loose", [])):
            self.assertEqual(ids(folder=folder), expected, folder)
        found = ask(folder="/nowhere/at/all", task="animations")["structuredContent"]
        self.assertEqual(found, {"project": None, "name": None, "task": "animations", "total": 0, "references": [], "next_offset": None})
        found = ask(folder=f"{home}/Projects/folio/ios/App", task="animations")["structuredContent"]
        self.assertEqual((found["project"], found["name"]), ("folio-ios", "Folio iOS"))
        repo = home / "Projects/folio"
        repo.mkdir(parents=True)
        checkout = home / "worktrees/alternate"
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                        "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "test: fixture"],
                       cwd=repo, check=True)
        subprocess.run(["git", "worktree", "add", "--detach", "-q", str(checkout)], cwd=repo, check=True)
        (checkout / "ios").mkdir()
        self.assertEqual(ids(folder=str(checkout)), [20])
        self.assertEqual(ids(folder=str(checkout / "ios")), [21])
        self.assertEqual(ids(project="folio", folder=f"{home}/Projects/folio/ios"), [20])
        with patch.object(mcp.references.settings, "path", return_value=str(home / "Projects")), \
             patch.object(mcp.references.settings, "load", return_value={"workstreams": [
                 {"slug": "folio", "aliases": ["folio-public"]},
             ]}):
            self.assertEqual(ids(folder=f"{home}/Projects/folio-public/src"), [20])
        self.assertTrue(ask(task="animations")["isError"])

    def test_searches_page_with_offset_until_next_offset_is_none(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id in range(20, 25):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,raw_json,synced_at) "
                             "VALUES(?,'x',?,'oak stool','oak stool',?,'@amy','adopted',?,'2026-10-09')",
                             (item_id, str(item_id), f"https://x.com/amy/status/{item_id}",
                              json.dumps({"text": "oak stool", "author": {"name": "Amy"}, "media": [
                                  {"type": "photo", "url": "https://pbs.twimg.com/media/stool.jpg", "width": 800, "height": 1000}]})))

        def page(name, **arguments):
            return call(name, **arguments)["result"]["structuredContent"]

        first = page("search_library", query="stool", limit=2)
        second = page("search_library", query="stool", limit=2, offset=first["next_offset"])
        last = page("search_library", query="stool", limit=2, offset=second["next_offset"])
        self.assertEqual((first["total"], first["next_offset"], second["next_offset"], last["next_offset"]), (5, 2, 4, None))
        self.assertEqual(sorted(save["id"] for p in (first, second, last) for save in p["saves"]), [20, 21, 22, 23, 24])
        with patch.object(vectors, "_run", return_value=json.dumps([[24, 0.5], [23, 0.4], [22, 0.3]])):
            meaning = page("search_library", query="seat", mode="meaning", limit=2, offset=2)
            similar = page("find_similar", id=1, limit=2, offset=2)
        self.assertEqual(([s["id"] for s in meaning["saves"]], meaning["next_offset"]), ([22], None))
        self.assertEqual(([s["id"] for s in similar["saves"]], similar["next_offset"]), ([22], None))
        for bad in (-1, "2", 1.5):
            self.assertTrue(call("search_library", query="stool", offset=bad)["result"]["isError"])

    def test_project_references_page_with_offset(self):
        db.seed_workstreams([{"slug": "folio", "name": "Folio", "backlog_path": "m.md"}])
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id in (30, 31, 32):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,status_note,raw_json,synced_at) "
                             "VALUES(?,'x',?,'ref','ref',?,'@amy','adopted','reference',?,'2026-10-09')",
                             (item_id, str(item_id), f"https://x.com/amy/status/{item_id}",
                              json.dumps({"text": "ref", "media": [], "author": {"name": "Amy"}})))
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT ?,id,0.8,1 FROM workstreams WHERE slug='folio'", (item_id,))
        first = call("project_references", project="folio", limit=2)["result"]["structuredContent"]
        last = call("project_references", project="folio", limit=2, offset=first["next_offset"])["result"]["structuredContent"]
        self.assertEqual(([r["id"] for r in first["references"]], first["next_offset"]), ([32, 31], 2))
        self.assertEqual(([r["id"] for r in last["references"]], last["next_offset"]), ([30], None))

    def test_keep_link_and_picture_can_file_the_save_on_a_shelf(self):
        picture = Path(self.temp.name) / "bowl.png"
        picture.write_bytes(b"\x89PNG fake")
        with patch.object(adds, "_download", return_value=(b"<title>A lamp</title>", "text/html")), \
             patch.object(vectors, "index_later"):
            lamp = call("keep_link", url="https://example.com/lamp", shelf="home")["result"]["structuredContent"]
            bowl = call("keep_picture", path=str(picture), shelf="food")["result"]["structuredContent"]
            plain = call("keep_link", url="https://example.com/plain")["result"]["structuredContent"]
            bad = call("keep_link", url="https://example.com/nope", shelf="garage")["result"]
        on = lambda name: [entry["id"] for entry in db.get_library(area=name)["items"]]
        self.assertEqual((on("home"), on("food")), ([lamp["id"]], [bowl["id"]]))
        self.assertIsNone(next(e for e in db.get_library()["items"] if e["id"] == plain["id"])["area"])
        self.assertTrue(bad["isError"])
        self.assertIn("garage", bad["content"][0]["text"])
        self.assertEqual(call("keep_link", url="https://example.com/lamp")["result"]["structuredContent"]["id"], lamp["id"])  # re-keeping w/o shelf keeps the shelf
        self.assertEqual(db.get_library(area="home")["total"], 1)

    def test_get_save_shows_the_agent_its_pictures_not_just_their_links(self):
        adds.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        (adds.MEDIA_DIR / "abc.jpg").write_bytes(b"\xff\xd8 local pasta")
        local = call("get_save", id=2, pictures=True)["result"]
        self.assertEqual(local["content"][1], {"type": "image", "mimeType": "image/jpeg",
                                               "data": base64.b64encode(b"\xff\xd8 local pasta").decode()})
        self.assertNotIn("data", json.dumps(local["structuredContent"]))  # the bytes ride in content only

        with patch.object(adds, "_download", return_value=(b"\xff\xd8 chair", "image/jpeg")) as fetched:
            remote = call("get_save", id=1, pictures=True)["result"]
        # X serves a small rendition on request, which keeps a save's pictures well under the agent's budget.
        fetched.assert_called_once_with("https://pbs.twimg.com/media/chair.jpg?name=small")
        self.assertEqual([block["type"] for block in remote["content"]], ["text", "image"])

        with patch.object(adds, "_download", side_effect=OSError("timed out")):
            offline = call("get_save", id=1, pictures=True)["result"]
        with patch.object(adds, "_download", return_value=(b"x" * (mcp.MAX_PICTURE + 1), "image/jpeg")):
            huge = call("get_save", id=1, pictures=True)["result"]
        with patch.object(adds, "_download", return_value=(b"<html>", "text/html")):
            not_a_picture = call("get_save", id=1, pictures=True)["result"]
        for result in (offline, huge, not_a_picture):
            self.assertFalse(result["isError"])
            self.assertEqual([block["type"] for block in result["content"]], ["text", "text"])
            self.assertIn("Couldn't show 1", result["content"][1]["text"])
        with patch.object(adds, "_download") as fetched:
            plain = call("get_save", id=1)["result"]
        fetched.assert_not_called()
        self.assertEqual(len(plain["content"]), 1)
        outside = Path(self.temp.name) / "outside.jpg"
        outside.write_bytes(b"\xff\xd8 outside the library")
        (adds.MEDIA_DIR / "abc.jpg").unlink()
        (adds.MEDIA_DIR / "abc.jpg").symlink_to(outside)
        linked = call("get_save", id=2, pictures=True)["result"]
        self.assertFalse(any(block["type"] == "image" for block in linked["content"]))

    def test_get_save_shows_at_most_four_pictures(self):
        photos = [{"type": "photo", "url": f"https://pbs.twimg.com/media/p{n}.png?format=png&name=large"} for n in range(6)]
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,raw_json,synced_at) "
                         "VALUES(40,'x','40','six','six','https://x.com/amy/status/40','@amy','adopted',?,'2026-10-09')",
                         (json.dumps({"text": "six", "media": photos, "author": {"name": "Amy"}}),))
        with patch.object(adds, "_download", return_value=(b"\x89PNG", "image/png")) as fetched:
            result = call("get_save", id=40, pictures=True)["result"]
        self.assertEqual(fetched.call_args_list[0].args[0], "https://pbs.twimg.com/media/p0.png?format=png&name=small")
        self.assertEqual([block["type"] for block in result["content"]], ["text"] + ["image"] * 4)

    def test_keep_answers_once_the_save_lands_and_indexes_afterwards(self):
        """An agent's tool call can time out; reading the save for search can take 30 minutes."""
        release, indexed, read = threading.Event(), threading.Event(), []

        def slow_index(limit, ids):
            release.wait(10)
            read.append((ids, db.current_path()))
            indexed.set()

        picture = Path(self.temp.name) / "bowl.png"
        picture.write_bytes(b"\x89PNG fake")
        with patch.object(adds, "_download", return_value=(b"<title>A lamp</title>", "text/html")), \
             patch.object(vectors, "index", side_effect=slow_index):
            lamp = call("keep_link", url="https://example.com/lamp")["result"]["structuredContent"]
            self.assertFalse(indexed.is_set())  # answered while indexing was still running
            self.assertEqual(db.get_library()["items"][0]["id"], lamp["id"])
            release.set()
            self.assertTrue(indexed.wait(5))
            indexed.clear()
            bowl = call("keep_picture", path=str(picture))["result"]["structuredContent"]
            self.assertTrue(indexed.wait(5))
        # Each save is read in the database it was kept in.
        self.assertEqual(read, [([lamp["id"]], db.DB_PATH), ([bowl["id"]], db.DB_PATH)])

    def test_a_keep_that_fails_is_an_error_and_nothing_is_indexed(self):
        with patch.object(adds, "add_link", side_effect=ValueError("Only web links (http or https) can be added.")), \
             patch.object(vectors, "index_later") as later:
            result = call("keep_link", url="file:///etc/passwd")["result"]
        self.assertTrue(result["isError"])
        later.assert_not_called()

    def test_keep_link_adds_to_the_library(self):
        with patch.object(adds, "_download", return_value=(b"<title>A lamp</title>", "text/html")), \
             patch.object(vectors, "index_later"):
            kept = call("keep_link", url="https://example.com/lamp")["result"]["structuredContent"]
        self.assertEqual(db.get_library()["items"][0]["id"], kept["id"])
