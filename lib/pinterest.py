"""Pinterest: public board RSS (pinterest.com/<user>/<board>.rss) as a Beacon source.

Board RSS needs no login or API approval, but it only covers public boards and only the most
recent pins a board's feed carries. Secret boards would need the Pinterest API.
"""
import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from . import db

MAX_FEED_BYTES = 2_000_000
USER_AGENT = "Mozilla/5.0 (compatible; Beacon/1.0)"
PIN_ID = re.compile(r"/pin/(\d+)")
# Pinterest serves each image in several widths under the same path: /236x/, /474x/, /736x/, /originals/.
IMAGE_WIDTH = re.compile(r"/(?:\d+x|originals)/")
BIG_IMAGE_WIDTH = "/736x/"


def board_feed_url(board):
    """'user/board', a board page URL or a .rss URL, as the feed URL. Only pinterest.com over https."""
    board = (board or "").strip()
    if not board:
        raise ValueError("Pinterest board is empty.")
    if "://" not in board:
        board = "https://www.pinterest.com/" + board.lstrip("/")
    parsed = urlparse(board)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "pinterest.com" or host.endswith(".pinterest.com")):
        raise ValueError(f"Not a Pinterest board URL: {board}")
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[-1].endswith(".rss"):
        parts[-1] = parts[-1][:-4]
    if len(parts) != 2:
        raise ValueError(f"Expected a board as user/board: {board}")
    return f"https://www.pinterest.com/{parts[0]}/{parts[1]}.rss"


def _board_owner(feed_url):
    return feed_url.split("/")[3]


class _Description(HTMLParser):
    """A pin's RSS description is an <a><img></a> followed by its text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == "img" and self.image is None:
            self.image = dict(attrs).get("src")

    def handle_data(self, data):
        self.text.append(data)


def _description(markup):
    parser = _Description()
    parser.feed(markup or "")
    return " ".join("".join(parser.text).split()), parser.image


def _image_url(src):
    if not src or urlparse(src).scheme != "https":
        return None
    return IMAGE_WIDTH.sub(BIG_IMAGE_WIDTH, src, count=1)


def _iso(pub_date):
    if not pub_date:
        return None
    try:
        parsed = parsedate_to_datetime(pub_date.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def parse_feed(xml_bytes, feed_url):
    """Pins from one board feed, as dicts ready for storage. Items with no pin id are skipped."""
    head = xml_bytes[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        # Stdlib XML expands entities; a feed has no reason to declare any.
        raise ValueError("Feed declares a DOCTYPE or entities; refusing to parse it.")
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as error:
        raise ValueError(f"Feed is not valid XML: {error}") from None
    channel = root.find("channel")
    if channel is None:
        raise ValueError("Feed has no channel; this doesn't look like a Pinterest board RSS.")
    board_title = (channel.findtext("title") or "").strip()
    owner = _board_owner(feed_url)
    pins = []
    for entry in channel.findall("item"):
        link = (entry.findtext("link") or "").strip()
        guid = (entry.findtext("guid") or "").strip()
        match = PIN_ID.search(link) or PIN_ID.search(guid)
        if not match:
            continue
        text, image = _description(entry.findtext("description"))
        title = (entry.findtext("title") or "").strip() or text[:100] or f"Pin {match.group(1)}"
        pins.append({
            "pin_id": match.group(1),
            "title": html.unescape(title),
            "text": text,
            "url": f"https://www.pinterest.com/pin/{match.group(1)}/",
            "image": _image_url(image),
            "thumbnail": image,
            "board": board_title,
            "board_feed": feed_url,
            "owner": owner,
            "published": _iso(entry.findtext("pubDate")),
        })
    return pins


def _download(feed_url):
    request = Request(feed_url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"})
    with urlopen(request, timeout=20) as response:
        data = response.read(MAX_FEED_BYTES + 1)
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Feed is larger than expected; refusing to parse it.")
    return data


def store_pin(pin, synced_at):
    """Save one pin; True if it's new to Beacon."""
    body = pin["text"]
    if pin["board"]:
        body = f"{body}\n\nPinterest board: {pin['board']}".strip()
    return db.upsert_item(
        source="pinterest",
        external_id=pin["pin_id"],
        title=pin["title"],
        body=body,
        url=pin["url"],
        author=pin["owner"],
        # "media" has the shape X posts use, so the Library and Triage render a pin like a picture post.
        raw_json=json.dumps({**{key: pin[key] for key in
                                ("pin_id", "image", "thumbnail", "board", "board_feed", "owner", "published")},
                             "media": [{"type": "photo", "url": pin["image"]}] if pin["image"] else []}),
        created_at=pin["published"] or "",
        synced_at=synced_at,
        saved_at=pin["published"],
    )


def fetch_pinterest_boards(boards, download=_download):
    """Sync each configured public board. One bad board never stops the others."""
    if not boards:
        print("  No Pinterest boards configured (sources.pinterest.boards in config.json). Skipping.")
        return 0
    print(f"Fetching {len(boards)} Pinterest board(s)...")
    synced_at = datetime.now(timezone.utc).isoformat()
    count_new = 0
    synced_boards = 0
    for board in boards:
        try:
            feed_url = board_feed_url(board)
            pins = parse_feed(download(feed_url), feed_url)
        except (ValueError, HTTPError, URLError, OSError) as error:
            print(f"  Pinterest board {board} didn't sync: {error}")
            continue
        added = sum(1 for pin in pins if store_pin(pin, synced_at))
        synced_boards += 1
        print(f"  {board}: {len(pins)} pins, {added} new.")
        count_new += added
    if not synced_boards:
        # Raise so the weekly job and the app's Sync report Pinterest as failed, not "0 new".
        raise RuntimeError(f"Pinterest didn't sync: no Pinterest board synced ({len(boards)} configured; see the lines above).")
    db.update_sync_state("pinterest", "", synced_at, count_new)
    print(f"  Synced {count_new} Pinterest pins.")
    return count_new
