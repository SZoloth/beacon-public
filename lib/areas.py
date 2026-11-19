"""Areas: lifestyle shelves (Style, Home, Food, ...) for saves that belong to no code project.

Project routing scores fit against each codebase and drops "non-technical" saves. Areas catch
those: one Jev choice question per held save ("which area, if any?"), stored in area_reviews so a
save is asked about once. Saves above the floor then fill each area up to k active at a time,
like route-top does for projects. Areas get no backlog file, no brief and no agent; triage shows
them with Keep instead of Do.
"""
import json
from contextlib import closing
from datetime import datetime, timezone

from . import db, intake, review, useful

NONE = "none"
MAX_PER_REQUEST = 20


def _question(index, areas):
    criteria = {area["slug"]: f"{area['name']}: {area['description']}" for area in areas}
    criteria[NONE] = "None of these: work or code material, or nothing worth keeping."
    return {"type": "choice", "criteria": criteria,
            "instructions": review.UNTRUSTED + f"Which personal shelf does `items[{index}]` belong on?"}


def _held(limit):
    """Held saves never asked about, newest first."""
    with closing(db.get_connection()) as conn:
        rows = conn.execute(
            "SELECT i.id, i.title, i.body FROM items i LEFT JOIN area_reviews a ON a.item_id = i.id "
            "WHERE i.status = 'new' AND a.item_id IS NULL ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": row["id"], "title": (row["title"] or "")[:240], "excerpt": (row["body"] or "")[:800]} for row in rows]


def _request(items, areas):
    questions = {f"area_{item['id']}": _question(index, areas) for index, item in enumerate(items)}
    return {"model": useful.MODEL, "state": {"items": [review._source(item) for item in items]}, "questions": questions}


def _save(items, request, response):
    """Store one answer per save; raises ValueError (nothing saved) if any answer is unreadable."""
    answers, usage = useful.validate_response(request, response)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for item in items:
        key = f"area_{item['id']}"
        answer = answers[key]
        probabilities = answer.get("probabilities") if isinstance(answer, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if not isinstance(probabilities, dict) or choice not in probabilities or choice not in request["questions"][key]["criteria"]:
            raise ValueError("Jev returned an area answer Beacon can't read.")
        rows.append((item["id"], choice, useful._number(probabilities[choice], 0, 1), now))
    with closing(db.get_connection()) as conn, conn:
        conn.executemany("INSERT OR REPLACE INTO area_reviews VALUES(?,?,?,?)", rows)
    return usage


def _fill(k, floor):
    """Queue the best-fitting reviewed saves into each area, up to k active per area."""
    assigned = 0
    now = datetime.now(timezone.utc).isoformat()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        areas = {row["slug"]: row["id"] for row in conn.execute("SELECT id, slug FROM workstreams WHERE kind='area' AND active=1")}
        statuses = ",".join("?" * len(intake.ACTIVE_STATUSES))
        for slug, workstream_id in areas.items():
            in_use = conn.execute(
                f"SELECT COUNT(*) FROM item_workstreams iw JOIN items i ON i.id = iw.item_id "
                f"WHERE iw.workstream_id = ? AND i.status IN ({statuses})", (workstream_id, *intake.ACTIVE_STATUSES)).fetchone()[0]
            candidates = conn.execute(
                "SELECT a.item_id, a.probability FROM area_reviews a JOIN items i ON i.id = a.item_id "
                "WHERE a.area = ? AND a.probability >= ? AND i.status = 'new' ORDER BY a.probability DESC, a.item_id LIMIT ?",
                (slug, floor, max(0, k - in_use))).fetchall()
            for row in candidates:
                # routed=1: areas have no backlog file to write.
                conn.execute("INSERT OR IGNORE INTO item_workstreams(item_id, workstream_id, confidence, routed) VALUES(?,?,?,1)",
                             (row["item_id"], workstream_id, row["probability"]))
                conn.execute("UPDATE items SET status='queued', status_at=? WHERE id=? AND status='new'", (now, row["item_id"]))
                assigned += 1
    return assigned


def assign(areas, *, limit=1000, allow_upload=False, k=25, floor=0.6):
    """Ask Jev about up to `limit` held saves, then fill each area. Returns counts and Jev usage."""
    areas = [area for area in areas if area.get("active", True)]
    totals = {"reviewed": 0, "assigned": 0, "errors": 0, "input_tokens": 0}
    pending = _held(limit) if areas else []
    batches = useful.pack_batches(pending, lambda batch: useful.encode_request(_request(batch, areas)), MAX_PER_REQUEST)
    for batch in batches:
        request = _request(batch, areas)
        response = useful.call_typesafe(request, allow_upload=allow_upload)  # a service failure stops the run
        try:
            usage = _save(batch, request, response)
        except ValueError:
            totals["errors"] += 1  # this batch stays unreviewed and is asked again next run
            continue
        totals["reviewed"] += len(batch)
        totals["input_tokens"] += usage["input_tokens"]
    totals["assigned"] = _fill(k, floor)
    totals["jev_cost_usd"] = totals["input_tokens"] * useful.PRICE_PER_MILLION / 1_000_000
    return totals
