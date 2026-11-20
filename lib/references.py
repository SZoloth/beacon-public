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
import json
import math
import os
import re
import subprocess
from collections import Counter
from contextlib import closing
from pathlib import Path

from . import briefs, db, owner_notes, settings, useful, vectors, vocabulary
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


SOURCE_NAMES = {"mymind": "mymind", "readwise": "Readwise", "granola": "Granola"}
DIGEST = "A Granola digest of your notes (summarized by a model)"


def _project_names(conn, project):
    """The slug, the configured name and every configured alias of `project`, each in owner_notes.plain form."""
    names = {project}
    row = conn.execute("SELECT name FROM workstreams WHERE slug = ?", (project,)).fetchone()
    names.add(row["name"] if row else "")
    config = settings.load()
    for entry in (config.get("workstreams") or []) + (config.get("areas") or []):
        if entry.get("slug") == project:
            names.update(alias for alias in entry.get("aliases") or [] if isinstance(alias, str))
    return {plain for plain in map(owner_notes.plain, names) if plain}


def _named(text, names):
    """The longest of `names` that `text` holds as whole words, or None (cos is not in cosmos)."""
    plain = f" {owner_notes.plain(text)} "
    return next((name for name in sorted(names, key=len, reverse=True) if f" {name} " in plain), None)


def _speaker(note):
    return DIGEST if note["origin"] == "owner_digest" else f"Your {SOURCE_NAMES.get(note['source'], note['source'])} {note['kind']}"


def client_saves(conn):
    """Ids of the saves with owner notes that stay out of every project but client work, by the rule activate.py enforces:
    filed under client work, or with a note or tag that names a client or a client project."""
    from . import activate  # activate imports this module
    filed, found = activate._client_item_ids(), set()  # first: reading the config loads the client rules
    slugs = {owner_notes.plain(slug) for slug in activate.CLIENT_SLUGS}
    found.update(row[0] for row in conn.execute("SELECT DISTINCT item_id FROM owner_notes") if row[0] in filed)
    # Terms the owner filed under client work count as client names, whatever they say.
    filed_terms = {term for term, targets in vocabulary.effective(conn).items()
                   if any(owner_notes.plain(target) in slugs for target, _ in targets)}
    if filed_terms:
        found.update(row["item_id"] for row in conn.execute("SELECT item_id, body FROM owner_notes WHERE kind = 'tag'")
                     if owner_notes.plain(row["body"]) in filed_terms)
    # Every client name holds one of the configured words, so SQLite skips the notes holding none of them or of a
    # client slug's words. With no words configured, every note is read.
    hints = sorted(set(activate.CLIENT_WORDS) | {word for slug in slugs for word in slug.split()}) \
        if activate.CLIENT_WORDS else []
    # A Granola meeting's name is the owner's context too, so it is read with the note.
    meeting = "CASE WHEN i.source = 'granola' THEN COALESCE(i.title, '') ELSE '' END"
    text = f"lower(n.body || ' ' || COALESCE(n.context, '') || ' ' || {meeting})"
    for row in conn.execute(f"SELECT n.item_id, n.origin, n.body, n.context, {meeting} AS meeting "
                            "FROM owner_notes n JOIN items i ON i.id = n.item_id"
                            + (" WHERE " + " OR ".join(f"instr({text}, ?) > 0" for _ in hints) if hints else ""), hints):
        if row["item_id"] in found:
            continue
        written = f"{row['body']} {row['context'] or ''} {row['meeting']}"
        digest = json.loads(row["context"]) if row["origin"] == "owner_digest" and row["context"] else []
        if activate._names_client(written) or _named(row["body"], slugs) \
                or slugs & {owner_notes.plain(slug) for slug in digest}:
            found.add(row["item_id"])
    return found


def _client_filter(conn, project):
    """The saves that must stay out of `project`: none for a client project, client_saves for any other."""
    from . import activate
    entries = activate._entries()
    row = conn.execute("SELECT backlog_path FROM workstreams WHERE slug = ?", (project,)).fetchone()
    if activate._client_workstream(project, row["backlog_path"] if row else None, entries.get(project, {}).get("agent")) \
            or activate.CLIENT_NAME.search(project):
        return set()
    return client_saves(conn)


def _stems(text):
    return {_stem(word) for word in re.findall(r"[^\W_]+", (text or "").lower())}


def _noted(conn, project, terms=None, item_id=None):
    """Saves whose owner notes point at `project` (or, with task terms, share them), best first.

    Matched now, against the project as configured now, so a project made after the note still finds it.
    An area is named only by a tag, since its name is an ordinary word. A save the owner rejected stays out.
    With task terms, every save must share them, as filed saves must: one naming the project may share them in
    its title, text or takeaway too, and one that doesn't name it must share them in its notes.
    Each is (item id, reason, task words matched): the ones naming the project first, then by task words matched, newest first.
    """
    if not project:
        return []
    names = _project_names(conn, project)
    kind = conn.execute("SELECT kind, name FROM workstreams WHERE slug = ?", (project,)).fetchone()
    area = bool(kind) and kind["kind"] == "area"  # an area is an ordinary word ("home page"), so only tags name it
    heading = kind["name"] if kind else project
    filed_terms = vocabulary.effective(conn)
    by_item = {}
    for row in conn.execute("SELECT n.item_id, n.source, n.kind, n.origin, n.body, n.context FROM owner_notes n "
                            "JOIN items i ON i.id = n.item_id WHERE i.status != 'rejected'"
                            + (" AND n.item_id = ?" if item_id is not None else ""),
                            (item_id,) if item_id is not None else ()):
        note = dict(row)
        note["slugs"] = json.loads(note["context"]) if note["origin"] == "owner_digest" and note["context"] else []
        by_item.setdefault(row["item_id"], []).append(note)
    client = _client_filter(conn, project)
    task, terms = terms is not None, terms or {}
    required = 1 if len(terms) < 3 else 2
    texts = {row["id"]: _stems(" ".join(row[field] or "" for field in ("title", "body", "takeaway"))) for row in conn.execute(
        "SELECT i.id, i.title, i.body, g.takeaway FROM items i LEFT JOIN item_gists g ON g.item_id = i.id "
        "WHERE i.id IN (SELECT item_id FROM owner_notes)")} if task else {}
    found = []
    for item, notes in by_item.items():
        if item in client:
            continue
        reason = None
        for note in notes:
            filed_under = [slug for slug in note["slugs"] if owner_notes.plain(slug) in names]
            # A tag, or a note that is just a term, the owner's vocabulary files under projects ([] = unlinked).
            mapped = filed_terms.get(owner_notes.plain(note["body"])) if note["origin"] == "owner" else None
            vocab = next((origin for target, origin in mapped or [] if target == project), None)
            if filed_under:
                reason = f'{DIGEST} files this under "{filed_under[0]}"'
            elif vocab == "owner":
                reason = f'{_speaker(note)} "{note["body"]}" (you filed this term under {heading})'
            elif vocab:
                reason = f'{_speaker(note)} "{note["body"]}" (Beacon files it under {heading}; a model proposed this)'
            elif mapped == []:
                continue
            elif (note["kind"] == "tag" or not area) and (name := _named(note["body"], names)):
                reason = (f'{_speaker(note)} "{note["body"]}" names this project' if note["kind"] == "tag"
                          else f'{_speaker(note)} mentions "{name}"')
            if reason:
                break
        matched, said = set(), None
        for note in notes:
            words = set(terms) & _stems(note["body"])
            if words and said is None:
                said = note
            matched |= words
        named = reason is not None
        if named:
            matched |= set(terms) & texts.get(item, set())
        if (task and len(matched) < required) or not (named or matched):
            continue
        if matched:
            words = ", ".join(f'"{terms[term]}"' for term in sorted(matched))
            reason = f"{reason} · matched words: {words}" if named else f"{_speaker(said)} mentions {words}"
        found.append((not named, -len(matched), -item, reason, matched))
    return [(-negative_id, reason, matched) for _, _, negative_id, reason, matched in sorted(found)]


def for_project(project, task=None, limit=10, offset=0, *, item_id=None):
    """The saves kept for a project or area, for an agent about to work there.

    First the ones filed for it: filed as a reference (adopted with note "reference") or kept (adopted, not
    sent to Reader), among the saves routed to `project`. Then the ones the owner's own notes point at
    ("noted"): a tag or note naming the project, wherever the save was routed. With a task, every save needs
    lexical evidence of a match, and noted saves whose notes share the task's words come in too.
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
        named = conn.execute("SELECT name FROM workstreams WHERE slug = ? AND active = 1", (project,)).fetchone()
        name, total = (named["name"] if named else None), len(rows)
        evidence, terms = {}, {}
        if task:
            terms = {_stem(word): word for word in useful.task_terms(task)}
        if task and rows:
            for row in rows:
                text = " ".join(row[field] or "" for field in ("title", "body", "takeaway")).lower()
                evidence[row["id"]] = set(terms) & _stems(text)
            frequency = Counter(term for matched in evidence.values() for term in matched)
            required = 1 if len(terms) < 3 else 2
            rows = [row for row in rows if len(evidence[row["id"]]) >= required]
            rows.sort(key=lambda row: -sum(math.log1p(total / frequency[term]) for term in sorted(evidence[row["id"]])))
        filed = {row["id"] for row in rows}
        noted = [entry for entry in _noted(conn, project, terms if task else None, item_id) if entry[0] not in filed]
        listed = [("filed", row) for row in rows] + [("noted", entry) for entry in noted]
        page = listed[offset:offset + limit]
        noted_ids = [entry[0] for kind, entry in page if kind == "noted"]
        noted_rows = {row["id"]: row for row in conn.execute(
            "SELECT i.id, i.source, i.title, i.body, i.url, i.author, i.raw_json, NULL AS area, g.takeaway "
            f"FROM items i LEFT JOIN item_gists g ON g.item_id = i.id WHERE i.id IN ({','.join('?' * len(noted_ids))})",
            noted_ids)} if noted_ids else {}
        notes = db.owner_notes_for(conn, [row["id"] for kind, row in page if kind == "filed"] + noted_ids)
    saves = []
    for kind, found in page:
        if kind == "filed":
            saves.append({**db.library_entry(found, notes.get(found["id"])),
                          "filed": "reference" if found["status_note"] == db.REFERENCE else "kept",
                          "takeaway": found["takeaway"], "matches_task": True if task else None,
                          "reason": "Matched words: " + ", ".join(terms[term] for term in sorted(evidence[found["id"]])) if task else None})
        else:
            noted_id, reason, matched = found
            row = noted_rows[noted_id]
            saves.append({**db.library_entry(row, notes.get(noted_id)), "filed": "noted", "takeaway": row["takeaway"],
                          "matches_task": True if task else None, "reason": reason})
    return {"name": name, "total": total + len(noted), "saves": saves,
            "next_offset": offset + limit if offset + limit < len(listed) else None}


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
