import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from lib import db

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "routing_eval.py"


def load():
    spec = importlib.util.spec_from_file_location("routing_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RoutingEvalTest(unittest.TestCase):
    def test_it_runs_against_a_database_that_has_never_been_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            routing_eval = load()
            with patch.object(db, "DB_PATH", str(Path(folder) / "beacon.db")), \
                    patch.object(routing_eval, "CACHE", Path(folder) / "cache"), \
                    patch.object(routing_eval, "LOG", Path(folder) / "perf" / "routing-eval.jsonl"), \
                    patch.object(sys, "argv", ["routing_eval.py", "--only", "haiku_taste"]), redirect_stdout(StringIO()):
                routing_eval.main()
            entry = json.loads((Path(folder) / "perf" / "routing-eval.jsonl").read_text())
        self.assertEqual((entry["truth_n"], entry["reference_n"]), (0, 0))


    def test_a_cached_answer_does_not_outlive_its_prompt(self):
        routing_eval = load()
        runs = []

        def run(todo):
            runs.append([s["id"] for s in todo])
            return {s["id"]: {"dest": "none", "action": "do"} for s in todo}, 0.5

        with tempfile.TemporaryDirectory() as folder, patch.object(routing_eval, "CACHE", Path(folder)):
            routing_eval.cached("taste", run, [{"id": 1}], "v1")
            answers, cost, _ = routing_eval.cached("taste", run, [{"id": 1}, {"id": 2}], "v1")
            self.assertEqual((runs, sorted(answers), cost), ([[1], [2]], [1, 2], 1.0))
            routing_eval.cached("taste", run, [{"id": 1}], "v2")
        self.assertEqual(runs[-1], [1])

    def test_keep_counts_do_and_read_against_pass(self):
        routing_eval = load()
        answers = {1: {"action": "do"}, 2: {"action": "read"}, 3: {"action": "pass"}, 4: {"action": "pass"}, 5: {"action": "do"}}
        rows = [{"id": 1, "action": "read"}, {"id": 2, "action": "do"}, {"id": 3, "action": "pass"},
                {"id": 4, "action": "do"}, {"id": 6, "action": "do"}, {"id": 5}]
        self.assertEqual(routing_eval.keep_score(answers, rows), (0.75, 4))


if __name__ == "__main__":
    unittest.main()
