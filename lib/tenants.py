"""One database per person, for a hosted Beacon.

Each person gets a folder under a data root holding their own `beacon.db`. Nothing is shared
between folders, so exporting someone's data is copying their file and deleting it is removing
their folder. A self-run Beacon never touches this module.
"""
import os
import re
import shutil
from contextlib import contextmanager
from pathlib import Path

from lib import db

# Ids are made by the server, never typed by people: lowercase letters, digits and dashes.
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def _folder(root, person_id):
    if not isinstance(person_id, str) or not _ID.match(person_id):
        raise ValueError(f"not a valid person id: {person_id!r}")
    root = Path(root).resolve()
    folder = (root / person_id).resolve()
    if folder.parent != root:
        raise ValueError(f"not a valid person id: {person_id!r}")
    return folder


def database_path(root, person_id):
    return _folder(root, person_id) / "beacon.db"


def create(root, person_id):
    """Make the person's folder and an empty database. Safe to call again."""
    path = database_path(root, person_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with db.using(path):
        db.init_db()
    return path


@contextmanager
def open_for(root, person_id):
    """Run the block against this person's database. Refuses one that was never created."""
    path = database_path(root, person_id)
    if not path.exists():
        raise LookupError(f"no database for {person_id}")
    with db.using(path):
        yield path


def delete(root, person_id):
    """Remove the person's folder: database, WAL files and anything else in it."""
    folder = _folder(root, person_id)
    if folder.exists():
        shutil.rmtree(folder)
        return True
    return False
