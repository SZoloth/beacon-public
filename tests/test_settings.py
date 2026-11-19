"""A stranger's checkout runs without any of the author's paths: config falls back to the example and
every machine-specific path is opt-in."""
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib import db, followups, route, settings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import export_public  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PERSONAL = re.compile(r"/(?:Users|Vaults)/|~/Vaults")


class ConfigPathTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_env_override_wins(self):
        target = self.root / "mine.json"
        with patch.dict(os.environ, {"BEACON_CONFIG": str(target)}):
            self.assertEqual(settings.config_path(), str(target))

    def test_a_checkout_without_config_json_uses_the_example(self):
        with patch.object(settings, "ROOT", str(self.root)), patch.dict(os.environ, {"BEACON_CONFIG": ""}):
            self.assertEqual(settings.config_path(), str(self.root / "config.example.json"))
            (self.root / "config.json").write_text("{}")
            self.assertEqual(settings.config_path(), str(self.root / "config.json"))

    def test_unset_and_blank_paths_are_none(self):
        self.assertIsNone(settings.path("reading_inbox", {}))
        self.assertIsNone(settings.path("reading_inbox", {"paths": {"reading_inbox": "  "}}))
        self.assertEqual(settings.path("live_db", {"paths": {"live_db": "~/b/beacon.db"}}),
                         os.path.expanduser("~/b/beacon.db"))

    def test_unknown_key_is_an_error(self):
        with self.assertRaises(KeyError):
            settings.path("vault", {})

    def test_a_broken_config_reads_as_empty(self):
        bad = self.root / "bad.json"
        bad.write_text("{not json")
        self.assertEqual(settings.load(str(bad)), {})


class ExampleConfigTest(unittest.TestCase):
    def test_example_config_is_valid_and_impersonal(self):
        text = (ROOT / "config.example.json").read_text()
        config = json.loads(text)
        self.assertEqual(PERSONAL.findall(text), [])
        self.assertTrue(config["workstreams"])
        self.assertTrue(set(config["paths"]) <= set(settings.PATH_KEYS))
        # Only sources a stranger can run are switched on.
        enabled = {name for name, source in config["sources"].items() if source.get("enabled", True)}
        self.assertEqual(enabled, {"github", "readwise"})

    def test_lib_has_no_personal_paths(self):
        hits = [f"{path.name}:{n}" for path in (ROOT / "lib").glob("*.py")
                for n, line in enumerate(path.read_text().splitlines(), 1)
                if PERSONAL.search(line) or "~/Projects/beacon" in line or "~/Vaults" in line]
        self.assertEqual(hits, [])


class NoLiveDatabaseTest(unittest.TestCase):
    def test_without_a_live_db_nothing_counts_as_live(self):
        with patch.object(db, "LIVE_DB_PATH", None):
            self.assertFalse(db.is_live())


class UnsetReadingInboxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for patcher in (patch.object(db, "DB_PATH", str(root / "beacon.db")),
                        patch.object(db, "is_live", return_value=True),
                        patch.object(route, "READING_INBOX", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        backlog = root / "beacon-backlog.md"
        db.seed_workstreams([{"slug": "proj", "name": "Proj", "backlog_path": str(backlog)}])
        with sqlite3.connect(db.DB_PATH) as conn:
            for item_id, human_read in ((1, 1), (2, 0)):
                conn.execute("INSERT INTO items(id,source,external_id,title,body,url,status,human_read,classified_at,synced_at) "
                             "VALUES(?,'x',?,?,'Body',?,'queued',?,'2026-10-10','2026-10-10')",
                             (item_id, str(item_id), f"Item {item_id}", f"https://x.com/a/{item_id}", human_read))
            conn.execute("INSERT INTO item_workstreams(item_id,workstream_id,confidence,routed) SELECT 2, id, 0.8, 0 FROM workstreams")

    def test_reads_stay_unrouted_and_it_says_why(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            route.route_items()
        self.assertIn("reading_inbox is not set", out.getvalue())
        self.assertEqual([i["id"] for i in db.get_unrouted_reading_items()], [1])


class FollowupContextTest(unittest.TestCase):
    def test_no_context_script_means_no_background(self):
        with patch.object(followups, "CONTEXT_SCRIPT", None), \
                patch.object(followups.subprocess, "run", side_effect=AssertionError("ran a script")):
            self.assertEqual(followups.context({"title": "t", "body": "b"}), "")

    def test_a_configured_script_is_run(self):
        result = subprocess.CompletedProcess([], 0, stdout="bg", stderr="")
        with patch.object(followups, "CONTEXT_SCRIPT", "/tmp/ctx.py"), \
                patch.object(followups.subprocess, "run", return_value=result) as run:
            self.assertEqual(followups.context({"title": "t", "body": "b"}), "bg")
        self.assertEqual(run.call_args.args[0][:2], [sys.executable, "/tmp/ctx.py"])


class PublicExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "public"

    def run_export(self, *extra, terms="no-such-term-anywhere\n"):
        denylist = Path(self.temp.name) / "denylist.txt"
        denylist.write_text(terms)
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "export_public.py"), str(self.out), *extra],
                              cwd=ROOT, capture_output=True, text=True,
                              env={**os.environ, "BEACON_DENYLIST": str(denylist)})

    def test_export_leaves_out_personal_files(self):
        self.run_export("--allow-hits")
        exported = {str(p.relative_to(self.out)) for p in self.out.rglob("*") if p.is_file()}
        self.assertIn("config.example.json", exported)
        self.assertIn("lib/settings.py", exported)
        for left_out in ("config.json", "plans/activation-log.md"):
            self.assertNotIn(left_out, exported)
        for prefix in ("runs/", "docs/perf/", ".pstack/", ".agents/"):
            self.assertFalse([p for p in exported if p.startswith(prefix)], prefix)

    def test_export_leaves_out_internal_records_and_the_hosted_app(self):
        self.run_export("--allow-hits")
        exported = {str(p.relative_to(self.out)) for p in self.out.rglob("*") if p.is_file()}
        for left_out in ("docs/ROADMAP.md", "app/docs/screenshot.jpg"):
            self.assertNotIn(left_out, exported)
        for prefix in ("hosted/", "docs/design/", "docs/research/", "docs/audits/", "docs/screenshots/"):
            self.assertFalse([p for p in exported if p.startswith(prefix)], prefix)

    def test_generated_backlog_blocks_are_emptied(self):
        page = Path(self.temp.name) / "page.md"
        page.write_text("a\n<!-- beacon:backlog:start -->\n- https://x.com/p/1\n<!-- beacon:backlog:end -->\nb\n")
        export_public.blank_generated(page)
        self.assertEqual(page.read_text(), "a\n<!-- beacon:backlog:start -->\n<!-- beacon:backlog:end -->\nb\n")

    def test_export_fails_while_a_denylisted_term_remains(self):
        result = self.run_export(terms="# names go here\nPATH_KEYS\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("lib/settings.py", result.stdout)

    def test_pictures_left_out_of_the_export_leave_no_broken_image_link(self):
        if "docs/screenshot.jpg" not in (ROOT / "app" / "README.md").read_text():
            self.skipTest("this checkout is itself an export, so the link is already gone")
        self.run_export("--allow-hits")
        self.assertNotIn("docs/screenshot.jpg", (self.out / "app" / "README.md").read_text())

    def test_terms_match_whole_words_only(self):
        result = self.run_export("--allow-hits", terms="PATH_KEY\n")
        self.assertNotIn("lib/settings.py", result.stdout)

    def test_a_term_right_after_a_written_newline_is_found(self):
        folder = Path(self.temp.name) / "scan"
        folder.mkdir()
        (folder / "code.py").write_text('text = "line one\\nZebra said hi"\n')
        denylist = Path(self.temp.name) / "terms.txt"
        denylist.write_text("Zebra\n")
        pattern, _, _ = export_public.denylist(denylist)
        self.assertEqual(export_public.scan(folder, pattern), ({"code.py": 1}, []))

    def test_a_missing_denylist_is_an_error(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "export_public.py"), str(self.out)],
                                cwd=ROOT, capture_output=True, text=True,
                                env={**os.environ, "BEACON_DENYLIST": str(Path(self.temp.name) / "missing.txt")})
        self.assertEqual(result.returncode, 2)
        self.assertIn("denylist", result.stderr)
        self.assertEqual(self.run_export(terms="# only a comment\n").returncode, 2)

    def test_allowed_text_is_not_a_hit(self):
        lines = [line.strip() for line in (ROOT / "lib" / "settings.py").read_text().splitlines() if "PATH_KEYS" in line]
        result = self.run_export(terms="PATH_KEYS\n" + "".join(f"allow: {line}\n" for line in lines))
        self.assertNotIn("lib/settings.py", result.stdout)

    def test_replacements_rewrite_the_exported_copy(self):
        result = self.run_export(terms="PATH_KEYS\nreplace: PATH_KEYS => PATH_NAMES\n")
        self.assertNotIn("lib/settings.py", result.stdout)
        self.assertIn("PATH_NAMES", (self.out / "lib" / "settings.py").read_text())
        self.assertIn("PATH_KEYS", (ROOT / "lib" / "settings.py").read_text())

    def test_pictures_are_listed_for_a_person_to_open(self):
        result = self.run_export("--allow-hits")
        self.assertIn("not scanned", result.stdout)
        self.assertIn("app/docs/widgets/", result.stdout)

    def test_refuses_a_non_empty_target(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("x")
        self.assertEqual(self.run_export().returncode, 2)


if __name__ == "__main__":
    unittest.main()
