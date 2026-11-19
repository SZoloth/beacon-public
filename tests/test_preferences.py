import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lib import deck, preferences, save_review, useful
from tests.test_deck import SINCE, TASK, UNTIL, response_for, seed_database

PROFILE = {"version": 1, "id": "synthetic-proof", "revision": 1, "purpose": "reference-selection",
           "consumers": ["beacon", "mosaic"], "confirmed": True, "expires_at": "2099-01-01T00:00:00Z",
           "statements": ["Prefer concrete authored experiments with explicit limits."]}


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "preferences.json"
        self.path.write_text(json.dumps(PROFILE))

    def test_consumer_purpose_confirmation_and_expiry(self):
        for consumer in ("beacon", "mosaic"):
            self.assertEqual(preferences.read(self.path, consumer=consumer)["statements"], PROFILE["statements"])
        for changes in ({"confirmed": False}, {"purpose": "authority"}, {"consumers": ["mosaic"]},
                        {"expires_at": "2000-01-01T00:00:00Z"}, {"extra": "private"}, {"version": True}, {"consumers": [[]]}):
            self.path.write_text(json.dumps({**PROFILE, **changes}))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                preferences.read(self.path)
        self.path.write_text(json.dumps(PROFILE))
        with self.assertRaises(ValueError):
            preferences.read(self.path, now=datetime(2100, 1, 1, tzinfo=timezone.utc))

    def test_actual_deck_uses_only_statements_and_invalidates_changed_profile(self):
        database = Path(self.temp.name) / "invented.db"
        seed_database(database)
        before = database.read_bytes()
        options = {"preferences_path": self.path, "provider": "typesafe"}
        with patch.object(useful, "call_typesafe", side_effect=AssertionError("no upload")):
            result = deck.generate(TASK, database, SINCE, UNTIL, preview=True, **options)
        state = json.dumps(result["request"]["state"])
        self.assertIn(PROFILE["statements"][0], state)
        self.assertNotIn("synthetic-proof", state)
        self.assertNotIn("2099", state)
        self.assertEqual(result["preferences"]["revision"], 1)
        def changed(request, **_):
            new = copy.deepcopy(PROFILE)
            new["revision"] = 2
            self.path.write_text(json.dumps(new))
            return response_for(request)
        with patch.object(useful, "call_typesafe", side_effect=changed), self.assertRaisesRegex(ValueError, "changed"):
            deck.generate(TASK, database, SINCE, UNTIL, allow_upload=True, **options)
        self.assertEqual(database.read_bytes(), before)

    def test_profile_visible_in_html_without_executing_text(self):
        profile = {**PROFILE, "statements": ["<script>not instructions</script>"]}
        page = save_review.render_html({"cards": [], "preferences": profile})
        self.assertIn("synthetic-proof", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)
