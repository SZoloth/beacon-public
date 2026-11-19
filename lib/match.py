"""Recommend configured projects and practices for a save. Nothing here is executed."""

import json
import os
from datetime import datetime, timezone

from lib import db
from lib.classify import (
    _github_repo_candidates,
    _item_search_text,
    _matching_keywords,
    _normalize_search_text,
    _search_text_contains_phrase,
    _workstream_aliases,
    helped_by_phrases,
)


_RECOMMENDATION = "Recommendation only. Nothing is changed, opened, or sent."
_NO_FIT_ACTION = "No useful match. Leave this save unmatched. Nothing was filed."


def catalog_from_config(config):
    projects = [ws for ws in config.get("workstreams", []) if ws.get("active", True) is not False]
    practices = list(config.get("practices") or [])
    gaps = []
    if "practices" not in config:
        gaps.append("practices is not configured. Ways of working have no evidence list.")
    invalid = []
    usable = []
    for practice in practices:
        if not isinstance(practice, dict) or not practice.get("slug") or not practice.get("evidence") or not practice.get("application"):
            invalid.append(practice.get("slug") if isinstance(practice, dict) else None)
            continue
        usable.append(practice)
    if invalid:
        gaps.append("Some practices are missing a slug, evidence list, or application.")
    uncovered = []
    for project in projects:
        named = _normalize_search_text(project.get("name"))
        if project.get("keywords") or helped_by_phrases(project.get("description")) or (named and len(named) >= 5):
            continue
        uncovered.append(project.get("slug"))
    if uncovered:
        gaps.append("These active projects have no keywords, Helped by phrase, or usable name: " + ", ".join(uncovered))
    return projects, usable, {"gaps": gaps, "uncovered_projects": uncovered, "inactive_skipped": _inactive_count(config)}


def match_item(item, projects, practices):
    blocked = _abstain_reason(item)
    if blocked:
        return {
            "outcome": "abstain",
            "reason": blocked,
            "recommendations": [],
            "next_action": f"{blocked} Nothing was filed.",
        }
    if not projects and not practices:
        return {
            "outcome": "insufficient_context",
            "reason": "No project descriptions, keywords, or practices are configured.",
            "recommendations": [],
            "next_action": "No project or practice is configured. Leave this save unmatched. Nothing was filed.",
        }
    search = _item_search_text(item)
    recommendations = []
    for project in projects:
        recommendation = _project_recommendation(item, project, search)
        if recommendation:
            recommendations.append(recommendation)
    for practice in practices:
        recommendation = _practice_recommendation(item, practice, search)
        if recommendation:
            recommendations.append(recommendation)
    recommendations.sort(key=lambda row: (-row["confidence"], row["target_kind"], row["target_slug"]))
    if not recommendations:
        return {
            "outcome": "no_fit",
            "reason": "No configured project or practice evidence appears in this save.",
            "recommendations": [],
            "next_action": _NO_FIT_ACTION,
        }
    if all(row["disposition"] == "reference" for row in recommendations):
        outcome = "reference"
        reason = f"{len(recommendations)} reference match(es). No task was filed."
    else:
        outcome = "matched"
        reason = f"Matched {len(recommendations)} configured target(s). No task was filed."
    return {
        "outcome": outcome,
        "reason": reason,
        "recommendations": recommendations,
        "next_action": recommendations[0]["next_action"],
    }


def match_catalog(db_path, projects, practices, write=False, repo_root=None):
    projects, context = context_report(projects, repo_root)
    if not db_path:
        raise ValueError("Database path is required.")
    conn = db.get_connection(db_path)
    matched_at = datetime.now(timezone.utc).isoformat()
    try:
        rows = conn.execute(
            """SELECT id, source, external_id, title, body, url, author, status, raw_json
               FROM items ORDER BY id"""
        ).fetchall()
        results = []
        counts = {"examined": 0, "matched": 0, "reference": 0, "no_fit": 0, "abstain": 0, "insufficient_context": 0}
        for row in rows:
            item = dict(row)
            decision = match_item(item, projects, practices)
            counts["examined"] += 1
            counts[decision["outcome"]] += 1
            results.append({"item_id": item["id"], **decision})
            if write:
                _store(conn, item["id"], decision, matched_at)
        if write:
            conn.commit()
        return {"write": bool(write), "executed": False, "counts": counts, "context": context, "results": results}
    finally:
        conn.close()


def _inactive_count(config):
    return sum(1 for ws in config.get("workstreams", []) if ws.get("active", True) is False)


def _abstain_reason(item):
    raw = _raw(item)
    text = (item.get("body") or item.get("title") or "").strip()
    signals = raw.get("x_signals") if isinstance(raw.get("x_signals"), dict) else {}
    bookmarked = item.get("source") == "x" or "bookmark" in signals
    if item.get("status") == "pending":
        return "Post text is not here yet. Not matched."
    if item.get("source") == "x-like":
        return "Liked only. Not matched to a project."
    if raw.get("echo") and not bookmarked:
        return "Already sent to Reader by Beacon. Not a new save."
    if raw.get("content_quality") == "title_only" and not bookmarked:
        return "Title only. Not enough text to match."
    if raw.get("content_quality") == "empty" or not text:
        return "No text. Not matched."
    if item.get("status") == "interest" and not bookmarked:
        return "Not an open save. Not matched."
    return None


def context_report(projects, repo_root):
    """Read beacon-context.md beside a backlog when that file exists. Do not invent phrases."""
    attached = []
    reports = []
    for project in projects:
        phrases, report = _load_context(project, repo_root)
        attached.append({**project, "context_phrases": phrases})
        reports.append(report)
    return attached, reports


def _expected_context_path(project):
    backlog = os.path.expanduser(project.get("backlog_path") or "")
    if not backlog:
        return ""
    return os.path.join(os.path.dirname(backlog), "beacon-context.md")


def _load_context(project, repo_root):
    expected = _expected_context_path(project)
    candidates = [expected]
    if repo_root and project.get("slug") == "beacon":
        candidates.append(os.path.join(repo_root, "docs", "beacon-context.md"))
    for path in candidates:
        if path and os.path.isfile(path):
            return _context_bullets(path), {"slug": project.get("slug"), "path": path, "loaded": True}
    return [], {"slug": project.get("slug"), "path": expected, "loaded": False}


def _context_bullets(path):
    phrases = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("- "):
                continue
            phrase = line[2:].strip()
            if len(_normalize_search_text(phrase).split()) >= 2:
                phrases.append(phrase)
    return phrases


def _project_recommendation(item, project, search):
    keywords = []
    matched = set(_matching_keywords(item, project))
    for keyword in project.get("keywords") or []:
        if _normalize_search_text(keyword) in matched:
            keywords.append(keyword)
    for phrase in project.get("context_phrases") or []:
        if _search_text_contains_phrase(search, _normalize_search_text(phrase)):
            keywords.append(phrase)
    helped = [
        phrase for phrase in helped_by_phrases(project.get("description"))
        if _search_text_contains_phrase(search, _normalize_search_text(phrase))
    ]
    name = _normalize_search_text(project.get("name"))
    named = bool(name) and len(name) >= 5 and _search_text_contains_phrase(search, name)
    repo = bool(_github_repo_candidates(item) & _workstream_aliases(project)) if item.get("source") == "github" else False
    if not keywords and not helped and not named and not repo:
        return None
    evidence = list(dict.fromkeys([*keywords, *helped]))
    kinds = []
    if keywords:
        kinds.append("keyword")
    if helped:
        kinds.append("helped_by")
    if named:
        kinds.append("name")
        evidence.append(name)
    if repo:
        kinds.append("repo")
        evidence.append("repo")
    description = project.get("description") or ""
    reference_notes = "reference notes" in description.lower()
    if repo or (("name" in kinds) and ("keyword" in kinds or "helped_by" in kinds) and not reference_notes):
        disposition = "apply"
    else:
        disposition = "reference"
    confidence = 0.6
    if repo:
        confidence = 0.95
    elif len([kind for kind in kinds if kind != "name"]) >= 2 or ("name" in kinds and len(kinds) > 1):
        confidence = 0.9
    elif keywords or helped:
        confidence = 0.8
    shown = ", ".join(f'"{phrase}"' for phrase in evidence if phrase != "repo")
    if repo and not shown:
        shown = "the repo id"
    name = project.get("name")
    slug = project.get("slug")
    if shown == "the repo id":
        explanation = f"{name} ({slug}) fits because this save is that project's configured GitHub repo."
    else:
        explanation = f"{name} ({slug}) fits because this save contains configured evidence {shown}."
    purpose = _purpose(description)
    if purpose:
        explanation += f" {name} is {purpose}."
    next_action = project_next_action(item, project, disposition, evidence)
    return _recommendation(item, "project", slug, name, disposition, confidence, explanation, next_action, evidence)


def _practice_recommendation(item, practice, search):
    hits = []
    for phrase in practice.get("evidence") or []:
        normalized = _normalize_search_text(phrase)
        if normalized and _search_text_contains_phrase(search, normalized):
            hits.append(normalized)
    if not hits:
        return None
    disposition = practice.get("disposition") if practice.get("disposition") in ("apply", "reference") else "reference"
    confidence = 0.9 if len(hits) > 1 else 0.8
    shown = ", ".join(f'"{phrase}"' for phrase in hits)
    explanation = (
        f"{practice.get('name')} ({practice.get('slug')}) fits because this save contains configured evidence {shown}."
    )
    next_action = practice_next_action(item, practice)
    return _recommendation(item, "practice", practice.get("slug"), practice.get("name"), disposition, confidence, explanation, next_action, hits)


def _claim(item):
    text = item.get("title") or item.get("body") or ""
    text = " ".join(str(text).replace('"', "'").split())
    if len(text) > 90:
        clipped = text[:87].rsplit(" ", 1)[0]
        text = (clipped or text[:87]) + "..."
    return text or "this save"


def _purpose(description):
    if not isinstance(description, str) or not description.strip():
        return ""
    head = description.split("Helped by", 1)[0].strip(" .;")
    head = " ".join(head.split())
    if len(head) > 180:
        shortened = head[:177].rsplit(" ", 1)[0]
        head = shortened or head[:177]
    return head


def project_next_action(item, project, disposition, evidence):
    """One doable step for a configured project. The step is not performed here."""
    name = project.get("name") or project.get("slug") or "this project"
    claim = _claim(item)
    normalized_name = _normalize_search_text(project.get("name"))
    phrase = next(
        (
            part for part in evidence
            if part and part != "repo" and _normalize_search_text(part) != normalized_name
        ),
        None,
    )
    if disposition == "apply" and phrase is None and "repo" in evidence:
        step = (
            f"In {name}, compare this repo with the current work and write down one difference. "
            "Stop after the note. Do not open a pull request."
        )
    elif disposition == "apply":
        topic = f'"{phrase}" from ' if phrase else ""
        step = f'In {name}, write one note on applying {topic}"{claim}". Stop after the note.'
    else:
        topic = f' for "{phrase}"' if phrase else ""
        step = (
            f'Keep "{claim}" as a {name} reference{topic}. '
            f"Open it the next time you work on {name}. Do not open a task."
        )
    return f"{step} {_RECOMMENDATION}"


def practice_next_action(item, practice):
    claim = _claim(item)
    application = (practice.get("application") or "Compare this save with the practice.").strip()
    return f'{application} Ground it in this save: "{claim}". {_RECOMMENDATION}'


def assigned_next_action(item, workstream):
    """Next step for a save already assigned to a project. Does not file or start that step."""
    blocked = _abstain_reason(item)
    if blocked:
        return f"{blocked} Nothing was filed."
    if (item.get("intent") or "") == "non-technical":
        return f'No useful match for {workstream.get("name")}. Leave "{_claim(item)}" unmatched. Nothing was filed.'
    recommendation = _project_recommendation(item, workstream, _item_search_text(item))
    if recommendation:
        return recommendation["next_action"]
    return project_next_action(item, workstream, "reference", [])


def _recommendation(item, kind, slug, name, disposition, confidence, explanation, next_action, evidence):
    return {
        "target_kind": kind,
        "target_slug": slug,
        "target_name": name,
        "disposition": disposition,
        "confidence": confidence,
        "explanation": explanation,
        "next_action": next_action,
        "evidence": evidence,
        "citation": {
            "source": item.get("source"),
            "external_id": item.get("external_id"),
            "url": item.get("url"),
        },
    }


def _store(conn, item_id, decision, matched_at):
    conn.execute("DELETE FROM match_recommendations WHERE item_id=?", (item_id,))
    for row in decision["recommendations"]:
        conn.execute(
            """INSERT INTO match_recommendations
               (item_id, target_kind, target_slug, disposition, confidence, explanation, next_action,
                evidence_json, citation_json, matched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, row["target_kind"], row["target_slug"], row["disposition"],
             row["confidence"], row["explanation"], row["next_action"],
             json.dumps(row["evidence"], ensure_ascii=False), json.dumps(row["citation"], ensure_ascii=False),
             matched_at),
        )
    conn.execute(
        """INSERT INTO match_outcomes (item_id, outcome, reason, matched_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET outcome=excluded.outcome, reason=excluded.reason, matched_at=excluded.matched_at""",
        (item_id, decision["outcome"], decision["reason"], matched_at),
    )


def _raw(item):
    try:
        raw = json.loads(item.get("raw_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}
