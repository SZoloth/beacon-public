import unittest
from unittest import mock


class DiscoverTestCase(unittest.TestCase):
    @mock.patch("lib.discover.run_json_prompt")
    def test_run_ai_filter_batches_candidates_and_combines_results(self, mock_run_json_prompt):
        from lib.discover import run_ai_filter

        candidates = {
            f"project-{i}": {
                "slug": f"project-{i}",
                "name": f"Project {i}",
                "description": "",
                "source": "local",
                "path": f"/tmp/project-{i}",
                "backlog_path": f"/tmp/project-{i}/docs/beacon-backlog.md",
            }
            for i in range(13)
        }
        existing = [{"slug": "folio", "name": "Folio"}]

        mock_run_json_prompt.side_effect = [
            [{"slug": f"project-{i}", "recommend": True, "reason": "fit"} for i in range(5)],
            [{"slug": f"project-{i}", "recommend": True, "reason": "fit"} for i in range(5, 10)],
            [{"slug": f"project-{i}", "recommend": False, "reason": "skip"} for i in range(10, 13)],
        ]

        with mock.patch.dict("os.environ", {"BEACON_DISCOVER_BATCH_SIZE": "5"}):
            results = run_ai_filter(candidates, existing)

        self.assertEqual(len(results), 13)
        self.assertEqual(mock_run_json_prompt.call_count, 3)
        self.assertEqual(
            [call.kwargs for call in mock_run_json_prompt.call_args_list],
            [{"timeout": 45}, {"timeout": 45}, {"timeout": 45}],
        )


if __name__ == "__main__":
    unittest.main()
