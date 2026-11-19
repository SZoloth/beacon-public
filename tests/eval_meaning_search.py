"""Frozen invented search examples, separate from private-library acceptance."""

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import db, search, useful


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-upload", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.live and not args.allow_upload:
        parser.error("Live evaluation requires --allow-upload for these invented examples.")
    fixture_path = Path(__file__).parent / "fixtures" / "meaning-search.json"
    raw = fixture_path.read_bytes()
    fixture = json.loads(raw)
    if fixture["scope"] != "invented saved-resource meaning-search calibration; no personal library data" or len(fixture["cases"]) != 6:
        raise ValueError("Unexpected evaluation scope or size.")
    trace = []
    real_call = useful.call_typesafe

    def tracked(request, **options):
        if not args.live or len(trace) >= 12:
            raise ValueError("Live evaluation is off or its 12-attempt budget is exhausted.")
        record = {"case_id": active_case, "request": request, "status": "attempted"}
        trace.append(record)
        start = time.monotonic()
        response = real_call(request, **options)
        record.update(status="received", response=response, elapsed_ms=round((time.monotonic() - start) * 1000, 2))
        return response

    report = {"project": "beacon", "mode": "live" if args.live else "preview",
              "model": useful.MODEL, "fixture_sha256": hashlib.sha256(raw).hexdigest(),
              "scope": fixture["scope"], "cases": [], "trace": trace,
              "limitation": "Authored calibration and held-out examples, not representative library accuracy or time savings."}
    active_case = None
    try:
        with tempfile.TemporaryDirectory(prefix="beacon-meaning-eval-") as folder:
            prepared = []
            for case in fixture["cases"]:
                path = Path(folder) / (case["id"] + ".db")
                with patch.object(db, "DB_PATH", str(path)):
                    db.init_db()
                with closing(sqlite3.connect(path)) as conn, conn:
                    for item in case["items"]:
                        conn.execute("INSERT INTO items (id,source,external_id,title,body,url,synced_at,status) "
                                     "VALUES (?, 'manual', ?, ?, ?, ?, '2026-09-20T00:00:00Z', ?)",
                                     (item["id"], str(item["id"]), item["title"], item["body"],
                                      f"https://example.com/invented/{item['id']}", item["status"]))
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                preview = search.semantic_search(case["query"], path, preview=True)
                baseline = useful.retrieve_candidates(case["query"], path, limit=20)
                prepared.append((case, path, before, preview, baseline))
            planned = sum(len(entry[3]["requests"]) for entry in prepared)
            if planned > 12:
                raise ValueError("Planned requests exceed the frozen evaluation budget.")
            report["planned_calls"] = planned
            with patch.object(useful, "call_typesafe", side_effect=tracked):
                for case, path, before, preview, baseline in prepared:
                    active_case = case["id"]
                    start = time.monotonic()
                    result = search.semantic_search(case["query"], path, allow_upload=True) if args.live else preview
                    unchanged = before == hashlib.sha256(path.read_bytes()).hexdigest()
                    if not unchanged:
                        raise AssertionError("Evaluation changed its source database.")
                    row = {"id": case["id"], "split": case["split"], "expected_matches": case["expected_matches"],
                           "baseline_candidate_ids": [item["id"] for item in baseline], "database_unchanged": unchanged,
                           "result": result, "elapsed_ms": round((time.monotonic() - start) * 1000, 2)}
                    if args.live:
                        matches = {item["id"] for item in result["matches"]}
                        expected = set(case["expected_matches"])
                        row.update(false_matches=sorted(matches - expected), missed_matches=sorted(expected - matches),
                                   exact_match=matches == expected)
                    report["cases"].append(row)
            report["completed"] = True
    except Exception as error:
        report.update(completed=False, error_type=type(error).__name__, error="Evaluation incomplete; do not accept a quality result. Earlier calls may have been billed.")
    received = [row["response"] for row in trace if row["status"] == "received"]
    report["attempted_calls"] = len(trace)
    report["received_calls"] = len(received)
    known_usage = [response["usage"] for response in received
                   if isinstance(response, dict) and isinstance(response.get("usage"), dict)
                   and all(type(response["usage"].get(key)) is int and response["usage"][key] >= 0
                           for key in ("input_tokens", "output_tokens"))]
    report["usage_unknown_calls"] = len(trace) - len(known_usage)
    report["usage"] = {key: sum(usage[key] for usage in known_usage) for key in ("input_tokens", "output_tokens")}
    report["estimated_cost_usd"] = report["usage"]["input_tokens"] * useful.PRICE_PER_MILLION / 1_000_000
    report["cost_scope"] = "Known usage only; failed or malformed calls may have additional charges."
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf8") as handle:
            handle.write(output)
    print(output)
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
