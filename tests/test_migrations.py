"""Audit B10: only an already-applied migration is skipped; any other schema error stops initialization."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "beacon.db")

    def test_running_init_twice_skips_columns_that_already_exist(self):
        db.init_db(self.path)
        db.init_db(self.path)
        with sqlite3.connect(self.path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_xinfo(items)")}
        self.assertTrue({"saved_at", "status", "in_library"} <= columns)

    def test_an_old_database_gains_the_missing_column(self):
        db.init_db(self.path)
        with sqlite3.connect(self.path) as conn:
            conn.execute("ALTER TABLE briefs DROP COLUMN for_mac")
        db.init_db(self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertIn("for_mac", {row[1] for row in conn.execute("PRAGMA table_info(briefs)")})

    def test_a_migration_that_fails_for_another_reason_stops_initialization(self):
        broken = db.MIGRATIONS + ["CREATE INDEX IF NOT EXISTS broken ON no_such_table(x)"]
        with patch.object(db, "MIGRATIONS", broken):
            with self.assertRaisesRegex(sqlite3.OperationalError, "no_such_table"):
                db.init_db(self.path)

    def test_an_old_pick_log_learns_about_the_same_and_keeps_its_picks(self):
        old = [m.replace("'skip','same')", "'skip')") if "pick_log" in m else m for m in db.MIGRATIONS]
        with patch.object(db, "MIGRATIONS", old), patch.object(db, "WIDENED_CHECKS", []):
            db.init_db(self.path)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO pick_rounds(axis, pairs_json, created_at) VALUES('working', '[]', 'x')")
            conn.execute("INSERT INTO pick_log(round_id, position, left_id, right_id, axis, kind, choice, beacon_guess, "
                         "chosen_at) VALUES(1, 1, 1, 2, 'working', 'matched', 'left', 'left', 'x')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO pick_log(round_id, position, left_id, right_id, axis, kind, choice, beacon_guess, "
                             "chosen_at) VALUES(1, 2, 1, 2, 'working', 'matched', 'same', 'left', 'x')")
        db.init_db(self.path)
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO pick_log(round_id, position, left_id, right_id, axis, kind, choice, beacon_guess, "
                         "chosen_at) VALUES(1, 2, 1, 2, 'working', 'matched', 'same', 'left', 'x')")
            self.assertEqual([r[0] for r in conn.execute("SELECT choice FROM pick_log ORDER BY position")], ["left", "same"])
            self.assertIn("scorer", {row[1] for row in conn.execute("PRAGMA table_info(pick_scores)")})
