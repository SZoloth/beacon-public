"""Frozen, bounded synthetic evaluation. Never reads or writes the live intake DB."""
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from . import intake, review, useful

FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/intake-routing-v2.json"


def evaluate(*, live=False, allow_upload=False):
    payload = FIXTURE.read_bytes()
    fixture = json.loads(payload)
    workstreams = fixture["workstreams"]
    plans = []
    for index, case in enumerate(fixture["cases"]):
        item = {"id": index + 1, "source": "manual", "external_id": case["id"],
                "title": case["title"], "excerpt": case["body"], "url": "https://example.test/" + case["id"]}
        plans.append((case, review.prepare_triage([item], workstreams)))
    requests = [plan["request"] for _, plan in plans]
    pair_requests = [review.build_comparison(case["items"], case["task"]) for case in fixture["pairs"]]
    # Conservative pre-call input allowance: each question can repeat the entire
    # encoded request. No retries and a fixed fixture bound the whole experiment.
    ceiling = sum(len(useful.encode_request(r)) * len(r["questions"]) for r in requests + pair_requests)
    if len(requests + pair_requests) > 20 or ceiling * useful.PRICE_PER_MILLION / 1_000_000 > 0.10:
        raise ValueError("Frozen evaluation exceeds its 20-call / $0.10 conservative input allowance.")
    rows, pairs, timings = [], [], []
    usage = {"input_tokens": 0, "output_tokens": 0}
    calls = 0

    def call(request):
        nonlocal calls
        if not live:
            return None
        calls += 1
        started = time.monotonic()
        try:
            response = useful.call_typesafe(request, allow_upload=allow_upload)
            useful.validate_response(request, response)
            for key in usage:
                usage[key] += response["usage"][key]
            return response
        finally:
            timings.append(round((time.monotonic() - started) * 1000, 2))

    for case, plan in plans:
        row = {"id": case["id"], "split": case["split"], "expected": case["expected"], "request": plan["request"]}
        baseline = plan["decisions"][0]["deterministic_baseline"]
        row["baseline_destinations"] = sorted(ws["slug"] for ws in (baseline or {}).get("workstreams", []))
        try:
            response = call(plan["request"])
            result = review.triage_result(plan, response)
            decision = intake.routing_decision(result["decisions"][0], semantic_routing=live)
            predicted = sorted(decision["workstreams"]) if decision["assessment"] == "approved" else []
            row.update(response=response, decision=decision, predicted=predicted,
                       false_routes=len(set(predicted) - set(case["expected"])),
                       missed_routes=len(set(case["expected"]) - set(predicted)), error=None)
        except (ValueError, OSError) as error:
            row.update(error=str(error), predicted=[], false_routes=0, missed_routes=len(case["expected"]))
        rows.append(row)
    for case, request in zip(fixture["pairs"], pair_requests):
        row = {"id": case["id"], "expected": case["expected"], "request": request}
        try:
            response = call(request)
            result = review.comparison_result(case["items"], request, response)
            row.update(response=response, result=result, correct=result["relationship"] == case["expected"], error=None)
        except (ValueError, OSError) as error:
            row.update(error=str(error), correct=False)
        pairs.append(row)
    held = [row for row in rows if row["split"] == "held_out"]
    false = sum(row["false_routes"] for row in held)
    correct = sum(bool(row["predicted"]) and set(row["predicted"]) == set(row["expected"]) for row in held)
    errors = sum(bool(row["error"]) for row in rows + pairs)
    return {"project": "beacon", "mode": "typesafe_live" if live else "local_baseline", "model": useful.MODEL,
            "generated_at": datetime.now(timezone.utc).isoformat(), "evidence": "synthetic_only_not_production_calibration",
            "fixture_sha256": hashlib.sha256(payload).hexdigest(), "label_status": fixture["label_status"],
            "policy": intake.POLICY, "policy_version": intake.POLICY_VERSION, "rubric_version": "intake-triage-v1",
            "baseline_scope": "existing_deterministic_classifier_on_same_capped_input_not_full_llm_classifier",
            "routing_gate": {"passed": live and not errors and false == 0 and correct >= 4 and len(held) >= 8,
                             "held_out_cases": len(held), "false_routes": false, "correct_routes": correct,
                             "missed_routes": sum(row["missed_routes"] for row in held)},
            "comparison_correct": sum(row["correct"] for row in pairs), "comparison_cases": len(pairs),
            "errors": errors, "live_model_calls": calls, "usage": usage,
            "estimated_cost_usd": usage["input_tokens"] * useful.PRICE_PER_MILLION / 1_000_000,
            "latency_ms": {"median": statistics.median(timings) if timings else None, "max": max(timings, default=None)},
            "cases": rows, "pairs": pairs}
