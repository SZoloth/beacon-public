"""`beacon mcp`: the library for agents (Claude Code, Codex), over the Model Context Protocol.

JSON-RPC messages, one per line on stdin and stdout. Every tool call is recorded in beacon.db's
agent_calls (read back by `beacon stats`). Eight tools, all through the same code the app
uses: search by words or meaning, find similar pictures, read one save, list the shelves, review saves
against a brief, pull the saves kept for a project, and keep a link or a picture. Pictures you added
come back as file paths on this Mac, so an agent can open them.
"""
import base64
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import adds, db, references, settings, useful, vectors

PROTOCOL = "2025-06-18"
CONFIG = Path(settings.config_path())
OWNER = settings.owner()
SESSION = {"id": uuid.uuid4().hex[:12], "client": None}  # one `beacon mcp` process is one agent session
INSTRUCTIONS = (
    f"Beacon is {OWNER}'s library of saved posts, pictures and links. "
    "Use it when prior saves should shape a design, a reference or a tool choice. "
    "For a project, call project_references first with your working directory. "
    "Otherwise search_library: mode 'words' for what a post or Beacon's notes would name, mode 'meaning' to "
    "describe what a picture shows. Results are text and picture links; open a save with get_save to see its "
    "pictures before you rely on it, and name the saves you used. Beacon never searches the web, and agents can "
    "keep links and pictures but never change or delete a save.")
EMPTY_HINT = {"words": "No save has these words. Try mode 'meaning' to search what the pictures show, fewer words, "
                       "or another word for the same thing.",
              "meaning": "No picture is close to this. Try mode 'words', or describe what the picture shows."}

_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "description": "How many saves to return (default 20)."}
_OFFSET = {"type": "integer", "minimum": 0, "description": "Skip this many saves; pass the previous reply's next_offset "
                                                           "to read the next page (null means that was the last)."}
_SHELF = {"type": "string", "description": "File the save on this area slug (see list_shelves). Without it the save "
                                           "is kept with no shelf."}
TOOLS = [
    {"name": "search_library",
     "description": f"Search {OWNER}'s saved posts, pictures and links, best match first. mode 'words' matches the post "
                    "and what Beacon wrote about it (takeaway, tags), in any word form; 'both' also ranks by meaning. "
                    "Mode 'meaning' finds pictures "
                    "that show what you describe (on-device SigLIP), even when the post never says it. Inside one "
                    "of your projects, call project_references first. Returns picture links: open "
                    "a save with get_save to look at it. Never searches the web.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"}, "mode": {"type": "string", "enum": ["words", "meaning", "both"]},
         "shelf": {"type": "string", "description": "Only this area slug (see list_shelves)."}, "limit": _LIMIT,
         "offset": _OFFSET}}},
    {"name": "find_similar", "description": "Saves whose pictures look like this save's picture. Use it once one "
                                            "save is right and you want more like it. A save with no picture "
                                            "finds nothing.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}, "limit": _LIMIT, "offset": _OFFSET}}},
    {"name": "get_save", "description": "One save in full: text, link, author, shelf and picture links. Request images "
                                        "to see up to four pictures, including the still image for a video.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "integer"}, "project": {"type": "string", "description": "Use the project slug returned by "
         "project_references to reopen a reference, including text-only saves."},
         "pictures": {"type": "boolean", "description": "Attach images, fetching linked pictures if needed. Default false."}}}},
    {"name": "list_shelves", "description": "The library's areas (Style, Home, Food...) with how many saves each holds.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "review_references",
     "description": f"Check saves against what {OWNER} is making: which fit (by picture, on this Mac, and optionally by "
                    "their words) and which repeat a picture. Best fits first, repeats last.",
     "inputSchema": {"type": "object", "required": ["brief", "ids"], "properties": {
         "brief": {"type": "string", "description": f"What {OWNER} is making, in a sentence."},
         "ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 40,
                 "description": "Save ids to review, e.g. from search_library."},
         "check_words": {"type": "boolean", "description": "Also have Jev read their text. Sends the brief and "
                                                           "post text to TypeSafe. Default false."}}}},
    {"name": "project_references",
     "description": f"Call this first when planning work in one of {OWNER}'s projects. Pass your working directory as "
                    f"folder. Returns the saves {OWNER} filed as references or kept for that project. With a task, "
                    "returns only word matches and explains each match. Cite the saves you use.",
     "inputSchema": {"type": "object", "properties": {
         "folder": {"type": "string", "description": "Your working directory. The project whose root holds it is used "
                                                     "(the deepest root wins)."},
         "task": {"type": "string", "description": "What you are about to do, in a sentence."},
         "project": {"type": "string", "description": "A project or area slug, e.g. my-app or travel. Use instead of "
                                                       "folder, or for an area, which has no folder."},
         "limit": {**_LIMIT, "description": "How many saves to return (default 3 with a task, otherwise 10)."}, "offset": _OFFSET}}},
    {"name": "keep_link", "description": f"Keep a web page or X post in {OWNER}'s library (it skips Triage).",
     "inputSchema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}, "shelf": _SHELF}}},
    {"name": "keep_picture", "description": f"Keep a picture file from this Mac in {OWNER}'s library.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "Absolute path to a jpg, png, gif, webp or heic file."},
         "note": {"type": "string", "description": "A few words about it."}, "shelf": _SHELF}}},
]


MAX_PICTURES = 4  # an X post carries at most four
MAX_PICTURE = 1_000_000  # bytes; a phone photo or an X "small" rendition fits, a raw 12 MP export doesn't
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


class ToolError(Exception):
    """A problem the agent should read (bad id, unknown mode), not a protocol failure."""


def _picture(url):
    return str(adds.MEDIA_DIR / url.removeprefix("/media/")) if url.startswith("/media/") else url


def _save(entry, full=False):
    text = entry["text"] if full else entry["text"][:400]
    # The cleaned post text reads better than X's raw title, which is often just a t.co link.
    title = (entry["text"].split("\n")[0][:120] or db._TCO.sub("", entry["title"] or "").strip()
             or f"Picture from {entry['author_name'] or entry['author'] or 'X'}")
    return {"id": entry["id"], "source": entry["source"], "title": title, "text": text, "url": entry["url"],
            "author": entry["author_name"] or entry["author"], "shelf": entry["area"], "date": entry["date"],
            "pictures": [_picture(m["url"]) for m in entry["media"]]}


def _small(url):
    """X's small rendition of a picture (680 px on the long side) instead of whatever size the save linked."""
    parts = urlparse(url)
    if parts.hostname != "pbs.twimg.com":
        return url
    query = dict(parse_qsl(parts.query))
    query["name"] = "small"
    return urlunparse(parts._replace(query=urlencode(query)))


def _image(picture):
    """One picture as MCP image content, or None if it can't be read, isn't a picture or is too big.

    Pictures you added are files on this Mac; the rest are fetched the way links are (public internet only).
    """
    try:
        if picture.startswith(("http://", "https://")):
            data, kind = adds._download(_small(picture))
        else:
            path = Path(picture).resolve()
            if not path.is_relative_to(adds.MEDIA_DIR.resolve()) or not path.is_file():
                return None
            kind = _IMAGE_TYPES.get(path.suffix.lower())
            with path.open("rb") as image:
                data = image.read(MAX_PICTURE + 1)
    except OSError:
        return None
    if kind not in _IMAGE_TYPES.values() or len(data) > MAX_PICTURE:
        return None
    return {"type": "image", "mimeType": kind, "data": base64.b64encode(data).decode()}


def _pictures(save):
    """Content blocks that let the agent see a save's pictures, plus one line for any it can't."""
    shown = [image for image in map(_image, save["pictures"][:MAX_PICTURES]) if image]
    missing = len(save["pictures"][:MAX_PICTURES]) - len(shown)
    note = (f"Couldn't show {missing} of the pictures (unreachable, not a picture, or over 1 MB); "
            "their links are in pictures.")
    return shown + ([{"type": "text", "text": note}] if missing else [])


def _limit(arguments, default=20):
    limit = arguments.get("limit", default)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolError("limit must be a whole number from 1 to 100")
    return limit


def _offset(arguments):
    offset = arguments.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise ToolError("offset must be a whole number, 0 or more")
    return offset


def _shelf(arguments):
    """The area slug to file a kept save on (None if the agent gave none); an unknown slug is an error."""
    shelf = arguments.get("shelf")
    if shelf is None:
        return None
    slugs = [area["slug"] for area in json.loads(CONFIG.read_text()).get("areas", [])]
    if shelf not in slugs:
        raise ToolError(f"no shelf '{shelf}'; shelves are {', '.join(slugs)}")
    return shelf


def _ranked(found, arguments, shelf=None):
    page = db.get_library(limit=_limit(arguments), offset=_offset(arguments), area=shelf,
                          ranked=[item_id for item_id, _ in found])
    return {"saves": [_save(entry) for entry in page["items"]], "total": page["total"], "next_offset": page["next_offset"]}


def _index(item_id):
    """Read the new save for search after the tool call is answered, in the database it was kept in."""
    path = db.current_path()

    def read(item_id):
        with db.using(path):
            vectors.index(limit=1, ids=[item_id])
    vectors.index_later(item_id, read)


def _call(name, arguments):
    if name == "search_library":
        query, mode, shelf = str(arguments.get("query", "")).strip(), arguments.get("mode", "words"), arguments.get("shelf")
        if not query:
            raise ToolError("query is empty")
        if mode == "both":
            ranked, answered = vectors.search(query)
            page = db.get_library(limit=_limit(arguments), offset=_offset(arguments), area=shelf, ranked=ranked)
            return {"saves": [_save(entry) for entry in page["items"]], "total": page["total"],
                    "next_offset": page["next_offset"], "searched": answered}
        if mode == "meaning":
            found = _ranked(vectors.meaning(query), arguments, shelf)
        elif mode == "words":
            page = db.get_library(limit=_limit(arguments), offset=_offset(arguments), area=shelf, query=query)
            found = {"saves": [_save(entry) for entry in page["items"]], "total": page["total"], "next_offset": page["next_offset"]}
        else:
            raise ToolError("mode must be 'words', 'meaning' or 'both'")
        if not found["total"]:
            found["hint"] = EMPTY_HINT[mode]
        return found
    if name == "find_similar":
        return _ranked(vectors.similar(int(arguments["id"])), arguments)
    if name == "get_save":
        item_id = int(arguments["id"])
        project = str(arguments.get("project") or "").strip()
        if project:
            found = references.for_project(project, item_id=item_id)["saves"]
        else:
            found = db.get_library(ranked=[item_id])["items"]
        if not found:
            raise ToolError(f"no save with id {item_id} in " + (f"project {project}" if project else "the library"))
        save = _save(found[0], full=True)
        return {**save, "_content": _pictures(save) if arguments.get("pictures") is True else []}
    if name == "list_shelves":
        areas = json.loads(CONFIG.read_text()).get("areas", [])
        return {"shelves": [{"slug": area["slug"], "name": area["name"],
                             "saves": db.get_library(limit=1, area=area["slug"])["total"]} for area in areas]}
    if name == "review_references":
        ids = arguments["ids"]
        if not isinstance(ids, list) or not all(type(i) is int for i in ids):
            raise ToolError("ids must be a list of save ids")
        if arguments.get("check_words") is True:
            useful.load_key()
        return references.review(str(arguments["brief"]), ids, allow_upload=arguments.get("check_words") is True)
    if name == "project_references":
        project, folder = str(arguments.get("project") or "").strip(), str(arguments.get("folder") or "").strip()
        task = str(arguments.get("task") or "").strip() or None
        if not (project or folder):
            raise ToolError("pass folder (your working directory) or project")
        project = project or references.project_for_folder(folder)
        found = references.for_project(project, task, _limit(arguments, 3 if task else 10), _offset(arguments))
        return {"project": project, "name": found["name"], "task": task, "total": found["total"],
                "next_offset": found["next_offset"],
                "references": [{**_save(entry, full=True), "filed": entry["filed"], "takeaway": entry["takeaway"],
                                "matches_task": entry["matches_task"], "reason": entry["reason"]}
                               for entry in found["saves"]]}
    if name == "keep_link":
        shelf = _shelf(arguments)  # checked first, so a bad shelf saves nothing
        item_id = adds.add_link(str(arguments["url"]))
        adds.file_on_shelf(item_id, shelf)
        _index(item_id)
        return {"id": item_id, "kept": True, "shelf": shelf}
    if name == "keep_picture":
        shelf = _shelf(arguments)
        item_id = adds.add_image(str(arguments["path"]), note=str(arguments.get("note", "")))
        adds.file_on_shelf(item_id, shelf)
        _index(item_id)
        return {"id": item_id, "kept": True, "shelf": shelf}
    raise KeyError(name)


def _found(content):
    """How many saves a reply found, and the ids it handed back, for the call log."""
    if content is None:
        return None, None
    for key in ("saves", "references", "shelves"):
        if key in content:
            ids = [entry["id"] for entry in content[key] if "id" in entry]
            return (len(ids) if key == "references" else content.get("total", len(content[key]))), ids
    return (1, [content["id"]]) if "id" in content else (None, None)


def _log(params, content, ok, started):
    found, ids = _found(content)
    try:
        db.log_agent_call(SESSION["id"], SESSION["client"], str(params.get("name")),
                          found, ids, ok, round((time.monotonic() - started) * 1000))
    except Exception:  # a locked or old database must never cost the agent its answer
        pass


def handle(message):
    """One JSON-RPC message in, one response out (None for notifications)."""
    if "id" not in message:
        return None
    reply = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        client = params.get("clientInfo") or {}
        SESSION["client"] = " ".join(str(part) for part in (client.get("name"), client.get("version")) if part) or None
        reply["result"] = {"protocolVersion": params.get("protocolVersion", PROTOCOL), "instructions": INSTRUCTIONS,
                           "capabilities": {"tools": {}}, "serverInfo": {"name": "beacon", "version": "1.0"}}
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        started, content = time.monotonic(), None
        try:
            content = _call(params.get("name"), params.get("arguments") or {})
            extra = content.pop("_content", [])  # images ride beside the JSON, never inside it
            reply["result"] = {"content": [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}, *extra],
                               "structuredContent": content, "isError": False}
        except KeyError as missing:
            reply["error"] = {"code": -32602, "message": f"unknown tool or missing argument: {missing}"}
        except (ToolError, ValueError, OSError, RuntimeError) as problem:
            reply["result"] = {"content": [{"type": "text", "text": str(problem)}], "isError": True}
        except Exception as problem:  # a locked database, a slow search: say so, keep the server up
            reply["result"] = {"content": [{"type": "text", "text": f"{type(problem).__name__}: {problem}"}], "isError": True}
        _log(params, content, "result" in reply and not reply["result"]["isError"], started)
    else:
        reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
    return reply


def serve(input=None, output=None):
    """Read messages from stdin until it closes; answer each on stdout.

    stdout carries only the protocol: anything Beacon code prints (warnings) goes to stderr instead.
    """
    input, output = input or sys.stdin, output or sys.stdout
    sys.stdout = sys.stderr
    for line in input:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            output.write(json.dumps(reply, ensure_ascii=False) + "\n")
            output.flush()
