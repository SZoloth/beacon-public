"""A hosted Beacon keeps each person's saves in their own database and never mixes them."""
import tempfile
from contextlib import closing
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import db, tenants


def add_item(title):
    with closing(db.get_connection()) as conn, conn:
        conn.execute("INSERT INTO items(source,external_id,title,url,synced_at) VALUES('link',?,?,?, '2026-11-12')",
                     (title, title, f"https://example.com/{title}"))


def titles():
    with closing(db.get_connection()) as conn:
        return [row["title"] for row in conn.execute("SELECT title FROM items ORDER BY title")]


class TenantTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "people"
        self.default = Path(self.temp.name) / "default.db"
        patcher = patch.object(db, "DB_PATH", str(self.default))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_person_sees_only_their_own_saves(self):
        for person in ("ana-1", "ben-2"):
            tenants.create(self.root, person)
        with tenants.open_for(self.root, "ana-1"):
            add_item("ana-save")
        with tenants.open_for(self.root, "ben-2"):
            add_item("ben-save")
            self.assertEqual(titles(), ["ben-save"])
        with tenants.open_for(self.root, "ana-1"):
            self.assertEqual(titles(), ["ana-save"])
        self.assertFalse(self.default.exists())

    def test_the_default_database_comes_back_after_the_block(self):
        tenants.create(self.root, "ana-1")
        with tenants.open_for(self.root, "ana-1") as path:
            self.assertEqual(db.current_path(), str(path))
        self.assertEqual(db.current_path(), str(self.default))

    def test_threads_do_not_share_a_database(self):
        for person in ("ana-1", "ben-2"):
            tenants.create(self.root, person)
        barrier, seen, errors = threading.Barrier(2), {}, []

        def work(person):
            try:
                with tenants.open_for(self.root, person):
                    barrier.wait(timeout=5)
                    add_item(f"{person}-save")
                    barrier.wait(timeout=5)
                    seen[person] = titles()
            except Exception as error:  # surfaced below
                errors.append(error)

        threads = [threading.Thread(target=work, args=(p,)) for p in ("ana-1", "ben-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(seen, {"ana-1": ["ana-1-save"], "ben-2": ["ben-2-save"]})

    def test_bad_ids_cannot_reach_outside_the_root(self):
        for bad in ("../ana", "ana/../../x", "", "A", "a", "ana_1", ".hidden", None):
            with self.assertRaises(ValueError, msg=bad):
                tenants.database_path(self.root, bad)

    def test_an_uncreated_person_is_refused(self):
        with self.assertRaises(LookupError):
            with tenants.open_for(self.root, "nobody-1"):
                pass

    def test_delete_removes_everything(self):
        path = tenants.create(self.root, "ana-1")
        with tenants.open_for(self.root, "ana-1"):
            add_item("x")
        self.assertTrue(tenants.delete(self.root, "ana-1"))
        self.assertFalse(path.parent.exists())
        self.assertFalse(tenants.delete(self.root, "ana-1"))

    def test_a_person_database_is_never_live(self):
        path = tenants.create(self.root, "ana-1")
        with patch.object(db, "LIVE_DB_PATH", str(self.default)), tenants.open_for(self.root, "ana-1"):
            self.assertFalse(db.is_live())
        with patch.object(db, "LIVE_DB_PATH", str(path)), tenants.open_for(self.root, "ana-1"):
            self.assertTrue(db.is_live())

    def test_folders_are_private(self):
        path = tenants.create(self.root, "ana-1")
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
