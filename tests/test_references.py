"""Reference review: which of these saves fit what you're making, and which repeat a picture."""
import array
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, references, server, useful, vectors

# Meaning search over a 101-picture library: 3 and 1 are in the top 2%, 2 just outside, 4 last.
MEANING = [(3, 0.30), (1, 0.29), (2, 0.28)] + [(1000 + k, 0.1) for k in range(97)] + [(4, 0.01)]


def fingerprint(*values):
    return array.array("f", values).tobytes()


def jev(strong_ids=(), fail=False):
    """A Jev reply that rates `strong_ids` strong references and the rest unrelated."""
    def respond(request, **_):
        if fail:
            raise ValueError("TypeSafe connection failed")
        answers = {}
        for key in request["questions"]:
            p2 = 0.9 if int(key.removeprefix("save_")) in strong_ids else 0.0
            p = [1 - p2 - 0.05, 0.05, p2]
            answers[key] = {"type": "score", "score": p[1] + 2 * p[2], "confidence": 0.9,
                            "probabilities": {str(i): v for i, v in enumerate(p)},
                            "legend": {str(i): label for i, label in enumerate(references.CRITERIA)}}
        return {"model": useful.MODEL, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 0}}
    return respond


class ReferencesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "beacon.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        texts = {1: "kitchen with green tiles", 2: "kitchen with green tiles again", 3: "lol", 4: "a walnut lounge chair in warm light"}
        with sqlite3.connect(db.DB_PATH) as conn:
            conn.execute("CREATE TABLE item_vectors (item_id INTEGER PRIMARY KEY, model TEXT NOT NULL, "
                         "text_vec BLOB, image_vec BLOB, indexed_at TEXT NOT NULL)")
            for item_id, text in texts.items():
                photo = {"type": "photo", "url": f"https://pbs.twimg.com/media/{item_id}.jpg"}
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,raw_json,synced_at) "
                             "VALUES(?,'x',?,?,?,?,'adopted',?,'2026-10-09')",
                             (item_id, str(item_id), text, text, f"https://x.com/a/{item_id}",
                              json.dumps({"text": text, "media": [photo]})))
            # 1 and 2 are the same picture (fingerprints nearly identical); 3 and 4 look different.
            for item_id, vector in ((1, (1.0, 0.0, 0.0)), (2, (0.99, 0.141, 0.0)), (3, (0.0, 1.0, 0.0)), (4, (0.0, 0.0, 1.0))):
                conn.execute("INSERT INTO item_vectors VALUES(?, 'm', NULL, ?, 'now')", (item_id, fingerprint(*vector)))
        meaning = patch.object(vectors, "meaning", return_value=MEANING)
        meaning.start()
        self.addCleanup(meaning.stop)

    def summary(self, result):
        return [(save["id"], save["fit"], save["duplicate_of"]) for save in result["saves"]]

    def test_pictures_alone_rank_the_saves_and_repeats_sink(self):
        with patch.object(useful, "call_typesafe", side_effect=AssertionError("no upload without consent")):
            result = references.review("a green kitchen", [4, 2, 3, 1])
        self.assertEqual(self.summary(result), [(3, "strong", None), (1, "strong", None), (4, "off", None), (2, "maybe", 1)])
        self.assertFalse(result["words_checked"])
        self.assertIn("top 1%", result["saves"][0]["reason"])

    def test_jev_reads_the_words_when_allowed(self):
        with patch.object(useful, "call_typesafe", side_effect=jev(strong_ids={4})) as call:
            result = references.review("a reading corner", [1, 2, 3, 4], allow_upload=True)
        self.assertEqual(self.summary(result), [(3, "strong", None), (1, "strong", None), (4, "strong", None), (2, "maybe", 1)])
        self.assertTrue(result["words_checked"])
        sent = call.call_args.args[0]
        # "lol" says too little to judge, so it isn't sent.
        self.assertEqual(sorted(sent["questions"]), ["save_1", "save_2", "save_4"])
        self.assertEqual(sent["state"]["brief"], "a reading corner")

    def test_a_jev_failure_still_returns_the_picture_review(self):
        with patch.object(useful, "call_typesafe", side_effect=jev(fail=True)):
            result = references.review("a green kitchen", [1, 3], allow_upload=True)
        self.assertEqual([save["id"] for save in result["saves"]], [3, 1])
        self.assertFalse(result["words_checked"])
        self.assertIn("connection failed", result["note"])

    def test_a_picture_not_read_yet_says_so(self):
        with patch.object(vectors, "meaning", return_value=[(1, 0.3)]):
            reasons = {save["id"]: save["reason"] for save in references.review("a green kitchen", [1, 3])["saves"]}
        self.assertEqual(reasons[3], "Picture not read yet")

    def test_bad_requests_are_refused(self):
        for brief, ids in (("", [1]), ("x" * 301, [1]), ("kitchen", []), ("kitchen", list(range(41)))):
            with self.assertRaises(ValueError):
                references.review(brief, ids)

    def test_server_passes_the_brief_and_saves_to_the_cli(self):
        calls = []
        run = lambda args: calls.append(args) or '{"saves": []}'
        self.assertEqual(server.route("POST", "/references", {"brief": "green kitchen", "ids": [3, 1], "words": True}, run),
                         (200, {"saves": []}))
        self.assertEqual(calls[-1], ["references", "--brief", "green kitchen", "--ids", "3,1", "--allow-upload"])
        server.route("POST", "/references", {"brief": "green kitchen", "ids": [3]}, run)
        self.assertEqual(calls[-1], ["references", "--brief", "green kitchen", "--ids", "3"])
        # A brief that's too long reaches the review, which says so, on the phone as on the Mac.
        server.route("POST", "/references", {"brief": "k" * 400, "ids": [3]}, run)
        self.assertEqual(calls[-1][2], "k" * 400)
        for bad in ({"brief": "-x", "ids": [1]}, {"brief": "k", "ids": ["1; rm"]}, {"brief": "k"}):
            with self.assertRaises(ValueError):
                server.route("POST", "/references", bad, run)


if __name__ == "__main__":
    unittest.main()
