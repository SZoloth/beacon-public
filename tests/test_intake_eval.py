import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import intake, intake_eval, useful


class IntakeEvaluationTest(unittest.TestCase):
    def test_local_evaluation_cannot_enable_routing_or_access_network(self):
        with patch.object(useful, "call_typesafe") as network:
            report = intake_eval.evaluate()
        network.assert_not_called()
        self.assertFalse(report["routing_gate"]["passed"])
        self.assertEqual(report["routing_gate"]["held_out_cases"], 10)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report))
            with self.assertRaises(ValueError):
                intake.load_routing_evaluation(path)

    def test_service_errors_count_as_failed_gate_not_accuracy_success(self):
        with patch.object(useful, "call_typesafe", side_effect=ValueError("unavailable")):
            report = intake_eval.evaluate(live=True, allow_upload=True)
        self.assertEqual(report["errors"], 12)
        self.assertFalse(report["routing_gate"]["passed"])
        self.assertEqual(report["live_model_calls"], 12)
