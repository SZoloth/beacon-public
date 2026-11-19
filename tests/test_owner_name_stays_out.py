"""The owner's name lives only in config.json, so the public export never carries it.

Files the export leaves out (scripts/export_public.py) are not checked.

This test reads the name from the local config (it never enters the repo) and fails when exported Python
(beacon.py, lib/, scripts/, tests/) spells it out. Write "the owner" instead. Without a local config it is skipped.
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from export_public import is_excluded  # noqa: E402

# test_owner.py is the older guard; its own pattern has to name the owner.
GUARDS = {"tests/test_owner.py"}


def owner_names():
    try:
        name = (json.loads((ROOT / "config.json").read_text()).get("owner") or {}).get("name") or ""
    except (OSError, ValueError):
        return []
    return [part for part in re.split(r"\W+", name) if len(part) >= 3]


class OwnerNameStaysOutTest(unittest.TestCase):
    def test_code_and_tests_never_spell_out_the_owners_name(self):
        names = owner_names()
        if not names:
            self.skipTest("no owner name in a local config.json")
        pattern = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b", re.IGNORECASE)
        found = []
        paths = [ROOT / "beacon.py"] + [p for folder in ("lib", "scripts", "tests") for p in sorted((ROOT / folder).rglob("*.py"))]
        for path in paths:
            name = str(path.relative_to(ROOT))
            if name in GUARDS or is_excluded(name):
                continue  # the public export leaves it out, or it is a guard
            for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{name}:{number}")
        self.assertEqual(found, [], "Write 'the owner' instead of the owner's name in code and tests")


if __name__ == "__main__":
    unittest.main()
