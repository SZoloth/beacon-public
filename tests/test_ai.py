import unittest
from unittest import mock


class JsonPromptRunnerTestCase(unittest.TestCase):
    def setUp(self):
        from lib import ai
        if hasattr(ai, "reset_runner_circuit"):
            ai.reset_runner_circuit()

    @mock.patch("subprocess.run")
    def test_run_json_prompt_falls_back_to_claude_when_codex_is_missing(self, mock_run):
        from lib.ai import run_json_prompt

        mock_run.side_effect = [
            FileNotFoundError(),
            mock.Mock(returncode=0, stdout='[{"ok": true}]', stderr=""),
        ]

        result = run_json_prompt("Return JSON")

        self.assertEqual(result, [{"ok": True}])
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args_list[0].args[0][0], "codex")
        self.assertEqual(mock_run.call_args_list[1].args[0][0], "claude")

    def test_claude_runner_disables_tools(self):
        from lib.ai import _runner_command

        cmd = _runner_command("claude")
        self.assertEqual(cmd, ["claude", "--print", "--output-format", "text", "--tools", ""])

    @mock.patch("subprocess.run")
    def test_runner_circuit_breaker_skips_runner_after_two_consecutive_failures(self, mock_run):
        from lib import ai

        ai.reset_runner_circuit()
        mock_run.side_effect = [
            mock.Mock(returncode=1, stdout="", stderr="quota limit"),
            mock.Mock(returncode=0, stdout='[{"id": 1}]', stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="quota limit"),
            mock.Mock(returncode=0, stdout='[{"id": 2}]', stderr=""),
            mock.Mock(returncode=0, stdout='[{"id": 3}]', stderr=""),
        ]

        self.assertEqual(ai.run_json_prompt("batch 1"), [{"id": 1}])
        self.assertEqual(ai.run_json_prompt("batch 2"), [{"id": 2}])
        self.assertEqual(ai.run_json_prompt("batch 3"), [{"id": 3}])

        called_binaries = [call.args[0][0] for call in mock_run.call_args_list]
        self.assertEqual(called_binaries, ["codex", "claude", "codex", "claude", "claude"])

    @mock.patch("subprocess.run")
    def test_run_json_prompt_result_reports_structured_failure(self, mock_run):
        from lib import ai

        ai.reset_runner_circuit()
        mock_run.side_effect = [
            FileNotFoundError(),
            mock.Mock(returncode=1, stdout="", stderr="weekly limit reached"),
        ]

        outcome = ai.run_json_prompt_result("batch")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["results"], [])
        self.assertIsNone(outcome["runner"])
        self.assertEqual(
            outcome["errors"],
            ["codex not installed", "claude error: weekly limit reached"],
        )


if __name__ == "__main__":
    unittest.main()
