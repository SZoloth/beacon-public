"""Prompts name the configured owner, and a checkout without one names nobody."""
import re
import unittest
from unittest.mock import patch

from lib import briefs, classify, followups, gists, pile, settings

PERSONAL = re.compile(r"\b(Sam|CoS)\b|\bhe\b|\bhim\b|\bhis\b|himself")


def prompts():
    project = {"slug": "job-search", "name": "Job hunt", "description": "Find a role."}
    item = {"id": 1, "title": "T", "body": "B", "url": "https://x.com/a/1", "source": "x", "author": "a"}
    return {
        "briefs": briefs._prompt([item], project),
        "classify": classify.build_classification_prompt([item], [{"slug": "p", "name": "P", "description": "d"}]),
        "gists": gists.PROMPT.format(owner=settings.owner(), kinds="post", items="x"),
        "gists taste": gists._shown([{"choice": "kept", "where": "p", "text": "t"}]),
        "pile": pile.PROMPT.format(owner=settings.owner(), destinations="p: P", none="none", saves="x"),
        "follow-up roles": " ".join(followups.role(slug) for slug in followups.ROLES),
    }


class OwnerTest(unittest.TestCase):
    def test_owner_defaults_to_the_user(self):
        self.assertEqual(settings.owner({}), "the user")
        self.assertEqual(settings.owner({"owner": {"name": " Ada "}}), "Ada")
        self.assertEqual(settings.owner_about({}), "")

    def test_without_an_owner_no_prompt_names_a_person(self):
        with patch.object(settings, "load", return_value={}):
            for name, text in prompts().items():
                self.assertEqual(PERSONAL.findall(text), [], name)
                if name != "follow-up roles":
                    self.assertIn("the user", text, name)

    def test_a_configured_owner_appears_in_every_prompt(self):
        config = {"owner": {"name": "Ada", "about": "The user is Ada, a chemist."}}
        with patch.object(settings, "load", return_value=config):
            texts = prompts()
        for name, text in texts.items():
            if name != "follow-up roles":
                self.assertIn("Ada", text, name)
        self.assertIn("The user is Ada, a chemist.", texts["classify"])

    def test_config_overrides_an_area_follow_up_role(self):
        config = {"areas": [{"slug": "travel", "followup_role": "Plan around my trips."}]}
        with patch.object(settings, "load", return_value=config):
            self.assertEqual(followups.role("travel"), "Plan around my trips.")
            self.assertEqual(followups.role("food"), followups.ROLES["food"])


if __name__ == "__main__":
    unittest.main()
