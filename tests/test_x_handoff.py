import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import beacon
from lib import db, fetch, x_handoff


BOOKMARK = "Bookmarked save. Eligible for classification and routing."
BOTH = "Bookmarked save, also liked. The bookmark is the save. The like is extra interest, not a second task."
LIKE = "Liked only. Weaker interest, not a save, so it stays out of classification, triage, routing, and the library."
CREATED = "2026-03-01T18:00:00+00:00"
FETCHED = "2026-11-01T00:00:00+00:00"


class XHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "handoff.json"
        self.database = self.root / "isolated.db"
        self.original_db = db.DB_PATH
        db.DB_PATH = str(self.database)
        self.addCleanup(setattr, db, "DB_PATH", self.original_db)

    def post(self, post_id="1900000000000000001", text="SYNTHETIC bookmark", source_types=None, **extra):
        item = {
            "post_id": post_id,
            "url": f"https://x.com/synthetic_user/status/{post_id}",
            "text": text,
            "created_at": "2026-03-01T12:00:00-06:00",
            "source_types": ["bookmark"] if source_types is None else source_types,
            "fetched_at": "2026-11-01T00:00:00Z",
            "author": {"username": "synthetic_user", "name": "Synthetic Name"},
        }
        item.update(extra)
        return item

    def write(self, posts, wrapped=True):
        payload = {"format": "x-handoff-v1", "posts": posts} if wrapped else posts
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def load(self, preview=False):
        return x_handoff.import_handoff(self.path, self.database, preview=preview)

    def rows(self):
        with closing(db.get_connection(self.database)) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM items ORDER BY id")]

    def signals(self, row):
        return json.loads(row["raw_json"])["x_signals"]

    def tweet(self, post_id, text="SYNTHETIC bird bookmark"):
        return {
            "id": post_id,
            "text": text,
            "createdAt": "Tue Sep 29 18:38:50 +0000 2026",
            "author": {"username": "synthetic_user", "name": "Synthetic Name"},
            "likeCount": 9,
        }

    def test_preview_counts_create_no_database_and_hide_post_text(self):
        posts = [
            self.post(post_id="1900000000000000101", text="SYNTHETIC saved", saved_at="2026-04-02T15:04:05Z"),
            self.post(post_id="1900000000000000102", text="SYNTHETIC liked", source_types=["like"],
                      liked_at="2026-06-01T00:00:00Z"),
            self.post(post_id="1900000000000000103", text="SYNTHETIC both", source_types=["bookmark", "like"],
                      saved_at=None, liked_at=None),
        ]
        self.write(posts)
        with mock.patch.object(db, "init_db") as init, mock.patch.object(db, "get_connection") as connect:
            result = self.load(preview=True)
            init.assert_not_called()
            connect.assert_not_called()
        self.assertEqual(result, {
            "source": "x", "preview": True, "posts": 3, "bookmark_only": 1, "like_only": 1, "both": 1,
            "missing_saved_at": 1, "missing_liked_at": 1, "eligible_saves": 2, "interest_only": 1,
        })
        blob = json.dumps(result)
        for secret in ("SYNTHETIC", "1900000000000000101", "synthetic_user", "https://", "Synthetic Name"):
            self.assertNotIn(secret, blob)
        self.assertFalse(self.database.exists())

    def test_bookmark_is_a_save_and_library_follows_media(self):
        plain = "1900000000000000001"
        pictured = "1900000000000000002"
        self.write([
            self.post(post_id=plain, url=f"https://twitter.com/synthetic_user/status/{plain}",
                      source_ranks={"bookmark": 0}, extra_field="keep-me"),
            self.post(post_id=pictured, text="SYNTHETIC pictured bookmark", media=[{
                "type": "photo", "url": "https://example.com/synthetic.jpg",
                "preview_image_url": "https://example.com/synthetic-small.jpg", "width": 640, "height": 480,
            }], public_metrics={"like_count": 4, "retweet_count": 9}),
        ])
        result = self.load()
        self.assertEqual((result["added"], result["promoted"], result["refreshed"]), (2, 0, 0))
        rows = {row["external_id"]: row for row in self.rows()}
        self.assertEqual(set(rows), {plain, pictured})
        for row in rows.values():
            self.assertEqual(row["source"], "x")
            self.assertEqual(row["status"], "new")
            self.assertEqual(row["created_at"], CREATED)
            self.assertIsNone(row["saved_at"])
            signal = self.signals(row)["bookmark"]
            self.assertEqual(signal["retrieved_at"], FETCHED)
            self.assertIsNone(signal["acted_at"])
            self.assertNotIn("like", self.signals(row))
        self.assertEqual(self.signals(rows[plain])["bookmark"]["rank"], 0)
        raw = json.loads(rows[pictured]["raw_json"])
        self.assertEqual(raw["likeCount"], 4)
        self.assertNotIn("retweet_count", raw)
        self.assertNotIn("Ada liked", json.dumps(raw))
        self.assertEqual(raw["media"], [{
            "height": 480, "previewUrl": "https://example.com/synthetic-small.jpg",
            "type": "photo", "url": "https://example.com/synthetic.jpg", "width": 640,
        }])
        self.assertEqual(raw["handoff"]["public_metrics"]["retweet_count"], 9)
        self.assertEqual(raw["handoff"]["media"][0]["preview_image_url"], "https://example.com/synthetic-small.jpg")
        self.assertEqual(json.loads(rows[plain]["raw_json"])["handoff"]["extra_field"], "keep-me")
        preview = db.item_preview("x", rows[pictured]["raw_json"], rows[pictured]["body"])
        self.assertEqual(preview["author_name"], "Synthetic Name")
        self.assertEqual(preview["likes"], 4)
        self.assertEqual(preview["media"][0]["preview_url"], "https://example.com/synthetic-small.jpg")
        self.assertEqual(preview["date"], CREATED)
        with closing(db.get_connection(self.database)) as conn:
            flags = {row["external_id"]: row["in_library"] for row in conn.execute("SELECT external_id, in_library FROM items")}
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workstreams").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0], 0)
        self.assertEqual(flags, {plain: 0, pictured: 1})
        library = db.get_library()
        self.assertEqual(library["total"], 1)
        self.assertIn(pictured, library["items"][0]["url"])
        self.assertEqual({item["external_id"] for item in db.get_unclassified_items()}, {plain, pictured})

    def test_like_only_stays_interest_and_out_of_library_classify_and_routing(self):
        self.write([self.post(
            post_id="1900000000000000003", text="SYNTHETIC liked picture", source_types=["like"],
            liked_at="2026-06-01T00:00:00Z", media=[{
                "type": "photo", "url": "https://example.com/synthetic-like.jpg",
                "preview_image_url": "https://example.com/synthetic-like-small.jpg", "width": 10, "height": 12,
            }], public_metrics={"like_count": 7},
        )])
        self.load()
        row = self.rows()[0]
        self.assertEqual(row["source"], "x-like")
        self.assertEqual(row["status"], "interest")
        self.assertIsNone(row["saved_at"])
        self.assertEqual(self.signals(row)["like"]["acted_at"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(json.loads(row["raw_json"])["x_explanation"], LIKE)
        self.assertEqual(json.loads(row["raw_json"])["likeCount"], 7)
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT in_library FROM items").fetchone()[0], 0)
        self.assertEqual(db.get_library()["total"], 0)
        self.assertEqual(db.get_unclassified_items(), [])
        with closing(db.get_connection(self.database)) as conn, conn:
            conn.execute("INSERT INTO workstreams (slug, name, backlog_path, active) VALUES ('synth', 'Synthetic', 's.md', 1)")
            conn.execute(
                "INSERT INTO item_workstreams (item_id, workstream_id, confidence, routed) "
                "SELECT ?, id, 0.9, 0 FROM workstreams WHERE slug='synth'",
                (row["id"],),
            )
            conn.execute("UPDATE items SET classified_at='2026-11-02T00:00:00+00:00' WHERE id=?", (row["id"],))
        self.assertEqual(db.get_unrouted_items(), [])
        self.assertEqual(db.get_triage_queue(), [])

    def test_both_signals_are_one_save_and_null_times_stay_null(self):
        self.write([self.post(post_id="33", source_types=["bookmark", "like"], saved_at=None, liked_at=None)])
        result = self.load()
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertEqual((row["source"], row["status"], row["saved_at"]), ("x", "new", None))
        signals = self.signals(row)
        self.assertEqual(set(signals), {"bookmark", "like"})
        self.assertIsNone(signals["bookmark"]["acted_at"])
        self.assertIsNone(signals["like"]["acted_at"])
        self.assertEqual(json.loads(row["raw_json"])["x_explanation"], BOTH)

    def test_duplicate_post_id_merges_matching_text_and_rejects_conflicts_before_the_database_exists(self):
        self.write([
            self.post(post_id="44", text="SYNTHETIC same", source_types=["bookmark"], source_ranks={"bookmark": 1}),
            self.post(post_id="44", text="SYNTHETIC same", source_types=["like"], source_ranks={"like": 2},
                      liked_at="2026-06-01T00:00:00Z"),
        ])
        result = self.load()
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(self.rows()), 1)
        signals = self.signals(self.rows()[0])
        self.assertEqual(signals["bookmark"]["rank"], 1)
        self.assertEqual(signals["like"]["rank"], 2)
        self.assertEqual(self.rows()[0]["source"], "x")

        self.database.unlink()
        for posts in (
            [self.post(text="SYNTHETIC one"), self.post(text="SYNTHETIC two")],
            [self.post(created_at="2026-03-01T12:00:00-06:00"), self.post(created_at="2026-04-01T00:00:00Z")],
        ):
            with self.subTest(posts=posts):
                self.write(posts)
                with mock.patch.object(db, "init_db") as init, mock.patch.object(db, "get_connection") as connect:
                    with self.assertRaises(ValueError) as raised:
                        self.load()
                    self.assertNotIn("SYNTHETIC", str(raised.exception))
                    init.assert_not_called()
                    connect.assert_not_called()
                self.assertFalse(self.database.exists())

    def test_bad_files_raise_before_the_database_exists(self):
        good = self.post()
        cases = [
            {**good, "post_id": 12},
            {**good, "url": "https://x.com/synthetic_user/status/999"},
            {**good, "source_types": []},
            {**good, "source_types": ["repost"]},
            {**good, "source_types": ["bookmark", "bookmark"]},
            {**good, "created_at": "2026-03-01T12:00:00"},
            {**good, "fetched_at": "2026-11-01T00:00:00"},
            {**good, "source_ranks": {"bookmark": True}},
            {**good, "source_ranks": {"like": 1}},
            {**good, "text": None},
            {**good, "media": "nope"},
            {**good, "liked_at": "yesterday"},
            {**good, "saved_at": "2026-01-01T00:00:00"},
            {"format": "x-handoff-v2", "posts": []},
            {"posts": []},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                if "post_id" not in payload:
                    self.path.write_text(json.dumps(payload), encoding="utf-8")
                else:
                    self.write([payload])
                with mock.patch.object(db, "init_db") as init, mock.patch.object(db, "get_connection") as connect:
                    with self.assertRaises(ValueError) as raised:
                        self.load()
                    self.assertNotIn("SYNTHETIC", str(raised.exception))
                    init.assert_not_called()
                    connect.assert_not_called()
                self.assertFalse(self.database.exists())
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.load()
        self.assertFalse(self.database.exists())
        self.path.write_bytes("SYNTHETIC secret".encode("utf-16"))
        with mock.patch.object(db, "init_db") as init:
            with self.assertRaises(UnicodeError):
                self.load()
            init.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_a_failed_import_rolls_back(self):
        db.init_db(self.database)
        with closing(db.get_connection(self.database)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER refuse_second BEFORE INSERT ON items WHEN NEW.external_id='22' "
                "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END"
            )
        self.write([self.post(post_id="11", text="SYNTHETIC first"), self.post(post_id="22", text="SYNTHETIC second")])
        with self.assertRaises(sqlite3.IntegrityError):
            self.load()
        self.assertEqual(self.rows(), [])

    def test_repeat_import_refreshes_and_keeps_queued_status_saved_at_and_created_at(self):
        self.write([self.post(saved_at="2026-04-02T15:04:05Z", source_ranks={"bookmark": 0})])
        self.load()
        item_id = self.rows()[0]["id"]
        db.update_item_status(item_id, "queued")
        self.write([self.post(text="SYNTHETIC edited bookmark", created_at="2026-08-08T00:00:00Z",
                               fetched_at="2026-11-05T00:00:00Z")])
        result = self.load()
        self.assertEqual((result["added"], result["promoted"], result["refreshed"]), (0, 0, 1))
        row = self.rows()[0]
        self.assertEqual(row["id"], item_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["body"], "SYNTHETIC edited bookmark")
        self.assertEqual(row["saved_at"], "2026-04-02T15:04:05+00:00")
        self.assertEqual(row["created_at"], CREATED)
        signal = self.signals(row)["bookmark"]
        self.assertEqual(signal["rank"], 0)
        self.assertEqual(signal["acted_at"], "2026-04-02T15:04:05+00:00")
        self.assertEqual(signal["retrieved_at"], "2026-11-05T00:00:00+00:00")

    def test_a_bookmark_promotes_the_like_row_in_place(self):
        self.write([self.post(post_id="55", source_types=["like"], text="SYNTHETIC like first")])
        self.load()
        original = self.rows()[0]["id"]
        self.write([self.post(post_id="55", text="SYNTHETIC like first", source_ranks={"bookmark": 3})])
        result = self.load()
        self.assertEqual((result["added"], result["promoted"], result["refreshed"]), (0, 1, 0))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], original)
        self.assertEqual(rows[0]["source"], "x")
        self.assertEqual(rows[0]["status"], "new")
        self.assertEqual(set(self.signals(rows[0])), {"bookmark", "like"})
        self.assertEqual(self.signals(rows[0])["bookmark"]["rank"], 3)

    def test_a_later_like_keeps_queued_status_classification_and_the_workstream(self):
        self.write([self.post(post_id="66", saved_at="2026-04-02T15:04:05Z", source_ranks={"bookmark": 2})])
        self.load()
        item_id = self.rows()[0]["id"]
        db.update_item_status(item_id, "queued")
        with closing(db.get_connection(self.database)) as conn, conn:
            conn.execute("UPDATE items SET intent='reference', classified_at='2026-11-02T00:00:00+00:00' WHERE id=?", (item_id,))
            conn.execute("INSERT INTO workstreams (slug, name, backlog_path, active) VALUES ('synth', 'Synthetic', 's.md', 1)")
            conn.execute(
                "INSERT INTO item_workstreams (item_id, workstream_id, confidence, routed) "
                "SELECT ?, id, 0.8, 0 FROM workstreams WHERE slug='synth'",
                (item_id,),
            )
        self.write([self.post(post_id="66", source_types=["like"], liked_at="2026-06-01T00:00:00Z", source_ranks={"like": 5})])
        result = self.load()
        self.assertEqual((result["added"], result["promoted"], result["refreshed"]), (0, 0, 1))
        row = self.rows()[0]
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(row["id"], item_id)
        self.assertEqual(row["source"], "x")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["intent"], "reference")
        self.assertEqual(row["classified_at"], "2026-11-02T00:00:00+00:00")
        self.assertEqual(row["saved_at"], "2026-04-02T15:04:05+00:00")
        signals = self.signals(row)
        self.assertEqual(signals["bookmark"]["rank"], 2)
        self.assertEqual(signals["like"]["rank"], 5)
        self.assertEqual(signals["like"]["acted_at"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(json.loads(row["raw_json"])["x_explanation"], BOTH)
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_workstreams").fetchone()[0], 1)

    def test_a_like_keeps_a_bookmark_that_was_stored_before_signals_existed(self):
        db.init_db(self.database)
        legacy = {
            "text": "SYNTHETIC legacy bookmark",
            "media": [{"type": "photo", "url": "https://example.com/legacy.jpg"}],
            "likeCount": 3,
            "author": {"name": "Synthetic Name", "username": "synthetic_user"},
        }
        db.upsert_item(
            source="x", external_id="1900000000000000099",
            title="SYNTHETIC legacy bookmark", body="SYNTHETIC legacy bookmark",
            url="https://x.com/synthetic_user/status/1900000000000000099",
            author="@synthetic_user", raw_json=json.dumps(legacy),
            created_at=CREATED, synced_at=FETCHED,
        )
        item_id = self.rows()[0]["id"]
        db.update_item_status(item_id, "queued")
        self.write([self.post(
            post_id="1900000000000000099", text="", source_types=["like"],
            source_ranks={"like": 8},
        )])
        result = self.load()
        self.assertEqual((result["added"], result["promoted"], result["refreshed"]), (0, 0, 1))
        row = self.rows()[0]
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(row["id"], item_id)
        self.assertEqual(row["source"], "x")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["body"], "SYNTHETIC legacy bookmark")
        self.assertIsNone(row["saved_at"])
        self.assertEqual(set(self.signals(row)), {"bookmark", "like"})
        self.assertIsNone(self.signals(row)["bookmark"]["acted_at"])
        self.assertEqual(self.signals(row)["like"]["rank"], 8)
        raw = json.loads(row["raw_json"])
        self.assertEqual(raw["media"], legacy["media"])
        self.assertEqual(raw["likeCount"], 3)
        self.assertEqual(raw["x_explanation"], BOTH)
        with closing(db.get_connection(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT in_library FROM items").fetchone()[0], 1)

    def test_topic_words_do_not_change_a_like_or_a_bookmark(self):
        self.write([
            self.post(post_id="71", text="SYNTHETIC the team won in overtime", source_types=["like"]),
            self.post(post_id="72", text="SYNTHETIC coding agents rewrite the build", source_types=["like"]),
            self.post(post_id="73", text="SYNTHETIC the team lost in the rain", source_types=["bookmark"]),
        ])
        self.load()
        by_text = {row["body"]: row for row in self.rows()}
        self.assertEqual((by_text["SYNTHETIC the team won in overtime"]["source"], by_text["SYNTHETIC the team won in overtime"]["status"]), ("x-like", "interest"))
        self.assertEqual((by_text["SYNTHETIC coding agents rewrite the build"]["source"], by_text["SYNTHETIC coding agents rewrite the build"]["status"]), ("x-like", "interest"))
        self.assertEqual((by_text["SYNTHETIC the team lost in the rain"]["source"], by_text["SYNTHETIC the team lost in the rain"]["status"]), ("x", "new"))
        for row in by_text.values():
            explanation = json.loads(row["raw_json"])["x_explanation"].lower()
            self.assertNotIn("team", explanation)
            self.assertNotIn("coding", explanation)
            self.assertNotIn("overtime", explanation)

    def test_a_bare_array_imports_one_save(self):
        self.write([self.post(post_id="44", text="SYNTHETIC array")], wrapped=False)
        self.assertEqual(self.load()["added"], 1)
        self.assertEqual(self.rows()[0]["source"], "x")

    def test_store_tweet_promotes_a_like_and_a_second_write_keeps_it(self):
        self.write([self.post(
            post_id="42", source_types=["like"], text="SYNTHETIC liked before bird",
            source_ranks={"like": 4}, liked_at="2026-05-01T00:00:00Z",
        )])
        self.load()
        tweet = self.tweet("42", "SYNTHETIC bird copy")
        synced = "2026-11-07T21:00:00+00:00"
        self.assertTrue(fetch.store_tweet(tweet, synced))
        self.assertNotIn("x_signals", tweet)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "x")
        self.assertEqual(rows[0]["status"], "new")
        self.assertEqual(rows[0]["created_at"], CREATED)
        self.assertIsNone(rows[0]["saved_at"])
        raw = json.loads(rows[0]["raw_json"])
        self.assertEqual(raw["text"], "SYNTHETIC bird copy")
        self.assertEqual(raw["likeCount"], 9)
        self.assertNotIn("handoff", raw)
        self.assertEqual(raw["x_signals"]["like"]["rank"], 4)
        self.assertEqual(raw["x_signals"]["like"]["acted_at"], "2026-05-01T00:00:00+00:00")
        self.assertIsNone(raw["x_signals"]["bookmark"]["rank"])
        self.assertIsNone(raw["x_signals"]["bookmark"]["acted_at"])
        self.assertEqual(raw["x_signals"]["bookmark"]["retrieved_at"], synced)
        self.assertEqual(raw["x_explanation"], BOTH)
        db.update_item_status(rows[0]["id"], "queued")
        self.assertFalse(fetch.store_tweet(tweet, "2026-11-07T22:00:00+00:00"))
        again = self.rows()
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["status"], "queued")
        kept = json.loads(again[0]["raw_json"])["x_signals"]
        self.assertEqual(kept["like"]["rank"], 4)
        self.assertEqual(kept["bookmark"]["retrieved_at"], "2026-11-07T22:00:00+00:00")
        self.assertIsNone(kept["bookmark"]["acted_at"])

    def test_store_tweet_of_an_existing_save_keeps_rank_saved_at_and_the_like(self):
        self.write([self.post(
            post_id="88", source_types=["bookmark", "like"], source_ranks={"bookmark": 2, "like": 4},
            saved_at="2026-04-02T15:04:05Z", liked_at="2026-06-01T00:00:00Z",
        )])
        self.load()
        tweet = self.tweet("88", "SYNTHETIC bird resync")
        self.assertFalse(fetch.store_tweet(tweet, "2026-11-07T21:00:00+00:00"))
        row = self.rows()[0]
        raw = json.loads(row["raw_json"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(row["status"], "new")
        self.assertEqual(row["saved_at"], "2026-04-02T15:04:05+00:00")
        self.assertEqual(raw["text"], "SYNTHETIC bird resync")
        self.assertNotIn("handoff", raw)
        self.assertEqual(raw["x_signals"]["bookmark"]["rank"], 2)
        self.assertEqual(raw["x_signals"]["bookmark"]["acted_at"], "2026-04-02T15:04:05+00:00")
        self.assertEqual(raw["x_signals"]["like"]["rank"], 4)
        self.assertEqual(raw["x_explanation"], BOTH)

    def test_likes_do_not_count_as_bookmarks_the_crawl_has_already_seen(self):
        ids = [str(2000 + index) for index in range(20)]
        self.write([self.post(post_id=post_id, source_types=["like"], text="SYNTHETIC liked post") for post_id in ids])
        self.load()
        page = [self.tweet(post_id) for post_id in ids] + [self.tweet("2099", "SYNTHETIC fresh bookmark")]
        result = SimpleNamespace(stdout=json.dumps({"tweets": page, "nextCursor": None}), stderr="", returncode=0)
        with mock.patch("lib.fetch.subprocess.run", return_value=result):
            self.assertEqual(fetch.fetch_x_bookmarks(count=50), 21)
        rows = self.rows()
        self.assertEqual(len(rows), 21)
        self.assertTrue(all(row["source"] == "x" for row in rows))
        self.assertIn("2099", {row["external_id"] for row in rows})
        sample = next(row for row in rows if row["external_id"] == "2000")
        self.assertEqual(set(self.signals(sample)), {"bookmark", "like"})

    def test_list_prints_nothing_extra_without_an_explanation(self):
        db.init_db()
        db.upsert_item(
            source="x", external_id="1", title="SYNTHETIC old", body="SYNTHETIC old",
            url="https://x.com/synthetic_user/status/1", author="@synthetic_user",
            raw_json="{}", created_at=CREATED, synced_at=FETCHED,
        )
        output = io.StringIO()
        with mock.patch("sys.argv", ["beacon", "list", "--source", "x"]), redirect_stdout(output):
            beacon.main()
        text = output.getvalue()
        self.assertIn("SYNTHETIC old", text)
        self.assertNotIn(BOOKMARK, text)
        self.assertNotIn(LIKE, text)
        self.assertEqual(len([line for line in text.splitlines() if line]), 3)

    def test_cli_hides_file_text_on_value_unicode_and_sqlite_errors(self):
        self.write([self.post(text="SYNTHETIC one"), self.post(text="SYNTHETIC two")])
        err = io.StringIO()
        with mock.patch.object(beacon, "load_config", side_effect=AssertionError("config access")), \
             mock.patch("sys.argv", ["beacon", "import-x", str(self.path), "--db", str(self.database)]), \
             redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                beacon.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Conflicting duplicate post_id.", err.getvalue())
        self.assertNotIn("SYNTHETIC", err.getvalue())

        self.path.write_bytes("SYNTHETIC secret".encode("utf-16"))
        err = io.StringIO()
        with mock.patch("sys.argv", ["beacon", "import-x", str(self.path), "--db", str(self.database)]), \
             redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                beacon.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Cannot read export or write destination database.", err.getvalue())
        self.assertNotIn("SYNTHETIC", err.getvalue())

        self.write([self.post(post_id="11", text="SYNTHETIC hidden")])
        db.init_db(self.database)
        with closing(db.get_connection(self.database)) as conn, conn:
            conn.execute("CREATE TRIGGER refuse BEFORE INSERT ON items BEGIN SELECT RAISE(ABORT, 'SYNTHETIC boom'); END")
        err = io.StringIO()
        with mock.patch("sys.argv", ["beacon", "import-x", str(self.path), "--db", str(self.database)]), \
             redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                beacon.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Cannot read export or write destination database.", err.getvalue())
        self.assertNotIn("SYNTHETIC", err.getvalue())
        self.assertEqual(self.rows(), [])


class XHandoffCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "handoff.json"
        posts = [
            {
                "post_id": "1900000000000000091",
                "url": "https://x.com/synthetic_user/status/1900000000000000091",
                "text": "SYNTHETIC bookmark for the handoff",
                "created_at": "2026-03-01T12:00:00Z",
                "source_types": ["bookmark"],
                "fetched_at": "2026-11-01T00:00:00Z",
                "author": {"username": "synthetic_user", "name": "Synthetic Name"},
            },
            {
                "post_id": "1900000000000000092",
                "url": "https://x.com/synthetic_user/status/1900000000000000092",
                "text": "SYNTHETIC like for the handoff",
                "created_at": "2026-03-02T12:00:00Z",
                "source_types": ["like"],
                "fetched_at": "2026-11-01T00:00:00Z",
                "author": {"username": "synthetic_user", "name": "Synthetic Name"},
            },
        ]
        self.path.write_text(json.dumps({"format": "x-handoff-v1", "posts": posts}), encoding="utf-8")
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        source = Path(beacon.__file__).parent
        shutil.copyfile(source / "beacon.py", self.checkout / "beacon.py")
        shutil.copytree(source / "lib", self.checkout / "lib", ignore=shutil.ignore_patterns("__pycache__"))

    def run_cli(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(self.checkout / "beacon.py"), *arguments],
            cwd=self.checkout, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_cli_preview_import_list_search_and_rejected_sync(self):
        preview = json.loads(self.run_cli("import-x", str(self.path), "--preview"))
        self.assertTrue(preview["preview"])
        self.assertNotIn("SYNTHETIC", json.dumps(preview))
        self.assertNotIn("1900000000000000091", json.dumps(preview))
        self.assertFalse((self.checkout / "beacon.db").exists())
        self.assertEqual(json.loads(self.run_cli("import-x", str(self.path)))["added"], 2)
        second = json.loads(self.run_cli("import-x", str(self.path)))
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["promoted"], 0)
        self.assertEqual(second["refreshed"], 2)
        (self.checkout / "config.json").write_text(json.dumps({"sources": {"x": {}}, "workstreams": []}))
        self.assertIn("import-x", self.run_cli("--help"))
        listed_help = self.run_cli("list", "--help")
        self.assertIn("x-like", listed_help)
        self.assertIn("interest", listed_help)
        likes = self.run_cli("list", "--source", "x-like")
        like_lines = likes.splitlines()
        like_index = like_lines.index("  " + LIKE)
        self.assertIn("SYNTHETIC like for the handoff", like_lines[like_index - 1])
        self.assertTrue(like_lines[like_index + 1].strip().startswith("https://"))
        saves = self.run_cli("list", "--source", "x")
        save_lines = saves.splitlines()
        save_index = save_lines.index("  " + BOOKMARK)
        self.assertIn("SYNTHETIC bookmark for the handoff", save_lines[save_index - 1])
        self.assertTrue(save_lines[save_index + 1].strip().startswith("https://"))
        self.assertIn(LIKE, self.run_cli("list", "--status", "interest"))
        self.assertIn("SYNTHETIC", self.run_cli("search", "SYNTHETIC"))
        rejected = subprocess.run(
            [sys.executable, str(self.checkout / "beacon.py"), "sync", "--source", "x-like"],
            cwd=self.checkout, text=True, capture_output=True,
        )
        self.assertEqual(rejected.returncode, 2)


if __name__ == "__main__":
    unittest.main()
