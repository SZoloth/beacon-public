"""Headless Claude calls move to the next subscription when one hits its usage limit."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib import ai, briefs, claude_accounts

LIMIT = "You've hit your weekly limit · resets 11pm (America/Chicago) · out of usage credits"


class AccountsCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patches = [mock.patch.object(claude_accounts, "STATE", Path(folder.name) / "limits.json"),
                   mock.patch.object(claude_accounts.settings, "load",
                                     return_value={"claude_accounts": ["default", "~/.claude-second"]})]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []

    def fake(self, answers):
        """A stand-in for subprocess.run: answers by the account each call ran on."""
        def run(command, **kwargs):
            account = kwargs["env"].get("CLAUDE_CONFIG_DIR", "default")
            self.calls.append((account, "CLAUDE_CODE_OAUTH_TOKEN" in kwargs["env"]))
            code, out, err = answers[account]
            return subprocess.CompletedProcess(command, code, out, err)
        return run


class FallbackTest(AccountsCase):
    def test_a_limited_account_hands_the_call_to_the_next_and_is_skipped_after(self):
        second = str(Path("~/.claude-second").expanduser())
        run = self.fake({"default": (1, "", LIMIT), second: (0, '[{"id": 1}]', "")})
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": "/somewhere"}
        with mock.patch.object(ai.subprocess, "run", side_effect=run):
            outcome = ai.run_json_prompt_result("prompt", runners=["claude"])
            ai.run_json_prompt_result("prompt", runners=["claude"])
        self.assertEqual((outcome["ok"], outcome["results"]), (True, [{"id": 1}]))
        self.assertEqual(self.calls, [("default", False), (second, False), (second, False)],
                         "the second account signs in with its own login, and the limited one waits its turn")

    def test_a_limit_reported_inside_json_output_counts_too(self):
        second = str(Path("~/.claude-second").expanduser())
        limited = json.dumps({"is_error": True, "result": LIMIT, "total_cost_usd": 0, "usage": {"input_tokens": 0}})
        answer = json.dumps({"is_error": False, "structured_output": {"ok": 1}, "total_cost_usd": 0.01})
        with mock.patch.object(briefs.subprocess, "run",
                               side_effect=self.fake({"default": (1, limited, ""), second: (0, answer, "")})):
            self.assertEqual(briefs.sonnet("p", {"type": "object"}, "s"), ({"ok": 1}, 0.01))

    def test_an_answer_that_mentions_limits_is_still_an_answer(self):
        long_answer = json.dumps([{"id": 1, "takeaway": "A post about hitting your usage limit early. " * 10}])
        with mock.patch.object(ai.subprocess, "run", side_effect=self.fake({"default": (0, long_answer, "")})):
            self.assertTrue(ai.run_json_prompt_result("prompt", runners=["claude"])["ok"])
        self.assertEqual(len(self.calls), 1)

    def test_when_every_account_is_limited_the_last_answer_comes_back(self):
        second = str(Path("~/.claude-second").expanduser())
        with mock.patch.object(ai.subprocess, "run", side_effect=self.fake({"default": (1, "", LIMIT), second: (1, "", LIMIT)})):
            outcome = ai.run_json_prompt_result("prompt", runners=["claude"])
        self.assertFalse(outcome["ok"])
        self.assertIn("limit", " ".join(outcome["errors"]))

    def test_an_agent_run_starts_on_the_first_account_not_cooling_down(self):
        claude_accounts.mark_limited("default")
        env = claude_accounts.env_for(claude_accounts.ordered()[0], {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": str(Path("~/.claude-second").expanduser())})


if __name__ == "__main__":
    unittest.main()
