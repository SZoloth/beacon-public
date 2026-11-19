"""Frozen invented event replay; default is an offline contract test, not model quality."""
import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import conditions, useful

CASES = [
    ("not-met", "The project is a static read-only page. It has no SQLite database and no backup requirement."),
    ("met", "The project now stores data in SQLite and requires consistent backups while writes continue."),
    ("reverted", "The project removed SQLite entirely and no longer needs database backups."),
]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-upload", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.live and not args.allow_upload:
        parser.error("--live requires --allow-upload")
    report = {"rubric": "future-condition-v1", "mode": "live" if args.live else "offline_contract",
              "model": useful.MODEL, "fixture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "cases": [], "calls": []}
    original = useful.call_typesafe
    def tracked(request, *, allow_upload=False):
        assert len(report["calls"]) < 3 and allow_upload
        row = {"request": request}
        report["calls"].append(row)
        row["response"] = original(request, allow_upload=True)
        return row["response"]
    with Path(args.output).open("x") as output, closing(sqlite3.connect(":memory:")) as conn:
        conditions.init_db(conn)
        conditions.save_condition(conn, condition_id="sqlite-backup", project_slug="synthetic",
            title="Revisit the SQLite snapshot guide", authored_by="synthetic-fixture",
            definition={"when": "This project uses SQLite and needs consistent backups while writes continue."})
        item = {"id": 1, "title": "SQLite consistent snapshots", "body": "A guide to consistent online backups.", "status": "queued"}
        try:
            with patch.object(useful, "call_typesafe", side_effect=tracked):
                for index, (expected, summary) in enumerate(CASES):
                    event = {"id": f"event-{index}", "project_slug": "synthetic", "summary": summary}
                    evaluator = conditions.make_typesafe_evaluator(allow_upload=True) if args.live else lambda *_: "not-met" if expected == "reverted" else expected
                    result = conditions.evaluate_event(conn, "sqlite-backup", item, event, evaluator)
                    repeated = conditions.evaluate_event(conn, "sqlite-backup", item, event, evaluator)
                    report["cases"].append({"expected": expected, "actual": result["state"],
                        "unchanged_skipped": repeated["skipped"], "result": result})
                report["active_suggestions_after_revert"] = conditions.list_suggestions(conn, active_only=True)
                report["passed"] = all(row["expected"] == row["actual"] and row["unchanged_skipped"] for row in report["cases"]) and not report["active_suggestions_after_revert"]
        except Exception as error:
            report.update(passed=False, error=type(error).__name__)
        finally:
            output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    if not report["passed"]:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
