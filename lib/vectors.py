"""Search by meaning and find similar pictures, through tools/beacon-embed (SigLIP 2 for meaning,
Apple Vision for look-alikes; both on this Mac). The helper writes and reads the item_vectors table; this module
picks what to index and turns its answers into ranked item ids for the library feed.

`search` is the one search box: it fuses three rankings (the words themselves through FTS5, what the
pictures show, and what the save's own words mean) with reciprocal rank fusion, so nobody has to pick
words or meaning first. Without the helper it still answers, from words alone.
"""
import json
import subprocess
import sys
import threading
from contextlib import closing
from itertools import combinations
from pathlib import Path

from . import adds, db, useful

HELPER = Path(__file__).resolve().parent.parent / "tools/beacon-embed/.build/release/beacon-embed"
BATCH = 400
# A helper that is alive but silent this long is stopped. Generous, because a cold start loads SigLIP and every vector.
ANSWER_SECONDS = 60
# A save whose words also contain what you typed ranks a little higher than meaning alone puts it.
WORD_BONUS = 0.05
# Reciprocal rank fusion: a save scores 1/(RRF_K + rank) in each ranking it appears in. 60 is the usual
# constant; it keeps one list's top hit from drowning out a save that two lists both rank well.
RRF_K = 60
# How deep each ranking goes before fusion. Up to three times this many saves can come back.
POOL = 300
# A picture match this close to the top is a strong result even when nothing else agrees (the author's call, Nov 12).
TOP_PICTURES = 20
# How deep a follow-up step looks across the whole library before keeping what's already in the results.
REFINE_POOL = 2000
# The words a save is read by (title and text), cut well past SigLIP's 64 tokens.
WORDS_CHARS = 400


class _Resident:
    """`beacon-embed serve`, started once by `beacon serve` and kept running.

    Starting the helper loads SigLIP and every vector, which took seconds per search; kept running,
    a meaning search takes milliseconds. One request at a time; a helper that dies is started again.
    """

    def __init__(self):
        self.process = None
        self.lock = threading.Lock()

    def ask(self, request):
        with self.lock:
            for _ in range(2):
                if self.process is None or self.process.poll() is not None:
                    if not HELPER.exists():
                        raise RuntimeError("beacon-embed isn't built: run `swift build -c release` in tools/beacon-embed.")
                    self.process = subprocess.Popen([str(HELPER), "serve", "--db", db.current_path()], stdin=subprocess.PIPE,
                                                    stdout=subprocess.PIPE, text=True, bufsize=1)
                process, silent = self.process, threading.Event()
                timer = threading.Timer(ANSWER_SECONDS, lambda: (silent.set(), process.kill()))
                timer.start()
                try:
                    process.stdin.write(json.dumps(request) + "\n")
                    process.stdin.flush()
                    line = process.stdout.readline()
                except OSError:
                    line = ""
                finally:
                    timer.cancel()
                if silent.is_set() and not line:  # an answer that beat the deadline still counts
                    process.wait()
                    self.process = None
                    raise RuntimeError(f"beacon-embed didn't answer within {ANSWER_SECONDS} s; it was stopped and the next search starts it again")
                if line:
                    answer = json.loads(line)
                    if isinstance(answer, dict) and "error" in answer:
                        raise RuntimeError(answer["error"] or "beacon-embed failed")
                    return answer
                self.process = None  # it died; start it again once
        raise RuntimeError("beacon-embed stopped answering")


# Set by `beacon serve`: searches go to one long-running helper instead of a new one each time.
resident = None


def warm():
    """Start the resident helper with a tiny search, so its model is loaded before anyone asks."""
    try:
        if resident:
            resident.ask({"meaning": "warm", "limit": 1})
    except RuntimeError as error:
        print(f"beacon-embed didn't start: {error}", file=sys.stderr)


def _search(kind, value, limit):
    if resident:
        return resident.ask({kind: value, "limit": limit})
    return json.loads(_run(kind, "--query" if kind == "meaning" else "--id", str(value), "--limit", str(limit)))


def _run(*args, stdin=None, timeout=120):
    if not HELPER.exists():
        raise RuntimeError("beacon-embed isn't built: run `swift build -c release` in tools/beacon-embed.")
    result = subprocess.run([str(HELPER), *args, "--db", db.current_path()], input=stdin, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "beacon-embed failed")
    return result.stdout


def _words(title, body):
    """What a save says, for its words vector: the title, then the text when it adds anything."""
    title, body = (title or "").strip(), (body or "").strip()
    text = body if not title or body.startswith(title) else f"{title}. {body}" if body else title
    return db._TCO.sub("", text).strip()[:WORDS_CHARS]


def _has_words_column(conn):
    """Add item_vectors.words_vec if an older helper made the table without it (the helper does the same)."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(item_vectors)")}
    if "words_vec" not in columns:
        conn.execute("ALTER TABLE item_vectors ADD COLUMN words_vec BLOB")
        conn.commit()


def pending(limit, ids=None):
    """Library saves with something left to read, newest first, as helper jobs.

    A job carries the save's first picture when its picture hasn't been read (a save whose picture
    couldn't be read last time is tried again, so a dropped connection doesn't leave it out of search
    for good), and its words when they haven't. GitHub stars and other saves without a picture get
    words only, which is what makes them findable by meaning at all. The helper owns the item_vectors
    table; before its first run there is none, and every library save is pending.
    """
    picture = ("i.source IN ('x', 'add') AND json_valid(i.raw_json) "
               "AND json_array_length(json_extract(i.raw_json, '$.media')) > 0")
    worded = "trim(coalesce(i.title, '') || coalesce(i.body, '')) != ''"
    with closing(db.get_connection()) as conn:
        indexed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_vectors'").fetchone()
        if indexed:
            _has_words_column(conn)
            needs_picture = f"({picture} AND v.text_vec IS NULL)"
            needs_words = f"({worded} AND v.words_vec IS NULL)"
            sql = (f"SELECT i.id, i.title, i.body, i.raw_json, {needs_picture} AS needs_picture, {needs_words} AS needs_words "
                   f"FROM items i LEFT JOIN item_vectors v ON v.item_id = i.id "
                   f"WHERE i.in_library = 1 AND ({needs_picture} OR {needs_words})")
        else:
            sql = (f"SELECT i.id, i.title, i.body, i.raw_json, ({picture}) AS needs_picture, ({worded}) AS needs_words "
                   f"FROM items i WHERE i.in_library = 1 AND (({picture}) OR ({worded}))")
        only = f" AND i.id IN ({','.join('?' * len(ids))})" if ids else ""
        rows = conn.execute(sql + only + " ORDER BY i.id DESC LIMIT ?", (*(ids or ()), limit)).fetchall()
    jobs = []
    for row in rows:
        image = None
        if row["needs_picture"]:
            media = (db.item_preview("x", row["raw_json"], row["body"]).get("media") or [{}])[0]
            image = media.get("preview_url") if media.get("type") in ("video", "animated_gif") else media.get("url")
            if image and image.startswith("/media/"):  # a picture you added, stored on this Mac
                image = (adds.MEDIA_DIR / image.removeprefix("/media/")).as_uri()
        words = _words(row["title"], row["body"]) if row["needs_words"] else None
        if image or words:
            jobs.append({"id": row["id"], "image": image, "words": words or None})
    return jobs


def index(limit=1000, ids=None):
    """Read up to `limit` library saves (or just `ids`), in batches so a stop keeps what's done."""
    totals = {"indexed": 0, "pictures_failed": 0, "words_read": 0}
    jobs = pending(limit, ids)
    for start in range(0, len(jobs), BATCH):
        batch = jobs[start:start + BATCH]
        summary = json.loads(_run("index", stdin="\n".join(json.dumps(job) for job in batch), timeout=1800))
        for key in totals:
            totals[key] += summary.get(key, 0)
    return totals


# One save read for search at a time after its keep was answered: each read loads the model.
_reading_later = threading.Lock()


def index_later(item_id, read):
    """Run `read(item_id)` on a background thread, so a keep is answered once the save is committed.

    Callers wait less than indexing can take (the phone 120 s, an agent's tool call often less; indexing
    up to 30 minutes), and the save has already landed, so waiting would only report it as failed.
    A failure is logged; the weekly `beacon vectors` picks the save up again.
    """
    def work():
        with _reading_later:
            try:
                read(item_id)
            except Exception as error:
                print(f"save {item_id} not indexed yet ({error}); the weekly `beacon vectors` will",
                      file=sys.stderr, flush=True)
    threading.Thread(target=work, name=f"index-{item_id}", daemon=True).start()


def meaning(query, limit=300):
    """[(item_id, score)] by meaning, nudged up where the words themselves appear."""
    ranked = [(int(item_id), score) for item_id, score in _search("meaning", query, limit)]
    fts = db.fts_query(query)
    if ranked and fts:
        # Every save with all the words, then keep the ranked ones: one index lookup (asking about
        # each ranked id took 140 ms).
        with closing(db.get_connection()) as conn:
            words = {row[0] for row in conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?", (fts,))}
        ranked = sorted(((i, s + (WORD_BONUS if i in words else 0)) for i, s in ranked), key=lambda pair: -pair[1])
    return ranked


def fuse(*rankings, k=RRF_K):
    """Reciprocal rank fusion of rankings (item ids, best first) into one, best first.

    Ties keep the order in which saves were first seen, so the words ranking (passed first) breaks them.
    """
    scores, first = {}, {}
    for ranking in rankings:
        for position, item_id in enumerate(dict.fromkeys(ranking)):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + position + 1)
            first.setdefault(item_id, len(first))
    return sorted(scores, key=lambda item_id: (-scores[item_id], first[item_id]))


def _singular(term):
    """The singular of a plain English plural, or None: chairs -> chair, stories -> story. Leaves glass, bus,
    and anything with a digit, underscore or capital inside alone, so codes and names aren't mangled."""
    if len(term) < 4 or not term.isalpha() or not term.islower() or term.endswith(("ss", "us", "is")):
        return None
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    return term[:-1] if term.endswith("s") else None


def _word_query(query):
    """FTS5 query: every word must appear, a plural also matching its singular ("chairs" finds "chair")."""
    groups = []
    for term in query.split():
        quoted = '"' + term.replace('"', '""') + '"'
        single = _singular(term)
        groups.append(f'({quoted} OR "{single}")' if single else quoted)
    return " AND ".join(groups)


def word_ranking(query, limit=POOL, *, partial=False):
    """Library saves containing every word (or its singular), best FTS5 match first."""
    if partial:
        terms = useful.task_terms(query)[:12]
        fts = " OR ".join(f'("{first}" AND "{second}")' for first, second in combinations(terms, 2))
    else:
        fts = _word_query(query)
    if not fts:
        return []
    with closing(db.get_connection()) as conn:
        return [row[0] for row in conn.execute(
            "SELECT i.id FROM library_fts f JOIN items i ON i.id = f.rowid "
            "WHERE library_fts MATCH ? AND i.in_library = 1 ORDER BY f.rank LIMIT ?", (fts, limit))]


def _meaning_rankings(query, limit):
    """{"pictures": [...], "words": [...]} from the helper, each [[id, score], ...]. Raises if it can't answer."""
    if resident:
        answer = resident.ask({"search": query, "limit": limit})
    else:
        answer = json.loads(_run("search", "--query", query, "--limit", str(limit)))
    if not isinstance(answer, dict) or not isinstance(answer.get("pictures"), list) or not isinstance(answer.get("words"), list):
        raise RuntimeError("beacon-embed gave an unexpected search answer")
    return answer


def rankings(query, limit=POOL):
    """[(name, [item_id, ...])] for one search: "words" (FTS5), then "pictures" (what the pictures show) and
    "meaning" (what the saves' words mean) when the helper answers. A helper too old to answer both
    meanings falls back to pictures only; no helper at all leaves words, so search still works on an
    install without the Swift helper."""
    query = query.strip()
    if not query:
        return []
    found = [("words", word_ranking(query, limit))]
    if not found[0][1] and len(query.split()) >= 6:
        try:
            keywords = word_ranking(query, min(limit, 60), partial=True)
        except ValueError:  # the prompt contains no usable keyword
            keywords = []
        if keywords:
            found = [("keywords", keywords)]
    try:
        both = _meaning_rankings(query, limit)
        found += [("pictures", [int(i) for i, _ in both["pictures"]]), ("meaning", [int(i) for i, _ in both["words"]])]
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
        try:  # a helper built before `search` existed still answers by picture
            found.append(("pictures", [int(i) for i, _ in _search("meaning", query, limit)]))
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return found


def search(query, limit=POOL):
    """One search box: ([item_id, ...] best first, [what answered])."""
    found = rankings(query, limit)
    return _fuse_search(found), [name for name, _ in found]


def _fuse_search(found):
    # Give concrete keyword hits the same possible weight as both meaning lists combined.
    return fuse(*(ranking for _, ranking in found), *(ranking for name, ranking in found if name == "keywords"))


def _found_by(found):
    """{item_id: [names of the rankings that found it]}."""
    by = {}
    for name, ranking in found:
        for item_id in ranking:
            if name not in by.setdefault(item_id, []):
                by[item_id].append(name)
    return by


def refined(query, steps=(), limit=POOL):
    """A search, then each follow-up step ("wooden", "on a table") narrowing what's there.

    Returns (ranked ids, {id: names that found it}, what answered, how many saves the steps set aside,
    the search's top TOP_PICTURES picture matches).
    A step keeps the saves its own rankings find inside the results so far (meaning ranked over the
    whole library, REFINE_POOL deep) and orders them by both. Ranking-based, so a step narrows roughly:
    SigLIP scores aren't calibrated enough for a yes-or-no cut.
    """
    found = rankings(query, limit)
    ranked, by = _fuse_search(found), _found_by(found)
    top_pictures = {i for name, r in found if name == "pictures" for i in r[:TOP_PICTURES]}
    answered = [name for name, _ in found]
    set_aside = 0
    for step in steps:
        if not step.strip() or not ranked:
            continue
        inside = set(ranked)
        step_found = [(name, [i for i in r if i in inside]) for name, r in rankings(step, REFINE_POOL)]
        kept = {i for _, r in step_found for i in r}
        set_aside += len(inside - kept)
        ranked = [i for i in fuse(*(r for _, r in step_found), ranked) if i in kept]
        by = {i: by.get(i, []) for i in ranked}
    return ranked, by, answered, set_aside, top_pictures


def fit(names, top_picture=False):
    """"strong" when the words match, two rankings agree, or it's one of the closest picture matches (the
    Library is a picture wall, so a picture that plainly shows the search belongs in the grid); "maybe" for
    one meaning alone further down, which the Library shows in a "might also fit" row instead of leaving out."""
    return "strong" if "words" in names or len(names) >= 2 or top_picture else "maybe"


def library_page(offset=0, limit=200, area=None, words=None, about=None, like=None, query=None, refine=()):
    """One page of the Library: newest first, the one search box (`query`: words and meaning fused, then
    any `refine` steps), saves with these `words`, saves `about` something (by meaning), or pictures `like`
    one save."""
    if query is not None and query.strip():
        ranked, by, answered, set_aside, top_pictures = refined(query, refine)
        page = db.get_library(offset=offset, limit=limit, area=area, ranked=ranked)
        for entry in page["items"]:
            entry["found_by"] = by.get(entry["id"], [])
            entry["fit"] = fit(entry["found_by"], entry["id"] in top_pictures)
        page["searched"] = answered
        if refine:
            page["set_aside"] = set_aside
        return page
    ranked = None
    if about or like:
        ranked = [item_id for item_id, _ in (meaning(about) if about else similar(like))]
    return db.get_library(offset=offset, limit=limit, area=area, query=None if ranked is not None else words, ranked=ranked)


def similar(item_id, limit=300):
    """[(item_id, score)] of pictures that look like item_id's."""
    return [(int(i), score) for i, score in _search("similar", item_id, limit)]
