"""How many Library saves each interface word finds, with the old word index and with the Library's own one.

Run on the Mac against the real library. It opens beacon.db read-only and builds the new index in memory,
so nothing is written and nothing leaves the Mac; it prints counts only, never save text.

    python3 tests/eval_word_search.py --db beacon.db
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import db

# marked.dev's Explore tags, then the kinds of things its agent prompts ask for.
QUERIES = ["hover", "scroll", "drag", "menu", "panel", "feedback", "card", "canvas", "reveal", "ai", "list", "data",
           "scroll animations", "empty state", "onboarding", "picker", "command menu", "toast", "loading", "chart"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=Path(db.DB_PATH))
    args = parser.parse_args()
    conn = sqlite3.connect("file::memory:", uri=True)  # uri, so the ATTACH below can open beacon.db read-only
    conn.execute("ATTACH DATABASE ? AS live", (args.db.expanduser().resolve().as_uri() + "?mode=ro",))
    conn.execute(db.LIBRARY_FTS_SCHEMA)  # in memory; the library tables are read from live
    conn.execute("INSERT INTO library_fts(rowid, words, about) " + db._LIBRARY_ROW + " WHERE i.in_library = 1")
    saves = conn.execute("SELECT count(*) FROM live.items WHERE in_library = 1").fetchone()[0]
    described = conn.execute("SELECT count(*) FROM library_fts WHERE length(trim(about)) > 0").fetchone()[0]
    print(f"{saves} saves in the Library; {described} ({described / max(saves, 1):.0%}) have words Beacon wrote about them\n")
    print(f"{'query':<20}{'before':>8}{'after':>8}{'gained':>8}")
    before_total = after_total = 0
    for query in QUERIES:
        match = db.fts_query(query)
        before = conn.execute("SELECT count(*) FROM live.items_fts(?) f JOIN live.items i ON i.id = f.rowid "
                              "WHERE i.in_library = 1", (match,)).fetchone()[0]
        after = conn.execute("SELECT count(*) FROM library_fts WHERE library_fts MATCH ?", (match,)).fetchone()[0]
        before_total, after_total = before_total + before, after_total + after
        print(f"{query:<20}{before:>8}{after:>8}{after - before:>+8}")
    print(f"{'all':<20}{before_total:>8}{after_total:>8}{after_total - before_total:>+8}")
    print("\nMore isn't automatically better: open a few of the gained saves for a word you care about "
          "(search it in the Library tab) and check they really show it.")


if __name__ == "__main__":
    main()
