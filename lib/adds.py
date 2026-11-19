"""Adding your own things to the library: any web link, an X post, or a picture file.

They land as kept saves (status adopted), so they skip Triage. An X link is stored exactly like
a bookmark; any other page keeps its title, description and preview picture (Open Graph tags);
a picture file is copied into media/ under its content hash and served by `beacon serve`.
"""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import subprocess
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from . import db, fetch

MEDIA_DIR = Path(__file__).resolve().parent.parent / "media"
PICTURE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}
_X_POST = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)", re.IGNORECASE)
_MAX_PAGE = 2_000_000


def _now():
    return datetime.now(timezone.utc).isoformat()


# Addresses that look public to `is_global` but carry an IPv4 address inside (NAT64, 6to4, Teredo).
_TRANSLATED = [ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")]
_MAX_REDIRECTS = 5


def _permitted(ip):
    """Only the public internet: not this Mac, the LAN, the tailnet (100.64/10), link-local (cloud metadata) or reserved."""
    ip = ip.ipv4_mapped or ip if ip.version == 6 else ip
    return ip.is_global and not ip.is_multicast and not any(ip in network for network in _TRANSLATED)


def _connect_public(host, port, timeout):
    """A socket to `host`, made only if every address it resolves to is public, and made to the address checked.

    Resolving once and connecting to that answer closes the gap where a name is public when checked and
    private when connected to. Written-out forms like 2130706433 or 0x7f.1 resolve to the real address first.
    """
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise URLError(error) from None
    if not answers or not all(_permitted(ipaddress.ip_address(a[4][0].partition("%")[0])) for a in answers):
        raise URLError(f"{host} is not a public web address")
    error = None
    for family, kind, proto, _, address in answers:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout if isinstance(timeout, (int, float)) else None)
        try:
            sock.connect(address)
            return sock
        except OSError as failure:
            sock.close()
            error = failure
    raise error


class _PublicHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect_public(self.host, self.port, self.timeout)


class _PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(_connect_public(self.host, self.port, self.timeout), server_hostname=self.host)


class _HTTP(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PublicHTTP, request)


class _HTTPS(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHTTPS, request, context=self._context)


def _check_scheme(url):
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise URLError(f"only web links can be fetched, not {parts.scheme or 'this'}")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise URLError("links with a username or password are not fetched")


class _Redirect(HTTPRedirectHandler):
    """Each hop must still be a web link; where it lands is checked when the connection is made."""
    max_redirections = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_scheme(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No proxy handler: a proxy would be the address checked, not the page behind it.
_opener = build_opener(ProxyHandler({}), _HTTP, _HTTPS, _Redirect)


def _download(url):
    """(first 2 MB of the body, content type). Raises OSError (HTTPError, URLError, timeout) on failure,
    and URLError for any address that isn't on the public internet."""
    _check_scheme(url)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Beacon/1.0"})
    with _opener.open(request, timeout=15) as response:
        return response.read(_MAX_PAGE), response.headers.get_content_type()


def _read_tweet(tweet_id):
    result = subprocess.run(["bird", "read", tweet_id, "--json"], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"bird couldn't read that post: {result.stderr.strip()[:200]}")
    tweet = json.loads(result.stdout)
    return tweet[0] if isinstance(tweet, list) else tweet


class _PageMeta(HTMLParser):
    """The page's <title> and its Open Graph / Twitter card tags."""

    def __init__(self):
        super().__init__()
        self.tags, self.title, self._in_title = {}, "", False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key and attrs.get("content") and key not in self.tags:
                self.tags[key] = attrs["content"].strip()
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _keep(source, external_id):
    """Mark the save kept (it's in your library, not in Triage) and return its id."""
    with db.get_connection() as conn:
        conn.execute("UPDATE items SET status='adopted', status_at=? WHERE source=? AND external_id=?",
                     (_now(), source, external_id))
        return conn.execute("SELECT id FROM items WHERE source=? AND external_id=?", (source, external_id)).fetchone()[0]


def file_on_shelf(item_id, shelf):
    """Put a kept save on an area shelf (sure of it, so probability 1) the way Jev's answers do; no shelf does nothing."""
    if shelf:
        with db.get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO area_reviews VALUES(?,?,1.0,?)", (item_id, shelf, _now()))


def add_link(url):
    """Save a web link; returns the item id."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Only web links (http or https) can be added.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("Links with a username or password can't be added; they would be saved in the library.")
    post = _X_POST.match(url)
    if post:
        tweet = _read_tweet(post.group(1))
        fetch.store_tweet(tweet, _now())
        return _keep("x", str(tweet.get("id") or post.group(1)))
    host = parsed.netloc.removeprefix("www.")
    size = {}
    try:
        body, content_type = _download(url)
    except OSError:  # refused (403), not found, offline or slow: keep the link anyway, as a card
        body, content_type = None, None
    if body is None:
        title, text, picture = f"{host}{parsed.path.rstrip('/')}", "", None
    elif content_type.startswith("image/"):
        title, text, picture = Path(parsed.path).name or host, "", url
    else:
        page = _PageMeta()
        page.feed(body.decode("utf-8", errors="replace"))
        tags = page.tags
        title = tags.get("og:title") or tags.get("twitter:title") or page.title.strip() or url
        text = tags.get("og:description") or tags.get("twitter:description") or tags.get("description") or ""
        picture = tags.get("og:image") or tags.get("twitter:image")
        picture = urljoin(url, picture) if picture else None
        # Most pages give the preview's size, so the Library shows it uncropped.
        size = {key: int(tags[f"og:image:{key}"]) for key in ("width", "height")
                if tags.get(f"og:image:{key}", "").isdigit()}
    raw = {"text": text, "author": {"name": host}, "media": [{"type": "photo", "url": picture, **size}] if picture else []}
    db.upsert_item("add", url, title[:300], text, url, host, json.dumps(raw), None, _now())
    return _keep("add", url)


def add_image(path, width=None, height=None, note=""):
    """Copy a picture into media/ (once per content) and save it; returns the item id."""
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in PICTURE_TYPES:
        raise ValueError(f"Pictures only ({', '.join(sorted(PICTURE_TYPES))}).")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()[:32]
    name = f"{digest}{'.jpg' if suffix == '.jpeg' else suffix}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    stored = MEDIA_DIR / name
    if not stored.exists():
        stored.write_bytes(data)
    note = (note or "").strip()
    raw = {"text": note, "author": {"name": "You"},
           "media": [{"type": "photo", "url": f"/media/{name}", "width": width, "height": height}]}
    db.upsert_item("add", f"picture:{digest}", note[:100] or "Picture", note, None, "You", json.dumps(raw), None, _now())
    return _keep("add", f"picture:{digest}")
