"""Read: send a saved item to Readwise Reader instead of a project agent."""
import json
import os
import re
import subprocess
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import db, settings

SAVE_URL = "https://readwise.io/api/v3/save/"
SECRETS = Path.home() / ".config" / "shell" / "secrets.zsh"
WITH_SECRET = Path.home() / ".local" / "bin" / "with-secret"


def _keychain_token():
    """The token from Keychain through with-secret; the value stays in this process and is never printed."""
    if not os.access(WITH_SECRET, os.X_OK):
        return None
    done = subprocess.run([str(WITH_SECRET), "READWISE_ACCESS_TOKEN", "--", "/usr/bin/printenv", "READWISE_ACCESS_TOKEN"],
                          capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def _token():
    """The environment first; the app and launchd jobs don't run the shell, so then Keychain, then the old file."""
    token = os.environ.get("READWISE_ACCESS_TOKEN") or _keychain_token()
    if token:
        return token
    if SECRETS.exists():
        match = re.search(r"^export READWISE_ACCESS_TOKEN=['\"]?([^'\"\s]+)", SECRETS.read_text(), re.MULTILINE)
        if match:
            return match.group(1)
    return None


def _cli():
    """The signed-in readwise CLI from config `paths.readwise_cli`, used when the stored token is missing or refused."""
    path = settings.path("readwise_cli")
    return path if path and os.access(path, os.X_OK) else None


def _save_with_cli(cli, url, tags):
    done = subprocess.run([cli, "--json", "reader-create-document", "--url", url, "--tags", json.dumps(tags)],
                          capture_output=True, text=True, timeout=60, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"Readwise Reader save failed through the readwise CLI: {done.stderr.strip()[:200]}")
    try:
        return json.loads(done.stdout or "{}")
    except ValueError:
        raise RuntimeError("Readwise Reader save through the readwise CLI returned no JSON.") from None


def _save(url, tags):
    token, cli = _token(), _cli()
    if not token:
        if cli:
            return _save_with_cli(cli, url, tags)
        raise RuntimeError("READWISE_ACCESS_TOKEN is not set; store it with `with-secret --add READWISE_ACCESS_TOKEN`.")
    payload = {"url": url, "tags": tags, "saved_using": "Beacon"}
    request = Request(SAVE_URL, data=json.dumps(payload).encode(), method="POST",
                      headers={"Authorization": f"Token {token}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read() or b"{}")
    except HTTPError as error:
        if error.code in (401, 403) and cli:
            return _save_with_cli(cli, url, tags)
        raise RuntimeError(f"Readwise Reader save failed: {error}") from None
    except (URLError, OSError, ValueError) as error:
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
