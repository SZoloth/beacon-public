"""Offline tests: model responses here are fixtures, never quality evidence."""

import copy
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from lib import db, useful
from lib import evaluate


class UsefulTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        with patch.object(db, "DB_PATH", str(self.path)):
            db.init_db()
            db.seed_workstreams([{"slug": "demo", "name": "Demo", "backlog_path": "unused"}])
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for index, (title, body, status) in enumerate([
                ("SQLite backup guide", "Create a consistent SQLite backup while writes continue.", "queued"),
                ("Database snapshots", "Preserve a consistent copy of a running data store.", "adopted"),
                ("SQLite backup rejected", "SQLite backup", "rejected"),
                ("SQLite backup stale", "SQLite backup", "stale"),
                ("Pasta recipe", "Boil water for dinner.", "queued"),
            ], 1):
                conn.execute("INSERT INTO items (id,source,external_id,title,body,url,synced_at,status) VALUES (?, 'manual', ?, ?, ?, ?, '2026-09-20', ?)",
                             (index, str(index), title, body, f"https://example.com/{index}", status))
                conn.execute("INSERT INTO item_workstreams (item_id,workstream_id) VALUES (?,1)", (index,))
        self.before = self.path.read_bytes()

    def candidates(self):
        return useful.retrieve_candidates("SQLite backup", self.path, workstream="demo", limit=5)

    def response(self, request, scores=None):
        scores = scores or [(0.01, 0.02, 0.97)] * len(request["questions"])
        return {"model": useful.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 50},
                "answers": {key: {"type": "score", "score": p[1] + 2*p[2],
                                  "confidence": 0.9 if max(p) > 0.8 else 0.1,
                                  "probabilities": {str(i): v for i, v in enumerate(p)},
                                  "legend": {str(i): v for i, v in enumerate(question["criteria"])}}
                            for (key, question), p in zip(request["questions"].items(), scores)}}

    def test_retrieval_preserves_database_and_filters_terminal_items(self):
        candidates = self.candidates()
        self.assertEqual({row["id"] for row in candidates}, {1, 2, 5})
        self.assertEqual(candidates[0]["id"], 1)
        self.assertEqual(candidates[0]["retrieval"], "lexical")
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertNotIn("raw_json", candidates[0])

    def test_empty_no_match_fts_punctuation_and_unknown_workstream(self):
        self.assertEqual(useful.retrieve_candidates('unicorn " OR *', self.path), [])
        for task in ["", "the and to", " "]:
            with self.assertRaises(ValueError):
                useful.retrieve_candidates(task, self.path)
        with self.assertRaises(ValueError):
            useful.retrieve_candidates("backup", self.path, workstream="unknown")
        for limit in [-1, 0, 21]:
            with self.assertRaises(ValueError):
                useful.retrieve_candidates("backup", self.path, limit=limit)

    def test_missing_database_is_not_created(self):
        path = self.path.with_name("missing.db")
        with self.assertRaises(sqlite3.OperationalError):
            useful.retrieve_candidates("backup", path)
        self.assertFalse(path.exists())

    def test_request_is_bounded_and_contains_only_source_fields(self):
        candidates = self.candidates()
        request = useful.build_request("SQLite backup", candidates)
        self.assertEqual(request["model"], useful.MODEL)
        self.assertEqual(len(request["questions"]), 3)
        self.assertNotIn("url", request["state"]["items"][0])
        self.assertNotIn("status", request["state"]["items"][0])
        self.assertNotIn("expected", json.dumps(request))
        for index, question in enumerate(request["questions"].values()):
            self.assertIn(f"`items[{index}]`", question["instructions"])
        with self.assertRaises(ValueError):
            useful.build_request("x" * 2001, candidates)
        too_big = copy.deepcopy(request)
        too_big["state"]["task"] = "x" * 100000
        with self.assertRaises(ValueError):
            useful.encode_request(too_big)

    def test_relevance_and_uncertainty_are_separate_from_execution(self):
        candidates = self.candidates()
        request = useful.build_request("SQLite backup", candidates)
        response = self.response(request, [(0.01, 0.02, 0.97), (0.3, 0.3, 0.4), (0.97, 0.02, 0.01)])
        result = useful.rank_response(request, candidates, response)
        self.assertEqual([row["id"] for row in result["matches"]], [1])
        self.assertEqual(len(result["review"]), 1)
        self.assertEqual(result["quality"], "unmeasured")
        self.assertEqual(result["project"], "beacon")
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.000042)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_malformed_answers_fail_closed(self):
        request = useful.build_request("SQLite backup", self.candidates())
        base = self.response(request)
        first = next(iter(request["questions"]))
        corruptions = [
            lambda r: r["answers"].pop(first),
            lambda r: r.update(model="unknown-model"),
            lambda r: r["answers"][first].update(score=float("nan")),
            lambda r: r["answers"][first].update(confidence=True),
            lambda r: r["answers"][first].update(score=0.0),
            lambda r: r["answers"][first].update(probabilities={"0": 1, "1": 1, "2": 1}),
            lambda r: r["answers"][first].update(type="choice"),
        ]
        for corrupt in corruptions:
            response = copy.deepcopy(base)
            corrupt(response)
            with self.subTest(response=response), self.assertRaises(ValueError):
                useful.rank_response(request, self.candidates(), response)

    def test_network_requires_explicit_upload_and_key(self):
        request = useful.build_request("SQLite backup", self.candidates())
        with patch.dict(os.environ, {}, clear=True), patch.object(useful, "build_opener") as opener:
            for allow in [False, True]:
                with self.assertRaises(ValueError):
                    useful.call_typesafe(request, allow_upload=allow)
            opener.assert_not_called()

    def test_http_contract_and_errors_do_not_leak_key_or_payload(self):
        request = useful.build_request("SQLite backup", self.candidates())
        response = self.response(request)
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}), patch.object(useful, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            self.assertEqual(useful.call_typesafe(request, allow_upload=True), response)
            call = opener.return_value.open.call_args
            self.assertEqual(call.args[0].full_url, "https://api.typesafe.ai/v1/systemone")
            self.assertEqual(json.loads(call.args[0].data), request)
            self.assertEqual(call.kwargs["timeout"], 20)
            opener.return_value.open.side_effect = HTTPError("url", 429, "test-secret", {}, None)
            with self.assertRaisesRegex(ValueError, "429") as error:
                useful.call_typesafe(request, allow_upload=True)
            self.assertNotIn("test-secret", str(error.exception))
            self.assertEqual(opener.return_value.open.call_count, 2)  # No implicit retries.

    def test_redirects_and_invalid_json_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "redirected"):
            useful._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")
        request = useful.build_request("SQLite backup", self.candidates())
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-secret"}), patch.object(useful, "build_opener") as opener:
            for body in [b"not json", b"\xff", b"x" * (useful.MAX_RESPONSE_BYTES + 1)]:
                opener.return_value.open.return_value = io.BytesIO(body)
                with self.assertRaises(ValueError):
                    useful.call_typesafe(request, allow_upload=True)

    def test_all_irrelevant_is_no_match_not_an_arbitrary_winner(self):
        candidates = self.candidates()
        request = useful.build_request("SQLite backup", candidates)
        result = useful.rank_response(request, candidates, self.response(request, [(0.98, 0.01, 0.01)] * len(candidates)))
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["review"], [])
        self.assertEqual(len(result["excluded_ids"]), 3)

    def test_excerpts_are_exact_source_slices(self):
        body = "x" * 600 + " sqlite backup " + "z" * 50
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE items SET body=? WHERE id=1", (body,))
        candidate = self.candidates()[0]
        self.assertEqual(candidate["excerpt"], body[candidate["excerpt_start"]:candidate["excerpt_end"]])
        self.assertTrue(candidate["excerpt_truncated"])

    def cli(self, *extra):
        return subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "beacon.py"),
                               "useful", "SQLite backup", "--db", str(self.path), "--json", *extra],
                              cwd=self.temp.name, capture_output=True, text=True,
                              env={k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"})

    def test_cli_local_preview_and_missing_access_are_read_only(self):
        run = self.cli()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["mode"], "local_baseline")
        self.assertEqual([row["id"] for row in result["matches"]], [1])
        preview = self.cli("--provider", "typesafe", "--preview")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("request", json.loads(preview.stdout))
        denied = self.cli("--provider", "typesafe")
        self.assertEqual(denied.returncode, 2)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_synthetic_evaluation_separates_baseline_from_model_quality(self):
        fixture = Path(__file__).parent / "fixtures" / "useful-now.json"
        with patch.object(useful, "call_typesafe") as network:
            result = evaluate.evaluate_useful_fixture(fixture)
            network.assert_not_called()
        self.assertEqual(result["case_count"], 5)
        self.assertEqual(result["project"], "beacon")
        self.assertEqual(result["candidate_recall"], 1)
        self.assertEqual(result["mode"], "local_baseline")
        self.assertEqual(result["live_model_calls"], 0)
        self.assertGreater(result["false_suggestions"], 0)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_cli_typesafe_success_uses_the_same_read_only_path(self):
        import beacon
        candidates = useful.retrieve_candidates("SQLite backup", self.path)
        request = useful.build_request("SQLite backup", candidates)
        output = io.StringIO()
        argv = ["beacon", "useful", "SQLite backup", "--db", str(self.path),
                "--provider", "typesafe", "--allow-upload", "--json"]
        with patch.object(sys, "argv", argv), redirect_stdout(output), patch.object(useful, "call_typesafe", return_value=self.response(request)) as network:
            beacon.main()
        self.assertEqual(json.loads(output.getvalue())["mode"], "typesafe_advisory")
        network.assert_called_once_with(request, allow_upload=True)
        self.assertEqual(self.path.read_bytes(), self.before)


if __name__ == "__main__":
    unittest.main()
