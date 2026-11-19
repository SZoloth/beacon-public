import os
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import beacon


class BeaconCliTestCase(unittest.TestCase):
    def test_condition_commands_preview_without_writes_and_deduplicate_live_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "beacon.db"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, status TEXT, title TEXT, body TEXT)")
                conn.execute("INSERT INTO items VALUES (1, 'queued', 'Backup notes', 'SQLite snapshot guide')")
                conn.execute("CREATE TABLE workstreams (slug TEXT, active INTEGER)")
                conn.execute("INSERT INTO workstreams VALUES ('demo', 1)")
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"id": "e1", "project_slug": "demo", "summary": "Backups are now needed"}))
            base = {"db": str(path), "id": "c1"}
            def run(**kwargs):
                output = io.StringIO()
                with redirect_stdout(output):
                    beacon.cmd_conditions(SimpleNamespace(**base, **kwargs))
                return json.loads(output.getvalue())
            run(action="add", workstream="demo", title="Backup guide", when="Backups needed", author="test")
            before = path.read_bytes()
            args = {"action": "check", "item_id": 1, "event": str(event), "live": False, "allow_upload": False}
            self.assertEqual(run(**args)["network_calls"], 0)
            self.assertEqual(path.read_bytes(), before)
            evaluator = mock.Mock(return_value="met")
            with mock.patch.object(beacon.conditions, "make_typesafe_evaluator", return_value=evaluator):
                self.assertEqual(len(run(**{**args, "live": True})["suggestions"]), 1)
                self.assertTrue(run(**{**args, "live": True})["skipped"])
            self.assertEqual(evaluator.call_count, 1)
            self.assertEqual(len(run(action="list")["suggestions"]), 1)
            with closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute("SELECT status FROM items").fetchone()[0], "queued")
                conn.execute("UPDATE items SET status='rejected'")
            self.assertEqual(run(action="list")["suggestions"], [])

    def test_classification_batch_size_prefers_cli_arg(self):
        config = {"classification": {"batch_size": 15}}
        args = SimpleNamespace(batch_size=4)

        with mock.patch.dict(os.environ, {"BEACON_CLASSIFY_BATCH_SIZE": "7"}):
            self.assertEqual(beacon._classification_batch_size(config, args), 4)

    def test_classification_batch_size_uses_env_override(self):
        config = {"classification": {"batch_size": 15}}
        args = SimpleNamespace(batch_size=None)

        with mock.patch.dict(os.environ, {"BEACON_CLASSIFY_BATCH_SIZE": "7"}):
            self.assertEqual(beacon._classification_batch_size(config, args), 7)

    def test_classification_batch_size_falls_back_to_config(self):
        config = {"classification": {"batch_size": 15}}
        args = SimpleNamespace(batch_size=None)

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(beacon._classification_batch_size(config, args), 15)

    @mock.patch("beacon.classify_items")
    @mock.patch("beacon.load_config")
    def test_cmd_classify_passes_config_workstreams(self, mock_load_config, mock_classify_items):
        config = {
            "classification": {"batch_size": 15},
            "workstreams": [{"slug": "zettel-wiki", "name": "Zettel Wiki", "aliases": ["zettel"]}],
        }
        mock_load_config.return_value = config

        beacon.cmd_classify(SimpleNamespace(batch_size=4))

        mock_classify_items.assert_called_once_with(
            batch_size=4,
            workstreams=config["workstreams"],
        )

    @mock.patch("beacon.fetch_last30days")
    @mock.patch("beacon.fetch_markdown_directory")
    @mock.patch("beacon.fetch_keep_local")
    @mock.patch("beacon.fetch_github_stars")
    @mock.patch("beacon.fetch_x_bookmarks")
    @mock.patch("beacon.load_config")
    def test_cmd_sync_supports_additional_sources(
        self,
        mock_load_config,
        mock_fetch_x,
        mock_fetch_github,
        mock_fetch_keep_local,
        mock_fetch_markdown_directory,
        mock_fetch_last30days,
    ):
        config = {
            "sources": {
                "x": {"default_count": 50},
                "github": {},
                "keep-local": {"db_path": "~/keep.db", "content_root": "~/keep-content", "default_count": 25},
                "wiki": {"root": "~/wiki", "default_count": 20},
                "omnisearch": {"root": "~/omni", "default_count": 10},
                "last30days": {"db_path": "~/research.db", "output_dir": "~/out", "default_count": 15},
            }
        }
        mock_load_config.return_value = config

        beacon.cmd_sync(SimpleNamespace(source="keep-local"))
        beacon.cmd_sync(SimpleNamespace(source="wiki"))
        beacon.cmd_sync(SimpleNamespace(source="omnisearch"))
        beacon.cmd_sync(SimpleNamespace(source="last30days"))

        mock_fetch_keep_local.assert_called_once()
        self.assertEqual(mock_fetch_markdown_directory.call_count, 2)
        mock_fetch_last30days.assert_called_once()
        mock_fetch_x.assert_not_called()
        mock_fetch_github.assert_not_called()

    @mock.patch("beacon.fetch_last30days")
    @mock.patch("beacon.fetch_markdown_directory")
    @mock.patch("beacon.fetch_keep_local")
    @mock.patch("beacon.fetch_github_stars")
    @mock.patch("beacon.fetch_x_bookmarks")
    @mock.patch("beacon.load_config")
    def test_sync_keeps_every_source_when_one_fails(
        self,
        mock_load_config,
        mock_fetch_x,
        mock_fetch_github,
        mock_fetch_keep_local,
        mock_fetch_markdown_directory,
        mock_fetch_last30days,
    ):
        mock_load_config.return_value = {
            "sources": {
                "x": {},
                "github": {},
                "keep-local": {},
                "wiki": {},
                "omnisearch": {},
                "last30days": {},
            }
        }
        mock_fetch_github.side_effect = RuntimeError("github down")
        with self.assertRaises(SystemExit) as raised:
            beacon.cmd_sync(SimpleNamespace(source=None))
        self.assertEqual(raised.exception.code, 1)
        mock_fetch_x.assert_called_once()
        mock_fetch_github.assert_called_once()
        mock_fetch_keep_local.assert_called_once()
        self.assertEqual(mock_fetch_markdown_directory.call_count, 2)
        mock_fetch_last30days.assert_called_once()

    @mock.patch("beacon.fetch_github_stars", side_effect=RuntimeError("GitHub stars didn't sync: gh CLI not found."))
    @mock.patch("beacon.load_config", return_value={"sources": {"github": {}}})
    def test_one_failing_source_ends_with_its_reason_not_a_traceback(self, _config, _fetch):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr", err):
            beacon.cmd_sync(SimpleNamespace(source="github"))
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("sync: GitHub stars didn't sync: gh CLI not found.\n", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    @mock.patch("beacon.fetch_x_bookmarks")
    def test_sync_source_full_x_backfill_ignores_known_id(self, mock_fetch_x):
        beacon._sync_source("x", {"default_count": 1200}, full=True)

        mock_fetch_x.assert_called_once_with(count=1200, stop_at_known=False)

    @mock.patch("beacon.pinterest.fetch_pinterest_boards", return_value=3)
    def test_sync_source_routes_pinterest_with_its_boards(self, mock_pinterest):
        self.assertEqual(beacon._sync_source("pinterest", {"boards": ["ada/kitchen"]}), 3)

        mock_pinterest.assert_called_once_with(["ada/kitchen"])

    def test_typed_classification_does_not_fall_back_on_service_failure(self):
        args = SimpleNamespace(provider="typesafe", limit=1, allow_upload=True, routing_evaluation=None)
        with mock.patch("beacon.load_config", return_value={"workstreams": []}), \
             mock.patch("beacon.intake.classify_pending", side_effect=ValueError("unavailable")), \
             mock.patch("beacon.classify_items") as legacy, \
             mock.patch("beacon.route_items") as router, \
             mock.patch("beacon.cmd_sync"), \
             self.assertRaises(SystemExit):
            beacon.cmd_run(args)
        legacy.assert_not_called()
        router.assert_not_called()

    def test_invalid_routing_gate_stops_before_classification(self):
        args = SimpleNamespace(provider="typesafe", limit=1, allow_upload=True, routing_evaluation="bad.json")
        with mock.patch("beacon.load_config", return_value={"workstreams": []}), \
             mock.patch("beacon.intake.load_routing_evaluation", side_effect=ValueError("mismatch")), \
             mock.patch("beacon.intake.classify_pending") as classify, self.assertRaises(SystemExit):
            beacon.cmd_classify(args)
        classify.assert_not_called()


class KeepIndexingTest(unittest.TestCase):
    """`keep` reads the new save for search unless `beacon serve` asks to do that after answering."""

    def keep(self, no_index):
        output = io.StringIO()
        args = SimpleNamespace(url="https://example.com", image=None, width=None, height=None, note=None,
                               no_index=no_index)
        with mock.patch("lib.adds.add_link", return_value=7), \
             mock.patch("lib.vectors.index") as index, redirect_stdout(output):
            beacon.cmd_keep(args)
        return json.loads(output.getvalue()), index

    def test_keep_indexes_right_away_by_default(self):
        result, index = self.keep(no_index=False)
        self.assertEqual(result, {"id": 7})
        index.assert_called_once_with(limit=1, ids=[7])

    def test_keep_no_index_leaves_indexing_to_the_caller(self):
        result, index = self.keep(no_index=True)
        self.assertEqual(result, {"id": 7})
        index.assert_not_called()

    def test_vectors_ids_indexes_only_those_saves(self):
        output = io.StringIO()
        with mock.patch("lib.vectors.index", return_value={"indexed": 2}) as index, redirect_stdout(output):
            beacon.cmd_vectors(SimpleNamespace(limit=1000, ids="7,9"))
        index.assert_called_once_with(limit=1000, ids=[7, 9])
        self.assertEqual(json.loads(output.getvalue()), {"indexed": 2})


if __name__ == "__main__":
    unittest.main()
