"""Adding your own things to the library: any link, an X post, or a picture file."""
import base64
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lib import adds, db, server

PAGE = b"""<html><head><title>Fallback title</title>
<meta property="og:title" content="A cabin in the woods &amp; more">
<meta property="og:description" content="Small house, big windows.">
<meta property="og:image" content="/images/cabin.jpg">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
</head><body>ignored</body></html>"""

TWEET = {"id": "1900", "text": "a nice chair", "createdAt": "Tue Sep 29 18:38:50 +0000 2026",
         "author": {"username": "amy", "name": "Amy"},
         "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/chair.jpg", "width": 800, "height": 1000}]}

# A 1x1 PNG.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")


class AddsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value, replacement in ((db, "DB_PATH", str(root / "beacon.db")), (adds, "MEDIA_DIR", root / "media")):
            patcher = patch.object(target, value, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()

    def status(self, item_id):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute("SELECT source, status FROM items WHERE id=?", (item_id,)).fetchone()

    def test_a_web_page_becomes_a_kept_save_with_its_preview_picture(self):
        with patch.object(adds, "_download", return_value=(PAGE, "text/html")):
            item_id = adds.add_link("https://example.com/post/cabin")
        self.assertEqual(self.status(item_id), ("add", "adopted"))
        entry = db.get_library()["items"][0]
        self.assertEqual((entry["id"], entry["title"], entry["text"]),
                         (item_id, "A cabin in the woods & more", "Small house, big windows."))
        # A relative og:image is resolved against the page.
        self.assertEqual(entry["media"][0], {"url": "https://example.com/images/cabin.jpg", "width": 1200, "height": 630})
        self.assertEqual(entry["author_name"], "example.com")

    def test_an_x_link_is_stored_like_a_bookmark(self):
        with patch.object(adds, "_read_tweet", return_value=TWEET) as read:
            item_id = adds.add_link("https://x.com/amy/status/1900?s=20")
        read.assert_called_once_with("1900")
        self.assertEqual(self.status(item_id), ("x", "adopted"))
        self.assertEqual(db.get_library()["items"][0]["media"][0]["url"], "https://pbs.twimg.com/media/chair.jpg")

    def test_a_picture_is_kept_once_and_served_from_media(self):
        source = Path(self.temp.name) / "photo.png"
        source.write_bytes(PNG)
        first = adds.add_image(str(source), width=1, height=1, note="my lamp")
        again = adds.add_image(str(source), width=1, height=1, note="my lamp")
        self.assertEqual(first, again)
        entry = db.get_library()["items"][0]
        name = entry["media"][0]["url"]
        self.assertTrue(name.startswith("/media/") and name.endswith(".png"), name)
        self.assertEqual((adds.MEDIA_DIR / name.removeprefix("/media/")).read_bytes(), PNG)
        self.assertEqual(entry["text"], "my lamp")

    def test_a_page_that_refuses_beacon_is_still_kept_as_a_link(self):
        refused = urllib.error.HTTPError("https://www.nytimes.com/a", 403, "Forbidden", {}, None)
        with patch.object(adds, "_download", side_effect=refused):
            item_id = adds.add_link("https://www.nytimes.com/2026/10/09/style/cabins.html")
        entry = db.get_library()["items"][0]
        self.assertEqual((entry["id"], entry["title"], entry["media"]),
                         (item_id, "nytimes.com/2026/10/09/style/cabins.html", []))

    def test_only_web_links_are_accepted(self):
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "not a link"):
            with self.assertRaises(ValueError):
                adds.add_link(bad)

    def test_server_adds_links_and_pictures_and_serves_media_safely(self):
        calls, uploaded = [], []

        def run(args):
            if args[0] == "vectors":  # indexing runs after the answer, on its own thread
                return "{}"
            calls.append(args)
            if "--image" in args:  # the upload exists while Beacon copies it, and is removed after
                uploaded.append(Path(args[2]).read_bytes())
            return '{"id": 7}'

        self.assertEqual(server.route("POST", "/add", {"url": "https://example.com"}, run), (200, {"id": 7}))
        self.assertEqual(calls[-1], ["keep", "--url", "https://example.com", "--no-index"])
        server.route("POST", "/add", {"image": base64.b64encode(PNG).decode(), "width": 1, "height": 1, "note": "n"}, run)
        args = calls[-1]
        self.assertEqual((args[:2], uploaded), (["keep", "--image"], [PNG]))
        self.assertFalse(Path(args[2]).exists())
        self.assertEqual(args[3:], ["--width", "1", "--height", "1", "--note", "n", "--no-index"])
        with self.assertRaises(ValueError):
            server.route("POST", "/add", {}, run)
        adds.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        name = "0123456789abcdef0123456789abcdef.png"
        (adds.MEDIA_DIR / name).write_bytes(PNG)
        self.assertEqual(server.media(f"/media/{name}"), (PNG, "image/png"))
        for bad in ("/media/../beacon.db", "/media/abc.png", "/media/a/b.png", f"/media/{name[:-4]}.svg"):
            self.assertIsNone(server.media(bad))
