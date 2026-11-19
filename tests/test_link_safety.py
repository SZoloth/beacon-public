"""Saving a link makes this Mac fetch it. It may only fetch public pages, however the address is written
and wherever the page redirects. Nothing here touches the internet or any real internal service."""
import http.server
import ipaddress
import socket
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lib import adds, db

PRIVATE = ["127.0.0.1", "127.1", "2130706433", "0x7f.0.0.1", "0177.0.0.1", "localhost", "10.0.0.5", "172.16.3.4",
           "192.168.1.20", "169.254.169.254", "100.64.0.1", "0.0.0.0", "[::1]", "[::ffff:127.0.0.1]", "[fe80::1]",
           "[fd7a:115c:a1e0::1]", "[64:ff9b::7f00:1]", "[2002:7f00:1::]", "224.0.0.1"]


class Recorder(http.server.BaseHTTPRequestHandler):
    """A pretend page on a random local port that notes every request it receives."""

    def do_GET(self):
        self.server.hits.append(self.path)
        target = self.server.redirect_to
        if target:
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
        else:
            body = b"<title>Reached</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not target:
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(test, redirect_to=None):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Recorder)
    httpd.hits, httpd.redirect_to = [], redirect_to
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    test.addCleanup(httpd.server_close)
    test.addCleanup(httpd.shutdown)
    return httpd


def url(httpd, path="/"):
    return f"http://127.0.0.1:{httpd.server_address[1]}{path}"


class DestinationTest(unittest.TestCase):
    def test_only_public_addresses_are_allowed(self):
        for text in ("93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946", "2a00:1450:4009:81f::200e"):
            self.assertTrue(adds._permitted(ipaddress.ip_address(text)), text)
        for text in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "10.1.2.3", "192.168.0.1", "169.254.169.254",
                     "100.100.100.100", "fd7a:115c:a1e0::5", "fe80::1", "0.0.0.0", "255.255.255.255", "224.0.0.251",
                     "64:ff9b::a00:1", "2002:a00:1::", "fc00::1"):
            self.assertFalse(adds._permitted(ipaddress.ip_address(text)), text)

    def test_every_way_of_writing_a_private_address_is_refused_before_connecting(self):
        # Resolvers differ: glibc reads 0177.0.0.1 as octal (127.0.0.1), macOS as 177.0.0.1, a public
        # address. Whatever the platform decides, a connection must never reach a private address.
        for host in PRIVATE:
            reached = []
            with patch.object(socket.socket, "connect", side_effect=lambda address: reached.append(address)):
                try:
                    adds._connect_public(host.strip("[]"), 80, 5)
                except urllib.error.URLError:
                    continue
            self.assertTrue(reached and all(ipaddress.ip_address(a[0]).is_global for a in reached), host)

    def test_a_name_with_any_private_answer_is_refused(self):
        # An attacker's DNS can return a public address and a private one.
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 80))]
        with patch.object(socket, "getaddrinfo", return_value=answers), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("connected")), \
             self.assertRaises(urllib.error.URLError):
            adds._connect_public("both.example", 80, 5)

    def test_the_vetted_address_is_the_one_connected_to(self):
        # No second lookup between the check and the connection, so DNS can't change its answer in between.
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        lookups = []

        def getaddrinfo(*args, **kwargs):
            lookups.append(args)
            return answers

        with patch.object(socket, "getaddrinfo", side_effect=getaddrinfo), \
             patch.object(socket.socket, "connect") as connect:
            sock = adds._connect_public("page.example", 80, 5)
            sock.close()
        self.assertEqual(len(lookups), 1)
        connect.assert_called_once_with(("93.184.216.34", 80))


class DownloadTest(unittest.TestCase):
    def test_a_private_link_is_never_requested(self):
        page = serve(self)
        with self.assertRaises(urllib.error.URLError):
            adds._download(url(page))
        self.assertEqual(page.hits, [])

    def test_a_redirect_is_checked_where_it_lands(self):
        internal = serve(self)
        public = serve(self, redirect_to=url(internal, "/admin"))
        # Treat the first host as public and everything after it as private.
        with patch.object(adds, "_permitted", side_effect=[True, False]), self.assertRaises(urllib.error.URLError):
            adds._download(url(public))
        self.assertEqual((len(public.hits), internal.hits), (1, []))

    def test_a_public_page_still_downloads(self):
        page = serve(self)
        with patch.object(adds, "_permitted", return_value=True):
            body, content_type = adds._download(url(page))
        self.assertEqual((body, content_type), (b"<title>Reached</title>", "text/html"))

    def test_redirects_to_other_kinds_of_address_are_refused(self):
        handler = adds._Redirect()
        request = adds.Request("https://example.com/a")
        for target in ("ftp://example.com/x", "file:///etc/passwd", "gopher://example.com/", "data:text/html,hi",
                       "https://user:secret@example.com/", "//user@example.com/x"):
            with self.assertRaises(urllib.error.URLError, msg=target):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/b")  # a plain web link: fine
        self.assertEqual(redirected.full_url, "https://example.com/b")

    def test_redirect_chains_are_bounded(self):
        self.assertLessEqual(adds._Redirect.max_redirections, 5)

    def test_downloads_do_not_go_through_a_proxy(self):
        # A proxy would hide the real destination from the check.
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}):
            page = serve(self)
            with patch.object(adds, "_permitted", return_value=True):
                self.assertEqual(adds._download(url(page))[1], "text/html")


class AddLinkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, name, value in ((db, "DB_PATH", str(root / "beacon.db")), (adds, "MEDIA_DIR", root / "media")):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()

    def test_a_private_or_redirecting_link_is_kept_as_a_plain_card_without_a_preview(self):
        page = serve(self)
        item_id = adds.add_link(url(page, "/notes"))
        self.assertEqual(page.hits, [])
        entry = next(e for e in db.get_library()["items"] if e["id"] == item_id)
        self.assertEqual(entry["media"], [])
        self.assertEqual(entry["title"], f"127.0.0.1:{page.server_address[1]}/notes")

    def test_links_with_a_password_in_them_are_not_saved(self):
        for bad in ("https://user:secret@example.com/a", "https://token@example.com/", "http://a:b@127.0.0.1/"):
            with patch.object(adds, "_download") as download, self.assertRaises(ValueError, msg=bad):
                adds.add_link(bad)
            download.assert_not_called()
        self.assertEqual(db.get_library()["items"], [])


if __name__ == "__main__":
    unittest.main()
