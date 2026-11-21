"""beacon serve: a fixed set of Triage actions over HTTP, answered only for Tailscale and this Mac."""
import gzip
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from email.message import Message
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from lib import autopilot, briefs, db, server


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def run_cli(self, args):
        self.calls.append(args)
        return json.dumps([{"id": 1}]) if args[0] in ("queue", "briefs") else ""

    def route(self, method, path, body=None):
        return server.route(method, path, body or {}, self.run_cli)

    def test_reads_are_answered_here_without_starting_the_cli(self):
        # Starting `beacon queue` per request took ~2 s; the query itself takes ~90 ms.
        with patch.object(db, "get_triage_queue", return_value=[{"id": 1}]), \
             patch.object(briefs, "list_plans", return_value=[{"id": 2}]):
            self.assertEqual(self.route("GET", "/queue"), (200, [{"id": 1}]))
            self.assertEqual(self.route("GET", "/briefs"), (200, [{"id": 2}]))
        with patch.object(autopilot, "handled", return_value=[{"id": 3, "action": "pass"}]):
            self.assertEqual(self.route("GET", "/handled"), (200, [{"id": 3, "action": "pass"}]))
        self.assertEqual(self.calls, [])

    def test_library_search_box_and_older_word_and_meaning_params(self):
        with patch.object(server.vectors, "library_page", return_value={"items": []}) as page:
            self.route("GET", "/library?search=red%20chair&area=home")
            self.route("GET", "/library?q=chair&meaning=dinner")
        self.assertEqual(page.call_args_list[0].kwargs, {"offset": 0, "limit": 200, "area": "home", "query": "red chair"})
        self.assertEqual(page.call_args_list[1].kwargs, {"offset": 0, "limit": 200, "words": "chair", "about": "dinner"})
        with patch.object(server.vectors, "library_page", return_value={"items": []}) as page:
            self.route("GET", "/library?search=chairs&refine=wooden&refine=tall")
        self.assertEqual(page.call_args.kwargs["refine"], ["wooden", "tall"])
        with self.assertRaisesRegex(ValueError, "refine needs search"):
            self.route("GET", "/library?q=chairs&refine=wooden")
        with self.assertRaisesRegex(ValueError, "search must not start with a dash"):
            self.route("GET", "/library?search=-rf")

    def test_status_names_the_database_and_checkout_being_served(self):
        with patch.object(db, "DB_PATH", "/tmp/elsewhere/beacon.db"):
            status, payload = self.route("GET", "/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"database": str(Path("/tmp/elsewhere/beacon.db").resolve()), "checkout": str(server.ROOT)})
        self.assertEqual(self.calls, [])

    def test_writes_map_to_the_same_cli_commands_the_mac_app_uses(self):
        self.route("POST", "/mark", {"status": "rejected", "ids": [3, 4]})
        self.route("POST", "/read", {"id": 5})
        self.route("POST", "/unhandle", {"id": 9})
        self.route("POST", "/brief-status", {"item_id": 6, "workstream": "job-search", "status": "dismissed"})
        self.route("POST", "/brief-mac", {"item_id": 7, "workstream": "job-search", "on": True})
        self.route("POST", "/brief-done", {"item_id": 8, "workstream": "job-search", "item_status": "tried"})
        self.assertEqual(self.calls, [["mark", "rejected", "3", "4"], ["read", "5"], ["unhandle", "9"],
                                      ["brief-status", "6", "job-search", "dismissed"],
                                      ["brief-mac", "7", "job-search", "on"],
                                      ["brief-done", "8", "job-search", "dispatched", "tried"]])

    def test_plan_runs_one_project_agent(self):
        self.assertEqual(self.route("POST", "/plan", {"project": "agent-workflows"})[0], 200)
        self.assertEqual(self.calls, [["brief", "--project", "agent-workflows", "--allow-upload"]])

    def test_anything_else_is_refused_before_the_cli_runs(self):
        for method, path, body in [("POST", "/mark", {"status": "stale; rm -rf", "ids": [1]}),
                                   ("POST", "/mark", {"status": "rejected", "ids": ["1 --db x"]}),
                                   ("POST", "/plan", {"project": "../etc"}),
                                   ("POST", "/brief-status", {"item_id": 1, "workstream": "x", "status": "ready"}),
                                   ("POST", "/brief-mac", {"item_id": 1, "workstream": "x", "on": "yes"}),
                                   ("POST", "/brief-done", {"item_id": 1, "workstream": "x", "item_status": "rejected"}),
                                   ("POST", "/brief-done", {"item_id": 1, "workstream": "x", "item_status": ["tried"]}),
                                   ("POST", "/brief-done", {"item_id": True, "workstream": "x", "item_status": "tried"}),
                                   ("POST", "/brief-status", {"item_id": 1, "workstream": "x", "status": {}}),
                                   ("POST", "/mark", {"status": [], "ids": [1]}),
                                   ("POST", "/unhandle", {"id": "9; rm"})]:
            with self.assertRaises(ValueError):
                self.route(method, path, body)
        self.assertEqual(self.route("POST", "/sync")[0], 404)
        self.assertEqual(self.calls, [])

    def test_the_dropped_arrange_routes_are_gone(self):
        for method, path, body in [("POST", "/library/compose", {"search": "chairs", "ids": [1, 2, 3, 4]}),
                                   ("POST", "/library/compose/choice", {"id": 1, "choice": "kept"}),
                                   ("GET", "/library/compose/summary", {})]:
            self.assertEqual(self.route(method, path, body)[0], 404, path)
        self.assertEqual(self.calls, [])

    def test_only_tailscale_and_this_mac_may_connect(self):
        self.assertTrue(server.allowed("127.0.0.1"))
        self.assertTrue(server.allowed("100.79.14.83"))
        self.assertTrue(server.allowed("fd7a:115c:a1e0::1"))
        self.assertFalse(server.allowed("192.168.1.20"))
        self.assertFalse(server.allowed("8.8.8.8"))


class ServerTest(unittest.TestCase):
    def test_http_round_trip(self):
        httpd = server.make_server("127.0.0.1", 0, run_cli=lambda args: "[]")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", "/mark", body=json.dumps({"status": "bogus", "ids": [1]}),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn("error", json.loads(response.read()))
        with patch.object(db, "get_triage_queue", return_value=[]):
            conn.request("GET", "/queue")
            self.assertEqual(json.loads(conn.getresponse().read()), [])

    def test_big_answers_are_gzipped_when_asked(self):
        httpd = server.make_server("127.0.0.1", 0, run_cli=lambda args: "[]", key=KEY)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        queue = [{"id": i, "body": "a long post " * 50} for i in range(200)]
        # As the phone over Tailscale; a request from this Mac itself isn't compressed.
        with patch.object(db, "get_triage_queue", return_value=queue), patch.object(server, "on_this_mac", return_value=False):
            conn.request("GET", "/queue", headers={"Accept-Encoding": "gzip, deflate", "Authorization": f"Bearer {KEY}"})
            response = conn.getresponse()
            body = response.read()
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        self.assertLess(len(body), len(json.dumps(queue)) / 5)
        self.assertEqual(json.loads(gzip.decompress(body)), queue)


class TimeoutTest(unittest.TestCase):
    def test_a_cli_timeout_still_gets_an_http_answer(self):
        import subprocess

        def slow(args):
            raise subprocess.TimeoutExpired(args, 900)

        httpd = server.make_server("127.0.0.1", 0, slow)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/mark", body=json.dumps({"status": "rejected", "ids": [1]}),
                     headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 500)


class KeepTest(unittest.TestCase):
    """The phone waits 120 s for a keep; reading the save for search can take 30 minutes."""

    def serve(self, run_cli):
        httpd = server.make_server("127.0.0.1", 0, run_cli)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        return conn

    def keep(self, conn):
        conn.request("POST", "/add", body=json.dumps({"url": "https://example.com"}),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read())

    def test_the_phone_hears_the_save_landed_before_slow_indexing_finishes(self):
        calls, release, indexed = [], threading.Event(), threading.Event()

        def run_cli(args):
            calls.append(args)
            if args[0] == "vectors":
                release.wait(10)  # indexing that takes far longer than the phone waits
                indexed.set()
                return "{}"
            return '{"id": 7}'

        conn = self.serve(run_cli)
        self.assertEqual(self.keep(conn), (200, {"id": 7}))
        self.assertFalse(indexed.is_set())  # answered while indexing was still running
        self.assertEqual(calls[0], ["keep", "--url", "https://example.com", "--no-index"])
        release.set()
        self.assertTrue(indexed.wait(5))
        self.assertEqual(calls[1], ["vectors", "--ids", "7"])

    def test_a_save_that_failed_is_an_error_and_nothing_is_indexed(self):
        calls = []

        def run_cli(args):
            calls.append(args)
            raise RuntimeError("bird signed out")

        status, payload = self.keep(self.serve(run_cli))
        self.assertEqual((status, payload), (500, {"error": "bird signed out"}))
        self.assertEqual([args[0] for args in calls], ["keep"])

    def test_indexing_that_fails_is_logged_and_the_save_still_counts(self):
        indexed = threading.Event()

        def run_cli(args):
            if args[0] == "vectors":
                indexed.set()
                raise RuntimeError("beacon-embed isn't built")
            return '{"id": 9}'

        with patch("sys.stderr", new_callable=io.StringIO) as log:
            self.assertEqual(self.keep(self.serve(run_cli)), (200, {"id": 9}))
            self.assertTrue(indexed.wait(5))
            for _ in range(100):  # the log line follows the failure on the indexing thread
                if "not indexed yet" in log.getvalue():
                    break
                threading.Event().wait(0.05)
        self.assertIn("save 9 not indexed yet (beacon-embed isn't built)", log.getvalue())

    def test_a_share_sheet_save_is_routed_with_its_why_and_copied_to_mymind(self):
        mirrored = threading.Event()
        run_cli = lambda args: "{}" if args[0] == "vectors" else '{"id": 7}'
        with patch.object(server.send_to, "route_save", return_value={"routed": ["site"], "kept_out": []}) as routed, \
                patch.object(server.send_to, "mirror_to_mymind", side_effect=lambda *a: mirrored.set()) as mirror:
            conn = self.serve(run_cli)
            conn.request("POST", "/add", body=json.dumps({"url": "https://example.com", "projects": ["site"], "note": "why"}),
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual((response.status, json.loads(response.read())),
                             (200, {"id": 7, "routed": ["site"], "kept_out": []}))
            self.assertTrue(mirrored.wait(5))
        routed.assert_called_once_with(7, ["site"], "why")
        mirror.assert_called_once_with("https://example.com", ["site"], "why")

    def test_a_plain_keep_routes_nothing_and_bad_projects_are_refused(self):
        with patch.object(server.send_to, "route_save") as routed:
            self.assertEqual(server._send_to({"id": 7}, "https://example.com", {}), {"id": 7})
            for bad in ({"projects": "site"}, {"projects": ["Not A Slug"]}, {"projects": ["a"] * 6}, {"note": 3}):
                with self.assertRaises(ValueError):
                    server._send_to({"id": 7}, "https://example.com", bad)
        routed.assert_not_called()

    def test_the_share_sheet_asks_for_projects_ranked_for_its_text(self):
        with patch.object(server.send_to, "ranked", return_value=[{"slug": "site"}]) as ranked:
            self.assertEqual(server.route("GET", "/projects?text=grid%20layout", {}), (200, [{"slug": "site"}]))
        ranked.assert_called_once_with("grid layout")

    def test_indexing_gets_its_own_longer_deadline(self):
        with patch.object(server.subprocess, "run") as run:
            run.return_value.returncode = 0
            server.run_cli(["vectors", "--ids", "7"])
            self.assertGreaterEqual(run.call_args.kwargs["timeout"], 30 * 60)
            server.run_cli(["keep", "--url", "https://example.com", "--no-index"])
            self.assertEqual(run.call_args.kwargs["timeout"], 900)


KEY = "k" * 43
_temp = tempfile.TemporaryDirectory()


def setUpModule():
    # A server made without a key of its own must not write one into the real home directory.
    patcher = patch.dict(os.environ, {"BEACON_SERVE_KEY_FILE": str(Path(_temp.name) / "serve-key")})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(_temp.cleanup)
PHONE = "100.79.14.83"  # a tailnet peer
MAC_NAME = "my-mac.example-tailnet.ts.net:8765"


def headers(**fields):
    message = Message()
    for name, value in fields.items():
        message[name.replace("_", "-")] = value
    return message


class AuthorizeTest(unittest.TestCase):
    """Who may do what: the address range only says a request came from the tailnet, not from the phone."""

    def check(self, peer=PHONE, method="GET", path="/queue", **fields):
        return server.authorize(peer, method, path, headers(**fields), KEY)

    def test_the_phone_needs_the_access_key(self):
        self.assertEqual(self.check(Host=MAC_NAME)[0], 401)
        self.assertEqual(self.check(Host=MAC_NAME, Authorization="Bearer wrong")[0], 401)
        self.assertEqual(self.check(Host=MAC_NAME, Authorization=KEY)[0], 401)  # must say Bearer
        self.assertIsNone(self.check(Host=MAC_NAME, Authorization=f"Bearer {KEY}"))
        # A key that isn't plain ASCII is a wrong key, not a crash.
        self.assertEqual(self.check(Host=MAC_NAME, Authorization="Bearer k\u00e9y")[0], 401)

    def test_this_mac_needs_no_key_when_it_asks_for_itself_by_a_local_name(self):
        for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765", "127.0.0.1"):
            self.assertIsNone(self.check(peer="127.0.0.1", Host=host), host)
        self.assertIsNone(self.check(peer="127.0.0.1", method="POST", path="/mark", Host="127.0.0.1:8765",
                                     Content_Type="application/json"))

    def test_a_proxy_on_this_mac_does_not_turn_the_tailnet_into_this_mac(self):
        # `tailscale serve` and similar relays connect from 127.0.0.1 but keep the caller's Host.
        self.assertEqual(self.check(peer="127.0.0.1", Host=MAC_NAME)[0], 401)
        self.assertEqual(self.check(peer="127.0.0.1", Host="127.0.0.1:8765", X_Forwarded_For=PHONE)[0], 401)
        self.assertEqual(self.check(peer="127.0.0.1", Host="127.0.0.1:8765", Forwarded="for=1.2.3.4")[0], 401)
        self.assertIsNone(self.check(peer="127.0.0.1", Host=MAC_NAME, Authorization=f"Bearer {KEY}"))

    def test_unknown_host_names_are_refused_even_with_the_key(self):
        # DNS rebinding: a web page re-points its own name at this Mac and the browser still sends that name.
        for host in ("evil.example", "evil.example:8765", "127.0.0.1.evil.example", "192.168.1.20:8765", None):
            fields = {"Authorization": f"Bearer {KEY}"} | ({"Host": host} if host else {})
            self.assertEqual(self.check(peer="127.0.0.1", **fields)[0], 403, host)
            self.assertEqual(self.check(**fields)[0], 403, host)

    def test_the_tailnet_name_and_address_are_known_hosts(self):
        for host in (MAC_NAME, "100.79.14.83:8765", "[fd7a:115c:a1e0::5]:8765"):
            self.assertIsNone(self.check(Host=host, Authorization=f"Bearer {KEY}"), host)

    def test_other_names_can_be_added_for_this_mac(self):
        with patch.dict(os.environ, {"BEACON_SERVE_HOSTS": "mac.example.net, other.example.net"}):
            self.assertIsNone(self.check(Host="mac.example.net:8765", Authorization=f"Bearer {KEY}"))
        self.assertEqual(self.check(Host="mac.example.net:8765", Authorization=f"Bearer {KEY}")[0], 403)

    def test_a_web_page_cannot_use_the_server_even_from_this_mac(self):
        for origin in ("https://evil.example", "http://evil.example:8765", "null", "http://127.0.0.1:9999"):
            self.assertEqual(self.check(peer="127.0.0.1", Host="127.0.0.1:8765", Origin=origin)[0], 403, origin)
            self.assertEqual(self.check(Host=MAC_NAME, Authorization=f"Bearer {KEY}", Origin=origin)[0], 403, origin)
        # Native apps send no Origin. No page is served from here, so a browser never has a reason to
        # be on this Mac's exemption, even from its own origin.
        self.assertEqual(self.check(peer="127.0.0.1", Host="127.0.0.1:8765", Origin="http://127.0.0.1:8765")[0], 401)
        self.assertIsNone(self.check(peer="127.0.0.1", Host="127.0.0.1:8765", Origin="http://127.0.0.1:8765",
                                     Authorization=f"Bearer {KEY}"))

    def test_writes_must_say_they_are_json(self):
        # text/plain is what a web page can send to another site without asking permission first.
        base = dict(peer="127.0.0.1", method="POST", path="/mark", Host="127.0.0.1:8765")
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", "application/jsonp", None):
            fields = {"Content_Type": content_type} if content_type else {}
            self.assertEqual(self.check(**base, **fields)[0], 415, content_type)
        for content_type in ("application/json", "application/json; charset=utf-8", "Application/JSON"):
            self.assertIsNone(self.check(**base, Content_Type=content_type), content_type)

    def test_pictures_load_without_the_key_but_only_for_known_hosts(self):
        # AsyncImage and the thumbnail cache can't add a header. Names are 128-bit content hashes
        # that only the authenticated /library hands out.
        name = "a" * 32 + ".jpg"
        self.assertIsNone(self.check(path=f"/media/{name}", Host=MAC_NAME))
        self.assertEqual(self.check(path=f"/media/{name}", Host="evil.example")[0], 403)
        self.assertEqual(self.check(path=f"/media/{name}", Host=MAC_NAME, Origin="https://evil.example")[0], 403)
        self.assertEqual(self.check(path="/media/../library", Host=MAC_NAME)[0], 401)  # only real picture names


class AccessKeyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "beacon" / "serve-key"
        patcher = patch.dict(os.environ, {"BEACON_SERVE_KEY_FILE": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("BEACON_SERVE_KEY", None)

    def test_the_first_start_makes_a_long_random_key_only_you_can_read(self):
        key = server.access_key()
        self.assertGreaterEqual(len(key), 32)
        self.assertEqual(self.path.read_text().strip(), key)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(server.access_key(), key)  # the same key on every restart

    def test_a_key_from_the_environment_wins_and_must_be_long_enough(self):
        with patch.dict(os.environ, {"BEACON_SERVE_KEY": KEY}):
            self.assertEqual(server.access_key(), KEY)
        with patch.dict(os.environ, {"BEACON_SERVE_KEY": "short"}), self.assertRaises(ValueError):
            server.access_key()
        self.assertFalse(self.path.exists())

    def test_show_key_prints_the_key_for_the_phone_and_does_not_start_a_server(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parent.parent
        env = {**os.environ, "BEACON_SERVE_KEY_FILE": str(self.path)}
        shown = subprocess.run([sys.executable, str(root / "beacon.py"), "serve", "--show-key"],
                               capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(shown.stdout.strip(), self.path.read_text().strip())

    def test_a_weak_key_file_is_refused_not_replaced(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("1234\n")
        with self.assertRaises(ValueError):
            server.access_key()
        self.assertEqual(self.path.read_text(), "1234\n")


class GuardedServerTest(unittest.TestCase):
    """The same rules, through a real socket."""

    def start(self, run_cli=None):
        self.calls = []
        run_cli = run_cli or (lambda args: self.calls.append(args) or "")
        httpd = server.make_server("127.0.0.1", 0, run_cli=run_cli, key=KEY)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        return conn

    def post(self, conn, content_type="application/json", **extra):
        conn.request("POST", "/mark", body=json.dumps({"status": "rejected", "ids": [1]}),
                     headers={"Content-Type": content_type, **extra})
        response = conn.getresponse()
        response.read()
        return response.status

    def test_a_cross_site_form_post_cannot_change_anything(self):
        conn = self.start()
        self.assertEqual(self.post(conn, content_type="text/plain"), 415)
        self.assertEqual(self.post(conn, Origin="https://evil.example"), 403)
        self.assertEqual(self.post(conn, Host="evil.example"), 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.post(conn), 200)
        self.assertEqual(self.calls, [["mark", "rejected", "1"]])

    def test_the_phone_is_refused_without_the_key_and_served_with_it(self):
        conn = self.start()
        with patch.object(server, "on_this_mac", return_value=False), patch.object(db, "get_triage_queue", return_value=[]):
            self.assertEqual(self.post(conn, Host=MAC_NAME), 401)
            self.assertEqual(self.calls, [])
            self.assertEqual(self.post(conn, Host=MAC_NAME, Authorization=f"Bearer {KEY}"), 200)
            conn.request("GET", "/queue", headers={"Host": MAC_NAME})
            response = conn.getresponse()
            self.assertEqual((response.status, response.getheader("WWW-Authenticate")), (401, "Bearer"))
            self.assertNotIn(KEY, response.read().decode())
            conn.request("GET", "/queue", headers={"Host": MAC_NAME, "Authorization": f"Bearer {KEY}"})
            response = conn.getresponse()
            self.assertEqual((response.status, json.loads(response.read())), (200, []))

    def test_pick_is_served_to_the_phone_only_with_the_key(self):
        conn = self.start()
        calls = []
        fake = {"round_id": None, "reason": "No round prepared. Run `beacon pick prepare`.", "preparing": False}
        with patch.object(server, "on_this_mac", return_value=False), \
                patch.object(server.pick, "current", return_value=fake), \
                patch.object(server.pick, "summary", return_value={"picks": 0, "axes": {}}), \
                patch.object(server.pick, "record", side_effect=lambda *a, **k: calls.append(a) or {"ok": True, "next_position": 2}), \
                patch.object(server.pick, "undo", side_effect=lambda r: calls.append((r,)) or {"position": 1}), \
                patch.object(server.pick, "start_prepare", side_effect=lambda axis: calls.append((axis,)) or {"started": True, "preparing": True}):
            requests = [("GET", "/pick/round", None, fake), ("GET", "/pick/summary", None, {"picks": 0, "axes": {}}),
                        ("POST", "/pick", {"round_id": 1, "position": 1, "choice": "left"}, {"ok": True, "next_position": 2}),
                        ("POST", "/pick/undo", {"round_id": 1}, {"position": 1}),
                        ("POST", "/pick/prepare", {}, {"started": True, "preparing": True})]
            for method, path, body, answer in requests:
                sent = {"Host": MAC_NAME, **({"Content-Type": "application/json"} if body is not None else {})}
                data = json.dumps(body) if body is not None else None
                conn.request(method, path, body=data, headers=sent)
                response = conn.getresponse()
                self.assertEqual((response.status, path), (401, path))
                response.read()
                conn.request(method, path, body=data, headers={**sent, "Authorization": f"Bearer {KEY}"})
                response = conn.getresponse()
                self.assertEqual((response.status, json.loads(response.read())), (200, answer), path)
        self.assertEqual(calls, [(1, 1, "left"), (1,), (None,)])  # only the requests with the key did anything

    def test_other_methods_are_not_answered(self):
        conn = self.start()
        for method in ("OPTIONS", "PUT", "DELETE"):
            conn.request(method, "/mark")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 501, method)
