"""beacon serve: the Triage app's actions over HTTP, so a phone can triage while Beacon stays on this Mac.

Every request runs the same `beacon.py` command the Mac app runs, so Python stays the only
writer and backlogs regenerate exactly as they do from the terminal. Only a fixed set of
actions is accepted, with validated arguments, and only from this Mac or the Tailscale
network (100.64.0.0/10 and its IPv6 range); there is no public exposure.

An address range says a request came over the tailnet, not that it came from the phone, so
`authorize` also checks, in this order: the Host name is one of this Mac's (a web page that
re-points its own name at this Mac gets refused), no other site's Origin, then the access key
for anyone but this Mac itself, then that writes are declared JSON (the one body type a web
page can't send to another site without asking permission first).
"""
import base64
import binascii
import gzip
import hmac
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from datetime import datetime

from . import activate, adds, autopilot, briefs, compose, db, pick, vectors

ROOT = Path(__file__).resolve().parent.parent
NETWORKS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48")]
STATUSES = {"queued", "reviewed", "tried", "adopted", "rejected"}
_MEDIA_NAME = re.compile(r"^/media/([0-9a-f]{32}\.(jpg|png|gif|webp|heic))$")
_MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp", "heic": "image/heic"}
# A phone photo as base64 in JSON; anything bigger is refused before it's read.
MAX_BODY = 30_000_000
BRIEF_STATUSES = {"dismissed", "dispatched"}
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_writes = threading.Lock()  # one write at a time, like the Mac app's ordered write queue
MIN_KEY = 32
# Set by a relay that connects from this Mac on someone else's behalf (`tailscale serve`, a tunnel).
_FORWARDED = ("X-Forwarded-For", "X-Forwarded-Host", "Forwarded", "X-Real-IP", "Via")


def key_path():
    return Path(os.environ.get("BEACON_SERVE_KEY_FILE") or Path.home() / ".config" / "beacon" / "serve-key")


def access_key():
    """The phone's access key: $BEACON_SERVE_KEY, else the key file, made on first use (owner-only)."""
    key = os.environ.get("BEACON_SERVE_KEY")
    path = key_path()
    if key is None and path.exists():
        key = path.read_text().strip()
    if key is None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = secrets.token_urlsafe(32)
        # Mode set at creation, so the key is never readable by others, even briefly.
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            file.write(key + "\n")
        return key
    key = key.strip()
    if len(key) < MIN_KEY or not key.isascii() or not key.isprintable() or " " in key:
        raise ValueError(f"The access key must be {MIN_KEY}+ plain characters; delete {path} to make a new one.")
    return key


def allowed(address):
    ip = ipaddress.ip_address(address)
    ip = ip.ipv4_mapped or ip if ip.version == 6 else ip
    return any(ip in network for network in NETWORKS)


def on_this_mac(address):
    """A request from this Mac itself, where gzip costs ~100 ms and saves nothing."""
    ip = ipaddress.ip_address(address)
    return (ip.ipv4_mapped or ip).is_loopback if ip.version == 6 else ip.is_loopback


def _host_name(value):
    """The name or address in a Host header, lower-cased and without the port; None if it isn't one."""
    if not value or "@" in value or "/" in value or "\\" in value:
        return None
    try:
        return urlsplit("//" + value).hostname
    except ValueError:
        return None


def _address(name):
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def _local_name(name):
    ip = _address(name)
    return name == "localhost" or bool(ip and ip.is_loopback)


def _known_host(name):
    """This Mac under a name we gave out: loopback, a tailnet address, *.ts.net, or $BEACON_SERVE_HOSTS."""
    if not name:
        return False
    ip = _address(name)
    if ip:
        return any((ip.ipv4_mapped or ip if ip.version == 6 else ip) in network for network in NETWORKS)
    extra = {h.strip().lower() for h in os.environ.get("BEACON_SERVE_HOSTS", "").split(",") if h.strip()}
    return name == "localhost" or name.endswith(".ts.net") or name in extra


def authorize(peer, method, path, headers, key):
    """None if the request may proceed, else (HTTP status, message)."""
    host = headers.get("Host")
    name = _host_name(host)
    if not _known_host(name):
        return 403, "unknown host name"
    origin = headers.get("Origin")
    if origin is not None:
        parts = urlsplit(origin)
        if parts.scheme != "http" or parts.netloc.lower() != host.lower():
            return 403, "web pages from other sites can't use Beacon"
    # Pictures are loaded by image views that can't add a header. Their names are 128-bit content
    # hashes that only the authenticated /library gives out, so the name is the capability.
    if method == "GET" and _MEDIA_NAME.match(path.partition("?")[0]):
        return None
    this_mac = on_this_mac(peer) and _local_name(name) and origin is None \
        and not any(h in headers for h in _FORWARDED)
    if not this_mac:
        scheme, _, supplied = (headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), key.encode()):
            return 401, "Beacon needs its access key (Settings › Beacon on the phone)"
    if method == "POST" and headers.get_content_type() != "application/json":
        return 415, "send JSON (Content-Type: application/json)"
    return None


# Each command's cap. Reading one save for search allows 30 minutes (lib/vectors.py), plus a minute
# so its own timeout is the one reported; it runs after the answer, so nobody waits on it.
_WAITS = {"vectors": 30 * 60 + 60}


def run_cli(args):
    result = subprocess.run([sys.executable, str(ROOT / "beacon.py"), *args], cwd=ROOT,
                            capture_output=True, text=True, timeout=_WAITS.get(args[0], 900))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-500:] or f"beacon exited {result.returncode}")
    return result.stdout


def _ids(value):
    if not isinstance(value, list) or not value or not all(isinstance(i, int) for i in value):
        raise ValueError("ids must be a non-empty list of integers")
    return [str(i) for i in value]


def _int(value, name):
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    return str(value)


def _one_of(value, allowed):
    """A list or object is a bad request (400), not a crash on the set lookup (500)."""
    return isinstance(value, str) and value in allowed


def _iso(value):
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _slug(value, name):
    if not isinstance(value, str) or not SLUG.match(value):
        raise ValueError(f"{name} must be a project slug")
    return value


def _picture_suffix(data):
    """The file type, from the picture's first bytes; None if it isn't one we keep."""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return ".heic"
    return None


def _keep(args, run):
    """Runs `beacon keep` without indexing, answers with the new save, and indexes it afterwards."""
    kept = json.loads(run(["keep", *args, "--no-index"]))
    vectors.index_later(kept["id"], lambda item_id: run(["vectors", "--ids", str(item_id)]))
    return kept


def _add(body, run):
    """POST /add: {"url": ...} or {"image": base64, "width", "height", "note"}."""
    if isinstance(body.get("url"), str) and body["url"].strip():
        return _keep(["--url", body["url"].strip()[:2000]], run)
    if not isinstance(body.get("image"), str):
        raise ValueError("send a url or an image")
    try:
        data = base64.b64decode(body["image"], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("image must be base64") from None
    suffix = _picture_suffix(data)
    if not suffix:
        raise ValueError("that isn't a JPEG, PNG, GIF, WebP or HEIC picture")
    args = []
    for name in ("width", "height"):
        if body.get(name) is not None:
            args += [f"--{name}", str(_int(body[name], name))]
    note = body.get("note")
    if isinstance(note, str) and note.strip():
        args += ["--note", note.strip()[:1000]]
    with tempfile.NamedTemporaryFile(suffix=suffix) as upload:
        upload.write(data)
        upload.flush()
        return _keep(["--image", upload.name, *args], run)


def media(path):
    """GET /media/<hash>.<type>: (bytes, content type) for a picture you added, else None."""
    match = _MEDIA_NAME.match(path)
    file = adds.MEDIA_DIR / match.group(1) if match else None
    if not file or not file.is_file():
        return None
    return file.read_bytes(), _MEDIA_TYPES[match.group(2)]


def route(method, path, body, run=run_cli):
    """Returns (HTTP status, JSON payload). Raises ValueError for a bad request."""
    path, _, query = path.partition("?")
    # Reads are answered here: starting `beacon queue` per request took ~2 s, the query ~90 ms.
    if method == "GET" and path == "/status":
        # Which checkout this server reads and writes, so an app can tell it is talking to its own.
        return 200, {"database": str(Path(db.current_path()).resolve()), "checkout": str(ROOT)}
    if method == "GET" and path == "/queue":
        return 200, db.get_triage_queue()
    if method == "GET" and path == "/briefs":
        return 200, briefs.list_plans()
    if method == "GET" and path == "/handled":
        return 200, autopilot.handled()
    if method == "GET" and path == "/activity":
        return 200, activate.activity()
    if method == "GET" and path == "/pick/round":
        return 200, pick.current()
    if method == "GET" and path == "/pick/summary":
        return 200, pick.summary()
    if method == "GET" and path == "/library/compose/summary":
        return 200, compose.summary()
    if method == "GET" and path == "/library":
        # Answered here, not by starting `beacon library`: a new Python process cost about a second.
        params = parse_qs(query)
        page = {}
        for name, default in (("offset", "0"), ("limit", "200")):
            value = params.get(name, [default])[0]
            if not value.isdigit() or (name == "limit" and not 0 < int(value) <= 1000):
                raise ValueError(f"{name} must be a whole number" + (" from 1 to 1000" if name == "limit" else ""))
            page[name] = int(value)
        if "area" in params:
            if not re.fullmatch(r"[a-z][a-z0-9-]*", params["area"][0]):
                raise ValueError("area must be an area slug")
            page["area"] = params["area"][0]
        # `search` is the one box (words and meaning fused); `q` and `meaning` stay for older app builds.
        for name, key in (("search", "query"), ("q", "words"), ("meaning", "about")):
            if name in params:
                text = params[name][0][:200]
                if text.startswith("-"):
                    raise ValueError(f"{name} must not start with a dash")  # it's passed to beacon-embed
                page[key] = text
        if "refine" in params:  # follow-up steps on a search, in order
            if "query" not in page or len(params["refine"]) > 5:
                raise ValueError("refine needs search, and at most 5 steps")
            if any(step.startswith("-") for step in params["refine"]):
                raise ValueError("refine must not start with a dash")
            page["refine"] = [step[:200] for step in params["refine"]]
        if "similar" in params:
            if not params["similar"][0].isdigit():
                raise ValueError("similar must be an item id")
            page["like"] = int(params["similar"][0])
        return 200, vectors.library_page(**page)
    if method != "POST":
        return 404, {"error": "not found"}
    if path == "/add":
        return 200, _add(body, run)
    if path == "/activate":
        return 200, activate.start(int(_int(body.get("item_id"), "item_id")))
    activity_path = re.match(r"^/activity/(\d+)$", path)
    if activity_path:
        activation_id = int(activity_path.group(1))
        if set(body) == {"state"} and body["state"] in activate.CLOSED:
            return 200, activate.close(activation_id, body["state"])
        if set(body) == {"answer"} and isinstance(body["answer"], str):
            return 200, activate.answer(activation_id, body["answer"])
        raise ValueError('send {"state": "done" or "didnt_work"} or {"answer": "..."}')
    if path == "/library/compose":
        # A layout for the saves the app is showing (best first). Seconds, not milliseconds: the app shows
        # its grid first and offers this when it arrives.
        return 200, compose.compose(str(body.get("search") or ""), body.get("ids"))
    if path == "/library/compose/choice":
        if type(body.get("id")) is not int:
            raise ValueError("id must be a layout id")
        return 200, compose.choose(body["id"], body.get("choice"))
    if path == "/pick/prepare":
        # Building a round takes minutes, so this starts it and returns; GET /pick/round says `preparing`.
        axis = body.get("axis")
        if axis is not None and not isinstance(axis, str):
            raise ValueError("axis must be text")
        return 200, pick.start_prepare(axis)
    if path in ("/pick", "/pick/undo"):
        # Answered here: a pick is one row, and the phone sends one per tap.
        round_id = body.get("round_id")
        if not isinstance(round_id, int) or isinstance(round_id, bool):
            raise ValueError("round_id must be an integer")
        with _writes:
            if path == "/pick/undo":
                return 200, pick.undo(round_id)
            position, why, shown_at = body.get("position"), body.get("why"), body.get("shown_at")
            if not isinstance(position, int) or isinstance(position, bool):
                raise ValueError("position must be an integer")
            if why is not None and not isinstance(why, str):
                raise ValueError("why must be text")
            if shown_at is not None and (not isinstance(shown_at, str) or not _iso(shown_at)):
                raise ValueError("shown_at must be an ISO 8601 time")
            return 200, pick.record(round_id, position, body.get("choice"), why=why, shown_at=shown_at)
    if path == "/references":
        brief = body.get("brief")
        if not isinstance(brief, str) or not brief.strip() or brief.strip().startswith("-"):
            raise ValueError("brief must be text that doesn't start with a dash")
        args = ["references", "--brief", brief.strip(), "--ids", ",".join(_ids(body.get("ids")))]
        return 200, json.loads(run(args + (["--allow-upload"] if body.get("words") is True else [])))
    if path == "/mark":
        if not _one_of(body.get("status"), STATUSES):
            raise ValueError("unknown status")
        args = ["mark", body["status"], *_ids(body.get("ids"))]
    elif path == "/read":
        args = ["read", _int(body.get("id"), "id")]
    elif path == "/unhandle":
        args = ["unhandle", _int(body.get("id"), "id")]
    elif path == "/brief-status":
        if not _one_of(body.get("status"), BRIEF_STATUSES):
            raise ValueError("unknown brief status")
        args = ["brief-status", _int(body.get("item_id"), "item_id"), _slug(body.get("workstream"), "workstream"), body["status"]]
    elif path == "/brief-done":
        if not _one_of(body.get("item_status"), {"adopted", "tried"}):
            raise ValueError("item_status must be adopted or tried")
        args = ["brief-done", _int(body.get("item_id"), "item_id"), _slug(body.get("workstream"), "workstream"),
                "dispatched", body["item_status"]]
    elif path == "/brief-mac":
        if not isinstance(body.get("on"), bool):
            raise ValueError("on must be true or false")
        args = ["brief-mac", _int(body.get("item_id"), "item_id"), _slug(body.get("workstream"), "workstream"),
                "on" if body["on"] else "off"]
    elif path == "/plan":
        # One read-only agent run, 1-2 minutes. It holds the request, so the phone shows "Planning…"
        # until the plan exists; if the phone drops the connection, the run still finishes here.
        run(["brief", "--project", _slug(body.get("project"), "project"), "--allow-upload"])
        return 200, {"ok": True}
    else:
        return 404, {"error": "not found"}
    with _writes:
        run(args)
    return 200, {"ok": True}


def make_server(host, port, run_cli=run_cli, key=None):
    key = key or access_key()

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            # The queue is over 1 MB of JSON; gzipped it crosses Tailscale several times faster.
            if len(data) > 16_000 and not on_this_mac(self.client_address[0]) \
                    and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                data = gzip.compress(data, compresslevel=5)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            if not allowed(self.client_address[0]):
                return self._respond(403, {"error": "only reachable over Tailscale"})
            refused = authorize(self.client_address[0], self.command, self.path, self.headers, key)
            if refused:
                return self._respond(*refused[:1], {"error": refused[1]})
            if self.command == "GET" and self.path.startswith("/media/"):
                found = media(self.path.partition("?")[0])
                if not found:
                    return self._respond(404, {"error": "not found"})
                self.send_response(200)
                self.send_header("Content-Type", found[1])
                self.send_header("Content-Length", str(len(found[0])))
                self.send_header("Cache-Control", "max-age=31536000, immutable")
                self.end_headers()
                return self.wfile.write(found[0])
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._respond(413, {"error": "too big; pictures up to about 20 MB"})
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                self._respond(*route(self.command, self.path, body, run_cli))
            except (BrokenPipeError, ConnectionResetError):
                return  # the app moved on (a newer search replaced this one); nobody is listening
            except ValueError as error:
                self._respond(400, {"error": str(error)})
            except Exception as error:  # a CLI timeout or crash still gets an answer, not a dropped connection
                self._respond(500, {"error": str(error) or type(error).__name__})

        do_GET = do_POST = _handle

    return ThreadingHTTPServer((host, port), Handler)


def serve(host="0.0.0.0", port=8765):
    key = access_key()  # fail before listening if the key is unusable
    vectors.resident = vectors._Resident()  # meaning and similar answer from one helper kept running
    # Load the model now, not on the first search (seconds when the Mac is busy).
    threading.Thread(target=vectors.warm, daemon=True).start()
    httpd = make_server(host, port, key=key)
    print(f"beacon serve: listening on {host}:{port} (Tailscale and this Mac only; phones need the access key"
          f" from `beacon serve --show-key`, stored at {key_path()})", flush=True)
    httpd.serve_forever()
