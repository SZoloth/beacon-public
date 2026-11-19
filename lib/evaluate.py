"""Build read-only human-label baselines and run receipts for Beacon."""

import json
import hashlib
import random
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import db, settings
from . import useful


DEFAULT_STATUSES = ("adopted", "tried", "reviewed", "queued", "stale")
LABEL_FIELDS = (
    "destination_fit",
    "intended_use",
    "action_quality",
    "confidence",
    "approval_required",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _select_stratified(candidates, size, seed):
    by_status = defaultdict(list)
    for candidate in candidates:
        by_status[candidate["status"]].append(candidate)

    rng = random.Random(seed)
    for items in by_status.values():
        rng.shuffle(items)

    available_statuses = [status for status in DEFAULT_STATUSES if by_status[status]]
    selected = []
    while len(selected) < size:
        added = False
        for status in available_statuses:
            if by_status[status] and len(selected) < size:
                selected.append(by_status[status].pop())
                added = True
        if not added:
            break
    return selected


def _baseline_item(candidate):
    workstreams = [
        value for value in (candidate.get("workstreams") or "").split(",") if value
    ]
    return {
        "id": candidate["id"],
        "source": candidate["source"],
        "external_id": candidate["external_id"],
        "title": candidate["title"],
        "url": candidate["url"],
        "author": candidate["author"],
        "current_status": candidate["status"],
        "current_intent": candidate["intent"],
        "current_workstreams": workstreams,
        "current_one_liner": candidate["one_liner"],
        "status_note": candidate["status_note"],
        "saved_at": candidate["saved_at"],
        "synced_at": candidate["synced_at"],
        "label_state": "pending_sam",
        "labels": {field: None for field in LABEL_FIELDS},
    }


def write_baseline_bundle(output_dir, size=30, seed=20260713):
    """Write a stable review set and receipt without changing Beacon data."""
    output_path = Path(output_dir).expanduser()
    output_path.mkdir(parents=True, exist_ok=True)

    generated_at = _now_iso()
    stamp = generated_at[:10]
    baseline_path = output_path / f"beacon-baseline-{stamp}.json"
    receipt_path = output_path / f"beacon-baseline-receipt-{stamp}.json"

    candidates = db.get_evaluation_candidates(DEFAULT_STATUSES)
    selected = _select_stratified(candidates, size=size, seed=seed)
    items = [_baseline_item(candidate) for candidate in selected]
    distribution = Counter(item["current_status"] for item in items)
    present_statuses = {candidate["status"] for candidate in candidates}
    missing_statuses = [
        status for status in DEFAULT_STATUSES if status not in present_statuses
    ]

    baseline = {
        "schema_version": 1,
        "generated_at": generated_at,
        "seed": seed,
        "requested_size": size,
        "sample_size": len(items),
        "requested_statuses": list(DEFAULT_STATUSES),
        "status_distribution": dict(sorted(distribution.items())),
        "label_instructions": {
            "destination_fit": "Does the current project destination fit?",
            "intended_use": "What should this source change or support?",
            "action_quality": "Is the proposed action specific, bounded, and useful?",
            "confidence": "Confidence from 0.0 to 1.0.",
            "approval_required": f"Does execution need {settings.owner()}'s approval?",
        },
        "items": items,
    }
    baseline_path.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    receipt = {
        "schema_version": 1,
        "generated_at": generated_at,
        "run_type": "evaluation_baseline",
        "inputs": {
            "database": db.current_path(),
            "requested_size": size,
            "seed": seed,
            "statuses": list(DEFAULT_STATUSES),
        },
        "evidence": {
            "candidate_count": len(candidates),
            "sample_size": len(items),
            "status_distribution": dict(sorted(distribution.items())),
            "baseline_path": str(baseline_path),
        },
        "routing_decisions": [],
        "unresolved": {
            "missing_statuses": missing_statuses,
            "pending_sam_labels": len(items),
        },
        "changed_files": [str(baseline_path), str(receipt_path)],
    }
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "baseline_path": str(baseline_path),
        "receipt_path": str(receipt_path),
        "sample_size": len(items),
        "missing_statuses": missing_statuses,
    }


def evaluate_useful_fixture(fixture_path, *, provider="local", allow_upload=False):
    """Run the frozen synthetic smoke set in disposable databases, never beacon.db."""
    payload = Path(fixture_path).read_bytes()
    if len(payload) > 100000:
        raise ValueError("Evaluation fixture exceeds 100 KB.")
    fixture = json.loads(payload)
    if provider not in {"local", "typesafe"}:
        raise ValueError("Unknown evaluation provider.")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 20:
        raise ValueError("Evaluation needs 1–20 cases.")
    for case in cases:
        useful.task_terms(case["task"])
        items = case["items"]
        useful._limit(len(items), 20)
        ids = {item["id"] for item in items}
        if len(ids) != len(items) or any(type(value) is not int for value in ids):
            raise ValueError("Fixture item IDs must be unique integers.")
        if not set(case["expected_useful_ids"]).issubset(ids):
            raise ValueError("Fixture labels refer to absent resources.")
        if case["split"] not in {"calibration", "held_out"}:
            raise ValueError("Fixture split must be calibration or held_out.")
    results = []
    input_tokens = output_tokens = live_calls = 0
    with tempfile.TemporaryDirectory(prefix="beacon-useful-eval-") as directory:
        for index, case in enumerate(cases):
            path = Path(directory) / f"{index}.db"
            # Reuse Beacon's schema/FTS definitions; add only the two lifecycle
            # columns required by retrieval in this isolated synthetic fixture.
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.executescript(db.SCHEMA + db.FTS_SCHEMA + db.FTS_TRIGGERS)
                conn.execute("ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'queued'")
                conn.execute("ALTER TABLE items ADD COLUMN saved_at TEXT")
                conn.execute("INSERT INTO workstreams (id,slug,name) VALUES (1,'fixture','Synthetic')")
                for item in case["items"]:
                    conn.execute("INSERT INTO items (id,source,external_id,title,body,url,synced_at) VALUES (?,'manual',?,?,?,?, '2026-09-20')",
                                 (item["id"], str(item["id"]), item["title"], item["body"], f"https://example.com/{item['id']}"))
                    conn.execute("INSERT INTO item_workstreams (item_id,workstream_id) VALUES (?,1)", (item["id"],))
            started = time.monotonic()
            candidates = useful.retrieve_candidates(case["task"], path, workstream="fixture", limit=8)
            if provider == "typesafe":
                request = useful.build_request(case["task"], candidates)
                live_calls += 1
                response = useful.call_typesafe(request, allow_upload=allow_upload)
                result = useful.rank_response(request, candidates, response)
                input_tokens += result["usage"]["input_tokens"]
                output_tokens += result["usage"]["output_tokens"]
            else:
                result = useful.local_result(candidates)
            predicted = {row["id"] for row in result["matches"]}
            expected = set(case["expected_useful_ids"])
            retrieved = {row["id"] for row in candidates}
            results.append({"id": case["id"], "split": case["split"],
                            "predicted_ids": sorted(predicted), "expected_ids": sorted(expected),
                            "retrieved_relevant": len(expected & retrieved), "relevant_count": len(expected),
                            "true_suggestions": len(predicted & expected), "false_suggestions": len(predicted - expected),
                            "missed_useful": len(expected - predicted), "review_count": len(result["review"]),
                            "exact_match": predicted == expected,
                            "elapsed_ms": round((time.monotonic() - started) * 1000, 2)})
    true = sum(row["true_suggestions"] for row in results)
    false = sum(row["false_suggestions"] for row in results)
    relevant = sum(row["relevant_count"] for row in results)
    return {"schema_version": 1, "project": "beacon", "mode": "typesafe_advisory" if provider == "typesafe" else "local_baseline",
            "evidence": "synthetic_smoke_only", "label_status": fixture.get("label_status", "unverified"),
            "fixture_sha256": hashlib.sha256(payload).hexdigest(), "rubric_version": useful.RUBRIC_VERSION,
            "case_count": len(results), "exact_match_cases": sum(row["exact_match"] for row in results),
            "candidate_recall": sum(row["retrieved_relevant"] for row in results) / relevant if relevant else None,
            "precision": true / (true + false) if true + false else None,
            "false_suggestions": false, "missed_useful": sum(row["missed_useful"] for row in results),
            "live_model_calls": live_calls, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "estimated_cost_usd": input_tokens * useful.PRICE_PER_MILLION / 1000000,
            "cases": results}
