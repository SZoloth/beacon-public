import contextlib
import io
import json
import unittest
from unittest.mock import patch

import eval_meaning_search


class MeaningEvaluationTests(unittest.TestCase):
    def test_malformed_usage_preserves_failed_receipt(self):
        with patch.object(eval_meaning_search.useful, "call_typesafe", return_value={}), \
             patch("sys.argv", ["eval", "--live", "--allow-upload"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = eval_meaning_search.main()
        report = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["completed"])
        self.assertEqual(report["attempted_calls"], 1)
        self.assertEqual(report["usage_unknown_calls"], 1)
        self.assertIn("additional charges", report["cost_scope"])
