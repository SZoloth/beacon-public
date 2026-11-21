"""scripts/doctor reads launchd's own report, so a job that failed last run is caught."""
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "scripts" / "doctor"
loader = importlib.machinery.SourceFileLoader("doctor", str(PATH))
doctor = importlib.util.module_from_spec(importlib.util.spec_from_loader("doctor", loader))
loader.exec_module(doctor)


class LaunchdStateTest(unittest.TestCase):
    def test_a_failed_last_run_is_read_as_its_exit_code(self):
        self.assertEqual(doctor.launchd_state("\tstate = not running\n\tlast exit code = 1\n"), (False, 1))

    def test_a_running_job_that_never_exited(self):
        self.assertEqual(doctor.launchd_state("\tstate = running\n\tlast exit code = (never exited)\n"), (True, None))

    def test_a_job_killed_by_a_signal_keeps_the_negative_code(self):
        self.assertEqual(doctor.launchd_state("\tstate = not running\n\tlast exit code = -15\n"), (False, -15))


class SourceFreshnessTest(unittest.TestCase):
    NOW = "2026-11-30T12:00:00+00:00"

    def test_a_source_that_last_synced_over_a_week_ago_fails_with_its_fix(self):
        found = list(doctor.source_freshness({"x": "2026-11-03T01:21:58+00:00", "mymind": "2026-11-29T08:00:00+00:00"},
                                             ["x", "mymind"], self.NOW))
        self.assertEqual(found[0][:2], ("fail", "x sync"))
        self.assertIn("27 days", found[0][2])
        self.assertIn("scripts/x-keys", found[0][2])
        self.assertEqual(found[1][:2], ("ok", "mymind sync"))

    def test_a_date_stored_without_a_zone_is_read_as_utc(self):
        self.assertEqual(list(doctor.source_freshness({"github": "2026-11-29T12:00:00"}, ["github"], self.NOW))[0][:2],
                         ("ok", "github sync"))

    def test_an_enabled_source_that_never_synced_fails(self):
        self.assertEqual(list(doctor.source_freshness({}, ["granola-digests"], self.NOW))[0][:2],
                         ("fail", "granola-digests sync"))



class SourceWarningsTest(unittest.TestCase):
    def test_a_field_mymind_started_sending_is_a_warning(self):
        found = list(doctor.source_warnings({"mymind": '{"unknown_fields": ["highlights", "colors"]}', "x": "123"}))
        self.assertEqual([line[:2] for line in found], [("warn", "mymind fields")])
        self.assertIn("highlights, colors", found[0][2])

    def test_no_new_fields_is_no_line(self):
        self.assertEqual(list(doctor.source_warnings({"mymind": "{}", "readwise-notes": None})), [])


if __name__ == "__main__":
    unittest.main()
