"""Meaning search and similar pictures: what gets indexed, and how words nudge meaning results."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, vectors


class VectorsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon.db"
        patcher = patch.object(db, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        photo = {"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg"}
        video = {"type": "video", "url": "https://video.twimg.com/v.mp4", "previewUrl": "https://pbs.twimg.com/thumb.jpg"}
        rows = [(1, "new", "fresh pasta at home", [photo]), (2, "rejected", "rejected pasta", [photo]),
                (3, "queued", "just words", []), (4, "adopted", "a video of noodles", [video])]
        with sqlite3.connect(self.path) as conn:
            for item_id, status, text, media in rows:
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,raw_json,synced_at) "
                             "VALUES(?,'x',?,?,?,?,?,?,'2026-10-09')",
                             (item_id, str(item_id), text, text, f"https://x.com/a/{item_id}", status,
                              json.dumps({"text": text, "media": media})))

    def test_unread_library_saves_are_queued_with_their_picture_and_words(self):
        jobs = vectors.pending(10)
        self.assertEqual(jobs, [
            {"id": 4, "image": "https://pbs.twimg.com/thumb.jpg", "words": "a video of noodles"},
            {"id": 1, "image": "https://pbs.twimg.com/media/a.jpg", "words": "fresh pasta at home"}])
        with sqlite3.connect(self.path) as conn:
            # A table from a helper built before words_vec: pending adds the column.
            conn.execute("CREATE TABLE item_vectors (item_id INTEGER PRIMARY KEY, model TEXT NOT NULL, "
                         "text_vec BLOB, image_vec BLOB, indexed_at TEXT NOT NULL)")
            conn.execute("INSERT INTO item_vectors(item_id, model, text_vec, indexed_at) VALUES(4, 'm', x'00', 'now')")
            # Its picture didn't download last time: no vector, so it's tried again.
            conn.execute("INSERT INTO item_vectors(item_id, model, indexed_at) VALUES(1, 'm', 'now')")
        # Save 4's picture is read, so only its words are asked for; the picture isn't downloaded again.
        self.assertEqual(vectors.pending(10), [
            {"id": 4, "image": None, "words": "a video of noodles"},
            {"id": 1, "image": "https://pbs.twimg.com/media/a.jpg", "words": "fresh pasta at home"}])
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE item_vectors SET words_vec = x'00'")
        self.assertEqual([job["id"] for job in vectors.pending(10)], [1])
        # Right after you add a save, only that one is read (not every older retry).
        self.assertEqual([job["id"] for job in vectors.pending(10, ids=[4])], [])

    def test_saves_without_a_picture_are_read_for_their_words(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,raw_json,synced_at) "
                         "VALUES(5,'github','o/r','o/r','A pasta-making robot','https://github.com/o/r','new','{}','2026-10-09')")
            conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,raw_json,synced_at) "
                         "VALUES(6,'github','o/e','','','https://github.com/o/e','new','{}','2026-10-09')")
        self.assertEqual(vectors.pending(10, ids=[5, 6]), [{"id": 5, "image": None, "words": "o/r. A pasta-making robot"}])

    def test_a_plural_finds_the_singular_but_codes_and_ss_words_are_left_alone(self):
        self.assertEqual(vectors.word_ranking("pastas"), [1])
        self.assertEqual([vectors._singular(t) for t in ["chairs", "stories", "glass", "bus", "womanC", "v2s", "s"]],
                         ["chair", "story", None, None, None, None, None])

    def test_fusion_ranks_saves_two_lists_agree_on_above_one_lists_top_hit(self):
        self.assertEqual(vectors.fuse([9, 1, 2], [1, 3], [3, 1]), [1, 3, 9, 2])
        self.assertEqual(vectors.fuse([], [5, 5, 6]), [5, 6])  # repeats in one list count once
        self.assertEqual(vectors.fuse([2, 1], [1, 2]), [2, 1])  # a tie keeps the words ranking's order

    def test_one_search_box_fuses_words_pictures_and_meaning(self):
        answer = {"pictures": [[4, 0.3], [1, 0.2]], "words": [[1, 0.9]]}
        with patch.object(vectors, "_run", return_value=json.dumps(answer)):
            ranked, answered = vectors.search("pasta")
        self.assertEqual(ranked, [1, 4])  # 1 has the word and both meanings; 4 only a picture match
        self.assertEqual(answered, ["words", "pictures", "meaning"])

    def test_search_still_answers_without_the_helper_or_with_an_old_one(self):
        with patch.object(vectors, "_run", side_effect=RuntimeError("beacon-embed isn't built")):
            self.assertEqual(vectors.search("pasta"), ([1], ["words"]))  # 2 is rejected, so not in the library

        def old_helper(kind, *args, **kwargs):
            if kind == "search":
                raise RuntimeError("unknown command search")
            return json.dumps([[4, 0.3]])
        with patch.object(vectors, "_run", side_effect=old_helper):
            self.assertEqual(vectors.search("pasta"), ([1, 4], ["words", "pictures"]))
        self.assertEqual(vectors.search("   "), ([], []))

    def test_a_follow_up_step_narrows_and_says_how_many_it_set_aside(self):
        answers = {"food": {"pictures": [[4, 0.3], [1, 0.2]], "words": [[1, 0.2]]},
                   "noodles": {"pictures": [[4, 0.4]], "words": []}}
        with patch.object(vectors, "_run", side_effect=lambda kind, flag, query, *rest: json.dumps(answers[query])):
            page = vectors.library_page(query="food", refine=["noodles"])
            whole = vectors.library_page(query="food")
            with patch.object(vectors, "TOP_PICTURES", 0):
                strict = vectors.library_page(query="food")
        self.assertEqual([entry["id"] for entry in page["items"]], [4])
        self.assertEqual(page["set_aside"], 1)
        # Before the step: 1 is found by picture and meaning (strong); 4 by its picture alone, but it's the
        # top picture match, so strong too. Past the top picture matches, a picture alone might also fit.
        self.assertEqual([(e["id"], e["found_by"], e["fit"]) for e in whole["items"]],
                         [(1, ["pictures", "meaning"], "strong"), (4, ["pictures"], "strong")])
        self.assertEqual([e["fit"] for e in strict["items"]], ["strong", "maybe"])
        self.assertNotIn("set_aside", whole)
        self.assertEqual(vectors.fit(["meaning"]), "maybe")

    def test_library_page_with_the_search_box_says_what_answered(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO item_gists VALUES(1,'Pasta cut into ribbons','tip','reference','r','2026-10-10')")
        with patch.object(vectors, "_run", side_effect=RuntimeError("no helper")):
            page = vectors.library_page(query="fresh ribbons")
            missing = vectors.library_page(query="dried pastas")
        self.assertEqual([entry["id"] for entry in page["items"]], [1])
        self.assertEqual(page["searched"], ["words"])
        self.assertEqual(missing["items"], [])

    def test_long_queries_try_shared_keywords_without_requiring_every_word(self):
        with patch.object(vectors, "_run", side_effect=RuntimeError("no helper")):
            page = vectors.library_page(query="Help me find fresh pastas for a family dinner")
            unrelated = vectors.library_page(query="Find steel roof panels and home insulation for winter")
        self.assertEqual([entry["id"] for entry in page["items"]], [1])
        self.assertEqual(page["items"][0]["fit"], "maybe")
        self.assertEqual(unrelated["items"], [])

    def test_saves_containing_the_words_move_up_in_meaning_results(self):
        with patch.object(vectors, "_run", return_value=json.dumps([[4, 0.80], [1, 0.78]])):
            ranked = vectors.meaning("pasta")
        self.assertEqual([item_id for item_id, _ in ranked], [1, 4])


FAKE_HELPER = """#!/usr/bin/env python3
import json, sys
# Answers one request, then quits, like a helper that crashed.
request = json.loads(sys.stdin.readline())
print(json.dumps([[request.get("similar", 7), 0.5]]) if "error" not in request else json.dumps({"error": "bad"}), flush=True)
"""


FAKE_SEARCH_HELPER = """#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    print(json.dumps({"pictures": [[1, 0.5]], "words": [[2, 0.4]]}), flush=True)
"""


class ResidentTest(unittest.TestCase):
    def test_a_search_answer_is_an_object_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "beacon-embed"
            helper.write_text(FAKE_SEARCH_HELPER)
            os.chmod(helper, 0o755)
            resident = vectors._Resident()
            with patch.object(vectors, "HELPER", helper):
                self.assertEqual(resident.ask({"search": "x"}), {"pictures": [[1, 0.5]], "words": [[2, 0.4]]})
            resident.process.stdin.close()
            resident.process.wait()

    def test_one_running_helper_answers_and_is_restarted_when_it_dies(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "beacon-embed"
            helper.write_text(FAKE_HELPER)
            os.chmod(helper, 0o755)
            resident = vectors._Resident()
            with patch.object(vectors, "HELPER", helper), patch.object(vectors, "resident", resident):
                self.assertEqual(vectors.similar(3), [(3, 0.5)])
                self.assertEqual(vectors.similar(4), [(4, 0.5)])  # the first helper quit; a new one answers
                with self.assertRaisesRegex(RuntimeError, "bad"):
                    resident.ask({"error": True})

    def test_picture_names_come_back_by_save_and_an_old_or_missing_helper_gives_none(self):
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "beacon-embed"
            helper.write_text(NAMES_HELPER)
            os.chmod(helper, 0o755)
            with patch.object(vectors, "HELPER", helper), patch.object(vectors, "resident", None):
                self.assertEqual(vectors.names([7, 8]), {7: [("armchair", 0.12)]})
                self.assertEqual(vectors.names([]), {})
            with patch.object(vectors, "HELPER", Path(temp) / "missing"), patch.object(vectors, "resident", None):
                self.assertEqual(vectors.names([7]), {})


NAMES_HELPER = """#!/usr/bin/env python3
import json, sys
assert sys.argv[1:4] == ["names", "--ids", "7,8"], sys.argv
print(json.dumps({"7": [["armchair", 0.12]]}))
"""

SILENT_HELPER = """#!/usr/bin/env python3
import sys, time
sys.stdin.readline()
time.sleep(600)  # alive, but never answers
"""


class HungHelperTest(unittest.TestCase):
    """Audit B08: a helper that stays alive but stops answering must not block every later search."""

    def test_a_silent_helper_is_stopped_and_the_search_fails_instead_of_hanging(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            helper = Path(temp) / "beacon-embed"
            helper.write_text(SILENT_HELPER)
            os.chmod(helper, 0o755)
            resident = vectors._Resident()
            outcome, started = {}, []
            real_popen = vectors.subprocess.Popen

            def popen(*args, **kwargs):
                started.append(real_popen(*args, **kwargs))
                return started[-1]

            def search():
                try:
                    resident.ask({"meaning": "pasta", "limit": 1})
                except RuntimeError as error:
                    outcome["error"] = str(error)

            with patch.object(vectors, "HELPER", helper), patch.object(vectors, "ANSWER_SECONDS", 0.5, create=True), \
                    patch.object(vectors.subprocess, "Popen", popen):
                worker = threading.Thread(target=search, daemon=True)
                worker.start()
                worker.join(10)
                if worker.is_alive():
                    started[-1].kill()  # let the stuck thread go before failing
                self.assertFalse(worker.is_alive(), "the search waited on the helper with no deadline")
            self.assertIn("didn't answer", outcome["error"])
            self.assertIsNone(resident.process)  # the next search starts a fresh helper
            self.assertEqual(len(started), 1)  # no retry against a hung helper
            self.assertIsNotNone(started[0].poll())  # stopped and reaped, not left running
