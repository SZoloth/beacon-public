"""Read: send a saved item to Readwise Reader instead of a project agent."""
import json
import os
import re
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import db

SAVE_URL = "https://readwise.io/api/v3/save/"
SECRETS = Path.home() / ".config" / "shell" / "secrets.zsh"


def _token():
    """The shell exports it; the Triage app and launchd don't run the shell, so read the same file."""
    token = os.environ.get("READWISE_ACCESS_TOKEN")
    if token:
        return token
    if SECRETS.exists():
        match = re.search(r"^export READWISE_ACCESS_TOKEN=['\"]?([^'\"\s]+)", SECRETS.read_text(), re.MULTILINE)
        if match:
            return match.group(1)
    return None


def _save(url, tags):
    token = _token()
    if not token:
        raise RuntimeError("READWISE_ACCESS_TOKEN is not set; add it to ~/.config/shell/secrets.zsh.")
    payload = {"url": url, "tags": tags, "saved_using": "Beacon"}
    request = Request(SAVE_URL, data=json.dumps(payload).encode(), method="POST",
                      headers={"Authorization": f"Token {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except (HTTPError, URLError, OSError, ValueError) as error:
        raise RuntimeError(f"Readwise Reader save failed: {error}") from None


def read_item(item_id):
    with closing(db.get_connection()) as conn:
        item = conn.execute("SELECT id, title, url FROM items WHERE id=?", (item_id,)).fetchone()
        project = conn.execute(
            "SELECT w.slug FROM item_workstreams iw JOIN workstreams w ON w.id=iw.workstream_id "
            "WHERE iw.item_id=? ORDER BY iw.confidence DESC LIMIT 1", (item_id,)).fetchone()
    if not item or not item["url"]:
        raise RuntimeError(f"Item {item_id} has no URL to save.")
    document = {} if db.skipping_outside_effects() else _save(item["url"], ["beacon"] + ([project["slug"]] if project else []))
    db.update_item_status(item_id, "adopted", note="read")
    return {"item_id": item_id, "reader_url": document.get("url")}
