"""Run Beacon's tests one test class per process, several at a time, and add up the results.

Discovery is the same as `python -m unittest discover -s tests`. Each class runs in a fresh
process, so classes that share class-level fixtures still share them, and one slow file (such as
test_activate) is split across workers. The exit code is 0 only when every class passes.
"""

import argparse
import os
import re
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")


def test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_ids(test)
        else:
            yield test.id()


def classes(modules):
    """Discovered `module.Class` names, in first-seen order, limited to `modules` when given."""
    suite = unittest.TestLoader().discover(TESTS, top_level_dir=TESTS)
    seen = {}
    for test_id in test_ids(suite):
        name = test_id.rsplit(".", 1)[0]
        if name.startswith("unittest.loader._FailedTest"):
            name = test_id  # an import failure: run it so its error is reported
        if not modules or name.split(".")[0] in modules:
            seen.setdefault(name, None)
    return list(seen)


def run(name):
    started = time.monotonic()
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([TESTS, ROOT]))
    proc = subprocess.run([sys.executable, "-B", "-m", "unittest", name], cwd=ROOT, env=env,
                          capture_output=True, text=True)
    output = proc.stdout + proc.stderr
    match = RAN.search(output)
    return name, proc.returncode, int(match.group(1)) if match else 0, time.monotonic() - started, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-j", "--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    parser.add_argument("modules", nargs="*", help="test module names, e.g. test_pick (default: all)")
    args = parser.parse_args()
    sys.path[:0] = [TESTS, ROOT]
    names = classes(set(args.modules))

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(run, names))

    failed = [r for r in results if r[1] != 0]
    for name, _, _, _, output in failed:
        print(f"\n===== {name} FAILED =====\n{output}")
    total = sum(r[2] for r in results)
    slowest = sorted(results, key=lambda r: -r[3])[:3]
    print(f"Ran {total} tests in {time.monotonic() - started:.1f}s across {len(names)} classes "
          f"({args.jobs} at a time); slowest: " + ", ".join(f"{r[0]} {r[3]:.0f}s" for r in slowest))
    print("FAILED: " + ", ".join(r[0] for r in failed) if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
