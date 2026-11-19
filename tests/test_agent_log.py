"""Beacon keeps its own record of what agents ask it, so nobody has to fill in a usage log by hand.

Twice a hand-written log (the July usage test, the October activation log) stayed empty, so whether
agents use the library at all was unknowable. Every tool call is now written to beacon.db, on the Mac
only, and `beacon stats` reads it back.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import adds, db, mcp


def call(name, **arguments):
    return mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


class LibraryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value, replacement in ((db, "DB_PATH", str(root / "beacon.db")), (adds, "MEDIA_DIR", root / "media")):
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        session = patch.dict(mcp.SESSION, {"id": "s1", "client": None})
        session.start()
        self.addCleanup(session.stop)
        db.init_db()
        photo = {"type": "photo", "url": "https://pbs.twimg.com/media/chair.jpg"}
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, text in ((1, "a walnut lounge chair"), (2, "an oak dining chair")):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,author,status,raw_json,synced_at) "
                             "VALUES(?,'x',?,?,?,?,'@amy','adopted',?,'2026-10-09')",
                             (item_id, str(item_id), text, text, f"https://x.com/amy/status/{item_id}",
                              json.dumps({"text": text, "media": [photo]})))

    def rows(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM agent_calls ORDER BY id")]


class AgentLogTest(LibraryCase):

    def test_every_tool_call_is_recorded_with_what_it_found(self):
        mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code", "version": "2.1"}}})
        call("search_library", query="chair")
        call("get_save", id=2, pictures=False)
        call("search_library", query="")  # an error is recorded too
        first, second, third = self.rows()
        self.assertEqual((first["session"], first["client"], first["tool"], first["found"], json.loads(first["ids"]), first["ok"]),
                         ("s1", "claude-code 2.1", "search_library", 2, [1, 2], 1))
        self.assertFalse(first.get("arguments"))
        self.assertEqual((second["tool"], json.loads(second["ids"]), second["ok"]), ("get_save", [2], 1))
        self.assertEqual((third["ok"], third["found"]), (0, None))
        self.assertTrue(first["called_at"].endswith("Z"))

    def test_search_terms_and_save_arguments_are_never_stored(self):
        call("search_library", query="chair " * 200)
        call("get_save", id=1)
        stored = self.rows()
        self.assertFalse(stored[0].get("arguments"))
        self.assertFalse(stored[1].get("arguments"))

    def test_a_broken_log_never_breaks_the_tool(self):
        with patch.object(db, "log_agent_call", side_effect=sqlite3.OperationalError("database is locked")):
            reply = call("search_library", query="chair")
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(len(reply["result"]["structuredContent"]["saves"]), 2)

    def test_usage_counts_calls_empty_searches_and_searches_that_led_to_an_open(self):
        call("search_library", query="chair")
        call("get_save", id=1, pictures=False)  # opened a result of that search
        call("search_library", query="reorder")  # found nothing
        call("search_library", query="oak")  # found something, never opened
        usage = db.agent_usage(days=7)
        self.assertEqual(usage["calls"], 4)
        self.assertEqual(usage["sessions"], 1)
        self.assertEqual(usage["by_tool"], {"search_library": 3, "get_save": 1})
        self.assertEqual(usage["searches"], 3)
        self.assertEqual(usage["empty_searches"], 1)
        self.assertEqual(usage["searches_opened"], 1)
        self.assertEqual(usage["errors"], 0)

    def test_usage_ignores_calls_older_than_the_window(self):
        call("search_library", query="chair")
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("UPDATE agent_calls SET called_at='2020-01-01T00:00:00Z'")
        self.assertEqual(db.agent_usage(days=7)["calls"], 0)


class AgentGuidanceTest(LibraryCase):
    """What the server tells an agent: when to use it, and what to try when a search comes back empty."""

    def test_the_handshake_says_when_to_use_beacon(self):
        hello = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
        self.assertIn("project_references", hello["instructions"])
        self.assertIn("get_save", hello["instructions"])

    def test_an_empty_word_search_says_what_to_try_next(self):
        found = call("search_library", query="reorder")["result"]["structuredContent"]
        self.assertEqual(found["saves"], [])
        self.assertIn("meaning", found["hint"])
        self.assertNotIn("hint", call("search_library", query="chair")["result"]["structuredContent"])

    def test_every_tool_says_when_to_use_it_or_what_it_cannot_do(self):
        tools = {tool["name"]: tool["description"] for tool in mcp.TOOLS}
        self.assertIn("get_save", tools["search_library"])
        self.assertIn("project_references", tools["search_library"])
        self.assertIn("no picture", tools["find_similar"])
        self.assertIn("still", tools["get_save"])


if __name__ == "__main__":
    unittest.main()
