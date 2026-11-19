"""Audit B07: the weekly run (and the app's Sync, which runs it) reports every stage that failed."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Stands in for beacon.py: records each stage and fails the ones named in FAIL_STAGES.
STUB = '''import json, os, sys
stage = sys.argv[1]
with open("stages.log", "a") as log:
    log.write(stage + "\\n")
if stage in os.environ.get("FAIL_STAGES", "").split(","):
    sys.exit(1)
if stage == "classify":
    print(json.dumps({"live_model_calls": 0, "held": 0, "errors": 0, "estimated_cost_usd": 0}))
'''


@unittest.skipUnless(Path("/usr/bin/lockf").exists(), "weekly.sh uses macOS lockf")
class WeeklyScriptTest(unittest.TestCase):
    def run_weekly(self, fail=""):
        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp)
        (temp / "scripts").mkdir()
        shutil.copy(ROOT / "scripts/weekly.sh", temp / "scripts/weekly.sh")
        (temp / "beacon.py").write_text(STUB)
        env = {"HOME": str(temp), "TYPESAFE_API_KEY": "test", "FAIL_STAGES": fail, "PATH": os.environ["PATH"]}
        code = subprocess.run(["/bin/zsh", str(temp / "scripts/weekly.sh")], env=env, timeout=60).returncode
        log = (temp / "logs/weekly.log").read_text().strip().splitlines()
        stages = (temp / "stages.log").read_text().split()
        return code, log[-1], stages

    def test_every_stage_succeeding_exits_zero(self):
        code, last, stages = self.run_weekly()
        self.assertEqual(code, 0)
        self.assertEqual(last, "=== done")
        self.assertEqual(stages[0], "sync")
        self.assertEqual(stages[-1], "sweep")

    def test_failed_stages_still_let_the_rest_run_and_are_named_in_the_last_line(self):
        code, last, stages = self.run_weekly(fail="gist,autopilot")
        self.assertEqual(code, 1)
        self.assertIn("gist", last)
        self.assertIn("autopilot", last)
        self.assertIn("follow-up", stages)  # independent stages keep going
        self.assertEqual(stages[-1], "sweep")  # sweep only ages out old items; safe after a failed stage

    def test_a_failed_score_is_caught_through_the_pipe(self):
        code, last, _ = self.run_weekly(fail="classify")
        self.assertEqual(code, 1)
        self.assertIn("classify", last)

    def test_a_failed_sync_is_named_with_the_others(self):
        code, last, stages = self.run_weekly(fail="sync,brief")
        self.assertEqual(code, 1)
        self.assertIn("sync", last)
        self.assertIn("brief", last)
