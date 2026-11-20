import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from roadmap_merge import resolve  # noqa: E402


def conflict(pr_rows, main_rows):
    return "before\n<<<<<<< HEAD\n" + "".join(pr_rows) + "=======\n" + "".join(main_rows) + ">>>>>>> origin/main\nafter\n"


class RoadmapMergeTest(unittest.TestCase):
    def test_the_prs_version_of_a_row_replaces_mains(self):
        text = conflict(["| **Pick** | shipped |\n"], ["| **Pick** | in review |\n"])
        self.assertEqual(resolve(text), "before\n| **Pick** | shipped |\nafter\n")

    def test_new_rows_from_both_sides_are_kept(self):
        text = conflict(["| **A** | new |\n"], ["| **B** | new |\n"])
        self.assertEqual(resolve(text), "before\n| **B** | new |\n| **A** | new |\nafter\n")

    def test_two_shipped_rows_from_the_same_day_are_both_kept(self):
        text = conflict(["| Nov 19 | Y shipped | #2 |\n"], ["| Nov 19 | X shipped | #1 |\n"])
        self.assertEqual(resolve(text), "before\n| Nov 19 | X shipped | #1 |\n| Nov 19 | Y shipped | #2 |\nafter\n")

    def test_text_without_conflicts_is_unchanged(self):
        self.assertEqual(resolve("| **A** | x |\n"), "| **A** | x |\n")


if __name__ == "__main__":
    unittest.main()
