"""Speed benchmark for the Library, against the running `beacon serve`.

    python3 scripts/bench.py [--label "what changed"] [--runs 10]

Times each request a few times and prints the median and slowest, in milliseconds. The first
(cold) call is reported on its own. Results are appended to docs/perf/bench.jsonl so each
change can be compared with the last. Targets: docs/plans/2026-10-09-speed-hillclimb.md.
"""
import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "docs/perf/bench.jsonl"
BASE = "http://127.0.0.1:8765"
CASES = {
    "queue": "/queue",
    "briefs": "/briefs",
    "first_page": "/library?offset=0&limit=200",
    "deep_page": "/library?offset=5000&limit=200",
    "words": "/library?offset=0&limit=200&q=kitchen",
    "meaning": "/library?offset=0&limit=200&meaning=a%20red%20chair%20in%20sunlight",
    "similar": "/library?offset=0&limit=200&similar=19467",
}


def timed(path):
    start = time.perf_counter()
    with urlopen(BASE + path, timeout=300) as response:
        response.read()
    return (time.perf_counter() - start) * 1000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    results = {}
    for name, path in CASES.items():
        cold = timed(path)
        warm = [timed(path) for _ in range(args.runs)]
        results[name] = {"cold": round(cold), "median": round(statistics.median(warm)), "max": round(max(warm))}
        print(f"{name:11} cold {cold:7.0f} ms   median {results[name]['median']:6} ms   max {results[name]['max']:6} ms")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as log:
        log.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "label": args.label, "api": results}) + "\n")


if __name__ == "__main__":
    main()
