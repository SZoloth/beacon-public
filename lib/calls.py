"""Calls: check Beacon's keep/pass calls, ten cards a session, so its mistakes get fixed and become examples.

Beacon's pile run (lib/pile.py) made thousands of keep/pass calls from about a hundred of the owner's own
decisions. A session shows ten of those saves with Beacon's call hidden and asks "keep or pass?". Two kinds of
card are mixed so the owner can't tell them apart:

- random: sampled uniformly inside one group (readable passes, near-empty passes, keeps). Only these feed the
  estimate of how often each group is wrong, so a group's estimate is never pooled with another's.
- suspect: the calls most likely wrong. A call's suspect score is the share of its 8 nearest owner decisions
  (gists.nearest, EmbeddingGemma 2) that went the other way. `rank()` computes it nightly; requests only read it.
- repeat: one card a session re-asks a check at least 7 days old, to measure the owner's own consistency.

Every check is the owner's own decision, so `gists.examples()` shows it to later model calls. A disagreement
changes the call (pile_decisions, the save's status and routing) and is recorded in call_overrides, so it can be
undone. A change also flags its nearest passes; they lead the next session's suspects. Nothing is re-decided
silently: a group whose estimate is clearly bad gets an offer the owner has to accept (`redecide`).
"""
import json
import math
import random
import secrets
import subprocess
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import db, gists, pick, pile, settings

GROUPS = {
    "readable": {"label": "Readable passes", "sql": "p.outcome = 'pass' AND length(COALESCE(i.body, '')) >= 40"},
    "near-empty": {"label": "Near-empty passes", "sql": "p.outcome = 'pass' AND length(COALESCE(i.body, '')) < 40"},
    "keeps": {"label": "Keeps", "sql": "p.outcome IN ('do', 'read', 'reference')"},
}
RANDOM_SHARE = {"readable": 3, "near-empty": 1, "keeps": 1}  # of every 5 random cards
SIZE = 10
PAIRS = 2
ANSWERS = ("keep", "pass", "unsure")
KINDS = ("random", "suspect", "repeat")
NEIGHBOURS = 8
REPEAT_AFTER = timedelta(days=7)
Z90 = 1.6449  # two-sided 90%
MIN_CHECKS = 10  # random checks in a group before its range is shown
CLOSE_BELOW = 0.05  # a group whose upper bound falls under this needs no more random checks
OFFER_ABOVE = 0.15  # a group whose lower bound rises above this gets the re-decide offer
COST_PER_SAVE = 0.0002  # Haiku re-decide, about $0.20 per 1,000 saves (the pile run's rate)
EMBED_BLOCK = 2000
CHECK_NOTE = "Passed in a check"

# A call that still stands: nobody (the owner, a check) has decided the save since Beacon called it.
STANDING_SQL = (
    "NOT EXISTS (SELECT 1 FROM call_overrides o WHERE o.item_id = i.id AND o.undone_at IS NULL AND o.source = 'check') "
    "AND NOT EXISTS (SELECT 1 FROM choice_log c WHERE c.item_id = i.id AND c.source = 'you') "
    "AND ((p.outcome = 'pass' AND (i.status IN ('new', 'stale') OR (i.status = 'rejected' AND EXISTS "
    "(SELECT 1 FROM auto_actions a WHERE a.item_id = i.id AND a.undone_at IS NULL)))) "
    "OR (p.outcome != 'pass' AND (i.status IN ('new', 'queued', 'stale') "
    "OR (i.status = 'adopted' AND i.status_note = 'reference')))) "
    "AND " + db.NOT_HELD_SQL)
# shortcut: a save on an unanswered card of an older session can be drawn again by `session --new`; answering both
# counts it twice. Rare (a new session starts once the last is done), so cards aren't reserved across sessions.
UNCHECKED_SQL = "NOT EXISTS (SELECT 1 FROM call_checks k WHERE k.item_id = i.id AND k.voided_at IS NULL)"


def _now():
    return datetime.now(timezone.utc).isoformat()


def side(outcome):
    """Beacon's call as keep or pass: do, read and reference all keep the save."""
    return "pass" if outcome == "pass" else "keep"


def _example_side(choice):
    """Which way an owner decision from gists.examples() went: keep, pass, or None if it says neither."""
    if choice.startswith("kept") or choice == gists.CHOSE:
        return "keep"
    if choice.startswith("passed") or choice == gists.TURNED_DOWN:
        return "pass"
    return None


def wilson(changed, n, z=Z90):
    """Wilson score interval for changed/n, as (low, high); (None, None) with no checks."""
    if n <= 0:
        return None, None
    p = changed / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    return max(0.0, (centre - spread) / denominator), min(1.0, (centre + spread) / denominator)


def group_of(outcome, body):
    if outcome != "pass":
        return "keeps"
    return "readable" if len(body or "") >= 40 else "near-empty"


def _pool(conn, group):
    return [r[0] for r in conn.execute(
        "SELECT i.id FROM pile_decisions p JOIN items i ON i.id = p.item_id "
        f"WHERE {GROUPS[group]['sql']} AND {STANDING_SQL} AND {UNCHECKED_SQL} ORDER BY i.id")]


def _standing_count(conn, group):
    return conn.execute("SELECT COUNT(*) FROM pile_decisions p JOIN items i ON i.id = p.item_id "
                        f"WHERE {GROUPS[group]['sql']} AND {STANDING_SQL}").fetchone()[0]


# Estimate

def estimate(conn):
    """Per group: random checks (can't-tell left out), how many you changed, the 90% range, closed or not."""
    rows = conn.execute("SELECT stratum, beacon_outcome, your_outcome FROM call_checks "
                        "WHERE kind = 'random' AND voided_at IS NULL AND your_outcome IS NOT NULL").fetchall()
    result = []
    for name, about in GROUPS.items():
        mine = [r for r in rows if r["stratum"] == name]
        changed = sum(side(r["beacon_outcome"]) != r["your_outcome"] for r in mine)
        low, high = wilson(changed, len(mine))
        enough = len(mine) >= MIN_CHECKS
        result.append({"name": name, "label": about["label"], "checked_random": len(mine), "changed_random": changed,
                       "low": round(low, 3) if enough else None, "high": round(high, 3) if enough else None,
                       "enough": enough, "closed": enough and high < CLOSE_BELOW})
    return result


# Sessions

def _card_item(item):
    """What a card shows: the save itself. Never Beacon's call, takeaway, group or kind."""
    preview = item.get("preview") or {}
    card = {"id": item["id"], "source": item["source"], "author": item.get("author"),
            "text": preview.get("text") or item.get("body") or item.get("title") or "", "url": item.get("url"),
            "media": preview.get("media") or []}
    for extra in ("quoted", "repo", "link"):
        if preview.get(extra):
            card[extra] = preview[extra]
    return card


def _load_items(ids):
    with closing(db.get_connection()) as conn:
        found = {}
        for start in range(0, len(ids), 500):
            part = list(ids)[start:start + 500]
            for r in conn.execute("SELECT id, source, title, body, url, author, raw_json FROM items "
                                  f"WHERE id IN ({','.join('?' * len(part))})", part):
                found[r["id"]] = {**dict(r), "preview": db.item_preview(r["source"], r["raw_json"], r["body"])}
    return found


def _repeat(conn, now, exclude):
    """A check at least 7 days old, never repeated, whose save is in no session card now."""
    cutoff = (now - REPEAT_AFTER).isoformat()
    for r in conn.execute("SELECT k.id, k.item_id, k.stratum FROM call_checks k WHERE k.voided_at IS NULL "
                          "AND k.your_outcome IS NOT NULL AND k.kind != 'repeat' AND k.checked_at <= ? "
                          "AND NOT EXISTS (SELECT 1 FROM call_checks r WHERE r.repeat_of = k.id AND r.voided_at IS NULL) "
                          "ORDER BY k.checked_at", (cutoff,)):
        if r["item_id"] not in exclude:
            return r
    return None


def _suspects(conn, want, exclude):
    """Flagged neighbours of your changes first (newest change first), then the highest suspect scores."""
    picked, flags = [], []
    for r in conn.execute("SELECT f.item_id FROM call_flags f JOIN pile_decisions p ON p.item_id = f.item_id "
                          f"JOIN items i ON i.id = f.item_id WHERE f.used_session IS NULL AND {STANDING_SQL} "
                          f"AND {UNCHECKED_SQL} ORDER BY f.change_id DESC, f.item_id"):
        if len(picked) < want and r[0] not in exclude:
            picked.append(r[0])
            flags.append(r[0])
            exclude.add(r[0])
    if len(picked) < want:
        for r in conn.execute("SELECT s.item_id FROM call_suspects s JOIN pile_decisions p ON p.item_id = s.item_id "
                              f"JOIN items i ON i.id = s.item_id WHERE {STANDING_SQL} AND {UNCHECKED_SQL} "
                              "ORDER BY s.score DESC, s.item_id"):
            if len(picked) >= want:
                break
            if r[0] not in exclude:
                picked.append(r[0])
                exclude.add(r[0])
    return picked, flags


def _group(conn, item_id):
    row = conn.execute("SELECT p.outcome, i.body FROM pile_decisions p JOIN items i ON i.id = p.item_id "
                       "WHERE p.item_id = ?", (item_id,)).fetchone()
    return group_of(row["outcome"], row["body"])


def build(size=SIZE, *, rng=None, now=None):
    """Plan a new session: random cards by group quota, suspects, one repeat once a check is a week old.

    Returns the stored cards, shuffled. A closed group's random slots go to suspects; if there aren't enough
    suspects (no ranking yet), the open groups fill the rest with more random cards."""
    rng, now = rng or random.Random(), now or datetime.now(timezone.utc)
    random_slots = size // 2
    with closing(db.get_connection()) as conn, conn:
        closed = {g["name"] for g in estimate(conn) if g["closed"]}
        quota = {g: round(random_slots * share / 5) for g, share in RANDOM_SHARE.items()}
        quota["readable"] += random_slots - sum(quota.values())
        cards, used, pools = [], set(), {}

        def pool(group):  # each group's standing, unchecked saves, read once per session
            if group not in pools:
                pools[group] = _pool(conn, group)
            return [i for i in pools[group] if i not in used]

        for group, n in quota.items():
            if group in closed:
                continue
            ids = pool(group)
            for item_id in rng.sample(ids, min(n, len(ids))):
                cards.append({"item_id": item_id, "kind": "random", "stratum": group})
                used.add(item_id)
        repeat = _repeat(conn, now, used)
        if repeat and len(cards) < size:
            cards.append({"item_id": repeat["item_id"], "kind": "repeat", "stratum": repeat["stratum"],
                          "repeat_of": repeat["id"]})
            used.add(repeat["item_id"])
        suspect_ids, flagged = _suspects(conn, size - len(cards), used)
        cards += [{"item_id": i, "kind": "suspect", "stratum": _group(conn, i)} for i in suspect_ids]
        open_groups = [g for g in GROUPS if g not in closed] or list(GROUPS)
        while len(cards) < size:
            filled = False
            for group in sorted(open_groups, key=lambda g: sum(c["stratum"] == g for c in cards)):
                ids = pool(group)
                if ids:
                    item_id = rng.choice(ids)
                    cards.append({"item_id": item_id, "kind": "random", "stratum": group})
                    used.add(item_id)
                    filled = True
                    break
            if not filled:
                break
        rng.shuffle(cards)
        session_id = secrets.token_hex(8)
        for position, card in enumerate(cards, 1):
            card.update(card_id=secrets.token_hex(4), position=position)
        conn.execute("INSERT INTO call_sessions(id, size, cards_json, created_at) VALUES(?,?,?,?)",
                     (session_id, size, json.dumps(cards), now.isoformat()))
        conn.executemany("UPDATE call_flags SET used_session = ? WHERE item_id = ?", [(session_id, i) for i in flagged])
    return session_id, cards


def _open_session(conn):
    """The newest session, if a card in it is not yet answered."""
    row = conn.execute("SELECT id, cards_json FROM call_sessions ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    if row is None:
        return None
    done = {r[0] for r in conn.execute("SELECT card_id FROM call_checks WHERE session_id = ? AND voided_at IS NULL",
                                       (row["id"],))}
    cards = json.loads(row["cards_json"])
    return (row["id"], cards) if any(c["card_id"] not in done for c in cards) else None


def _pairs():
    """Two pairs from the current Pick round: one matched pair and one repeat, not yet picked.

    Only rounds built after the scorer fix (no foils or crosses) are used; a session never builds a round."""
    round_ = pick.current()
    if not round_.get("round_id") or not round_.get("fresh"):
        return []
    done = {p["position"] for p in round_["picks"]}
    waiting = [p for p in round_["pairs"] if p["position"] not in done]
    chosen = [next((p for p in waiting if p["kind"] == kind), None) for kind in ("matched", "repeat")]
    return [{**p, "round_id": round_["round_id"]} for p in chosen if p][:PAIRS]


def session(size=SIZE, *, new=False, rng=None, now=None):
    """The open session (or a new one with `new`, or when none is open), as cards the app can show."""
    with closing(db.get_connection()) as conn:
        found = None if new else _open_session(conn)
    if found:
        session_id, cards = found
    else:
        session_id, cards = build(size, rng=rng, now=now)
    items = _load_items([c["item_id"] for c in cards])
    with closing(db.get_connection()) as conn:
        answered = {r[0]: r[1] for r in conn.execute(
            "SELECT card_id, COALESCE(your_outcome, 'unsure') FROM call_checks WHERE session_id = ? AND voided_at IS NULL",
            (session_id,))}
    shown = [{"card_id": c["card_id"], "position": c["position"], "item": _card_item(items[c["item_id"]]),
              "answer": answered.get(c["card_id"])}
             for c in sorted(cards, key=lambda c: c["position"]) if c["item_id"] in items]
    return {"session_id": session_id, "cards": shown, "chips": chips(), "pairs": _pairs()}


def chips():
    """The reasons you can tap on a change; each belongs to one taste axis, so it counts as 'you said'."""
    return [chip for axis in pick.AXES.values() for chip in axis["chips"]]


def _axis_of_chip(chip):
    return next((name for name, axis in pick.AXES.items() if chip in axis["chips"]), None)


# Checks, changes and undo

def _second_person(text):
    """Beacon's notes say 'you', never the owner's name or 'his'."""
    if not text:
        return text
    owner = settings.owner()
    for before, after in ((f"{owner}'s", "your"), (owner, "you")):
        if before and before.strip():
            text = text.replace(before, after)
    return text


def _routes(conn, item_id):
    return [dict(r) for r in conn.execute(
        "SELECT w.slug, iw.confidence, iw.routed, iw.routed_at FROM item_workstreams iw "
        "JOIN workstreams w ON w.id = iw.workstream_id WHERE iw.item_id = ?", (item_id,))]


def _active_destination(conn, slug):
    return bool(slug) and conn.execute("SELECT 1 FROM workstreams WHERE slug = ? AND active = 1", (slug,)).fetchone() is not None


def suggested_destination(conn, item_id):
    """Where a kept save should go: Beacon's own destination for it, else the one its kept neighbours share."""
    row = conn.execute("SELECT destination FROM pile_decisions WHERE item_id = ?", (item_id,)).fetchone()
    if row and _active_destination(conn, row[0]):
        return row[0]
    row = conn.execute("SELECT suggested_destination FROM call_suspects WHERE item_id = ?", (item_id,)).fetchone()
    return row[0] if row and _active_destination(conn, row[0]) else None


def _apply(conn, item_id, outcome, destination, status, source, check_id=None):
    """Change one call inside the caller's transaction: pile_decisions, routing and status (None leaves status).

    Writes a call_overrides row with everything needed to put it back, and returns its id."""
    pile_row = conn.execute("SELECT outcome, destination FROM pile_decisions WHERE item_id = ?", (item_id,)).fetchone()
    item = conn.execute("SELECT status, status_note, status_at FROM items WHERE id = ?", (item_id,)).fetchone()
    routes = _routes(conn, item_id)
    now = _now()
    conn.execute("UPDATE pile_decisions SET outcome = ?, destination = ?, decided_at = ? WHERE item_id = ?",
                 (outcome, destination, now, item_id))
    if destination and outcome in ("do", "reference"):
        ws = conn.execute("SELECT id FROM workstreams WHERE slug = ?", (destination,)).fetchone()
        conn.execute("INSERT INTO item_workstreams(item_id, workstream_id, confidence, routed, routed_at) VALUES(?,?,?,1,?) "
                     "ON CONFLICT(item_id, workstream_id) DO UPDATE SET confidence = excluded.confidence, "
                     "routed = excluded.routed, routed_at = excluded.routed_at", (item_id, ws["id"], pile.CONFIDENCE, now))
    if status:
        db.set_item_status(conn, item_id, status[0], status[1])
    return conn.execute(
        "INSERT INTO call_overrides(item_id, check_id, source, old_outcome, old_destination, old_status, old_status_note, "
        "old_status_at, old_routes_json, new_outcome, new_destination, new_status, at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, check_id, source, pile_row["outcome"], pile_row["destination"], item["status"], item["status_note"],
         item["status_at"], json.dumps(routes), outcome, destination, status[0] if status else item["status"], now)).lastrowid


def _restore(conn, change_id):
    """Put a change back exactly as it was. False if it was already undone."""
    o = conn.execute("SELECT * FROM call_overrides WHERE id = ?", (change_id,)).fetchone()
    if o is None:
        raise ValueError("unknown change")
    if o["undone_at"]:
        return False
    conn.execute("UPDATE pile_decisions SET outcome = ?, destination = ? WHERE item_id = ?",
                 (o["old_outcome"], o["old_destination"], o["item_id"]))
    conn.execute("UPDATE items SET status = ?, status_note = ?, status_at = ? WHERE id = ?",
                 (o["old_status"], o["old_status_note"], o["old_status_at"], o["item_id"]))
    before = {r["slug"]: r for r in json.loads(o["old_routes_json"])}
    for route in _routes(conn, o["item_id"]):
        old = before.get(route["slug"])
        ws = conn.execute("SELECT id FROM workstreams WHERE slug = ?", (route["slug"],)).fetchone()["id"]
        if old is None:
            conn.execute("DELETE FROM item_workstreams WHERE item_id = ? AND workstream_id = ?", (o["item_id"], ws))
        else:
            conn.execute("UPDATE item_workstreams SET confidence = ?, routed = ?, routed_at = ? "
                         "WHERE item_id = ? AND workstream_id = ?",
                         (old["confidence"], old["routed"], old["routed_at"], o["item_id"], ws))
    now = _now()
    conn.execute("UPDATE call_overrides SET undone_at = ? WHERE id = ?", (now, change_id))
    if o["check_id"]:
        conn.execute("UPDATE call_checks SET voided_at = ? WHERE id = ?", (now, o["check_id"]))
    return True


def _card(conn, session_id, card_id):
    row = conn.execute("SELECT cards_json FROM call_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise ValueError("unknown session")
    card = next((c for c in json.loads(row["cards_json"]) if c["card_id"] == card_id), None)
    if card is None:
        raise ValueError("no such card in this session")
    return card


def check(session_id, card_id, answer, *, destination=None, chip=None):
    """Record your keep/pass/can't-tell on one card. Answering a card again replaces the earlier answer
    (and undoes its change). A disagreement changes Beacon's call; undo puts it back."""
    if answer not in ANSWERS:
        raise ValueError(f"answer must be one of {', '.join(ANSWERS)}")
    if chip is not None and chip not in chips():
        raise ValueError("chip must be one of the session's chips")
    with closing(db.get_connection()) as conn, conn:
        card = _card(conn, session_id, card_id)
        if destination is not None and not _active_destination(conn, destination):
            raise ValueError("destination must be an active project or area slug")
        for earlier in conn.execute("SELECT id FROM call_checks WHERE session_id = ? AND card_id = ? AND voided_at IS NULL",
                                    (session_id, card_id)).fetchall():
            for change in conn.execute("SELECT id FROM call_overrides WHERE check_id = ? AND undone_at IS NULL",
                                       (earlier["id"],)).fetchall():
                _restore(conn, change["id"])
            conn.execute("UPDATE call_checks SET voided_at = ? WHERE id = ?", (_now(), earlier["id"]))
        item_id = card["item_id"]
        call = conn.execute("SELECT outcome, destination, takeaway FROM pile_decisions WHERE item_id = ?",
                            (item_id,)).fetchone()
        if call is None:
            raise ValueError("this save has no call to check")
        yours = None if answer == "unsure" else answer
        agreed = yours == side(call["outcome"])
        suggested = suggested_destination(conn, item_id)
        check_id = conn.execute(
            "INSERT INTO call_checks(session_id, card_id, item_id, stratum, kind, repeat_of, beacon_outcome, your_outcome, "
            "unsure, destination, chip, checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, card_id, item_id, card["stratum"], card["kind"], card.get("repeat_of"), call["outcome"], yours,
             int(yours is None), destination, chip, _now())).lastrowid
        change_id = None
        if yours == "keep" and not agreed:
            where = destination or suggested
            status = ("adopted", db.REFERENCE) if where else ("new", None)
            change_id = _apply(conn, item_id, "reference" if where else "read", where, status, "check", check_id)
        elif yours == "pass" and not agreed:
            change_id = _apply(conn, item_id, "pass", None, ("rejected", CHECK_NOTE), "check", check_id)
    takeaway = _second_person(call["takeaway"]) or None
    verb = "passed" if call["outcome"] == "pass" else "kept"
    return {"agreed": agreed, "unsure": yours is None, "beacon_outcome": side(call["outcome"]),
            "beacon_call": call["outcome"],
            "beacon_reason": f"Beacon {verb} this: {takeaway}" if takeaway else None,
            "suggested_destination": suggested, "change_id": change_id}


def undo(change_id):
    """Put one change back: the call, the save's status and routing. Its check stops counting."""
    with closing(db.get_connection()) as conn, conn:
        return {"restored": _restore(conn, change_id)}


# Summary (for you to read)

def _axes(conn):
    said = Counter(_axis_of_chip(r[0]) for r in conn.execute(
        "SELECT k.chip FROM call_checks k JOIN call_overrides o ON o.check_id = k.id AND o.undone_at IS NULL "
        "WHERE k.voided_at IS NULL AND k.chip IS NOT NULL"))
    rows = []
    for name, axis in pick.summary()["axes"].items():
        decisive, state = axis["decisive"], axis["state"]
        if decisive < 8:
            needed = 8 - decisive
        elif state in ("confirmed", "overturned") or decisive >= 15:
            needed = 0
        else:
            needed = 15 - decisive
        rows.append({"axis": name, "question": axis["question"], "decisive": decisive, "needed": needed,
                     "lean": round(axis["toward_keep"] / decisive, 3) if decisive >= 8 else None,
                     "state": state, "said": said.get(name, 0)})
    return rows


def _offer(conn, groups):
    for g in groups:
        if not g["enough"] or g["low"] is None or g["low"] <= OFFER_ABOVE:
            continue
        latest = conn.execute("SELECT MAX(checked_at) FROM call_checks WHERE stratum = ? AND kind = 'random' "
                              "AND voided_at IS NULL", (g["name"],)).fetchone()[0]
        done = conn.execute("SELECT 1 FROM call_redecides WHERE stratum = ? AND started_at >= ?",
                            (g["name"], latest)).fetchone()
        if done:
            continue
        items = len(_pool(conn, g["name"]))
        return {"group": g["name"], "label": g["label"], "items": items, "cost_usd": round(items * COST_PER_SAVE, 2)}
    return None


def summary(session_id=None):
    with closing(db.get_connection()) as conn:
        if session_id is None:
            row = conn.execute("SELECT id FROM call_sessions ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
            session_id = row[0] if row else None
        mine = conn.execute("SELECT k.your_outcome, k.unsure, o.id AS change FROM call_checks k "
                            "LEFT JOIN call_overrides o ON o.check_id = k.id AND o.undone_at IS NULL "
                            "WHERE k.session_id = ? AND k.voided_at IS NULL", (session_id,)).fetchall()
        groups = estimate(conn)
        for g in groups:
            g["standing"] = _standing_count(conn, g["name"])
        repeats = conn.execute("SELECT r.your_outcome = k.your_outcome FROM call_checks r JOIN call_checks k ON k.id = r.repeat_of "
                               "WHERE r.voided_at IS NULL AND r.your_outcome IS NOT NULL AND k.your_outcome IS NOT NULL").fetchall()
        flagged = conn.execute("SELECT COUNT(*) FROM call_flags f JOIN pile_decisions p ON p.item_id = f.item_id "
                               f"JOIN items i ON i.id = f.item_id WHERE f.used_session IS NULL AND {STANDING_SQL} "
                               f"AND {UNCHECKED_SQL}").fetchone()[0]
        changes = [dict(r) for r in conn.execute(
            "SELECT o.id AS change_id, o.item_id, o.old_outcome, o.new_outcome FROM call_overrides o "
            "JOIN call_checks k ON k.id = o.check_id WHERE k.session_id = ? AND o.undone_at IS NULL ORDER BY o.id",
            (session_id,))]
        result = {"session_id": session_id,
                  "session": {"checked": len(mine), "changed": sum(r["change"] is not None for r in mine),
                              "unsure": sum(r["unsure"] for r in mine)},
                  "changes": changes, "groups": groups, "flagged_next": flagged,
                  "repeats": {"checked": len(repeats), "same": sum(r[0] for r in repeats)},
                  "axes": _axes(conn), "offer": _offer(conn, groups)}
    with _background_lock:
        result["redecide"] = dict(_background)
    return result


# Nightly ranking and flags (EmbeddingGemma; never on a request)

def _standing_items(conn, groups=tuple(GROUPS)):
    where = " OR ".join(f"({GROUPS[g]['sql']})" for g in groups)
    return [r[0] for r in conn.execute("SELECT i.id FROM pile_decisions p JOIN items i ON i.id = p.item_id "
                                       f"WHERE ({where}) AND {STANDING_SQL} AND {UNCHECKED_SQL} ORDER BY i.id")]


def suspect_score(outcome, neighbours):
    """Share of the neighbours (owner decisions) that went the other way from Beacon's call."""
    sides = [_example_side(e["choice"]) for e in neighbours]
    sides = [s for s in sides if s]
    if not sides:
        return None
    return sum(s != side(outcome) for s in sides) / len(sides)


def rank(*, nearest=None, block=EMBED_BLOCK):
    """Score every standing call by its nearest owner decisions, and flag the nearest passes of new changes.

    Minutes on this Mac (EmbeddingGemma). Run nightly; sessions only read what it stores."""
    nearest = nearest or gists.nearest
    started = time.monotonic()
    pool = gists.examples()
    with closing(db.get_connection()) as conn:
        ids = _standing_items(conn)
        outcomes = dict(conn.execute("SELECT item_id, outcome FROM pile_decisions").fetchall())
        slug = {r["name"]: r["slug"] for r in conn.execute("SELECT name, slug FROM workstreams WHERE active = 1")}
    rows = []
    for start in range(0, len(ids), block):
        items = list(_load_items(ids[start:start + block]).values())
        near = nearest(items, pool, k=NEIGHBOURS)
        if pool and items and not near:
            raise RuntimeError("No neighbours came back (uv or the embedding model failed); kept the last ranking.")
        for item in items:
            neighbours = near.get(item["id"], [])
            score = suspect_score(outcomes[item["id"]], neighbours)
            if score is None:
                continue
            kept_where = Counter(slug.get(e.get("where")) for e in neighbours
                                 if _example_side(e["choice"]) == "keep" and slug.get(e.get("where")))
            rows.append((item["id"], score, len(neighbours), kept_where.most_common(1)[0][0] if kept_where else None, _now()))
    with closing(db.get_connection()) as conn, conn:
        conn.execute("DELETE FROM call_suspects")
        conn.executemany("INSERT INTO call_suspects(item_id, score, neighbours, suggested_destination, ranked_at) "
                         "VALUES(?,?,?,?,?)", rows)
    flagged = flag(nearest=nearest)
    return {"ranked": len(rows), "standing": len(ids), "examples": len(pool), "flagged": flagged,
            "seconds": round(time.monotonic() - started, 1)}


def flag(*, nearest=None):
    """For each change not yet flagged: its 8 nearest passes that still stand lead the next session."""
    nearest = nearest or gists.nearest
    with closing(db.get_connection()) as conn:
        changes = conn.execute("SELECT id, item_id FROM call_overrides WHERE source = 'check' AND undone_at IS NULL "
                               "AND flagged_at IS NULL ORDER BY id").fetchall()
        passes = _standing_items(conn, ("readable", "near-empty")) if changes else []
    if not changes:
        return 0
    loaded = _load_items(passes + [c["item_id"] for c in changes])
    pool = [{"id": i, "text": gists._text(loaded[i])} for i in passes if i in loaded]
    changed = [loaded[c["item_id"]] for c in changes if c["item_id"] in loaded]
    near = nearest(changed, pool, k=NEIGHBOURS) if pool else {}
    if pool and changed and not near:
        raise RuntimeError("No neighbours came back (uv or the embedding model failed); changes stay unflagged.")
    rows = [(n["id"], c["id"], _now()) for c in changes for n in near.get(c["item_id"], [])]
    with closing(db.get_connection()) as conn, conn:
        conn.executemany("INSERT OR IGNORE INTO call_flags(item_id, change_id, flagged_at) VALUES(?,?,?)", rows)
        conn.executemany("UPDATE call_overrides SET flagged_at = ? WHERE id = ?", [(_now(), c["id"]) for c in changes])
    return len(rows)


# Re-decide a group (only when you accept the offer)

def redecide(group, described, *, max_cost=5.0, judge=None, nearest=None):
    """Ask Haiku again for every standing, unchecked call in `group`, with your checks now among the examples.

    Each changed call is recorded in call_overrides (source 'redecide'), so `undo` puts it back."""
    if group not in GROUPS:
        raise ValueError(f"group must be one of {', '.join(GROUPS)}")
    judge = judge or pile.haiku_judge
    dests = pile.destinations(described)
    allowed = {d["slug"] for d in dests} | {pile.NONE}
    with closing(db.get_connection()) as conn, conn:
        ids = _pool(conn, group)
        run_id = conn.execute("INSERT INTO call_redecides(stratum, started_at) VALUES(?,?)", (group, _now())).lastrowid
    totals = {"group": group, "pending": len(ids), "changed": 0, "same": 0, "failed_chunks": 0, "cost_usd": 0.0,
              "stopped_at_cap": False}
    for chunk in pile._chunks(ids, nearest or gists.nearest):
        if totals["cost_usd"] >= max_cost:
            totals["stopped_at_cap"] = True
            break
        try:
            answers, cost = judge(chunk, dests)
        except (RuntimeError, subprocess.TimeoutExpired):
            totals["failed_chunks"] += 1
            continue
        totals["cost_usd"] += cost
        wanted = {item["id"] for item in chunk}
        with closing(db.get_connection()) as conn, conn:
            for a in answers:
                if a.get("item_id") not in wanted or a.get("outcome") not in pile.OUTCOMES \
                        or a.get("destination") not in allowed:
                    continue
                wanted.discard(a["item_id"])
                old = conn.execute("SELECT p.outcome, i.status, i.status_note FROM pile_decisions p "
                                   "JOIN items i ON i.id = p.item_id WHERE p.item_id = ?", (a["item_id"],)).fetchone()
                if side(old["outcome"]) == side(a["outcome"]):
                    totals["same"] += 1
                    continue
                destination = None if a["destination"] == pile.NONE else a["destination"]
                outcome = a["outcome"] if destination or a["outcome"] in ("read", "pass") else "read"
                if outcome == "pass":
                    filed = old["status"] == "adopted" and old["status_note"] == db.REFERENCE
                    status = ("new", None) if filed else None
                elif outcome == "reference":
                    status = ("adopted", db.REFERENCE)
                else:
                    status = ("new", None) if old["status"] == "rejected" else None
                _apply(conn, a["item_id"], outcome, destination, status, "redecide")
                totals["changed"] += 1
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    with closing(db.get_connection()) as conn, conn:
        conn.execute("UPDATE call_redecides SET finished_at = ?, result_json = ? WHERE id = ?",
                     (_now(), json.dumps(totals), run_id))
    return totals


_background = {"running": False, "error": None, "result": None}
_background_lock = threading.Lock()


def start_redecide(group, described, *, work=None, thread=threading.Thread):
    """`redecide` on its own thread, for `beacon serve` (minutes). One at a time."""
    if group not in GROUPS:
        raise ValueError(f"group must be one of {', '.join(GROUPS)}")
    with _background_lock:
        if _background["running"]:
            return {"started": False, "running": True}
        _background.update(running=True, error=None, result=None)
    work = work or redecide

    def run():
        result, error = None, None
        try:
            result = work(group, described)
        except Exception as failure:  # the thread's edge: anything not caught here would be lost silently
            error = str(failure) or type(failure).__name__
        finally:
            with _background_lock:
                _background.update(running=False, error=error, result=result)

    thread(target=run, daemon=True).start()
    return {"started": True, "running": True}
