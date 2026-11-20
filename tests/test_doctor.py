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


if __name__ == "__main__":
    unittest.main()
