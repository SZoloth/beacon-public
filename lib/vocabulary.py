"""The owner's vocabulary: which project or area each of the owner's own tags points at.

An authority file, as libraries keep one: an owner term points at one or more
workstream slugs. The owner's rows always win, and a row with target `none` marks a term as deliberately
unlinked. A model may propose rows (origin `model_proposed`) for terms nothing resolves, and only those.
Unresolved terms are never stored: they are computed, owner tags with no literal name match and no row.

A term is an owner tag in owner_notes.plain form, so "Pencil-Review" and "pencil review" are one term.
"""

import hashlib
import json
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone

from . import ai, db, owner_notes, settings

NONE = "none"
CONFIDENCE = 0.7  # a model proposal below this is dropped and the term stays unresolved
MAX_TARGETS = 2


def term(text):
    return owner_notes.plain(text)


def effective(conn):
    """{term: [(target, origin)]} as matching uses it: owner rows hide model rows, and `none` leaves []."""
    rows = defaultdict(list)
    for row in conn.execute("SELECT term, target, origin FROM owner_vocabulary ORDER BY created_at, target"):
        rows[row["term"]].append((row["target"], row["origin"]))
    found = {}
    for found_term, entries in rows.items():
        owner = [entry for entry in entries if entry[1] == "owner"]
        found[found_term] = [entry for entry in (owner or entries) if entry[0] != NONE]
    return found


def _targets(conn):
    """Every configured project and area: slug, name, kind, active, description and the names that call it."""
    from . import references  # references imports this module
    config = settings.load()
    described = {entry.get("slug"): entry for entry in (config.get("workstreams") or []) + (config.get("areas") or [])}
    return [{"slug": row["slug"], "name": row["name"], "kind": row["kind"], "active": bool(row["active"]),
             "description": (described.get(row["slug"]) or {}).get("description") or "",
             "names": references._project_names(conn, row["slug"])}
            for row in conn.execute("SELECT slug, name, kind, active FROM workstreams ORDER BY kind DESC, slug")]


def _literal(found_term, targets):
    padded = f" {found_term} "
    return [target["slug"] for target in targets if any(f" {name} " in padded for name in target["names"])]


def owner_terms(conn):
    """{term: Counter(source: saves carrying it)} over every tag the owner added, in any source."""
    found = defaultdict(Counter)
    for row in conn.execute("SELECT source, body, COUNT(DISTINCT item_id) AS saves FROM owner_notes "
                            "WHERE kind = 'tag' AND origin = 'owner' GROUP BY source, body"):
        if (key := term(row["body"])):
            found[key][row["source"]] += row["saves"]
    return found


def listing(conn):
    """Each owner term with its saves per source and how it resolves: literal, owner, model, none or unresolved."""
    targets, vocabulary = _targets(conn), effective(conn)
    rows = []
    for found_term, sources in owner_terms(conn).items():
        literal = _literal(found_term, targets)
        if found_term in vocabulary:
            mapped = vocabulary[found_term]
            how = "none" if not mapped else mapped[0][1].replace("model_proposed", "model")
            resolved = [target for target, _ in mapped]
            if how != "none":
                resolved = list(dict.fromkeys(literal + resolved))
        else:
            how, resolved = ("literal", literal) if literal else ("unresolved", [])
        rows.append({"term": found_term, "saves": sum(sources.values()), "sources": dict(sources), "how": how,
                     "targets": resolved})
    return sorted(rows, key=lambda row: (row["how"] != "unresolved", -row["saves"], row["term"]))


def resolve(conn, bodies):
    """{tag as written: [{"target", "how"}]} for get_save: where each of a save's owner tags points."""
    targets, vocabulary = _targets(conn), effective(conn)
    found = {}
    for body in bodies:
        key = term(body)
        if key in vocabulary and not vocabulary[key]:
            found[body] = []
            continue
        pointed = [{"target": slug, "how": "literal"} for slug in _literal(key, targets)]
        pointed += [{"target": slug, "how": origin.replace("model_proposed", "model")}
                    for slug, origin in vocabulary.get(key, []) if slug not in {entry["target"] for entry in pointed}]
        found[body] = pointed
    return found


def set_term(text, target, *, origin="owner", db_path=None):
    db.init_db(db_path)
    key = term(text)
    if not key or not target:
        raise ValueError("Give a term and a target slug (or none).")
    with closing(db.get_connection(db_path)) as conn, conn:
        conn.execute("INSERT INTO owner_vocabulary (term, target, origin, created_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(term, target) DO UPDATE SET origin = excluded.origin, created_at = excluded.created_at",
                     (key, target, origin, datetime.now(timezone.utc).isoformat()))
    return key


def unset_term(text, target, db_path=None):
    db.init_db(db_path)
    with closing(db.get_connection(db_path)) as conn, conn:
        return conn.execute("DELETE FROM owner_vocabulary WHERE term = ? AND target = ?", (term(text), target)).rowcount


def _offered(targets):
    """Active projects and areas a model may propose. Client work is never offered, so only the owner files a term there."""
    from . import activate
    config = {entry.get("slug"): entry for entry in settings.load().get("workstreams") or []}
    return [target for target in targets if target["active"] and not activate.CLIENT_NAME.search(target["slug"])
            and not activate._client_workstream(target["slug"], (config.get(target["slug"]) or {}).get("backlog_path"),
                                                (config.get(target["slug"]) or {}).get("agent"))]


def slug_set(offered):
    return hashlib.sha256(json.dumps(sorted(target["slug"] for target in offered)).encode()).hexdigest()[:16]


def evidence(conn, rows, terms, offered):
    """{term: [(slug, saves)]}: for each term, the top offered places the owner's other tags on its saves point at.

    "north star" sits on 30 saves, 22 of which also carry a tag filed under home. A tag points where `listing` says
    (the owner's rows, a model's rows or a literal name), and only offered slugs count, so no client project shows.
    """
    pointed = {row["term"]: set(row["targets"]) for row in rows}
    slugs, wanted = {target["slug"] for target in offered}, set(terms)
    tags = defaultdict(set)
    for item_id, body in conn.execute("SELECT item_id, body FROM owner_notes WHERE kind = 'tag' AND origin = 'owner'"):
        tags[item_id].add(term(body))
    counts = defaultdict(Counter)
    for carried in tags.values():
        for found_term in carried & wanted:
            counts[found_term].update({slug for other in carried - {found_term} for slug in pointed.get(other, ()) if slug in slugs})
    return {found_term: sorted(counted.items(), key=lambda entry: (-entry[1], entry[0]))[:MAX_TARGETS]
            for found_term, counted in counts.items() if counted}


def _asked_as(batch, places):
    """What a term is recorded as asked against: the slug set, and the places its evidence points, if any."""
    return batch if not places else hashlib.sha256(json.dumps([batch, sorted(slug for slug, _ in places)]).encode()).hexdigest()[:16]


def pending(conn):
    """The unresolved terms not yet asked about against the current projects and areas and their evidence.

    Each is (term, saves, evidence, asked_as). A term is asked again when a project or area is added or removed, or
    once the places its evidence points at change. A term naming a client may be sent: it is a word, and no client
    project is offered.
    """
    targets = _targets(conn)
    offered = _offered(targets)
    batch = slug_set(offered)
    asked = {(row["term"], row["slug_set"]) for row in conn.execute("SELECT term, slug_set FROM vocab_proposals")}
    rows = listing(conn)
    unresolved = [(row["term"], row["saves"]) for row in rows if row["how"] == "unresolved"]
    places = evidence(conn, rows, [found_term for found_term, _ in unresolved], offered)
    terms = [(found_term, saves, places.get(found_term, []), _asked_as(batch, places.get(found_term)))
             for found_term, saves in unresolved]
    return [entry for entry in terms if (entry[0], entry[3]) not in asked], offered, batch


def _line(found_term, saves, places):
    counted = f"{saves} save" + ("" if saves == 1 else "s")
    if places:
        counted += "; " + ", ".join(f"{count} {'also filed under' if index == 0 else 'under'} {slug}"
                                    for index, (slug, count) in enumerate(places))
    return f"- {found_term} ({counted})"


def prompt(terms, offered):
    lines = "\n".join(f"- {target['slug']} ({target['kind']}): {target['name']}. {target['description']}".rstrip()
                      for target in offered)
    listed = "\n".join(_line(found_term, saves, places) for found_term, saves, places, _ in terms)
    return (
        "The owner tags saved links and pictures with short terms of their own. Each term below matched no "
        "project or area by name. For each term, say which of the projects and areas it points at, if any.\n\n"
        f"Projects and areas (slug, kind, name, description):\n{lines}\n\n"
        "Owner terms, each with the number of saves carrying it and where the owner's other tags on those saves "
        f"already point:\n{listed}\n\n"
        "Rules:\n"
        "- Use only the slugs listed above.\n"
        f"- At most {MAX_TARGETS} targets per term. Leave a term out when no target clearly fits: a person's name, "
        "a place or a vague word usually fits none.\n"
        "- Where the other tags point is evidence: a term whose saves are mostly filed under one place likely "
        "belongs there too.\n"
        "- confidence is 0 to 1: how sure you are the owner meant that target.\n\n"
        'Answer with JSON only: [{"term": "<term as listed, without the counts>", "targets": [{"slug": "<slug>", "confidence": 0.0}]}]'
    )


def propose(*, preview=False, db_path=None, run=ai.run_json_prompt_result):
    """Ask Claude (`claude --print`, never another runner) where pending unresolved terms point; keep confident answers.

    Sends only the terms, their counts, where the owner's other tags on their saves point (see `evidence`) and the
    projects' and areas' slugs, names and descriptions, never a save's title or text. Every sent term is recorded as
    asked (see `pending`). Returns counts, or the prompt with `preview`.
    """
    db.init_db(db_path)
    with closing(db.get_connection(db_path)) as conn:
        terms, offered, batch = pending(conn)
    if not terms:
        return {"sent": 0, "stored": 0, "slug_set": batch}
    text = prompt(terms, offered)
    if preview:
        return {"sent": 0, "would_send": len(terms), "slug_set": batch, "prompt": text}
    outcome = run(text, timeout=600, runners=["claude"])
    if not outcome["ok"]:
        raise RuntimeError("No model answered: " + "; ".join(outcome["errors"]))
    if not isinstance(outcome["results"], list):
        raise RuntimeError("The model's answer was not a list, so no term was recorded as asked.")
    sent, slugs = {entry[0]: entry[3] for entry in terms}, {target["slug"] for target in offered}
    now, stored = datetime.now(timezone.utc).isoformat(), []
    for answer in outcome["results"]:
        # A model may echo the line, counts and all: the term is what comes before them.
        answered = term(str(answer.get("term") or "").split("(")[0]) if isinstance(answer, dict) else None
        if answered not in sent or not isinstance(answer.get("targets"), list):
            continue
        confident = [target for target in answer["targets"] if isinstance(target, dict) and target.get("slug") in slugs
                     and isinstance(target.get("confidence"), (int, float)) and target["confidence"] >= CONFIDENCE]
        for target in sorted(confident, key=lambda target: -target["confidence"])[:MAX_TARGETS]:
            stored.append((answered, target["slug"], target["confidence"]))
    with closing(db.get_connection(db_path)) as conn, conn:
        conn.executemany("INSERT OR IGNORE INTO owner_vocabulary (term, target, origin, created_at) "
                         "VALUES (?, ?, 'model_proposed', ?)", [(found_term, slug, now) for found_term, slug, _ in stored])
        conn.executemany("INSERT OR IGNORE INTO vocab_proposals (term, slug_set, proposed_at) VALUES (?, ?, ?)",
                         [(found_term, asked_as, now) for found_term, asked_as in sent.items()])
    saves = {entry[0]: entry[1] for entry in terms}
    return {"sent": len(sent), "stored": len(stored), "slug_set": batch, "runner": outcome.get("runner"),
            "proposals": sorted(stored, key=lambda entry: -saves.get(entry[0], 0))}
