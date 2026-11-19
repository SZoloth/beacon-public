"""beacon init: a stranger gets a working config from a few answers, and nothing is overwritten."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lib import settings, setup

ROOT = Path(__file__).resolve().parent.parent


class BuildConfigTest(unittest.TestCase):
    def test_projects_become_workstreams_with_backlogs_in_their_docs_folder(self):
        config = setup.build_config("Ada", "", [("Recipe App", "/tmp/recipes", "An iPhone recipe app.")],
                                    root="/tmp/beacon", example={"sources": {"github": {}}, "areas": [{"slug": "food"}]})
        self.assertEqual(config["workstreams"], [{"slug": "recipe-app", "name": "Recipe App",
                                                  "backlog_path": "/tmp/recipes/docs/beacon-backlog.md",
                                                  "description": "An iPhone recipe app."}])
        self.assertEqual(config["owner"], {"name": "Ada", "about": "The user is Ada."})
        self.assertEqual(config["paths"], {"live_db": "/tmp/beacon/beacon.db", "reading_inbox": "/tmp/beacon/reading-inbox.md"})
        self.assertEqual((config["sources"], config["areas"]), ({"github": {}}, [{"slug": "food"}]))
        self.assertEqual(settings.owner(config), "Ada")

    def test_home_paths_are_written_with_a_tilde(self):
        config = setup.build_config("Ada", "x", [("A", "~/code/a", "d")], root=os.path.expanduser("~/beacon"), example={})
        self.assertEqual(config["workstreams"][0]["backlog_path"], "~/code/a/docs/beacon-backlog.md")
        self.assertEqual(config["paths"]["live_db"], "~/beacon/beacon.db")

    def test_bad_answers_are_refused(self):
        for owner, projects in (("", [("A", "/a", "d")]), ("Ada", []), ("Ada", [("A", "/a", "d"), ("a", "/b", "d")]),
                                ("Ada", [("!!", "/a", "d")]), ("Ada", [("P%d" % i, "/a", "d") for i in range(9)])):
            with self.assertRaises(ValueError):
                setup.build_config(owner, "", projects, root="/tmp", example={})

    def test_parse_project(self):
        self.assertEqual(setup.parse_project(" App | ~/a | Helps | with pipes "), ("App", "~/a", "Helps | with pipes"))
        with self.assertRaises(ValueError):
            setup.parse_project("App|~/a")

    def test_interactive_answers_stop_at_a_blank_name(self):
        answers = iter(["Ada", "Reads design", "App", "~/a", "Helps", ""])
        self.assertEqual(setup.ask(lambda _: next(answers), lambda *_: None), ("Ada", "Reads design", [("App", "~/a", "Helps")]))


class InitCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "beacon"
        shutil.copytree(ROOT / "lib", self.root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("beacon.py", "config.example.json"):
            shutil.copy(ROOT / name, self.root / name)

    def init(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "BEACON_CONFIG"}
        return subprocess.run([sys.executable, "beacon.py", "init", *args], cwd=self.root, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, env=env)

    def test_writes_a_config_the_rest_of_beacon_reads(self):
        result = self.init("--owner", "Ada", "--project", f"App|{self.root.parent / 'app'}|An app.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Wrote config.json", result.stdout)
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual([w["slug"] for w in config["workstreams"]], ["app"])
        stats = subprocess.run([sys.executable, "beacon.py", "workstreams"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(stats.returncode, 0, stats.stderr)
        self.assertIn("app", stats.stdout)

    def test_never_overwrites(self):
        (self.root / "config.json").write_text("{}")
        result = self.init("--owner", "Ada", "--project", "App|/a|An app.")
        self.assertEqual((result.returncode, (self.root / "config.json").read_text()), (2, "{}"))
        self.assertIn("never overwrites", result.stderr)

    def test_without_answers_or_a_terminal_it_says_what_to_pass(self):
        result = self.init()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--owner", result.stderr)
        self.assertFalse((self.root / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
