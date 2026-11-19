"""A generated layout for one Library search: Claude groups the results from a fixed set of parts.

The idea is from Suave's composed results, rebuilt for Beacon (no code from it): the model gets the
search and the saves numbered 1..N, and answers only with parts the app knows how to draw.

- Group: a titled set of saves someone would choose between.
- Pick: the one save that clearly answers a specific search, with a few words on why.
- next: two to four follow-up steps to narrow by, from real differences in these saves.
- off: saves that aren't what the search asks for.

Saves are referred to by number, so the model can't invent one, and the server checks every number.
Nothing goes missing: a save no part claims, or one the model calls off, lands in "Other matches". When
the model says the saves fit poorly, or the parts don't hold up, the answer is no layout and the app keeps
its plain grid; a nicer arrangement of weak results would raise trust exactly when it should drop.

The model reads words only (title, text, author, shelf, Beacon's takeaway), not the pictures, so picture
saves with no words group poorly. Each save also says which searched words its own words contain (through
the Library's word index): on the 20-search judgment (runs/2026-11-19-arrange) that was the difference
between headline groups full of picture matches about something else and groups of saves that fit.
Every answer is logged in compose_log with what it cost, so the layout can be judged against the plain
grid on real searches before anyone builds more on it.
"""
import hashlib
import json
import tempfile
import threading
from collections import OrderedDict
from contextlib import closing
from datetime import datetime, timezone

from . import briefs, db, settings, vectors

MODEL = "haiku"
# More than this and Haiku's grouping gets sloppy (and slower); the rest of the results stay below.
MAX_SAVES = 40
MIN_GROUPS, MAX_GROUPS = 2, 5
TITLE_CHARS = 32
OTHER = "Other matches"

SYSTEM = "You lay out search results from a personal library of saved posts, pictures and links. Be plain and exact."

PROMPT = """{owner} searched their saved library for: {search}

These are the saves the search found, best first. Each line is: number | what it says | where it's from | shelf | Beacon's takeaway | which searched words the save's own words contain.

{saves}

Most saves are pictures, marked [picture], and you can't see them. A save that has none of the searched words was found by its picture or by a loose meaning match: some show what was searched, many don't, and you can't tell which. So:
- A save is on-topic only when its words are about what was searched, not just mention a word in passing or use it in another sense ("animation" library for "anime", a "chair" of a committee).
- Put a save with none of the searched words in a group only when its words plainly describe a kind of the searched thing. Otherwise leave it out of every group: it is shown under "{other}" anyway. Never use off for these.
- off: saves whose words show they are about something else. They are shown under "{other}" too.

Lay out the on-topic saves from these parts only:
- groups: {min_groups} to {max_groups} titled sets of on-topic saves {owner} would choose between, the set that best answers the search first. Each title names one kind of the searched thing, told apart by kind, style, material, use, place, or who made it: for "chairs", "Armchairs" or "Dining chairs"; for "obsidian", "Plugins" or "Vault setups". Every save in a group must fit its title by its own words. Put every on-topic save in a group: "{other}" is for saves you can't place, and an on-topic save left there is buried among off-topic ones. Make each group a different kind, never near-synonyms ("Fall-winter" and "Cold-weather") or overlapping titles; when two groups would overlap, merge them. Never title by source or format ("Posts", "Repos", "Tools", "Videos"), never a catch-all ("Misc", "Design", "Lifestyle", "Other", "Tutorials and news"). Fewer, fuller groups beat many small ones. If the search says how to group, group only that way and title each group by its value ("Green", "Red"), never by the grouping ("By color").
- pick: one save, only when one clearly answers a specific search, with under 8 words on what fits. Otherwise null.
- next: 2 to 4 words {owner} could add to narrow these results, 1 to 3 words each, like "armchair", "Windsor" or "movie posters": the words themselves, never an instruction ("Search for...", "Filter to...") and never a shelf or another search. Take them from real differences in the on-topic saves. Never repeat a group title.
- fit: "good" only when at least 6 saves are on-topic and they split into at least two real kinds. Otherwise "weak", and the plain grid stays, which is the right answer when the search mostly found other things.

Rules: use only numbers from the list, each at most once across groups and pick. Titles are 1 to 3 plain words, no counts, no emoji, no marketing words. A save you're unsure of goes in no group."""

SCHEMA = {
    "type": "object", "required": ["groups", "pick", "next", "off", "fit"],
    "properties": {
        "groups": {"type": "array", "items": {"type": "object", "required": ["title", "saves"], "properties": {
            "title": {"type": "string"}, "saves": {"type": "array", "items": {"type": "integer"}}}}},
        "pick": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["save", "why"], "properties": {
            "save": {"type": "integer"}, "why": {"type": "string"}}}]},
        "next": {"type": "array", "items": {"type": "string"}},
        "off": {"type": "array", "items": {"type": "integer"}},
        "fit": {"type": "string", "enum": ["good", "weak"]},
    },
}

# Answers the server process already has, newest last, so the same search over the same saves is free.
_cache = OrderedDict()
_CACHE_SIZE = 200
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _line(number, entry, takeaway, has):
    text = " ".join((entry["text"] or db._TCO.sub("", entry["title"] or "")).split())[:160]
    repo = (entry["repo"] or {}).get("name") if isinstance(entry["repo"], dict) else entry["repo"]
    origin = entry["author_name"] or entry["author"] or repo or entry["source"]
    if not text:
        text = "a picture with no words"
    if entry.get("media"):
        text = f"[picture] {text}"
    return " | ".join([str(number), text, f"{entry['source']}, {origin}", entry["area"] or "no shelf",
                       (takeaway or "")[:120] or "none", "has: " + (", ".join(has) or "none of the words")])


# Words a search uses that say nothing about what it's for.
_FILLER = frozenset("a an and app apps are as at best by for from good how i in into is it me my of on or some "
                    "that the things this to with".split())


def _has(search, ids):
    """{item_id: [searched words its words contain]}, through the Library's word index (title, text, quoted
    post, and Beacon's takeaways and tags), so "chair" counts for "chairs" and "animated" for "animation".
    The search found the rest by its picture or by meaning alone, which the model can't check."""
    terms = [t for t in dict.fromkeys(" ".join(search.lower().replace("-", " ").split()).split())
             if t not in _FILLER and any(c.isalnum() for c in t)]
    has = {item_id: [] for item_id in ids}
    with closing(db.get_connection()) as conn:
        if not ids or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='library_fts'").fetchone():
            return has
        for term in terms:
            single = vectors._singular(term)
            quoted = '"' + term.replace('"', '""') + '"'
            match = f'{quoted} OR "{single}"' if single else quoted
            rows = conn.execute(f"SELECT rowid FROM library_fts WHERE library_fts MATCH ? AND rowid IN "
                                f"({','.join('?' * len(ids))})", (match, *ids))
            for (item_id,) in rows:
                has[item_id].append(term)
    return has


def _takeaways(ids):
    with closing(db.get_connection()) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_gists'").fetchone()
        if not exists or not ids:
            return {}
        rows = conn.execute(f"SELECT item_id, takeaway FROM item_gists WHERE item_id IN ({','.join('?' * len(ids))})", ids)
        return {row[0]: row[1] for row in rows}


def prompt(search, entries):
    ids = [entry["id"] for entry in entries]
    takeaways, has = _takeaways(ids), _has(search, ids)
    saves = "\n".join(_line(n, entry, takeaways.get(entry["id"]), has[entry["id"]]) for n, entry in enumerate(entries, 1))
    return PROMPT.format(owner=settings.owner(), search=search, saves=saves, min_groups=MIN_GROUPS,
                         max_groups=MAX_GROUPS, other=OTHER)


def haiku_writer(text):
    return briefs.sonnet(text, SCHEMA, SYSTEM, timeout=60, model=MODEL, cache=False, cwd=tempfile.gettempdir())


def checked(answer, ids):
    """The model's answer as a layout over item ids, or (None, why) when it shouldn't replace the grid.

    Unknown and repeated numbers are dropped; a group left with fewer than two saves dissolves; everything
    unclaimed, and everything the model called off, goes to "Other matches" in search order.
    """
    if answer.get("fit") != "good":
        return None, "the saves fit this search poorly, so the plain grid stays"
    number = {n: item_id for n, item_id in enumerate(ids, 1)}
    off = {number[n] for n in answer.get("off") or [] if n in number}
    used = set()

    def take(n):
        item_id = number.get(n) if type(n) is int else None
        if item_id is None or item_id in used or item_id in off:
            return None
        used.add(item_id)
        return item_id

    pick = None
    raw_pick = answer.get("pick")
    if isinstance(raw_pick, dict):
        chosen = take(raw_pick.get("save"))
        if chosen is not None:
            pick = {"id": chosen, "why": " ".join(str(raw_pick.get("why") or "").split())[:60]}
    groups, titles = [], set()
    for group in answer.get("groups") or []:
        title = " ".join(str(group.get("title") or "").split())[:TITLE_CHARS]
        if not title or title.lower() in titles or title.lower() == OTHER.lower():
            continue
        members = [i for i in (take(n) for n in group.get("saves") or []) if i is not None]
        if len(members) < 2:
            used.difference_update(members)  # back to Other matches
            continue
        titles.add(title.lower())
        groups.append({"title": title, "ids": members})
    if len(groups) < MIN_GROUPS:
        return None, "the saves didn't split into groups, so the plain grid stays"
    groups = groups[:MAX_GROUPS]
    used = {i for g in groups for i in g["ids"]} | ({pick["id"]} if pick else set())
    rest = [i for i in ids if i not in used]
    if rest:
        groups.append({"title": OTHER, "ids": rest})
    follow_ups = []
    for step in answer.get("next") or []:
        step = " ".join(str(step).split()).strip(" .\"'")
        # A follow-up is added to the search word for word, so anything longer than a few words is an
        # instruction or a sentence, not a refinement.
        if len(step.split()) > 3 or len(step) > 30 or step.lower().startswith(("search", "filter", "show", "narrow")):
            continue
        if step and step.lower() not in titles and step.lower() not in (s.lower() for s in follow_ups):
            follow_ups.append(step)
    return {"pick": pick, "groups": groups, "next": follow_ups[:4]}, None


def _log(search, ids, layout, why, cost):
    with closing(db.get_connection()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO compose_log(search, ids_json, layout_json, why, model, cost_usd, created_at) VALUES(?,?,?,?,?,?,?)",
            (search, json.dumps(ids), json.dumps(layout) if layout else None, why, MODEL, cost, _now()))
        return cursor.lastrowid


def compose(search, ids, *, writer=None):
    """{"id", "layout" (or None), "why", "cost_usd", "cached"} for this search over these saves (best first)."""
    search = " ".join(search.split())[:200]
    if not search:
        raise ValueError("search is empty")
    if not isinstance(ids, list) or not all(type(i) is int for i in ids):
        raise ValueError("ids must be a list of save ids")
    entries = db.get_library(limit=MAX_SAVES, ranked=list(dict.fromkeys(ids))[:MAX_SAVES])["items"]
    ids = [entry["id"] for entry in entries]
    if len(ids) < 2 * MIN_GROUPS:
        return {"id": None, "layout": None, "why": "too few saves to group", "cost_usd": 0.0, "cached": False}
    key = hashlib.sha256(json.dumps([search.lower(), ids]).encode()).hexdigest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return {**_cache[key], "cached": True}
    try:
        answer, cost = (writer or haiku_writer)(prompt(search, entries))
        layout, why = checked(answer, ids)
    except RuntimeError as error:
        # Not cached: a busy or signed-out Claude can answer next time.
        return {"id": None, "layout": None, "why": f"Claude didn't answer: {str(error)[:120]}", "cost_usd": 0.0, "cached": False}
    result = {"id": _log(search, ids, layout, why, cost), "layout": layout, "why": why, "cost_usd": cost}
    with _lock:
        _cache[key] = result
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return {**result, "cached": False}


def choose(compose_id, choice):
    """Record what the person did with an offered layout: "kept" (showed it), "dismissed", or "ignored"."""
    if choice not in ("kept", "dismissed", "ignored"):
        raise ValueError("choice must be kept, dismissed or ignored")
    with closing(db.get_connection()) as conn, conn:
        changed = conn.execute("UPDATE compose_log SET choice = ?, chosen_at = ? WHERE id = ?",
                               (choice, _now(), compose_id)).rowcount
    if not changed:
        raise ValueError(f"no layout {compose_id}")
    return {"id": compose_id, "choice": choice}


def summary():
    """How offered layouts have done so far: the evidence for keeping or killing them."""
    with closing(db.get_connection()) as conn:
        rows = conn.execute("SELECT layout_json IS NOT NULL AS offered, choice, cost_usd FROM compose_log").fetchall()
    offered = [row for row in rows if row["offered"]]
    return {"searches": len(rows), "offered": len(offered),
            "kept": sum(row["choice"] == "kept" for row in offered),
            "dismissed": sum(row["choice"] == "dismissed" for row in offered),
            "ignored": sum(row["choice"] in ("ignored", None) for row in offered),
            "cost_usd": round(sum(row["cost_usd"] or 0 for row in rows), 4)}
