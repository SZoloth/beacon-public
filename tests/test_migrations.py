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
