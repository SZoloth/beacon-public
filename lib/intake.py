"""Bounded TypeSafe intake integrated with the existing classification/router."""
import hashlib
import json
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import classify, db, review, useful

POLICY_VERSION = "typed-routing-v2"
# Frozen before the fresh v2 holdout. Calibration positive=.88, negatives<=.04;
# old v1 holdout is retired, never relabeled as fresh evaluation evidence.
POLICY = {"fit_min": 0.85, "other_fit_max": 0.1, "intent_min": 0.9, "confidence_min": 0.8}
# ~$0.0002 per item at current pricing, so one full run stays well under $1.
MAX_CLASSIFY_LIMIT = 1000
SOURCE_FIELDS = ("id", "source", "external_id", "title", "body", "url", "author")


def source_hash(item):
    value = {key: item.get(key) for key in SOURCE_FIELDS}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def routing_decision(row, *, semantic_routing=False):
    result = {**row, "routing_policy": POLICY_VERSION, "reasons": list(row.get("reasons", []))}
    if row.get("method") == "existing_exact_match" and row.get("workstreams"):
        result["assessment"] = "approved"
    elif row.get("assessment") == "proposed":
        intent = row["intent_judgment"]
        fits = row["destination_probabilities"]
        certain = (intent["confidence"] >= POLICY["confidence_min"]
                   and intent["probabilities"][intent["choice"]] >= POLICY["intent_min"]
                   and all(p >= POLICY["fit_min"] if slug in row["workstreams"] else p <= POLICY["other_fit_max"] for slug, p in fits.items()))
        result["assessment"] = "approved" if semantic_routing and certain else "review"
        if result["assessment"] == "review":
            result["reasons"].append("Automatic routing is disabled or its stricter thresholds are not met.")
    return result


def load_routing_evaluation(path):
    """A local evaluation is an activation prerequisite, not universal accuracy proof."""
    report = json.loads(Path(path).read_text())
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/intake-routing-v2.json"
    gate = report.get("routing_gate", {})
    if (report.get("model") != useful.MODEL or report.get("policy") != POLICY
            or report.get("policy_version") != POLICY_VERSION
            or report.get("fixture_sha256") != hashlib.sha256(fixture.read_bytes()).hexdigest()
            or report.get("rubric_version") != "intake-triage-v1" or report.get("errors") != 0
            or report.get("mode") != "typesafe_live" or gate.get("passed") is not True
            or gate.get("held_out_cases", 0) < 8 or gate.get("false_routes") != 0
            or gate.get("correct_routes", 0) < 4):
        raise ValueError("Routing evaluation does not pass the current pinned policy; semantic routes remain disabled.")
    return report


def persist(decision, expected_hash, workstreams, *, summary=None):
    """Compare and write inside one transaction; stale model results cannot apply."""
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM items WHERE id=?", (decision["id"],)).fetchone()
        if not current or current["status"] != "new" or source_hash(dict(current)) != expected_hash:
            raise ValueError("Source or lifecycle changed during intake; no result applied.")
        if current["classified_at"] is not None:
            raise ValueError("Item was classified concurrently; no result applied.")
        now = datetime.now(timezone.utc).isoformat()
        approved = decision["assessment"] == "approved"
        destinations = decision.get("workstreams", []) if approved else []
        active = {row["slug"]: row["id"] for row in conn.execute("SELECT id,slug FROM workstreams WHERE active=1")}
        if not set(destinations).issubset(active) or not set(destinations).issubset({ws["slug"] for ws in workstreams}):
            raise ValueError("An approved destination is no longer active; no result applied.")
        if approved:
            if conn.execute("SELECT 1 FROM item_workstreams WHERE item_id=?", (decision["id"],)).fetchone():
                raise ValueError("Item already has assignments; keep them for explicit review.")
            metadata = summary or decision.get("deterministic_baseline") or {}
            conn.execute("UPDATE items SET intent=?,classified_at=?,human_read=?,one_liner=COALESCE(one_liner,?) WHERE id=?",
                         (decision["intent"], now, int(bool(metadata.get("human_read", False))), metadata.get("one_liner"), decision["id"]))
            for tag in metadata.get("tags", []):
                if not isinstance(tag, str) or len(tag) > 100:
                    continue
                conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (tag,))
                conn.execute("INSERT OR IGNORE INTO item_tags(item_id,tag_id) SELECT ?,id FROM tags WHERE name=?", (decision["id"], tag))
            for slug in destinations:
                probability = decision.get("destination_probabilities", {}).get(slug, 1.0)
                conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence) VALUES(?,?,?)", (decision["id"], active[slug], probability))
        elif decision["assessment"] == "no_fit" and decision.get("intent"):
            # No project, but keep the intent so `beacon list --intent reference` still finds it.
            conn.execute("UPDATE items SET intent=? WHERE id=?", (decision["intent"], decision["id"]))
        conn.execute("INSERT OR REPLACE INTO intake_reviews VALUES(?,?,?,?,?)",
                     (decision["id"], expected_hash, decision["assessment"], json.dumps(decision, allow_nan=False), now))


def classify_pending(workstreams, *, limit=5, allow_upload=False, semantic_routing=False, batch_size=20, since=None):
    useful._limit(limit, MAX_CLASSIFY_LIMIT)
    active = {ws["slug"] for ws in db.get_workstreams() if ws["active"]}
    workstreams = [ws for ws in workstreams if ws["slug"] in active]
    totals = {"project": "beacon", "classified": 0, "held": 0, "errors": 0, "live_model_calls": 0,
              "summary_generation_calls": 0, "summary_usage": "Legacy generation tokens and price are unavailable; excluded from typed cost.",
              "usage": {"input_tokens": 0, "output_tokens": 0}, "policy": POLICY, "policy_version": POLICY_VERSION}
    started = time.monotonic()
    items = db.get_unclassified_items(limit=limit, since=since)
    by_id = {item["id"]: item for item in items}
    snapshots = [{**item, "title": (item["title"] or "")[:240], "excerpt": (item["body"] or "")[:800]} for item in items]
    # The retained routing calibration judged singletons, not batch neighbors.
    plans = []
    for start in range(0, len(snapshots), 20):
        plans.extend(review.prepare_triage_batches(snapshots[start:start + 20], workstreams,
                                                   batch_size=1 if semantic_routing else batch_size))
    totals["planned_requests"] = sum(plan["request"] is not None for plan in plans)
    for plan in plans:
        response = None
        if plan["request"]:
            response = useful.call_typesafe(plan["request"], allow_upload=allow_upload)
            totals["live_model_calls"] += 1
        try:
            result = review.triage_result(plan, response)
        except ValueError as error:
            # One malformed answer should not abort a 1,000-item run; its items stay pending.
            # Service failures still raise from call_typesafe above.
            totals["errors"] += 1
            print(f"  Skipped {len(plan['decisions'])} item(s): {error}", file=sys.stderr)
            continue
        for key in totals["usage"]:
            totals["usage"][key] += result["usage"][key]
        for row in result["decisions"]:
            item = by_id[row["id"]]
            decision = routing_decision(row, semantic_routing=semantic_routing)
            summary = None
            if decision["assessment"] == "approved" and not item.get("one_liner"):
                if classify._deterministic_classification(item, workstreams) is None:
                    totals["summary_generation_calls"] += 1
                summaries = classify.classify_batch([item], workstreams)
                summary = next((row for row in summaries if row.get("id") == item["id"]), None)
                if summary is None:
                    decision.update(assessment="review", reasons=["Existing summary generation failed; item held."])
            persist(decision, source_hash(item), workstreams, summary=summary)
            totals["classified" if decision["assessment"] == "approved" else "held"] += 1
    totals.update(estimated_cost_usd=totals["usage"]["input_tokens"] * useful.PRICE_PER_MILLION / 1000000,
                  elapsed_ms=round((time.monotonic() - started) * 1000, 2))
    return totals


TOP_K_POLICY = "top-k-v1"
ACTIVE_STATUSES = ("new", "queued", "reviewed", "tried")


def route_top(*, k=25, floor=0.5, dry_run=False):
    """Route each project's highest-scoring held items, up to k active at a time.

    Jev's fit scores on real bookmarks rarely reach the 0.85 automatic-routing bar
    (1 of 10,963 on 2026-09-29), so rank instead of gating. Items routed here stay
    active until reviewed, rejected or swept stale, which frees their slot.
    """
    plan = {}
    now = datetime.now(timezone.utc).isoformat()
    with closing(db.get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        active = {row["slug"]: row["id"] for row in conn.execute("SELECT id,slug FROM workstreams WHERE active=1")}
        in_use = {slug: 0 for slug in active}
        for row in conn.execute(
                "SELECT r.result_json FROM intake_reviews r JOIN items i ON i.id=r.item_id "
                f"WHERE r.disposition='approved' AND i.status IN ({','.join('?' * len(ACTIVE_STATUSES))})", ACTIVE_STATUSES):
            for slug in json.loads(row["result_json"]).get("top_k_workstreams", []):
                if slug in in_use:
                    in_use[slug] += 1
        candidates = {slug: [] for slug in active}
        for row in conn.execute(
                "SELECT r.item_id, r.result_json FROM intake_reviews r JOIN items i ON i.id=r.item_id "
                "WHERE r.disposition!='approved' AND i.status='new' AND i.classified_at IS NULL"):
            result = json.loads(row["result_json"])
            if result.get("intent") in ("non-technical", "unclear"):
                continue
            for slug, fit in (result.get("destination_probabilities") or {}).items():
                if slug in candidates and fit >= floor:
                    candidates[slug].append((row["item_id"], fit))
        for slug, items in candidates.items():
            items.sort(key=lambda pair: (-pair[1], pair[0]))
            chosen = items[:max(0, k - in_use[slug])]
            if chosen:
                plan[slug] = chosen
        if dry_run:
            return plan
        by_item = {}
        for slug, items in plan.items():
            for item_id, fit in items:
                by_item.setdefault(item_id, []).append((slug, fit))
        for item_id, targets in by_item.items():
            review_row = conn.execute("SELECT result_json FROM intake_reviews WHERE item_id=?", (item_id,)).fetchone()
            result = json.loads(review_row["result_json"])
            result.update(routing_policy=TOP_K_POLICY, top_k_workstreams=[slug for slug, _ in targets])
            conn.execute("UPDATE items SET intent=COALESCE(?,intent), classified_at=?, "
                         "one_liner=COALESCE(one_liner, substr(title,1,120)) WHERE id=?",
                         (result.get("intent"), now, item_id))
            for slug, fit in targets:
                conn.execute("INSERT OR IGNORE INTO item_workstreams(item_id,workstream_id,confidence) VALUES(?,?,?)",
                             (item_id, active[slug], fit))
            conn.execute("UPDATE intake_reviews SET disposition='approved', result_json=?, reviewed_at=? WHERE item_id=?",
                         (json.dumps(result, allow_nan=False), now, item_id))
    return plan


def review_queue(path):
    from .review_actions import is_filed
    with closing(review._connect(path)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='intake_reviews'").fetchone():
            return []
        return [{"id": row["item_id"], "reviewed_at": row["reviewed_at"], "result": json.loads(row["result_json"])}
                for row in conn.execute("SELECT * FROM intake_reviews WHERE disposition!='approved' ORDER BY reviewed_at,item_id")
                if not is_filed(conn, f"intake:{row['item_id']}")]
