"""Reference review: given what you're making (a brief) and some saves, which fit and which repeat.

Two signals, each one a plain reason the app can show:
- Picture: where the save's picture ranks in your whole library for the brief, by meaning (SigLIP,
  on this Mac). Top 2% is a strong fit, top 10% a maybe.
- Words: with your consent, Jev reads each save's text against the brief (TypeSafe; the brief and
  post text leave this Mac). Saves with almost no text aren't sent.
A save's fit is the better of the two. A save whose picture is the same as a better-fitting one's
(Vision fingerprints at least 0.95 alike) is marked as a repeat and moves to the end.
"""
import array
import math
import os
import re
import subprocess
from collections import Counter
from contextlib import closing
from pathlib import Path

from . import briefs, db, settings, useful, vectors
from .review import UNTRUSTED

MAX_SAVES = 40
MAX_BRIEF = 300
STRONG_TOP, MAYBE_TOP = 2, 10  # percent of the library, by meaning
# Measured on the author's library: the same video frame twice scored 0.96; two different terminal
# screenshots 0.92; random pairs 0.26 typical, 0.72 at the 99.9th percentile.
SAME_PICTURE = 0.95
MIN_WORDS = 15  # characters; "lol" tells Jev nothing
CRITERIA = [
    "It doesn't fit the brief, or its text says too little to tell.",
    "It's related to the brief's subject or mood, but not a strong reference.",
    "A strong reference for the brief: it shows the subject, style or mood the brief asks for.",
]
LEVELS = ("off", "maybe", "strong")
WORDS = {"strong": "strong fit", "maybe": "related", "off": "off brief"}


def _request(brief, saves):
    items = [{"id": save["id"], "title": (save["title"] or "")[:200], "excerpt": save["text"][:300]} for save in saves]
    questions = {f"save_{save['id']}": {
        "type": "score", "criteria": CRITERIA,
        "instructions": UNTRUSTED + f"How well does `items[{index}]` serve as a reference for `brief`? "
                                           "Judge only its title and text."} for index, save in enumerate(saves)}
    return {"model": useful.MODEL, "state": {"brief": brief, "items": items}, "questions": questions}


def _words(brief, saves):
    """{id: (level, strength 0-1)} from Jev, for saves with enough text to judge."""
    readable = [save for save in saves if len(save["text"].strip()) >= MIN_WORDS]
    found = {}
    for batch in useful.pack_batches(readable, lambda batch: useful.encode_request(_request(brief, batch)), 20):
        request = _request(brief, batch)
        answers, _ = useful.validate_response(request, useful.call_typesafe(request, allow_upload=True))
        for save in batch:
            score, _, p = useful.read_score(answers[f"save_{save['id']}"], CRITERIA)
            found[save["id"]] = ("strong" if p[2] >= 0.6 else "maybe" if score >= 0.8 else "off", score / 2)
    return found


def _fingerprints(ids):
    with closing(db.get_connection()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_vectors'").fetchone():
            return {}  # nothing indexed yet: the helper makes the table on its first run
        rows = conn.execute(f"SELECT item_id, image_vec FROM item_vectors WHERE image_vec IS NOT NULL "
                            f"AND item_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {row[0]: array.array("f", row[1]) for row in rows}


def _stem(word):
    for ending in ("ing", "ed", "es", "s"):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[:-len(ending)]
    return word


def _root(backlog_path):
    path = os.path.expanduser(backlog_path)
    return Path(os.path.realpath(briefs.project_root(path))) if os.path.isabs(path) else None


def _alias_roots(active_slugs):
    projects_dir = settings.path("projects_dir")
    if not projects_dir:
        return []
    base = Path(os.path.realpath(projects_dir))
    found = []
    for entry in settings.load().get("workstreams") or []:
        slug = entry.get("slug")
        if slug not in active_slugs:
            continue
        for alias in entry.get("aliases") or []:
            if isinstance(alias, str) and alias.strip():
                found.append((Path(os.path.realpath(base / alias.strip())), slug))
    return found


def project_for_folder(folder):
    """The slug of the active project whose root is `folder` or an ancestor of it; the deepest root wins."""
    folder = os.path.expanduser(folder)
    if not os.path.isabs(folder):
        return None
    here = Path(os.path.realpath(folder))
    with closing(db.get_connection()) as conn:
        rows = conn.execute("SELECT slug, backlog_path FROM workstreams "
                            "WHERE kind = 'project' AND active = 1 AND backlog_path IS NOT NULL").fetchall()
    roots = [(_root(row["backlog_path"]), row["slug"]) for row in rows]
    roots.extend(_alias_roots({row["slug"] for row in rows}))
    def project_at(path):
        holding = [(root, slug) for root, slug in roots if root and path.is_relative_to(root)]
        return max(holding, key=lambda found: len(found[0].parts))[1] if holding else None

    direct = project_at(here)
    if direct or not here.is_dir():
        return direct
    try:
        result = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--show-toplevel",
                                 "--git-common-dir"], cwd=here, capture_output=True, text=True, check=True, timeout=2)
        checkout, common = map(Path, result.stdout.splitlines())
        if common.name == ".git":
            return project_at(common.parent / here.relative_to(checkout))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def for_project(project, task=None, limit=10, offset=0, *, item_id=None):
    """The saves kept for a project or area, for an agent about to work there.

    Filed as a reference (adopted with note "reference") or kept (adopted, not sent to Reader),
    among the saves routed to `project`. With a task, require lexical evidence of a match.
    """
    with closing(db.get_connection()) as conn:
        rows = conn.execute(
            "SELECT i.id, i.source, i.title, i.body, i.url, i.author, i.raw_json, i.status_note, NULL AS area, "
            "w.name AS project_name, g.takeaway "
            "FROM items i JOIN item_workstreams iw ON iw.item_id = i.id "
            "JOIN workstreams w ON w.id = iw.workstream_id AND w.slug = ? AND w.active = 1 "
            "LEFT JOIN item_gists g ON g.item_id = i.id "
            "WHERE i.status = 'adopted' AND COALESCE(i.status_note, '') != 'read' AND " + db.NOT_HELD_SQL
            + (" AND i.id = ?" if item_id is not None else "") + " ORDER BY i.id DESC",
            (project, item_id) if item_id is not None else (project,)).fetchall()
    name, total = (rows[0]["project_name"] if rows else None), len(rows)
    evidence = {}
    if task and rows:
        terms = {_stem(word): word for word in useful.task_terms(task)}
        for row in rows:
            text = " ".join(row[field] or "" for field in ("title", "body", "takeaway")).lower()
            evidence[row["id"]] = set(terms) & {_stem(word) for word in re.findall(r"[^\W_]+", text)}
        frequency = Counter(term for matched in evidence.values() for term in matched)
        required = 1 if len(terms) < 3 else 2
        rows = [row for row in rows if len(evidence[row["id"]]) >= required]
        rows.sort(key=lambda row: -sum(math.log1p(total / frequency[term]) for term in sorted(evidence[row["id"]])))
    saves = [{**db.library_entry(row), "filed": "reference" if row["status_note"] == db.REFERENCE else "kept",
              "takeaway": row["takeaway"], "matches_task": True if task else None,
              "reason": "Matched words: " + ", ".join(terms[term] for term in sorted(evidence[row["id"]])) if task else None}
             for row in rows[offset:offset + limit]]
    return {"name": name, "total": total, "saves": saves,
            "next_offset": offset + limit if offset + limit < len(rows) else None}


def review(brief, ids, *, allow_upload=False):
    brief = (brief or "").strip()
    if not 0 < len(brief) <= MAX_BRIEF:
        raise ValueError(f"Say what you're making in 1 to {MAX_BRIEF} characters.")
    if not 0 < len(ids) <= MAX_SAVES:
        raise ValueError(f"Review 1 to {MAX_SAVES} saves at a time.")
    saves = db.get_library(limit=MAX_SAVES, ranked=list(dict.fromkeys(ids)))["items"]

    ranked = vectors.meaning(brief, limit=100_000)
    top = {item_id: (position + 1) * 100 / len(ranked) for position, (item_id, _) in enumerate(ranked)}
    words, note = {}, None
    if allow_upload:
        try:
            words = _words(brief, saves)
        except ValueError as error:
            note = f"Couldn't check the words ({error}). Ranked by pictures only."

    rows = []
    for save in saves:
        percent = top.get(save["id"])
        picture = None if percent is None else ("strong" if percent <= STRONG_TOP else "maybe" if percent <= MAYBE_TOP else "off")
        word_level, word_strength = words.get(save["id"], (None, 0))
        levels = [level for level in (picture, word_level) if level]
        fit = max(levels, key=LEVELS.index) if levels else "off"
        strength = max(0 if percent is None else 1 - percent / 100, word_strength)
        reason = [f"Picture: top {math.ceil(percent)}% of your library for this brief" if percent is not None
                  else "Picture not read yet" if save["media"] else "No picture to compare"]
        if allow_upload and not note:
            reason.append(f"Words: {WORDS[word_level]}" if word_level else "Words: too few to judge")
        rows.append({"id": save["id"], "fit": fit, "duplicate_of": None, "reason": " · ".join(reason),
                     "_order": (-LEVELS.index(fit), -strength)})
    rows.sort(key=lambda row: row["_order"])

    prints, kept = _fingerprints([row["id"] for row in rows]), []
    for row in rows:
        mine = prints.get(row["id"])
        if mine is not None:
            row["duplicate_of"] = next((other for other in kept if len(prints[other]) == len(mine)
                                        and sum(a * b for a, b in zip(prints[other], mine)) >= SAME_PICTURE), None)
            if row["duplicate_of"] is None:
                kept.append(row["id"])
        if row["duplicate_of"]:
            row["reason"] = "Same picture as a save above · " + row["reason"]
    rows.sort(key=lambda row: row["duplicate_of"] is not None)  # stable: repeats go last, in order
    for row in rows:
        del row["_order"]
    return {"brief": brief, "words_checked": bool(allow_upload and not note), "note": note, "saves": rows}
