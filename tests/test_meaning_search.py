import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

from lib import search


class MeaningSearchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "meaning.db")
        conn = sqlite3.connect(self.path)
        conn.executescript(
            """
            CREATE TABLE items (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                title TEXT, body TEXT, url TEXT, author TEXT, status TEXT NOT NULL
            );
            CREATE TABLE workstreams (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, active INTEGER);
            CREATE TABLE item_workstreams (item_id INTEGER, workstream_id INTEGER);
            """
        )
        conn.execute("INSERT INTO workstreams VALUES (1, 'alpha', 'Alpha', 1)")
        conn.execute("INSERT INTO workstreams VALUES (2, 'paused', 'Paused', 0)")
        rows = [
            (1, "x", "one", "WAL notes", "Make a consistent SQLite backup while writes continue.", "https://one", "a", "adopted"),
            (2, "x", "two", "Dinner", "Roast carrots with olive oil.", "https://two", "b", "queued"),
            (3, "x", "three", "Rejected", "Create a consistent backup.", "https://three", "c", "rejected"),
            (4, "x", "four", "Tail", "A short title with a changing tail", "https://four", "d", "reviewed"),
        ]
        conn.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO item_workstreams VALUES (1, 1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_preview_builds_bounded_request_without_network(self):
        result = search.semantic_search(
            "preserve a coherent copy without pausing transactions", self.path,
            scan_limit=4, limit=2, preview=True,
        )
        self.assertEqual(result["call_count"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["review"], [])
        self.assertEqual(result["excluded_ids"], [])
        self.assertEqual(len(result["requests"]), 1)
        self.assertEqual([item["id"] for item in result["requests"][0]["state"]["items"]], [4, 2, 1])
        payload = result["requests"][0]
        self.assertEqual(payload["state"]["items"][0].keys(), {"id", "title", "excerpt"})
        self.assertNotIn("https://one", json.dumps(payload))
        self.assertNotIn("adopted", json.dumps(payload))
        self.assertLessEqual(len(search.useful.encode_request(payload)), 24000)

    def test_window_counts_cursor_uses_last_examined_id(self):
        result = search.semantic_search("backup", self.path, scan_limit=2, limit=1, preview=True)
        self.assertEqual(result["examined_count"], 2)
        self.assertEqual(result["eligible_total"], 3)
        self.assertEqual(result["skipped_newer_count"], 0)
        self.assertEqual(result["next_before_id"], 2)

    def test_unknown_workstream_and_missing_db_do_not_create(self):
        with self.assertRaises(ValueError):
            search.semantic_search("backup", self.path, workstream="missing", preview=True)
        missing = os.path.join(self.tempdir.name, "missing.db")
        with self.assertRaises((sqlite3.OperationalError, ValueError)):
            search.semantic_search("backup", missing, preview=True)
        self.assertFalse(os.path.exists(missing))

    def test_query_validation_and_consent_fail_before_upload(self):
        for query in ("", "   ", "x" * 2001):
            with self.subTest(query=query), self.assertRaises(ValueError):
                search.semantic_search(query, self.path, preview=True)
        with mock.patch.object(search.useful, "build_opener") as network:
            with self.assertRaisesRegex(ValueError, "request attempt.*approval") as error:
                search.semantic_search("AI UX", self.path)
        network.assert_not_called()
        self.assertNotIn("1 call(s)", str(error.exception))

    def _noul_response(self, request, probability=0.9, **_):
        return {
            "model": search.useful.MODEL,
            "answers": {key: {"type": "noul", "noul": probability}
                        for key in request["questions"]},
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }

    def test_tail_change_discards_result_and_malformed_response_warns(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE items SET body=? WHERE id=1", ("x" * 1000,))
        def changed(request, **_):
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute("UPDATE items SET body=body || ' changed-tail' WHERE id=1")
            return self._noul_response(request)

        with mock.patch.object(search.useful, "call_typesafe", side_effect=changed) as call:
            with self.assertRaisesRegex(ValueError, "may have been billed.*changed"):
                search.semantic_search("AI UX", self.path, allow_upload=True)
        self.assertEqual(call.call_count, 1)

        with mock.patch.object(search.useful, "call_typesafe", return_value={"model": search.useful.MODEL, "answers": {}, "usage": {"input_tokens": 0, "output_tokens": 0}}) as call:
            with self.assertRaisesRegex(ValueError, "may have been billed"):
                search.semantic_search("AI UX", self.path, allow_upload=True)
        self.assertEqual(call.call_count, 1)

    def test_second_batch_is_not_uploaded_after_mid_run_change(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for item_id in range(5, 30):
                conn.execute(
                    "INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
                    (item_id, "x", str(item_id), f"Item {item_id}", "Evidence", f"https://{item_id}", None, "queued"),
                )
            conn.commit()

        preview = search.semantic_search("AI UX", self.path, scan_limit=29, preview=True)
        self.assertEqual(len(preview["requests"]), 2)
        self.assertTrue(all(len(search.useful.encode_request(request)) <= 24000 for request in preview["requests"]))

        calls = []
        def first_call(request, **_):
            calls.append(request)
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute("UPDATE items SET body=body || ' changed' WHERE id=1")
            return self._noul_response(request)

        with mock.patch.object(search.useful, "call_typesafe", side_effect=first_call):
            with self.assertRaisesRegex(ValueError, "changed"):
                search.semantic_search("AI UX", self.path, scan_limit=29, allow_upload=True)
        self.assertEqual(len(calls), 1)

    def test_cli_meaning_bypasses_init_and_seed(self):
        import beacon
        output = StringIO()
        with mock.patch.object(sys, "argv", ["beacon.py", "search", "backup", "--meaning", "--db", self.path, "--preview", "--json"]), \
             mock.patch.object(beacon.db, "init_db", side_effect=AssertionError("init_db called")), \
             mock.patch.object(beacon.db, "seed_workstreams", side_effect=AssertionError("seed called")), \
             redirect_stdout(output):
            beacon.main()
        self.assertEqual(json.loads(output.getvalue())["network_calls"], 0)

    def test_cli_meaning_subprocess_is_read_only(self):
        with open(self.path, "rb") as handle:
            before = handle.read()
        completed = subprocess.run(
            [sys.executable, "beacon.py", "search", "backup", "--meaning", "--db", self.path, "--preview", "--json"],
            text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["network_calls"], 0)
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_legacy_search_keeps_its_existing_dispatch(self):
        import beacon
        with mock.patch.object(sys, "argv", ["beacon.py", "search", "backup"]), \
             mock.patch.object(beacon, "search_items", return_value=[]), \
             mock.patch.object(beacon.db, "init_db"), \
             mock.patch.object(beacon.db, "seed_workstreams"), \
             redirect_stdout(StringIO()) as output:
            beacon.main()
        self.assertIn("No results found", output.getvalue())

    def test_cli_rejects_explicit_meaning_options_before_legacy_writes(self):
        import beacon
        for option in (["--scan-limit", "0"], ["--before-id", "0"], ["--db", beacon.db.DB_PATH]):
            with self.subTest(option=option), \
                 mock.patch.object(sys, "argv", ["beacon.py", "search", "backup", *option]), \
                 mock.patch.object(beacon.db, "init_db") as init, \
                 mock.patch.object(beacon.db, "seed_workstreams"), \
                 mock.patch.object(beacon, "search_items", return_value=[]), \
                 redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as error:
                    beacon.main()
                self.assertEqual(error.exception.code, 2)
                init.assert_not_called()

    def test_cli_rejects_zero_scan_limit(self):
        completed = subprocess.run(
            [sys.executable, "beacon.py", "search", "backup", "--meaning", "--db", self.path,
             "--scan-limit", "0", "--preview"], text=True, capture_output=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("scan_limit", completed.stderr)

    def test_empty_database_argument_never_uses_default_library(self):
        import beacon
        with mock.patch.object(sys, "argv", ["beacon.py", "search", "backup", "--meaning", "--db", "", "--preview"]), \
             mock.patch.object(beacon.db, "DB_PATH", self.path), \
             redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                beacon.main()
        self.assertEqual(error.exception.code, 2)

    def test_coverage_after_cursor_and_empty_window(self):
        result = search.semantic_search("AI UX", self.path, scan_limit=1, before_id=4, preview=True)
        self.assertEqual(result["eligible_total"], 3)
        self.assertEqual(result["examined_count"], 1)
        self.assertEqual(result["skipped_newer_count"], 1)
        self.assertEqual(result["remaining_older_count"], 1)
        self.assertEqual(result["unsearched_count"], 2)
        self.assertEqual(result["next_before_id"], 2)
        with mock.patch.object(search.useful, "call_typesafe") as call:
            empty = search.semantic_search("AI UX", self.path, before_id=1)
        self.assertEqual(empty["examined_count"], 0)
        self.assertEqual(empty["unsearched_count"], 3)
        self.assertIsNone(empty["next_before_id"])
        call.assert_not_called()

    def test_all_unicode_batches_are_planned_before_any_upload(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for item_id in range(5, 106):
                conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
                             (item_id, "x", str(item_id), "🎵" * 240, "🎵" * 800,
                              "https://example.com", None, "queued"))
        planned = search.semantic_search("AI UX", self.path, scan_limit=20, preview=True)
        self.assertGreater(len(planned["requests"]), 1)
        self.assertTrue(all(n <= 24000 for n in planned["request_bytes"]))
        ids = [item["id"] for request in planned["requests"] for item in request["state"]["items"]]
        self.assertEqual(ids, list(range(105, 85, -1)))
        with mock.patch.object(search.useful, "call_typesafe") as call:
            with self.assertRaisesRegex(ValueError, "more than 5"):
                search.semantic_search("AI UX", self.path, scan_limit=100, allow_upload=True)
        call.assert_not_called()

    def test_multiple_batches_preserve_matches_uncertainty_and_usage(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for item_id in range(5, 30):
                conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
                             (item_id, "x", str(item_id), "Reference", "Saved source",
                              "https://example.com", None, "queued"))
        def respond(request, **_):
            result = self._noul_response(request, .1)
            for key in result["answers"]:
                result["answers"][key]["noul"] = {"item_1": .95, "item_4": .95, "item_2": .5}.get(key, .1)
            return result
        with mock.patch.object(search.useful, "call_typesafe", side_effect=respond) as call:
            result = search.semantic_search("AI UX", self.path, scan_limit=25, limit=1, allow_upload=True)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["review"], [])
        self.assertEqual(result["additional_match_count"], 0)
        self.assertEqual(result["next_before_id"], 5)
        with mock.patch.object(search.useful, "call_typesafe", side_effect=respond):
            result = search.semantic_search("AI UX", self.path, scan_limit=40, limit=1, allow_upload=True)
        self.assertEqual([item["id"] for item in result["matches"]], [1])
        self.assertEqual([item["id"] for item in result["review"]], [2])
        self.assertEqual(result["additional_match_count"], 1)
        self.assertEqual(result["usage"], {"input_tokens": 14, "output_tokens": 6})

    def test_invalid_probabilities_and_provider_failure_release_nothing(self):
        for value in (float("nan"), float("inf"), True, -.1, 1.1, "0.9"):
            with self.subTest(value=value), \
                 mock.patch.object(search.useful, "call_typesafe", side_effect=lambda request, **_: self._noul_response(request, value)):
                with self.assertRaises(ValueError):
                    search.semantic_search("AI UX", self.path, allow_upload=True)
        with mock.patch.object(search.useful, "call_typesafe", side_effect=ValueError("service unavailable")) as call:
            with self.assertRaisesRegex(ValueError, "no results accepted"):
                search.semantic_search("AI UX", self.path, allow_upload=True)
        self.assertEqual(call.call_count, 1)

    def test_changed_url_status_or_workstream_stops_before_first_upload(self):
        build = search._build_requests
        mutations = ["UPDATE items SET url='https://changed' WHERE id=1",
                     "UPDATE items SET status='rejected' WHERE id=1",
                     "DELETE FROM item_workstreams WHERE item_id=1"]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with closing(sqlite3.connect(self.path)) as conn, conn:
                    conn.execute("UPDATE items SET status='adopted',url='https://one' WHERE id=1")
                    conn.execute("DELETE FROM item_workstreams WHERE item_id=1")
                    conn.execute("INSERT INTO item_workstreams VALUES (1,1)")
                def change_after_planning(query, window):
                    requests = build(query, window)
                    with closing(sqlite3.connect(self.path)) as conn, conn:
                        conn.execute(mutation)
                    return requests
                with mock.patch.object(search, "_build_requests", side_effect=change_after_planning), \
                     mock.patch.object(search.useful, "call_typesafe") as call:
                    with self.assertRaisesRegex(ValueError, "changed"):
                        search.semantic_search("backup", self.path, workstream="alpha", allow_upload=True)
                call.assert_not_called()

    def test_large_source_is_capped_without_losing_its_fingerprint(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE items SET body=? WHERE id=1", ("a" * 100_000,))
        with mock.patch.object(search.useful, "call_typesafe", side_effect=self._noul_response):
            result = search.semantic_search("backup", self.path, workstream="alpha", allow_upload=True)
        item = result["matches"][0]
        self.assertEqual(item["excerpt"], "a" * 800)
        self.assertTrue(item["excerpt_truncated"])
        self.assertEqual(item["excerpt_end"], 800)
        self.assertEqual(len(item["source_fingerprint"]), 64)

    def test_console_shows_openable_matches_and_uncertain_evidence(self):
        import beacon
        def respond(request, **_):
            response = self._noul_response(request)
            for key, value in {"item_1": .95, "item_2": .5, "item_4": .1}.items():
                response["answers"][key]["noul"] = value
            return response
        with mock.patch.object(sys, "argv", ["beacon.py", "search", "backup", "--meaning", "--db", self.path, "--allow-upload"]), \
             mock.patch.object(search.useful, "call_typesafe", side_effect=respond), \
             redirect_stdout(StringIO()) as output:
            beacon.main()
        self.assertIn("https://one", output.getvalue())
        self.assertIn("Dinner", output.getvalue())
        self.assertIn("800", output.getvalue())


if __name__ == "__main__":
    unittest.main()
