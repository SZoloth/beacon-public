"""One frozen synthetic deck through the actual CLI; no personal database is read."""
import argparse
import hashlib
import io
import json
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import beacon
from lib import useful
from test_deck import FIXTURES, SINCE, UNTIL, TASK, seed_database, response_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-upload", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.live and not args.allow_upload:
        parser.error("--live requires --allow-upload")
    report = {"product": "beacon", "rubric": "morning-deck-v1", "model": useful.MODEL,
              "mode": "live_synthetic_smoke" if args.live else "offline_contract", "calls": [],
              "fixture_sha256": hashlib.sha256(json.dumps(FIXTURES).encode()).hexdigest(),
              "expected": {str(item_id): action for item_id, _, _, action in FIXTURES}}
    original = useful.call_typesafe
    def tracked(request, *, allow_upload=False):
        assert not report["calls"] and allow_upload
        row = {"request": request}
        report["calls"].append(row)
        row["response"] = original(request, allow_upload=True) if args.live else response_for(request)
        return row["response"]
    # Exclusive creation prevents accidentally repeating a paid evaluation on the same receipt.
    with Path(args.output).open("x") as output, tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "synthetic.db"
        seed_database(path)
        before = path.read_bytes()
        start = time.monotonic()
        try:
            with patch("sys.argv", ["beacon", "deck", TASK, "--db", str(path), "--since", SINCE, "--until", UNTIL,
                                     "--provider", "typesafe", "--allow-upload", "--json"]), \
                 patch.object(useful, "call_typesafe", side_effect=tracked), redirect_stdout(io.StringIO()) as cli:
                beacon.main()
            result = json.loads(cli.getvalue())
            actual = {str(card["id"]): card["action"] for card in result["cards"]}
            actual.update({str(item_id): "ignore" for item_id in result["ignored_ids"]})
            actual.update({str(item_id): "uncertain" for item_id in result["review_ids"]})
            report.update(result=result, actual=actual, correct=sum(actual.get(key) == value for key, value in report["expected"].items()),
                          database_unchanged=before == path.read_bytes(), elapsed_ms=round((time.monotonic()-start)*1000, 2))
            report["passed"] = actual == report["expected"] and report["database_unchanged"]
        except (Exception, SystemExit) as error:
            report.update(passed=False, error=type(error).__name__)
        output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key not in {"calls", "result", "expected"}}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
