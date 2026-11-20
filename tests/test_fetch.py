import os
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib import db, fetch


class FetchSourcesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmpdir.name, "beacon.db")
        db.init_db()
        self.addCleanup(self._restore_db_path)

        # No test waits between pages; the clock is ours so cooldowns are exact.
        self.now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        sleeper = mock.patch("lib.fetch.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        clock = mock.patch("lib.fetch._utcnow", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def _restore_db_path(self):
        db.DB_PATH = self.original_db_path

    @staticmethod
    def _tweet(tweet_id):
        return {
            "id": str(tweet_id),
            "text": f"Bookmark {tweet_id}",
            "createdAt": "2026-07-13T12:00:00+00:00",
            "author": {"username": "ada"},
        }

    @staticmethod
    def _result(payload, returncode=0, stderr=""):
        return SimpleNamespace(
            stdout=__import__("json").dumps(payload),
            stderr=stderr,
            returncode=returncode,
        )

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_pages_until_previous_latest_id(self, mock_run):
        created = "2026-07-12T12:00:00+00:00"
        db.upsert_item(
            source="x",
            external_id="known",
            title="Known bookmark",
            body="Known bookmark",
            url="https://x.com/ada/status/known",
            author="@ada",
            raw_json="{}",
            created_at=created,
            synced_at=created,
        )
        db.update_sync_state("x", "known", created, 1)

        mock_run.side_effect = [
            self._result(
                {"tweets": [self._tweet("new-3"), self._tweet("new-2")], "nextCursor": "cursor-1"}
            ),
            self._result(
                {"tweets": [self._tweet("new-1"), self._tweet("known")], "nextCursor": "cursor-2"}
            ),
        ]

        count = fetch.fetch_x_bookmarks(count=1200)

        self.assertEqual(count, 3)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(
            mock_run.call_args_list[0].args[0],
            ["bird", "bookmarks", "--all", "--max-pages", "1", "--json"],
        )
        self.assertEqual(
            mock_run.call_args_list[1].args[0],
            ["bird", "bookmarks", "--cursor", "cursor-1", "--max-pages", "1", "--json"],
        )
        conn = db.get_connection()
        try:
            ids = {
                row["external_id"]
                for row in conn.execute("SELECT external_id FROM items WHERE source='x'").fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(ids, {"known", "new-1", "new-2", "new-3"})
        self.assertEqual(db.get_sync_state("x")["last_id"], "new-3")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_repeat_sync_stops_at_known_id_without_duplicates(self, mock_run):
        created = "2026-07-12T12:00:00+00:00"
        db.upsert_item(
            source="x",
            external_id="latest",
            title="Latest bookmark",
            body="Latest bookmark",
            url="https://x.com/ada/status/latest",
            author="@ada",
            raw_json="{}",
            created_at=created,
            synced_at=created,
        )
        db.update_sync_state("x", "latest", created, 1)
        mock_run.return_value = self._result(
            {"tweets": [self._tweet("latest"), self._tweet("older")], "nextCursor": "cursor-1"}
        )

        count = fetch.fetch_x_bookmarks(count=1200)

        self.assertEqual(count, 0)
        mock_run.assert_called_once()
        conn = db.get_connection()
        try:
            item_count = conn.execute("SELECT COUNT(*) AS c FROM items WHERE source='x'").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(item_count, 1)

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_stops_at_stored_run_when_marker_was_unbookmarked(self, mock_run):
        created = "2026-07-12T12:00:00+00:00"
        for tweet_id in range(100, 130):
            db.upsert_item(
                source="x",
                external_id=str(tweet_id),
                title="Stored",
                body="Stored",
                url=f"https://x.com/ada/status/{tweet_id}",
                author="@ada",
                raw_json="{}",
                created_at=created,
                synced_at=created,
            )
        db.update_sync_state("x", "gone-marker", created, 30)
        stored_page = [self._tweet(tweet_id) for tweet_id in range(129, 99, -1)]
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("new-2"), self._tweet("new-1")], "nextCursor": "c1"}),
            self._result({"tweets": stored_page, "nextCursor": "c2"}),
            self._result({"tweets": [self._tweet("never-fetched")], "nextCursor": None}),
        ]

        count = fetch.fetch_x_bookmarks(count=1200)

        self.assertEqual(count, 2)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(db.get_sync_state("x")["last_id"], "new-2")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_known_run_counts_across_small_pages(self, mock_run):
        created = "2026-07-12T12:00:00+00:00"
        for tweet_id in range(100, 130):
            db.upsert_item(source="x", external_id=str(tweet_id), title="Stored", body="Stored",
                           url="u", author="@ada", raw_json="{}", created_at=created, synced_at=created)
        pages = [self._result({"tweets": [self._tweet("new")], "nextCursor": "c0"})]
        for start in (129, 119, 109):
            tweets = [self._tweet(i) for i in range(start, start - 10, -1)]
            pages.append(self._result({"tweets": tweets, "nextCursor": f"c{start}"}))
        pages.append(self._result({"tweets": [self._tweet("never")], "nextCursor": None}))
        mock_run.side_effect = pages

        self.assertEqual(fetch.fetch_x_bookmarks(count=1200), 1)
        self.assertEqual(mock_run.call_count, 3)

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_count_cap_keeps_whole_page_so_resume_loses_nothing(self, mock_run):
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet(i) for i in range(5)], "nextCursor": "c1"}),
            self._result({"tweets": [self._tweet(i) for i in range(5, 10)], "nextCursor": None}),
        ]
        self.assertEqual(fetch.fetch_x_bookmarks(count=3, stop_at_known=False), 5)
        self.assertEqual(db.get_sync_state("x-backfill")["last_id"], "c1")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_full_backfill_resumes_after_rate_limit(self, mock_run):
        rate_limited = SimpleNamespace(stdout="", stderr="HTTP 429", returncode=1)
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("a")], "nextCursor": "c1"}),
            rate_limited,
        ]
        self.assertEqual(fetch.fetch_x_bookmarks(count=100, stop_at_known=False), 1)
        self.assertEqual(db.get_sync_state("x-backfill")["last_id"], "c1")

        self.now += timedelta(hours=1)  # the 429 cooldown has passed
        mock_run.side_effect = [self._result({"tweets": [self._tweet("b")], "nextCursor": None})]
        self.assertEqual(fetch.fetch_x_bookmarks(count=100, stop_at_known=False), 1)
        self.assertIn("--cursor", mock_run.call_args_list[-1].args[0])
        self.assertIn("c1", mock_run.call_args_list[-1].args[0])
        self.assertEqual(db.get_sync_state("x-backfill")["last_id"], "")

    @staticmethod
    def _failed(stderr):
        return SimpleNamespace(stdout="", stderr=stderr, returncode=1)

    def _cooldown(self):
        state = db.get_sync_state("x-cooldown")
        return datetime.fromisoformat(__import__("json").loads(state["last_id"])["until"]) if state and state["last_id"] else None

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_pauses_between_pages_but_not_before_the_first(self, mock_run):
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("a")], "nextCursor": "c1"}),
            self._result({"tweets": [self._tweet("b")], "nextCursor": "c2"}),
            self._result({"tweets": [self._tweet("c")], "nextCursor": None}),
        ]
        self.assertEqual(fetch.fetch_x_bookmarks(count=100, stop_at_known=False), 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(fetch.PAGE_DELAY_SECONDS)] * 2)

    @mock.patch("lib.fetch.subprocess.run")
    def test_a_rate_limit_with_retry_after_starts_a_cooldown_of_that_length(self, mock_run):
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("a")], "nextCursor": "c1"}),
            self._failed("HTTP 429 Too Many Requests (Retry-After: 120)"),
        ]
        fetch.fetch_x_bookmarks(count=100, stop_at_known=False)
        self.assertEqual(self._cooldown(), self.now + timedelta(seconds=120))

    @mock.patch("lib.fetch.subprocess.run")
    def test_a_rate_limit_without_retry_after_uses_the_default_cooldown(self, mock_run):
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("a")], "nextCursor": "c1"}),
            self._failed("Rate limit exceeded"),
        ]
        fetch.fetch_x_bookmarks(count=100, stop_at_known=False)
        self.assertEqual(self._cooldown(), self.now + fetch.RATE_LIMIT_COOLDOWN)

    @mock.patch("lib.fetch.subprocess.run")
    def test_retry_after_is_capped_so_a_bad_header_cannot_park_x_for_days(self, mock_run):
        mock_run.side_effect = [self._failed("429 Retry-After: 99999999")]
        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=100, stop_at_known=False)
        self.assertEqual(self._cooldown(), self.now + fetch.MAX_COOLDOWN)

    @mock.patch("lib.fetch.subprocess.run")
    def test_a_challenge_page_stops_x_for_a_day(self, mock_run):
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("a")], "nextCursor": "c1"}),
            self._failed("Account checkpoint: please verify you are human"),
        ]
        fetch.fetch_x_bookmarks(count=100, stop_at_known=False)
        self.assertEqual(self._cooldown(), self.now + fetch.CHALLENGE_COOLDOWN)

    @mock.patch("lib.fetch.subprocess.run")
    def test_an_ordinary_failure_starts_no_cooldown(self, mock_run):
        mock_run.side_effect = [self._failed("Keychain is locked"), self._failed("network unreachable")]
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                fetch.fetch_x_bookmarks(count=100, stop_at_known=False)
        self.assertIsNone(self._cooldown())

    @mock.patch("lib.fetch.subprocess.run")
    def test_an_active_cooldown_blocks_the_sync_without_calling_bird(self, mock_run):
        mock_run.side_effect = [self._failed("HTTP 429")]
        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=100)
        mock_run.reset_mock()

        self.now += timedelta(minutes=5)
        with self.assertRaises(RuntimeError) as raised:
            fetch.fetch_x_bookmarks(count=100)
        self.assertIn("cooling down", str(raised.exception))
        mock_run.assert_not_called()

    @mock.patch("lib.fetch.subprocess.run")
    def test_the_sync_runs_again_once_the_cooldown_has_passed(self, mock_run):
        mock_run.side_effect = [self._failed("HTTP 429")]
        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=100)

        self.now += fetch.RATE_LIMIT_COOLDOWN + timedelta(seconds=1)
        mock_run.side_effect = [self._result({"tweets": [self._tweet("a")], "nextCursor": None})]
        self.assertEqual(fetch.fetch_x_bookmarks(count=100), 1)

    @mock.patch("lib.fetch.subprocess.run")
    def test_a_cooldown_keeps_the_resume_cursor_so_nothing_is_lost(self, mock_run):
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("a")], "nextCursor": "c1"}),
            self._failed("HTTP 429"),
        ]
        fetch.fetch_x_bookmarks(count=100, stop_at_known=False)
        self.assertEqual(db.get_sync_state("x-backfill")["last_id"], "c1")

    @mock.patch("lib.fetch.subprocess.run")
    def test_an_x_cooldown_does_not_stop_github_stars(self, mock_run):
        """A cooling-down X account does not stop GitHub stars from syncing."""
        mock_run.side_effect = [self._failed("HTTP 429")]
        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=100)
        mock_run.reset_mock()
        mock_run.side_effect = None
        mock_run.return_value = SimpleNamespace(stdout="", stderr="", returncode=0)
        self.assertEqual(fetch.fetch_github_stars(), 0)
        mock_run.assert_called_once()


    def _seed_marker(self, marker="known"):
        created = "2026-07-12T12:00:00+00:00"
        db.upsert_item(source="x", external_id=marker, title="Known", body="Known",
                       url=f"https://x.com/ada/status/{marker}", author="@ada", raw_json="{}",
                       created_at=created, synced_at=created)
        db.update_sync_state("x", marker, created, 1)

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_second_page_failure_keeps_the_marker_and_resumes_from_the_failed_page(self, mock_run):
        self._seed_marker()
        rate_limited = SimpleNamespace(stdout="", stderr="HTTP 429", returncode=1)
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("new-4"), self._tweet("new-3")], "nextCursor": "c1"}),
            rate_limited,
        ]
        self.assertEqual(fetch.fetch_x_bookmarks(count=1200), 2)
        # The range between new-3 and the old marker was never read, so the marker must not move.
        self.assertEqual(db.get_sync_state("x")["last_id"], "known")

        self.now += timedelta(hours=1)  # the 429 cooldown has passed
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("new-2"), self._tweet("new-1"), self._tweet("known")], "nextCursor": "c2"}),
        ]
        self.assertEqual(fetch.fetch_x_bookmarks(count=1200), 2)
        # Picks up at the failed page, not the newest page, and then promotes the newest ID it saw.
        self.assertEqual(mock_run.call_args_list[-1].args[0],
                         ["bird", "bookmarks", "--cursor", "c1", "--max-pages", "1", "--json"])
        self.assertEqual(db.get_sync_state("x")["last_id"], "new-4")

        # Caught up: the next sync reads one page and stops at the new marker.
        mock_run.side_effect = [self._result({"tweets": [self._tweet("new-4")], "nextCursor": "c9"})]
        self.assertEqual(fetch.fetch_x_bookmarks(count=1200), 0)
        self.assertEqual(mock_run.call_args_list[-1].args[0],
                         ["bird", "bookmarks", "--all", "--max-pages", "1", "--json"])

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_missing_or_timed_out_bird_is_a_failed_sync_not_zero_new(self, mock_run):
        # Audit B07: no bird, or bird hanging on the first page, means X wasn't read, not that it was empty.
        self._seed_marker()
        for failure, reason in [(FileNotFoundError("bird"), "not found"),
                                (fetch.subprocess.TimeoutExpired(cmd="bird", timeout=120), "timed out")]:
            mock_run.side_effect = [failure]
            with self.assertRaisesRegex(RuntimeError, reason):
                fetch.fetch_x_bookmarks(count=1200)
            self.assertEqual(db.get_sync_state("x")["last_id"], "known")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_timeout_after_the_first_page_keeps_the_marker_and_resumes(self, mock_run):
        self._seed_marker()
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("new-2")], "nextCursor": "c1"}),
            fetch.subprocess.TimeoutExpired(cmd="bird", timeout=120),
        ]
        fetch.fetch_x_bookmarks(count=1200)
        self.assertEqual(db.get_sync_state("x")["last_id"], "known")

        mock_run.side_effect = [self._result({"tweets": [self._tweet("new-1"), self._tweet("known")], "nextCursor": None})]
        fetch.fetch_x_bookmarks(count=1200)
        self.assertIn("c1", mock_run.call_args_list[-1].args[0])
        self.assertEqual(db.get_sync_state("x")["last_id"], "new-2")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_count_cap_before_the_marker_keeps_the_marker_and_resumes(self, mock_run):
        self._seed_marker()
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet(f"new-{i}") for i in (5, 4, 3)], "nextCursor": "c1"}),
        ]
        self.assertEqual(fetch.fetch_x_bookmarks(count=3), 3)
        self.assertEqual(db.get_sync_state("x")["last_id"], "known")

        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("new-2"), self._tweet("known")], "nextCursor": "c2"}),
        ]
        self.assertEqual(fetch.fetch_x_bookmarks(count=3), 1)
        self.assertIn("c1", mock_run.call_args_list[-1].args[0])
        self.assertEqual(db.get_sync_state("x")["last_id"], "new-5")

    def _fail_inserts_of(self, external_id):
        conn = db.get_connection()
        try:
            conn.execute(
                f"CREATE TRIGGER fail_insert BEFORE INSERT ON items WHEN NEW.external_id='{external_id}' "
                "BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END")
            conn.commit()
        finally:
            conn.close()

    def _drop_failing_trigger(self):
        conn = db.get_connection()
        try:
            conn.execute("DROP TRIGGER fail_insert")
            conn.commit()
        finally:
            conn.close()

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_failed_write_keeps_the_marker_and_the_next_sync_retries_that_bookmark(self, mock_run):
        self._seed_marker()
        # More stored bookmarks ahead of the failure than the known-run stop, so a retry that
        # trusted already-stored items would quit before ever reaching the one that failed.
        ids = [f"t{i:02d}" for i in range(25, 0, -1)]
        failing = "t02"
        page = [self._tweet(i) for i in ids] + [self._tweet("known")]
        mock_run.side_effect = [self._result({"tweets": page, "nextCursor": "c1"})]
        self._fail_inserts_of(failing)

        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=1200)
        self.assertEqual(db.get_sync_state("x")["last_id"], "known")
        conn = db.get_connection()
        try:
            stored = {r[0] for r in conn.execute("SELECT external_id FROM items WHERE source='x'")}
        finally:
            conn.close()
        self.assertNotIn(failing, stored)
        self.assertIn("t25", stored)  # the rest of the page still landed

        self._drop_failing_trigger()
        mock_run.side_effect = [self._result({"tweets": page, "nextCursor": "c1"})]
        self.assertEqual(fetch.fetch_x_bookmarks(count=1200), 1)
        conn = db.get_connection()
        try:
            stored = {r[0] for r in conn.execute("SELECT external_id FROM items WHERE source='x'")}
        finally:
            conn.close()
        self.assertIn(failing, stored)
        self.assertEqual(db.get_sync_state("x")["last_id"], "t25")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_failed_write_on_a_later_page_resumes_at_that_page(self, mock_run):
        self._seed_marker()
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("new-3")], "nextCursor": "c1"}),
            self._result({"tweets": [self._tweet("bad"), self._tweet("new-1")], "nextCursor": "c2"}),
        ]
        self._fail_inserts_of("bad")
        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=1200)
        self.assertEqual(db.get_sync_state("x")["last_id"], "known")

        self._drop_failing_trigger()
        mock_run.side_effect = [self._result({"tweets": [self._tweet("bad"), self._tweet("new-1"), self._tweet("known")], "nextCursor": None})]
        self.assertEqual(fetch.fetch_x_bookmarks(count=1200), 1)
        self.assertIn("c1", mock_run.call_args_list[-1].args[0])
        self.assertEqual(db.get_sync_state("x")["last_id"], "new-3")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_backfill_failed_write_resumes_at_the_failed_page(self, mock_run):
        mock_run.side_effect = [
            self._result({"tweets": [self._tweet("a")], "nextCursor": "c1"}),
            self._result({"tweets": [self._tweet("bad"), self._tweet("b")], "nextCursor": "c2"}),
        ]
        self._fail_inserts_of("bad")
        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=100, stop_at_known=False)
        self.assertEqual(db.get_sync_state("x-backfill")["last_id"], "c1")

    def test_upsert_item_strict_raises_instead_of_looking_like_a_known_item(self):
        self._fail_inserts_of("bad")
        args = dict(source="x", external_id="bad", title="t", body="b", url="u", author="a",
                    raw_json="{}", created_at="", synced_at="")
        self.assertFalse(db.upsert_item(**args))
        with self.assertRaises(sqlite3.Error):
            db.upsert_item(**args, strict=True)

    @mock.patch("lib.fetch.subprocess.run")
    def test_scheduled_x_uses_keychain_without_printing_authenticated_diagnostics(self, mock_run):
        mock_run.return_value = SimpleNamespace(stdout="", stderr="private diagnostic", returncode=1)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"BEACON_X_KEYCHAIN": "1"}), redirect_stdout(output):
            with self.assertRaises(RuntimeError):
                fetch.fetch_x_bookmarks(count=100)
        command = mock_run.call_args.args[0]
        self.assertEqual(command[:4], [str(Path.home() / ".local/bin/with-secret"),
                                      "BEACON_X_AUTH_TOKEN", "BEACON_X_CT0", "--"])
        self.assertIn("bookmarks", command)
        self.assertIn("scripts/x-keys", output.getvalue())
        self.assertNotIn("private diagnostic", output.getvalue())

    @mock.patch("lib.fetch.subprocess.run")
    def test_a_keychain_rate_limit_starts_a_cooldown_without_printing_bird_stderr(self, mock_run):
        mock_run.return_value = self._failed("HTTP 429 Too Many Requests (Retry-After: 120)")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"BEACON_X_KEYCHAIN": "1"}), redirect_stdout(output):
            with self.assertRaises(RuntimeError):
                fetch.fetch_x_bookmarks(count=100)
        self.assertEqual(self._cooldown(), self.now + timedelta(seconds=120))
        self.assertNotIn("429", output.getvalue())

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_raises_when_the_first_page_fails_so_a_signed_out_bird_is_not_a_quiet_zero(self, mock_run):
        mock_run.return_value = SimpleNamespace(stdout="", stderr="Missing required credentials", returncode=1)
        with self.assertRaises(RuntimeError):
            fetch.fetch_x_bookmarks(count=100)

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_stores_created_at_as_iso(self, mock_run):
        tweet = self._tweet("1")
        tweet["createdAt"] = "Tue Sep 29 18:38:50 +0000 2026"
        mock_run.return_value = self._result({"tweets": [tweet], "nextCursor": None})

        fetch.fetch_x_bookmarks(count=10, stop_at_known=False)

        conn = db.get_connection()
        try:
            created = conn.execute("SELECT created_at FROM items WHERE external_id='1'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(created, "2026-09-29T18:38:50+00:00")

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_x_full_backfill_ingests_390_bookmarks_across_pages(self, mock_run):
        pages = []
        for page_index in range(13):
            first = page_index * 30
            tweets = [self._tweet(first + offset) for offset in range(30)]
            next_cursor = f"cursor-{page_index + 1}" if page_index < 12 else None
            pages.append(self._result({"tweets": tweets, "nextCursor": next_cursor}))
        mock_run.side_effect = pages

        count = fetch.fetch_x_bookmarks(count=1200, stop_at_known=False)

        self.assertEqual(count, 390)
        self.assertEqual(mock_run.call_count, 13)
        conn = db.get_connection()
        try:
            item_count = conn.execute("SELECT COUNT(*) AS c FROM items WHERE source='x'").fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(item_count, 390)

    @mock.patch("lib.fetch.subprocess.run")
    def test_fetch_github_preserves_starred_at_and_is_idempotent(self, mock_run):
        repo = {
            "starred_at": "2026-07-13T10:30:00Z",
            "full_name": "example/repo",
            "description": "Example",
            "html_url": "https://github.com/example/repo",
            "owner": "example",
            "created_at": "2020-01-01T00:00:00Z",
            "stargazers_count": 42,
            "language": "Python",
            "topics": ["agents"],
        }
        mock_run.return_value = self._result(repo)

        self.assertEqual(fetch.fetch_github_stars(), 1)
        self.assertEqual(fetch.fetch_github_stars(), 0)

        command = mock_run.call_args_list[0].args[0]
        self.assertIn("Accept: application/vnd.github.star+json", command)
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT created_at, saved_at FROM items WHERE source='github' AND external_id='example/repo'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["created_at"], "2020-01-01T00:00:00Z")
        self.assertEqual(row["saved_at"], "2026-07-13T10:30:00Z")

    def test_fetch_keep_local_imports_active_items_and_prefers_ai_text(self):
        keep_db = os.path.join(self.tmpdir.name, "keep.db")
        content_root = Path(self.tmpdir.name) / "keep-data"
        (content_root / "content").mkdir(parents=True)

        conn = sqlite3.connect(keep_db)
        try:
            conn.execute(
                """
                CREATE TABLE items (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT,
                    author TEXT,
                    platform TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    notes TEXT,
                    content_key TEXT,
                    ai_text_key TEXT,
                    deleted_at INTEGER
                )
                """
            )
            conn.execute(
                """
                INSERT INTO items
                    (id, url, title, author, platform, created_at, updated_at, status, notes, content_key, ai_text_key, deleted_at)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "kl-1",
                    "https://example.com/article",
                    "Keep Local Article",
                    "Ada",
                    "example.com",
                    1_700_000_000_000,
                    1_700_000_001_000,
                    "active",
                    "note body",
                    "content/kl-1.md",
                    "content/kl-1.ai.txt",
                    None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        (content_root / "content" / "kl-1.md").write_text("# Markdown body\n\nOriginal content", encoding="utf-8")
        (content_root / "content" / "kl-1.ai.txt").write_text("AI summary body", encoding="utf-8")

        count = fetch.fetch_keep_local(count=5, db_path=keep_db, content_root=str(content_root))

        self.assertEqual(count, 1)
        row = db.get_connection().execute(
            "SELECT source, external_id, title, body, url, author FROM items WHERE source='keep-local'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["external_id"], "kl-1")
        self.assertEqual(row["body"], "AI summary body")
        self.assertEqual(row["author"], "Ada")

    def test_fetch_markdown_directory_imports_files_and_skips_excludes(self):
        root = Path(self.tmpdir.name) / "wiki"
        root.mkdir()
        included = root / "concept.md"
        excluded = root / "beacon-backlog.md"
        nested = root / "nested"
        nested.mkdir()
        nested_file = nested / "idea.md"

        included.write_text("# Concept Title\n\nBody text", encoding="utf-8")
        excluded.write_text("# Ignore me", encoding="utf-8")
        nested_file.write_text("No heading here", encoding="utf-8")

        count = fetch.fetch_markdown_directory(
            source="wiki",
            root=str(root),
            count=10,
            exclude_names={"beacon-backlog.md"},
            author="Zettel Wiki",
        )

        self.assertEqual(count, 2)
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT external_id, title, author FROM items WHERE source='wiki' ORDER BY external_id"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual([row["external_id"] for row in rows], ["concept.md", "nested/idea.md"])
        self.assertEqual(rows[0]["title"], "Concept Title")
        self.assertEqual(rows[1]["title"], "idea")

    def test_fetch_last30days_imports_findings_from_store_db(self):
        research_db = os.path.join(self.tmpdir.name, "research.db")
        conn = sqlite3.connect(research_db)
        try:
            conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            conn.execute(
                """
                CREATE TABLE findings (
                    id INTEGER PRIMARY KEY,
                    topic_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT,
                    source_title TEXT,
                    author TEXT,
                    content TEXT,
                    summary TEXT,
                    first_seen TEXT,
                    last_seen TEXT
                )
                """
            )
            conn.execute("INSERT INTO topics (id, name) VALUES (1, 'agent workflows')")
            conn.execute(
                """
                INSERT INTO findings
                    (id, topic_id, source, source_url, source_title, author, content, summary, first_seen, last_seen)
                VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    7,
                    1,
                    "x",
                    "https://x.com/example/status/1",
                    "Thread about agent workflows",
                    "@ada",
                    "Full finding content",
                    "Short finding summary",
                    "2026-04-15T00:00:00+00:00",
                    "2026-04-16T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        count = fetch.fetch_last30days(count=5, db_path=research_db, output_dir=os.path.join(self.tmpdir.name, "missing-out"))

        self.assertEqual(count, 1)
        row = db.get_connection().execute(
            "SELECT source, external_id, title, body, url, author FROM items WHERE source='last30days'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["external_id"], "finding:7")
        self.assertEqual(row["title"], "Thread about agent workflows")
        self.assertIn("Short finding summary", row["body"])
        self.assertEqual(row["url"], "https://x.com/example/status/1")

    def test_fetch_last30days_falls_back_to_output_context(self):
        output_dir = Path(self.tmpdir.name) / "last30-out"
        output_dir.mkdir()
        (output_dir / "last30days.context.md").write_text("# Context\n\nRecent findings", encoding="utf-8")

        count = fetch.fetch_last30days(
            count=5,
            db_path=os.path.join(self.tmpdir.name, "missing.db"),
            output_dir=str(output_dir),
        )

        self.assertEqual(count, 1)
        row = db.get_connection().execute(
            "SELECT source, external_id, title, body FROM items WHERE source='last30days'"
        ).fetchone()
        self.assertEqual(row["external_id"], "context:last30days.context.md")
        self.assertEqual(row["title"], "last30days context")
        self.assertIn("Recent findings", row["body"])

    def test_fetch_last30days_falls_back_to_output_when_db_has_no_findings(self):
        research_db = os.path.join(self.tmpdir.name, "research.db")
        conn = sqlite3.connect(research_db)
        try:
            conn.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            conn.execute(
                """
                CREATE TABLE findings (
                    id INTEGER PRIMARY KEY,
                    topic_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT,
                    source_title TEXT,
                    author TEXT,
                    content TEXT,
                    summary TEXT,
                    first_seen TEXT,
                    last_seen TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        output_dir = Path(self.tmpdir.name) / "last30-out"
        output_dir.mkdir()
        (output_dir / "last30days.context.md").write_text("# Context\n\nRecent findings", encoding="utf-8")

        count = fetch.fetch_last30days(
            count=5,
            db_path=research_db,
            output_dir=str(output_dir),
        )

        self.assertEqual(count, 1)
        row = db.get_connection().execute(
            "SELECT source, external_id, title, body FROM items WHERE source='last30days'"
        ).fetchone()
        self.assertEqual(row["external_id"], "context:last30days.context.md")
        self.assertEqual(row["title"], "last30days context")
        self.assertIn("Recent findings", row["body"])


if __name__ == "__main__":
    unittest.main()
