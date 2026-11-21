#!/usr/bin/env python3
"""Beacon — Bookmark & Star Intake System.

Syncs X bookmarks and GitHub stars into a local SQLite DB,
AI-classifies them, and routes actionable items to project backlogs.
"""

import argparse
import hashlib
import json
import os
import subprocess
import re
import sqlite3
import sys
from collections import Counter
import time
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from lib import db, settings, setup
from lib.fetch import (
    fetch_github_stars,
    fetch_keep_local,
    fetch_last30days,
    fetch_markdown_directory,
    fetch_x_bookmarks,
)
from lib.classify import classify_items, backfill_workstream
from lib.route import route_items, regenerate_all_backlogs
from lib.search import search_items, semantic_search
from lib.discover import discover_workstreams
from lib.evaluate import write_baseline_bundle, evaluate_useful_fixture
from lib import useful, review, intake, intake_eval, conditions, deck, save_review, advisory, curator, review_actions
from lib import task_kit, resurface, briefs, reader, stacks, gists, mymind, x_handoff, saves_handoff, areas, pile, pick, pinterest, calls
from lib import match as save_match
from lib import owner_notes, vocabulary
from lib import sources as source_status
from lib.readwise_sync import fetch_readwise

CONFIG_PATH = settings.config_path()


_config = None


def load_config():
    global _config
    if _config is None:
        with open(CONFIG_PATH) as f:
            _config = json.load(f)
    return _config


def save_config(config):
    global _config
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    _config = config


def _classification_batch_size(config, args):
    if getattr(args, "batch_size", None):
        return args.batch_size

    override = os.environ.get("BEACON_CLASSIFY_BATCH_SIZE")
    if override:
        return int(override)

    return config["classification"]["batch_size"]


def _source_choices(config):
    """Configured sources in LIVE_ORDER, then any others by name: X runs before the notes that attach to its posts."""
    order = {name: index for index, name in enumerate(source_status.LIVE_ORDER)}
    return sorted(config.get("sources", {}), key=lambda name: (order.get(name, len(order)), name))


def _sync_source(source, source_config, full=False):
    if source == "x":
        count = source_config.get("default_count", 1200)
        return fetch_x_bookmarks(count=count, stop_at_known=not full)
    if source == "github":
        return fetch_github_stars()
    if source == "keep-local":
        count = source_config.get("default_count", 50)
        return fetch_keep_local(
            count=count,
            db_path=source_config.get("db_path"),
            content_root=source_config.get("content_root"),
        )
    if source in {"wiki", "omnisearch"}:
        count = source_config.get("default_count", 50)
        return fetch_markdown_directory(
            source=source,
            root=source_config.get("root"),
            count=count,
            exclude_names=set(source_config.get("exclude_names", [])),
            author=source_config.get("author"),
        )
    if source == "pinterest":
        return pinterest.fetch_pinterest_boards(source_config.get("boards", []))
    if source == "last30days":
        count = source_config.get("default_count", 50)
        return fetch_last30days(
            count=count,
            db_path=source_config.get("db_path"),
            output_dir=source_config.get("output_dir"),
        )
    if source == "readwise":
        count = source_config.get("default_count", 200)
        return fetch_readwise(
            count=count,
            handoff_dir=source_config.get("handoff_dir"),
            db_path=db.DB_PATH,
            full=full,
        )
    if source in owner_notes.SYNCS:
        return owner_notes.sync(source, full=full)
    raise RuntimeError(f"Unknown source: {source}. It is not a live fetch.")


def add_workstream_to_config(slug, name, backlog_path):
    config = load_config()
    for workstream in config["workstreams"]:
        if workstream["slug"] == slug:
            return False

    config["workstreams"].append(
        {"slug": slug, "name": name, "backlog_path": backlog_path}
    )
    save_config(config)
    return True


def clean(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value or "")


def cmd_run(args):
    """Full pipeline: sync + classify + route + sweep."""
    print("=== Sync ===")
    cmd_sync(args)
    print("\n=== Classify ===")
    cmd_classify(args)
    print("\n=== Route ===")
    route_items(dry_run=False)
    print("\n=== Sweep ===")
    count = db.sweep_stale()
    if count:
        print(f"  Archived {count} stale items")
        regenerate_all_backlogs()
    else:
        print("  No stale items")
    print("\n=== Vocabulary ===")
    try:
        result = vocabulary.propose()
        print(f"  Asked about {result['sent']} unresolved owner terms; kept {result['stored']} proposals")
    except RuntimeError as error:
        print(f"  Vocabulary proposals skipped: {error}")


def cmd_vocab(args):
    """The owner's terms and where they point; set, unset or ask a model to propose."""
    from contextlib import closing
    if args.action == "set":
        print(f"{vocabulary.set_term(args.term, args.target)} -> {args.target} (owner)")
    elif args.action == "unset":
        print(f"removed {vocabulary.unset_term(args.term, args.target)} row(s)")
    elif args.action == "propose":
        try:
            result = vocabulary.propose(preview=args.preview)
        except RuntimeError as error:
            print(f"vocab: {error}", file=sys.stderr)
            raise SystemExit(1) from None
        if args.preview:
            print(result.get("prompt") or "Nothing to propose: every owner term is resolved or already asked about.")
        else:
            print(json.dumps(result, ensure_ascii=False))
    else:
        with closing(db.get_connection()) as conn:
            rows = vocabulary.listing(conn)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
            return
        for row in rows:
            where = ", ".join(row["targets"]) if row["targets"] else "-"
            print(f"{row['saves']:>5}  {row['how']:<10}  {row['term']}  ->  {where}")


def cmd_sync(args):
    """Fetch new items from sources."""
    config = load_config()
    source = getattr(args, "source", None)
    sources = config.get("sources", {})

    if source:
        if source not in sources:
            print(f"sync: {source} is not a live source. File handoffs stay on their import commands.", file=sys.stderr)
            raise SystemExit(2)
        try:
            _sync_source(source, sources[source], full=getattr(args, "full", False))
        except RuntimeError as error:
            print(f"sync: {error}", file=sys.stderr)
            raise SystemExit(1) from None
        _after_ingest(config)
        return

    failures = []
    for source_name in _source_choices(config):
        try:
            _sync_source(source_name, sources[source_name], full=getattr(args, "full", False))
        except Exception as error:
            print(f"  {source_name} failed: {error}")
            failures.append(source_name)
    _after_ingest(config)
    if failures:
        print(json.dumps({"sync_failures": failures}))
        raise SystemExit(1)


def _after_ingest(config):
    """Run only explicitly configured local hooks; no schedule or model upload is implied."""
    if "curator" not in config:
        return
    inbox = config.get("curator", {}).get("advisory_inbox")
    if inbox:
        try:
            advisory_result = advisory.ingest_packet(inbox, db.DB_PATH)
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as error:
            print(json.dumps({"advisory_import": {"status": "failed", "error": str(error), "hold": True}}))
        else:
            print(json.dumps({"advisory_import": advisory_result}, ensure_ascii=False))
    try:
        result = curator.review_new_revisions(
            db.DB_PATH, config.get("curator", {}).get("context_path"),
            limit=config.get("curator", {}).get("max_save_reviews", 20),
            provider=config.get("curator", {}).get("provider", "local"),
            allow_upload=config.get("curator", {}).get("allow_upload") is True,
            max_calls=config.get("curator", {}).get("max_calls", 20),
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"status": "failed", "error": str(error), "hold": True}))
        return
    if result["examined"]:
        print(json.dumps({"save_reviews": result}, ensure_ascii=False))


def cmd_curate(args):
    """Run one configured, resumable, local-only curator pass."""
    config = load_config()
    if not getattr(args, "once", False):
        raise ValueError("Curator is run-once only; pass --once. Scheduling remains external.")
    result = curator.run_once(args.db, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


def cmd_baseline(args):
    """Build a read-only human-label baseline and run receipt."""
    result = write_baseline_bundle(
        output_dir=args.output_dir,
        size=args.size,
        seed=args.seed,
    )
    print(f"Baseline: {result['baseline_path']}")
    print(f"Receipt:  {result['receipt_path']}")
    print(f"Items:    {result['sample_size']}")
    if result["missing_statuses"]:
        print(f"Missing lifecycle examples: {', '.join(result['missing_statuses'])}")


def cmd_classify(args):
    """Classify unclassified items."""
    config = load_config()
    if getattr(args, "provider", "legacy") == "typesafe":
        try:
            evaluation = getattr(args, "routing_evaluation", None)
            if evaluation:
                intake.load_routing_evaluation(evaluation)
            result = intake.classify_pending(config["workstreams"], limit=args.limit,
                                             allow_upload=args.allow_upload, semantic_routing=bool(evaluation), batch_size=getattr(args, "typed_batch_size", 20),
                                             since=getattr(args, "since", None))
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"classify: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        print(json.dumps(result, indent=2))
        return
    classify_items(
        batch_size=_classification_batch_size(config, args),
        workstreams=config["workstreams"],
    )


def _load_typesafe_key(args):
    if args.allow_upload:
        useful.load_key()


def cmd_stack(args):
    """Group the triage queue into themed stacks and flag near-duplicates."""
    _load_typesafe_key(args)
    try:
        result = stacks.build(allow_upload=args.allow_upload)
    except (ValueError, RuntimeError) as error:
        print(f"stack: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result))


def cmd_serve(args):
    """Serve the Triage app's actions over HTTP for the phone (Tailscale and this Mac only)."""
    from lib import server
    if args.show_key:
        print(server.access_key())  # for the phone's Settings; the one place the key is printed
        return
    server.serve(args.host, args.port)


def cmd_gist(args):
    """Write a plain takeaway and a Do/Read/Skip suggestion for queued items that lack one."""
    config = load_config()
    print(json.dumps(gists.build(config["workstreams"] + config.get("areas", []))))


def cmd_pile(args):
    if args.action == "decide":
        config = load_config()
        print(json.dumps(pile.decide(args.limit, config["workstreams"] + config.get("areas", []))))
    else:
        print(json.dumps(pile.release(args.per)))


def cmd_pick(args):
    if args.action == "score":
        ids = [int(i) for i in args.ids.split(",")] if args.ids else None
        print(json.dumps(pick.score(args.axis, args.limit, rescore=args.rescore, ids=ids)))
    elif args.action == "prepare":
        try:
            result = pick.prepare(args.axis)
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"round_id": result["round_id"], "axis": result["axis"], "pairs": result["total"],
                          "kinds": dict(Counter(p["kind"] for p in result["pairs"]))}))
    elif args.action == "round":
        print(json.dumps(pick.current(), ensure_ascii=False))
    elif args.action in ("record", "undo"):
        # The Mac app's way to pick when the server isn't serving its checkout; the phone uses POST /pick.
        try:
            result = pick.undo(args.round) if args.action == "undo" else \
                pick.record(args.round, args.position, args.choice, why=args.why, shown_at=args.shown_at)
        except ValueError as error:
            print(f"pick: {error}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(result))
    elif args.action == "holdout":
        try:
            result = pick.holdout(share=args.share, seed=args.seed, max_cost=args.max_cost, preview=args.preview)
        except (RuntimeError, ValueError) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
        print(json.dumps(result))
    else:
        print(json.dumps(pick.summary(), ensure_ascii=False))


def cmd_calls(args):
    try:
        if args.action == "session":
            result = calls.session(args.size, new=args.new)
        elif args.action == "check":
            result = calls.check(args.session, args.card, args.answer, destination=args.destination, chip=args.chip)
        elif args.action == "undo":
            result = calls.undo(args.change)
        elif args.action == "summary":
            result = calls.summary(args.session)
        elif args.action == "rank":
            result = calls.rank()
        else:
            config = load_config()
            result = calls.redecide(args.group, config["workstreams"] + config.get("areas", []), max_cost=args.max_cost)
    except ValueError as error:
        print(f"calls: {error}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as error:
        print(f"calls: {error}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


def cmd_areas(args):
    """Ask Jev which lifestyle area each held save belongs on, then fill each area's triage stack."""
    _load_typesafe_key(args)
    try:
        result = areas.assign(load_config().get("areas", []), limit=args.limit, allow_upload=args.allow_upload, k=args.k)
    except ValueError as error:
        print(f"areas: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result))


def cmd_brief(args):
    """Plan Do items: one read-only agent run per project, then a Jev claim check."""
    _load_typesafe_key(args)
    try:
        result = briefs.generate(load_config()["workstreams"], project=args.project, allow_upload=args.allow_upload)
    except ValueError as error:
        print(f"brief: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result))


def cmd_read(args):
    """Send an item to Readwise Reader and take it out of the active backlog."""
    try:
        result = reader.read_item(args.item_id)
    except RuntimeError as error:
        print(f"read: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    from lib import autopilot
    autopilot.record_choices([args.item_id], "read")
    _refresh_backlogs_after_save()
    print(json.dumps(result))


def cmd_briefs(args):
    print(json.dumps(briefs.list_plans(args.status), ensure_ascii=False))


def cmd_follow_up(args):
    from lib import followups
    print(json.dumps(followups.generate(limit=args.limit, dry_run=args.dry_run)))


def cmd_brief_status(args):
    briefs.set_status(args.item_id, args.workstream, args.status)


def cmd_brief_done(args):
    try:
        changed = briefs.finish(args.item_id, args.workstream, args.status, args.item_status)
    except LookupError as error:
        print(f"brief-done: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    if changed:
        _refresh_backlogs_after_save()


def _refresh_backlogs_after_save():
    """Rewrites the backlog files after a save has committed. They are derived from the database,
    so a file that can't be written is a warning: failing here would tell the app the save didn't
    land, and it would put the rows back (or send a Read to Reader a second time)."""
    try:
        regenerate_all_backlogs()
    except Exception as error:
        print(f"  saved, but a backlog file wasn't updated ({error}); the next run rewrites it", file=sys.stderr)


def cmd_brief_mac(args):
    briefs.set_for_mac(args.item_id, args.workstream, args.state == "on")


def cmd_library(args):
    """Print a page of the library feed as JSON: by words, by meaning, or like one picture."""
    from lib import vectors
    page = vectors.library_page(offset=args.offset, limit=args.limit, area=args.area, words=args.query,
                                about=args.meaning, like=args.similar, query=args.search, refine=args.refine)
    print(json.dumps(page, ensure_ascii=False))


def cmd_keep(args):
    """Keep your own link or picture in the library (outside Triage), then read it for search."""
    from lib import adds, vectors
    if args.url:
        item_id = adds.add_link(args.url)
    else:
        item_id = adds.add_image(args.image, width=args.width, height=args.height, note=args.note or "")
    if args.no_index:  # `beacon serve` answers the phone now and runs `vectors --ids` afterwards
        print(json.dumps({"id": item_id}))
        return
    try:
        vectors.index(limit=1, ids=[item_id])  # so meaning search and More like this find it right away
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"  not indexed yet ({error}); the weekly run will", file=sys.stderr)
    print(json.dumps({"id": item_id}))


def cmd_references(args):
    """Review saves against a brief: which fit, and which repeat a picture. JSON out."""
    from lib import references
    _load_typesafe_key(args)
    try:
        ids = [int(part) for part in args.ids.split(",") if part]
        print(json.dumps(references.review(args.brief, ids, allow_upload=args.allow_upload), ensure_ascii=False))
    except ValueError as error:
        print(f"references: {error}", file=sys.stderr)
        sys.exit(1)


def cmd_autopilot(args):
    """Pass what Beacon judged noise and send reading to Readwise Reader; Do picks stay for you."""
    from lib import autopilot
    limit = autopilot.READ_LIMIT if args.read_limit is None else args.read_limit
    print(json.dumps(autopilot.run(dry_run=args.dry_run, read_limit=limit)))


def cmd_handled(args):
    from lib import autopilot
    print(json.dumps(autopilot.handled(), ensure_ascii=False))


def cmd_unhandle(args):
    from lib import autopilot
    autopilot.unhandle(args.item_id)
    regenerate_all_backlogs()


def cmd_activate(args):
    from lib import activate
    pending = []
    try:
        print(json.dumps(activate.start(args.item_id, launch=pending.append), ensure_ascii=False), flush=True)
        for job in pending:
            print(json.dumps(job(), ensure_ascii=False), flush=True)
    except ValueError as error:
        print(f"activate: {error}", file=sys.stderr)
        sys.exit(2)


def cmd_activity_close(args):
    from lib import activate
    try:
        print(json.dumps(activate.close(args.activation_id, args.state), ensure_ascii=False))
    except ValueError as error:
        print(f"activity-close: {error}", file=sys.stderr)
        sys.exit(2)


def cmd_activity_answer(args):
    """Prints the restarted record, then waits for the run like `activate`, so the run outlives no process."""
    from lib import activate
    pending = []
    try:
        print(json.dumps(activate.answer(args.activation_id, args.text, launch=pending.append), ensure_ascii=False), flush=True)
        for job in pending:
            print(json.dumps(job(), ensure_ascii=False), flush=True)
    except ValueError as error:
        print(f"activity-answer: {error}", file=sys.stderr)
        sys.exit(2)


def cmd_activity(args):
    from lib import activate
    try:
        print(json.dumps(activate.activity(args.limit), ensure_ascii=False))
    except ValueError as error:
        print(f"activity: {error}", file=sys.stderr)
        sys.exit(2)


def cmd_mcp(args):
    """Serve the library to agents (Claude Code, Codex) over MCP on stdin/stdout."""
    from lib import mcp
    mcp.serve()


def cmd_vectors(args):
    """Index new picture saves for meaning search and similar pictures (on this Mac)."""
    from lib import vectors
    ids = [int(part) for part in args.ids.split(",") if part.strip()] if args.ids else None
    print(json.dumps(vectors.index(limit=args.limit, ids=ids)))


def cmd_queue(args):
    """Print the triage queue as JSON for the Beacon Triage app."""
    print(json.dumps(db.get_triage_queue(), ensure_ascii=False))


def cmd_route_top(args):
    """Assign each project its best-scoring held items, then write backlogs."""
    plan = intake.route_top(k=args.k, floor=args.floor, dry_run=args.dry_run)
    for slug, items in sorted(plan.items()):
        print(f"{slug}: {len(items)} item(s), best {items[0][1]:.2f}")
    if not plan:
        print("No held items qualify; every project is full or nothing scores above the floor.")
    if not args.dry_run and plan:
        route_items()


def cmd_route(args):
    """Route classified items to backlogs."""
    route_items(dry_run=getattr(args, "dry_run", False))


def cmd_route_tags(args):
    """Owner tags that name a project route their saves there, no model call (lib/tag_routes.py)."""
    from lib import tag_routes
    print(json.dumps(tag_routes.route_tagged(dry_run=args.dry_run), ensure_ascii=False))


def cmd_quick_intake(args):
    """The fast lane: sync mymind, then route tagged saves. Meant to run every 15 minutes on the Mac."""
    from lib import tag_routes
    config = load_config()
    source = config.get("sources", {}).get("mymind")
    if source:
        try:
            _sync_source("mymind", source)
        except RuntimeError as error:
            print(f"quick-intake: {error}", file=sys.stderr)
            raise SystemExit(1) from None
    else:
        print("quick-intake: mymind is not a configured source, so only tags already synced route", file=sys.stderr)
    print(json.dumps(tag_routes.route_tagged(dry_run=args.dry_run), ensure_ascii=False))


def cmd_search(args):
    """Search by words or explicitly approved semantic judgments."""
    query = " ".join(args.query)
    if args.meaning:
        if args.db == "":
            raise ValueError("Provide an existing database path; --db cannot be empty.")
        result = semantic_search(query, db.DB_PATH if args.db is None else args.db,
                                 scan_limit=40 if args.scan_limit is None else args.scan_limit, before_id=args.before_id,
                                 workstream=args.workstream, limit=args.limit, preview=args.preview,
                                 allow_upload=args.allow_upload)
        if args.preview or args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return
        print("Meaning search · provisional semantic judgments · this bounded window only")
        print("Only each saved title and its first 800 body characters are judged.")
        if result["matches"]:
            for item in result["matches"]:
                print(f"\n#{item['id']} {clean(item['title'])}\n  {clean(item['excerpt'])}\n  {clean(item['url'])}")
        else:
            print("No supported matches in this window.")
        print(f"\nExamined {result['examined_count']} · unsearched {result['unsearched_count']} · cursor {result['next_before_id']}")
        print(f"{result['additional_match_count']} additional supported matches omitted by --limit.")
        if result["review"]:
            print(f"\n{len(result['review'])} uncertain items. Showing up to {args.limit}; --json includes all.")
            for item in result["review"][:args.limit]:
                print(f"\n[uncertain] #{item['id']} {clean(item['title'])}\n  {clean(item['excerpt'])}\n  {clean(item['url'])}")
        return
    results = search_items(query, limit=args.limit)

    if not results:
        print("No results found.")
        return

    for r in results:
        intent_badge = f"[{r['intent']}]" if r["intent"] else ""
        print(f"\n{r['source']} | {r['author']} {intent_badge}")
        print(f"  {(r['title'] or r['body'] or r['url'] or '')[:80]}")
        if r["body_highlight"] and r["body_highlight"] != r["title"]:
            print(f"  {r['body_highlight'][:120]}")
        print(f"  {r['url']}")


def cmd_deck(args):
    """Preview at most five cards without sync, migrations, routing or status writes."""
    try:
        if getattr(args, "html", False) and (args.preview or args.json):
            raise ValueError("Choose HTML or JSON/preview, not both.")
        result = deck.generate(" ".join(args.task), args.db, args.since, args.until,
                               candidates=args.candidates, provider=args.provider,
                               preview=args.preview, allow_upload=args.allow_upload,
                               preferences_path=getattr(args, "preferences", None))
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"deck: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    if getattr(args, "html", False):
        print(save_review.render_html(result))
        return
    if args.json or args.preview:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    print("Five-card deck · read-only · judgments uncalibrated" if result["model"] else "Five-card deck · recent saves, not yet judged · read-only")
    if result.get("preferences"):
        profile = result["preferences"]
        print(f"Preferences: {clean(profile.get('id'))} revision {profile.get('revision')}")
        for statement in profile.get("statements", []):
            print(f"  - {clean(statement)}")
    for card in result["cards"]:
        print(f"\n[{card['action']}] #{card['id']} {clean(card['title'])}\n  {clean(card['excerpt'])}\n  {clean(card['url'])}")
    more = result.get("additional_match_count", result.get("additional_candidate_count", 0))
    print(f"\n{len(result['review_ids'])} need review · {len(result['ignored_ids'])} not useful for this task (still saved) · {more} more in this batch · {result['unexamined_count']} outside this batch")


def cmd_useful(args):
    """Suggest resources for one task without changing Beacon's state."""
    started = time.monotonic()
    try:
        useful._limit(args.limit, 20)
        task = " ".join(args.task)
        candidates = useful.retrieve_candidates(task, args.db, workstream=args.workstream, limit=args.candidates)
        if args.preview:
            if args.provider != "typesafe":
                raise ValueError("--preview requires --provider typesafe.")
            request = useful.build_request(task, candidates) if candidates else None
            result = {"mode": "preview", "request": request, "network_calls": 0,
                      "request_sha256": hashlib.sha256(useful.encode_request(request)).hexdigest() if request else None,
                      "request_bytes": len(useful.encode_request(request)) if request else 0}
        elif args.provider == "typesafe" and candidates:
            request = useful.build_request(task, candidates)
            response = useful.call_typesafe(request, allow_upload=args.allow_upload)
            result = useful.rank_response(request, candidates, response, limit=args.limit)
        else:
            result = useful.local_result(candidates, limit=args.limit)
        result.update(task=task, workstream=args.workstream, candidate_count=len(candidates),
                      elapsed_ms=round((time.monotonic() - started) * 1000, 2))
    except (ValueError, sqlite3.Error) as error:
        print(f"useful: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.json or args.preview:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    label = "Local keyword candidates" if result["mode"] == "local_baseline" else "TypeSafe suggestions (uncalibrated)"
    print(f"{label} · {len(candidates)} candidates checked · no routing or status changes")
    for row in result["matches"]:
        print(f"\n#{row['id']} {clean(row['title'])}\n  {clean(row['excerpt'])}\n  {clean(row['url'])}")
    if not result["matches"]:
        print("No clear matches. Try a more specific task or --workstream to broaden the shortlist.")
    if result["review"]:
        print(f"\n{len(result['review'])} uncertain/background items held for review; inspect with --json.")


def cmd_consider(args):
    try:
        if args.html and args.preview:
            raise ValueError("Choose HTML or preview, not both.")
        result = save_review.generate(args.item_id, args.db, args.context, provider=args.provider,
                                      preview=args.preview, allow_upload=args.allow_upload)
        print(save_review.render_html(result) if args.html else json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        print(f"consider: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_advisory(args):
    """Ingest or manually resolve explicit external advisory packets."""
    try:
        if args.action == "ingest":
            result = advisory.ingest_packet(args.packet, args.db)
        else:
            result = advisory.resolve(args.db, args.advisory_id, args.note)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError, OSError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"advisory: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_useful_eval(args):
    try:
        result = evaluate_useful_fixture(args.fixture, provider=args.provider, allow_upload=args.allow_upload)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        print(f"useful-eval: incomplete evaluation; no quality result accepted. {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, indent=2, allow_nan=False))


def cmd_kit(args):
    try:
        result = task_kit.generate(args.task, args.requirement, args.db,
            scan_limit=args.scan_limit, before_id=args.before_id, workstream=args.workstream,
            limit=args.limit, preview=args.preview, allow_upload=args.allow_upload,
            replay=review.read_json(args.answers) if args.answers else None,
            conflicts=review.read_json(args.conflicts) if args.conflicts else None)
        print(json.dumps(result, indent=2, allow_nan=False) if args.json else task_kit.render(result))
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        print(f"kit: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_consider_batch(args):
    try:
        result = save_review.generate_many(args.item_ids, args.db, args.context,
            preview=args.preview, allow_upload=args.allow_upload,
            replay=review.read_json(args.answers) if args.answers else None, max_calls=args.max_calls)
        print(json.dumps(result, indent=2, allow_nan=False) if args.json else "\n".join(
            ["Advisory save reviews. No routing or notifications."] +
            [f"[{row['item']['id']}] {row['future_use']['text'] or 'Hold: no supported authored condition.'}" for row in result["reviews"]] +
            ([json.dumps(result["requests"], indent=2)] if args.preview else [])))
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        print(f"consider-batch: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_conditions(args):
    """Explicit local conditions; check consumes a supplied project event only."""
    from contextlib import closing
    from pathlib import Path
    try:
        path = Path(args.db).expanduser().resolve()
        if not path.is_file():
            raise ValueError("Use an existing Beacon database.")
        if args.action == "event" and args.apply and (args.preview or not (args.allow_upload or args.answers)):
            raise ValueError("--apply requires evaluated answers, not preview or unassessed evidence.")
        preview = (args.action == "check" and not args.live) or (args.action == "event" and not args.apply)
        with closing(sqlite3.connect(path.as_uri() + ("?mode=ro" if preview else "?mode=rw"), uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            if not preview:
                conditions.init_db(conn)
            if args.action == "add":
                active = conn.execute("SELECT 1 FROM workstreams WHERE slug=? AND active=1", (args.workstream,)).fetchone()
                if not active:
                    raise ValueError("Condition workstream must be active.")
                result = conditions.save_condition(conn, condition_id=args.id, project_slug=args.workstream,
                            title=args.title, definition={"when": args.when}, authored_by=args.author)
            elif args.action == "list":
                result = {"suggestions": conditions.list_suggestions(conn, active_only=True), "network_calls": 0}
            elif args.action == "bind":
                result = resurface.bind(conn, args.id, args.item_id, args.author, args.expires_at)
            elif args.action == "event":
                result = resurface.run(conn, review.read_json(args.event), preview=args.preview,
                    allow_upload=args.allow_upload, replay=review.read_json(args.answers) if args.answers else None, apply=args.apply)
            else:
                item = conn.execute("SELECT * FROM items WHERE id=?", (args.item_id,)).fetchone()
                if not item:
                    raise ValueError("Saved item not found.")
                event_path = Path(args.event)
                if event_path.stat().st_size > 24000:
                    raise ValueError("Project event exceeds 24 KB.")
                event = json.loads(event_path.read_text())
                condition = conditions.get_condition(conn, args.id)
                if preview:
                    result = {"mode": "preview", "request": conditions.build_typesafe_request(condition, dict(item), event), "network_calls": 0}
                else:
                    result = conditions.evaluate_event(conn, args.id, dict(item), event,
                                conditions.make_typesafe_evaluator(allow_upload=args.allow_upload))
        print(resurface.render(result) if args.action == "event" and not args.json else json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        print(f"conditions: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_review(args):
    """Read-only advisory commands; never enter initialization or routing."""
    started = time.monotonic()
    try:
        if args.command == "triage":
            if args.held:
                if args.item_ids or args.provider != "local" or args.preview:
                    raise ValueError("--held is a local view; do not combine it with item IDs or a remote provider.")
                print(json.dumps({"project": "beacon", "held": intake.review_queue(args.db),
                                  "save_reviews": curator.review_queue(args.db),
                                  "advisories": advisory.review_queue(args.db), "network_calls": 0}, indent=2))
                return
            items = review.read_items(args.db, item_ids=args.item_ids or None, limit=args.limit)
            workstreams = review.read_workstreams(args.db, load_config()["workstreams"])
            if args.batched:
                revisions = {str(item["id"]): save_review._revision(args.db, item["id"]) for item in items}
                items = review.read_items(args.db, item_ids=[item["id"] for item in items]) if items else []
                items = [{**item, "source_fingerprint": revisions[str(item["id"])]} for item in items]
                plans = review.prepare_triage_batches(items, workstreams)
                requests = [p["request"] for p in plans if p["request"]]
                replay = review.read_json(args.answers) if args.answers else None
                if replay is not None:
                    if args.preview or args.allow_upload:
                        raise ValueError("Replay cannot be combined with preview or upload.")
                    review.validate_replay(requests, replay)
                result = {"mode": "batched_triage", "requests": requests if args.preview else [],
                          "request_hashes": [review.request_hash(r) for r in requests], "decisions": [],
                          "network_calls": 0, "replayed_calls": 0, "database_writes": 0, "automation_allowed": False,
                          "usage": {"input_tokens": 0, "output_tokens": 0}}
                response_index = 0
                for plan in plans:
                    if revisions != {str(item["id"]): save_review._revision(args.db, item["id"]) for item in items}:
                        raise ValueError("Source changed before batched triage dispatch.")
                    response = None
                    if not args.preview and plan["request"]:
                        if replay is not None:
                            response = replay[response_index]["response"]
                            result["replayed_calls"] += 1
                        elif args.provider == "typesafe":
                            response = useful.call_typesafe(plan["request"], allow_upload=args.allow_upload)
                            result["network_calls"] += 1
                        response_index += 1
                    reviewed = review.triage_result(plan, response)
                    result["decisions"].extend(reviewed["decisions"])
                    for key in result["usage"]:
                        result["usage"][key] += reviewed["usage"][key]
                if revisions != {str(item["id"]): save_review._revision(args.db, item["id"]) for item in items}:
                    raise ValueError("Source changed during batched triage.")
                print(json.dumps(result, indent=2) if args.json else "\n".join(
                    [f"Batched advisory triage. {len(result['decisions'])} items; no writes."] +
                    [f"[{r['id']}] {r['assessment']}: {', '.join(r['workstreams']) or 'no destination established'}" for r in result["decisions"]] +
                    ([json.dumps(requests, indent=2)] if args.preview else [])))
                return
            if args.answers:
                raise ValueError("--answers requires --batched.")
            plan = review.prepare_triage(items, workstreams)
            request = plan["request"]
        else:
            items = review.read_items(args.db, item_ids=args.item_ids)
            request = review.build_comparison(items, args.task)
        if args.preview:
            if args.provider != "typesafe":
                raise ValueError("--preview requires --provider typesafe.")
            payload = useful.encode_request(request) if request else b""
            result = {"mode": "preview", "project": "beacon", "request": request,
                      "request_bytes": len(payload), "request_sha256": hashlib.sha256(payload).hexdigest() if payload else None,
                      "network_calls": 0, "automation_allowed": False}
        else:
            response = useful.call_typesafe(request, allow_upload=args.allow_upload) if args.provider == "typesafe" and request else None
            result = review.triage_result(plan, response) if args.command == "triage" else review.comparison_result(items, request, response)
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
    except (ValueError, TypeError, KeyError, sqlite3.Error) as error:
        print(f"{args.command}: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


def cmd_list(args):
    """List items with filters."""
    items = db.get_items(
        source=args.source,
        workstream=args.workstream,
        intent=args.intent,
        unrouted=args.unrouted,
        status=args.status,
        limit=args.limit,
    )

    if not items:
        print("No items found.")
        return

    for item in items:
        intent_badge = f"[{item['intent']}]" if item["intent"] else "[pending]"
        status_badge = f"({item['status']})" if item.get("status") else ""
        print(f"\n#{item['id']} {item['source']} | {item['author']} {intent_badge} {status_badge}")
        print(f"  {(item['title'] or item['body'] or item['url'] or '')[:80]}")
        if item.get("x_explanation"):
            print(f"  {item['x_explanation']}")
        print(f"  {item['url']}")


def cmd_stats(args):
    """Show processing health."""
    stats = db.get_processing_stats()

    status_counts = stats["by_status"]
    print(f"=== Beacon Health ===")
    print(f"Total items:    {stats['total']}")

    parts = []
    for s in ["queued", "reviewed", "tried", "adopted", "rejected", "stale", "interest", "pending"]:
        c = status_counts.get(s, 0)
        if c:
            parts.append(f"{c} {s}")
    print(f"Processing:     {' | '.join(parts)}")

    if stats["approaching_stale"]:
        print(f"\nApproaching stale (queued > 14d): {stats['approaching_stale']}")

    print(f"\nBy workstream (active / total):")
    for ws in stats["by_workstream"]:
        print(f"  {ws['slug']:20s} {ws['active']} active | {ws['total']} total")

    usage = db.agent_usage(days=7)
    print(f"\nAgents, last 7 days (beacon mcp):")
    if not usage["calls"]:
        print("  no calls")
        return
    clients = ", ".join(f"{name}: {count}" for name, count in sorted(usage["clients"].items(), key=lambda pair: -pair[1]))
    sessions = f"{usage['sessions']} session" + ("s" if usage["sessions"] != 1 else "")
    print(f"  {usage['calls']} calls in {sessions} ({clients}); {usage['errors']} errors")
    print("  " + " | ".join(f"{count} {tool}" for tool, count in sorted(usage["by_tool"].items(), key=lambda pair: -pair[1])))
    if usage["searches"]:
        print(f"  {usage['searches_opened']} of {usage['searches']} searches led to opening a result; "
              f"{usage['empty_searches']} found nothing")


def cmd_mark(args):
    """Set status on items."""
    status = args.status
    item_ids = args.item_ids
    note = args.note

    for item_id in item_ids:
        try:
            db.update_item_status(item_id, status, note=note)
            print(f"  #{item_id} → {status}")
        except ValueError as e:
            print(f"  Error: {e}")
            return
    from lib import autopilot
    autopilot.record_choices(item_ids, status)
    _refresh_backlogs_after_save()


def cmd_sweep(args):
    """Auto-archive stale items."""
    queued_days = args.queued_days
    reviewed_days = args.reviewed_days

    count = db.sweep_stale(queued_days=queued_days, reviewed_days=reviewed_days)
    if count:
        print(f"Archived {count} stale items")
        regenerate_all_backlogs()
    else:
        print("No stale items to archive")


def cmd_reading(args):
    """Show items flagged for human reading."""
    items = db.get_unrouted_reading_items()
    if not items:
        print("No new reading items.")
        return

    for item in items:
        title = (item["title"] or "Untitled").replace("\n", " ").strip()[:80]
        one_liner = item.get("one_liner") or ""
        print(f"\n{item['source']} | {item['author']} [{item['intent']}]")
        print(f"  {title}")
        if one_liner:
            print(f"  {one_liner}")
        print(f"  {item['url']}")

    print(f"\n{len(items)} items to read.")


def cmd_workstreams(args):
    """List or add workstreams."""
    if args.action == "add":
        if not args.slug or not args.name or not args.path:
            print("--slug, --name, and --path are required for add.")
            return
        add_workstream_to_config(args.slug, args.name, args.path)
        db.add_workstream(args.slug, args.name, args.path)
        print(f"Added workstream: {args.slug}")
        return

    workstreams = db.get_workstreams()
    for ws in workstreams:
        status = "active" if ws["active"] else "inactive"
        print(f"  {ws['slug']}: {ws['name']} ({status})")
        if ws["backlog_path"]:
            print(f"    -> {ws['backlog_path']}")


def cmd_discover(args):
    """Discover new workstreams from local projects, manifest, and GitHub."""
    discover_workstreams(
        config_path=CONFIG_PATH,
        dry_run=getattr(args, "dry_run", False),
        skip_github=getattr(args, "skip_github", False),
        show_all=getattr(args, "show_all", False),
    )


def cmd_regenerate(args):
    """Regenerate all backlog files from DB state."""
    print("Regenerating backlog files...")
    regenerate_all_backlogs()
    print("Done.")


def _detect_source_and_id(url):
    """Auto-detect source and external_id from a URL."""
    # X / Twitter
    m = re.match(r'https?://(?:www\.)?(x\.com|twitter\.com)/([^/]+)/status/(\d+)', url)
    if m:
        return "x", m.group(3), m.group(2)

    m = re.match(r'https?://(?:www\.)?(x\.com|twitter\.com)/([^/?#]+)', url)
    if m:
        return "x", m.group(3) if 'status' in url else hashlib.sha256(url.encode()).hexdigest()[:12], m.group(2)

    # Gist
    m = re.match(r'https?://gist\.github\.com/([^/]+)/([a-f0-9]+)', url)
    if m:
        return "github", f"gist/{m.group(2)}", m.group(1)

    # GitHub repo
    m = re.match(r'https?://github\.com/([^/]+)/([^/?\#]+)', url)
    if m:
        return "github", f"{m.group(1)}/{m.group(2)}", m.group(1)

    # Everything else
    return "manual", hashlib.sha256(url.encode()).hexdigest()[:12], ""


def cmd_add(args):
    """Manually add one or more URLs to Beacon."""
    now = datetime.now(timezone.utc).isoformat()

    for url in args.urls:
        source, external_id, detected_author = _detect_source_and_id(url)

        if args.source:
            source = args.source
        title = args.title or url
        author = args.author or detected_author

        db.upsert_item(
            source=source,
            external_id=external_id,
            title=title,
            body=None,
            url=url,
            author=author,
            raw_json=None,
            created_at=now,
            synced_at=now,
        )

        # Look up the inserted item's id
        conn = db.get_connection()
        row = conn.execute(
            "SELECT id FROM items WHERE source=? AND external_id=?",
            (source, external_id),
        ).fetchone()
        conn.close()
        item_id = row["id"] if row else "?"

        print(f"Added: #{item_id} {title} ({source}/{external_id})")
        if "curator" in load_config() and isinstance(item_id, int):
            _after_ingest(load_config())


def cmd_backfill(args):
    """Backfill a new workstream using already-classified active items."""
    config = load_config()
    batch_size = args.batch_size or config["classification"]["batch_size"]
    backfill_workstream(
        workstream_slug=args.workstream,
        batch_size=batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        min_confidence=args.min_confidence,
    )


def _redacted_import_error(error):
    if isinstance(error, ValueError) and not isinstance(error, UnicodeError):
        return str(error)
    return "Cannot read export or write destination database."


def main():
    parser = argparse.ArgumentParser(description="Beacon — Bookmark & Star Intake System")
    subparsers = parser.add_subparsers(dest="command")
    config = {} if len(sys.argv) > 1 and sys.argv[1] in {"review", "import-mymind", "import-x", "import-saves", "init"} else load_config()
    source_choices = _source_choices(config)

    init_parser = subparsers.add_parser("init", help="Write a first config.json from a few answers (never overwrites)")
    init_parser.add_argument("--owner", help="Your name, as prompts use it")
    init_parser.add_argument("--about", default="", help="One line on what you read and build")
    init_parser.add_argument("--project", action="append", default=[], metavar="NAME|FOLDER|DESCRIPTION",
                             help="A project; repeat for each (up to 8)")

    import_parser = subparsers.add_parser("import-mymind", help="Import a local mymind cards.csv export without network calls")
    import_parser.add_argument("path", help="cards.csv or its export directory")
    import_parser.add_argument("--db", default=db.DB_PATH, help="Destination Beacon database")
    import_parser.add_argument("--preview", action="store_true", help="Validate and print counts without opening a database")

    x_parser = subparsers.add_parser("import-x", help="Import a local x-handoff-v1 file without network calls")
    x_parser.add_argument("path", help="x-handoff-v1 JSON file")
    x_parser.add_argument("--db", default=db.DB_PATH, help="Destination Beacon database")
    x_parser.add_argument("--preview", action="store_true", help="Validate and print counts without opening a database")

    saves_parser = subparsers.add_parser("import-saves", help="Import a local x-saves-v1 or JSONL handoff without network calls")
    saves_parser.add_argument("path", help="x-saves-v1 JSON, a JSON array, or JSONL")
    saves_parser.add_argument("--db", default=db.DB_PATH, help="Destination Beacon database")
    saves_parser.add_argument("--preview", action="store_true", help="Validate and print counts without opening a database")

    sources_parser = subparsers.add_parser("sources", help="Show live sources and file handoffs without fetching")
    sources_parser.add_argument("--db", default=db.DB_PATH, help="Database whose checkpoints to read")

    match_parser = subparsers.add_parser("match", help="Recommend configured projects and practices for saved items")
    match_parser.add_argument("--db", default=db.DB_PATH, help="Beacon database to read")
    match_parser.add_argument("--write", action="store_true", help="Store recommendations. Does not route, file, or execute.")

    review_parser = subparsers.add_parser("review", help="Resolve one saved hold through explicit local review; no model or backlog writes")
    review_parser.add_argument("target", help="intake:ITEM_ID, save:REVIEW_ID or advisory:REVIEW_ID from triage --held")
    review_parser.add_argument("--db", default=db.DB_PATH, help="Existing Beacon database")
    review_parser.add_argument("--show", action="store_true", help="Inspect the saved review and freshness digest without prompting or writing")

    # run
    run_parser = subparsers.add_parser("run", help="Full pipeline: sync + classify + route + sweep")
    run_parser.add_argument("--batch-size", type=int, help="Override classification batch size")

    # sync
    sync_parser = subparsers.add_parser("sync", help="Fetch new items")
    sync_parser.add_argument("--source", choices=source_choices, help="Only sync this source")
    sync_parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore the previous X bookmark ID and backfill to the safety cap",
    )

    curator_parser = subparsers.add_parser(
        "curate", help="Run one configured local curator pass; scheduling stays external"
    )
    curator_parser.add_argument("--once", action="store_true", help="Run or resume exactly one pass")
    curator_parser.add_argument("--db", default=db.DB_PATH, help="Beacon database")

    # classify
    classify_parser = subparsers.add_parser("classify", help="AI-classify unclassified items")
    classify_parser.add_argument("--batch-size", type=int, help="Override classification batch size")
    for intake_parser in (run_parser, classify_parser):
        intake_parser.add_argument("--provider", choices=["legacy", "typesafe"], default="legacy")
        intake_parser.add_argument("--limit", type=int, default=5, help="Maximum TypeSafe intake items, 1–1000")
        intake_parser.add_argument("--typed-batch-size", type=int, default=20, help="Maximum independent typed items per request; byte bounds may split it")
        intake_parser.add_argument("--allow-upload", action="store_true", help="Permit the reviewed intake payload to leave this process")
        intake_parser.add_argument("--since", help="Only score items posted or saved on/after this ISO date (typesafe provider)")
        intake_parser.add_argument("--routing-evaluation", help="Passing current-policy evaluation required for semantic auto-routing")

    # route
    route_parser = subparsers.add_parser("route", help="Push items to backlogs")
    route_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    route_tags_parser = subparsers.add_parser("route-tags", help="Route saves whose owner tags name a project (no model call)")
    route_tags_parser.add_argument("--dry-run", action="store_true", help="Show what would route without writing")
    intake_fast = subparsers.add_parser("quick-intake", help="Sync mymind, then route tagged saves; for a 15-minute launchd job")
    intake_fast.add_argument("--dry-run", action="store_true", help="Sync, but only show what would route")
    subparsers.add_parser("queue", help="Print queued items awaiting triage as JSON")
    brief_parser = subparsers.add_parser("brief", help="Plan Do items: one read-only agent run per project")
    brief_parser.add_argument("--project", help="Only this project slug")
    brief_parser.add_argument("--allow-upload", action="store_true", help="Permit item text to go to TypeSafe")
    stack_parser = subparsers.add_parser("stack", help="Group queued items into themed stacks (Sonnet themes, Jev assignment and duplicate check)")
    stack_parser.add_argument("--allow-upload", action="store_true", help="Permit item excerpts to go to TypeSafe")
    serve_parser = subparsers.add_parser("serve", help="Serve Triage actions over HTTP for the phone (Tailscale only)")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--show-key", action="store_true", help="Print the phone's access key (made on first use) and exit")
    subparsers.add_parser("gist", help="Takeaway + Do/Read/Skip suggestion per queued item (one Sonnet call per 20 new items)")
    pile_parser = subparsers.add_parser("pile", help="Route the untriaged pile: decide each save once, release a few to Triage")
    pile_actions = pile_parser.add_subparsers(dest="action", required=True)
    decide_parser = pile_actions.add_parser("decide", help="Haiku decides do, read, reference or pass for undecided saves (about $0.20 per 1,000 saves, 15 minutes per 1,000; skips saves held by intake review)")
    decide_parser.add_argument("--limit", type=int, default=300, help="Maximum saves to decide this run")
    release_parser = pile_actions.add_parser("release", help="Queue a few do saves per destination and file every reference")
    release_parser.add_argument("--per", type=int, default=3, help="Maximum do saves queued per destination")
    pick_parser = subparsers.add_parser("pick", help="Pick one of two of your saves, 20 a round, so Beacon learns your taste (see docs/pick.md)")
    pick_actions = pick_parser.add_subparsers(dest="action", required=True)
    score_parser = pick_actions.add_parser("score", help="Haiku scores unscored saves on one axis, 15 per call (about $0.20 per 1,000)")
    score_parser.add_argument("--axis", required=True, choices=list(pick.AXES))
    score_parser.add_argument("--limit", type=int, default=600, help="Maximum saves to score this run")
    score_parser.add_argument("--rescore", action="store_true", help="Also replace scores made by an older scoring prompt")
    score_parser.add_argument("--ids", help="Only these saves (comma-separated ids)")
    prepare_parser = pick_actions.add_parser("prepare", help="Build the next round of 20 pairs (EmbeddingGemma topic match, on this Mac)")
    prepare_parser.add_argument("--axis", choices=list(pick.AXES), help="Default: the least settled axis")
    pick_actions.add_parser("round", help="Print the current round and your picks so far as JSON")
    pick_actions.add_parser("summary", help="Per-axis lean from your picks, for you to read (never sent to a model)")
    record_parser = pick_actions.add_parser("record", help="Record (or change) one pick in a round, as POST /pick does")
    record_parser.add_argument("--round", type=int, required=True)
    record_parser.add_argument("--position", type=int, required=True)
    record_parser.add_argument("--choice", required=True, choices=list(pick.CHOICES))
    record_parser.add_argument("--why", help="One of the round's chips, tapped by you")
    record_parser.add_argument("--shown-at", help="When the pair was shown (ISO 8601)")
    undo_parser = pick_actions.add_parser("undo", help="Take back the round's last pick, as POST /pick/undo does")
    undo_parser.add_argument("--round", type=int, required=True)
    holdout_parser = pick_actions.add_parser("holdout", help="Measure whether your picks, shown as examples, help Haiku predict picks it never saw (reads only; Haiku, capped spend)")
    holdout_parser.add_argument("--share", type=float, default=pick.HOLDOUT_SHARE, help="Share of decisive picks held out (default 0.3)")
    holdout_parser.add_argument("--seed", type=int, default=0, help="Seed for the split, so a run can be repeated")
    holdout_parser.add_argument("--max-cost", type=float, default=3.0, help="Stop before a batch would pass this many USD (default 3)")
    holdout_parser.add_argument("--preview", action="store_true", help="Show the split and stop: no embedding, no model calls")
    calls_parser = subparsers.add_parser("calls", help="Check Beacon's keep/pass calls, ten cards a session (see docs/calls.md)")
    calls_actions = calls_parser.add_subparsers(dest="action", required=True)
    session_parser = calls_actions.add_parser("session", help="The open session's cards as JSON (a new one if none is open)")
    session_parser.add_argument("--size", type=int, default=calls.SIZE)
    session_parser.add_argument("--new", action="store_true", help="Start a new session even if one is open ('10 more')")
    check_parser = calls_actions.add_parser("check", help="Answer one card: keep, pass or unsure (can't tell)")
    check_parser.add_argument("--session", required=True)
    check_parser.add_argument("--card", required=True)
    check_parser.add_argument("--answer", required=True, choices=list(calls.ANSWERS))
    check_parser.add_argument("--destination", help="Where a save you keep goes (project or area slug)")
    check_parser.add_argument("--chip", help="One of the session's chips, tapped by you")
    calls_undo_parser = calls_actions.add_parser("undo", help="Put one change back")
    calls_undo_parser.add_argument("--change", type=int, required=True)
    calls_summary_parser = calls_actions.add_parser("summary", help="The end-of-session portrait as JSON")
    calls_summary_parser.add_argument("--session")
    calls_actions.add_parser("rank", help="Nightly: suspect scores and flagged neighbours (EmbeddingGemma, on this Mac, minutes)")
    redecide_parser = calls_actions.add_parser("redecide", help="Haiku re-decides one group's standing calls with your checks as examples (only after you accept the offer)")
    redecide_parser.add_argument("--group", required=True, choices=list(calls.GROUPS))
    redecide_parser.add_argument("--max-cost", type=float, default=5.0, help="Stop once this many USD are spent (default 5)")
    read_parser = subparsers.add_parser("read", help="Send an item to Readwise Reader (marks it adopted, note: read)")
    read_parser.add_argument("item_id", type=int)
    briefs_parser = subparsers.add_parser("briefs", help="Print project plans with their briefs as JSON")
    follow_parser = subparsers.add_parser('follow-up', help='Draft follow-ups for newly kept area saves')
    follow_parser.add_argument('--limit', type=int, default=10)
    follow_parser.add_argument('--dry-run', action='store_true')
    briefs_parser.add_argument("--status", default="ready", choices=["ready", "skipped", "dismissed", "dispatched", "failed"])
    brief_status_parser = subparsers.add_parser("brief-status", help="Dismiss or mark a brief dispatched")
    brief_status_parser.add_argument("item_id", type=int)
    brief_status_parser.add_argument("workstream")
    brief_status_parser.add_argument("status", choices=["ready", "dismissed", "dispatched"])
    brief_done_parser = subparsers.add_parser("brief-done", help="Mark a brief dispatched and its save adopted or tried, together")
    brief_done_parser.add_argument("item_id", type=int)
    brief_done_parser.add_argument("workstream")
    brief_done_parser.add_argument("status", choices=["dispatched"])
    brief_done_parser.add_argument("item_status", choices=["adopted", "tried"])
    areas_parser = subparsers.add_parser("areas", help="Place held saves on lifestyle areas (Style, Food, ...) with Jev")
    areas_parser.add_argument("--limit", type=int, default=1000, help="Held saves to ask about this run")
    areas_parser.add_argument("--k", type=int, default=25, help="Max active items per area (default 25)")
    areas_parser.add_argument("--allow-upload", action="store_true", help="Send save text to TypeSafe (Jev)")
    vocab_parser = subparsers.add_parser("vocab", help="The owner's tags and the projects or areas they point at")
    vocab_parser.add_argument("--json", action="store_true")
    vocab_actions = vocab_parser.add_subparsers(dest="action")
    for action in ("set", "unset"):
        vocab_action = vocab_actions.add_parser(action, help=f"{action.capitalize()} an owner row: TERM points at TARGET (a slug, or none)")
        vocab_action.add_argument("term")
        vocab_action.add_argument("target")
    propose_parser = vocab_actions.add_parser("propose", help="Ask a model where unresolved terms point (terms and project descriptions only)")
    propose_parser.add_argument("--preview", action="store_true", help="Print the exact prompt; no call")
    library_parser = subparsers.add_parser("library", help="Print a page of saves with pictures, plus GitHub stars, as JSON for Mosaic")
    library_parser.add_argument("--offset", type=int, default=0)
    library_parser.add_argument("--limit", type=int, default=200)
    library_parser.add_argument("--area", help="Only saves on this area (shelf) slug")
    library_parser.add_argument("--search", help="The one search box: words and meaning fused, best first")
    library_parser.add_argument("--refine", action="append", default=[], help="A follow-up step narrowing --search (repeatable)")
    library_parser.add_argument("--query", help="Only saves containing every word, best match first")
    library_parser.add_argument("--meaning", help="Saves closest in meaning to this text (needs `beacon vectors`)")
    library_parser.add_argument("--similar", type=int, help="Pictures that look like this item's")
    subparsers.add_parser("mcp", help="Serve the library to agents over MCP (stdin/stdout)")
    keep_parser = subparsers.add_parser("keep", help="Keep your own link or picture in the library (skips Triage)")
    keep_what = keep_parser.add_mutually_exclusive_group(required=True)
    keep_what.add_argument("--url", help="A web page or X post")
    keep_what.add_argument("--image", help="A picture file (jpg, png, gif, webp, heic)")
    keep_parser.add_argument("--width", type=int)
    keep_parser.add_argument("--height", type=int)
    keep_parser.add_argument("--note", help="A few words about the picture")
    keep_parser.add_argument("--no-index", action="store_true",
                             help="Don't read it for search now; the caller runs `beacon vectors --ids` after")
    autopilot_parser = subparsers.add_parser("autopilot", help="Pass noise and send reading to Reader on your behalf")
    autopilot_parser.add_argument("--dry-run", action="store_true", help="Count what it would do; change nothing")
    autopilot_parser.add_argument("--read-limit", type=int, help="Reader saves per run (default 45)")
    subparsers.add_parser("handled", help="What autopilot did in the last 14 days, as JSON")
    unhandle_parser = subparsers.add_parser("unhandle", help="Undo autopilot for one save: back to the queue")
    unhandle_parser.add_argument("item_id", type=int)
    activate_parser = subparsers.add_parser("activate", help="Hand a kept save to its destination's agent and wait for the run")
    activate_parser.add_argument("item_id", type=int)
    activate_parser.add_argument("--db", default=db.DB_PATH, help="Beacon database the hand-off is recorded in")
    activity_parser = subparsers.add_parser("activity", help="Every hand-off to an agent, newest first, as JSON")
    activity_parser.add_argument("--limit", type=int, default=200)
    activity_parser.add_argument("--db", default=db.DB_PATH, help="Beacon database to read")
    close_parser = subparsers.add_parser("activity-close", help="Close a hand-off you've looked at: done or didnt_work")
    close_parser.add_argument("activation_id", type=int)
    close_parser.add_argument("state", choices=["done", "didnt_work"])
    close_parser.add_argument("--db", default=db.DB_PATH, help="Beacon database the hand-off is recorded in")
    answer_parser = subparsers.add_parser("activity-answer", help="Answer a hand-off that needs you; its run starts again and is waited for")
    answer_parser.add_argument("activation_id", type=int)
    answer_parser.add_argument("text")
    answer_parser.add_argument("--db", default=db.DB_PATH, help="Beacon database the hand-off is recorded in")
    references_parser = subparsers.add_parser("references", help="Review saves against what you're making: fit and repeats")
    references_parser.add_argument("--brief", required=True, help="What you're making, in a sentence")
    references_parser.add_argument("--ids", required=True, help="Save ids, comma-separated (up to 40)")
    references_parser.add_argument("--allow-upload", action="store_true", help="Let Jev read the brief and post text (TypeSafe)")
    vectors_parser = subparsers.add_parser("vectors", help="Read new library saves (pictures and words) for search by meaning and similar pictures")
    vectors_parser.add_argument("--limit", type=int, default=1000)
    vectors_parser.add_argument("--ids", help="Only these saves, comma-separated ids")
    brief_mac_parser = subparsers.add_parser("brief-mac", help="Flag a brief to start from the Mac (set from the phone)")
    brief_mac_parser.add_argument("item_id", type=int)
    brief_mac_parser.add_argument("workstream")
    brief_mac_parser.add_argument("state", choices=["on", "off"])
    route_top_parser = subparsers.add_parser("route-top", help="Route each project's top Jev-scored held items")
    route_top_parser.add_argument("--k", type=int, default=25, help="Max active items per project (default 25)")
    route_top_parser.add_argument("--floor", type=float, default=0.5, help="Minimum Jev fit score (default 0.5)")
    route_top_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")

    # search
    search_parser = subparsers.add_parser("search", help="Search items")
    search_parser.add_argument("query", nargs="+", help="Search query")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--meaning", action="store_true", help="Judge meaning over a bounded read-only window")
    search_parser.add_argument("--scan-limit", type=int)
    search_parser.add_argument("--before-id", type=int)
    search_parser.add_argument("--db", help="Existing Beacon database; opened read-only")
    search_parser.add_argument("--workstream", help="Restrict to an active workstream")
    search_parser.add_argument("--preview", action="store_true", help="Show exact semantic requests without sending them")
    search_parser.add_argument("--allow-upload", action="store_true", help="Confirm approved external upload and API spend")
    search_parser.add_argument("--json", action="store_true")

    # This command is deliberately separate from the mutating intake pipeline.
    useful_parser = subparsers.add_parser("useful", help="Find saved resources for a task (read-only, local by default)")
    useful_parser.add_argument("task", nargs="+")
    useful_parser.add_argument("--db", default=db.DB_PATH, help="Existing Beacon database; opened read-only")
    useful_parser.add_argument("--workstream", help="Restrict to an active workstream and include its recent resources")
    useful_parser.add_argument("--candidates", type=int, default=8, help="Shortlist size, 1–20 (default: 8)")
    useful_parser.add_argument("--limit", type=int, default=5, help="Displayed matches, 1–20 (default: 5)")
    useful_parser.add_argument("--provider", choices=["local", "typesafe"], default="local")
    useful_parser.add_argument("--preview", action="store_true", help="Print the exact TypeSafe payload without sending it")
    useful_parser.add_argument("--allow-upload", action="store_true", help="Confirm approved external upload and API spend for this invocation")
    useful_parser.add_argument("--json", action="store_true")

    deck_parser = subparsers.add_parser("deck", help="Preview up to five cards from a time window; no routing or schedule")
    deck_parser.add_argument("task", nargs="+")
    deck_parser.add_argument("--db", default=db.DB_PATH)
    deck_parser.add_argument("--since", required=True, help="Inclusive timezone-aware ISO timestamp")
    deck_parser.add_argument("--until", required=True, help="Exclusive timezone-aware ISO timestamp")
    deck_parser.add_argument("--candidates", type=int, default=8, help="Read at most 1–20 recent eligible saves")
    deck_parser.add_argument("--provider", choices=["local", "typesafe"], default="local")
    deck_parser.add_argument("--preview", action="store_true", help="Show exact payload; no network request")
    deck_parser.add_argument("--allow-upload", action="store_true", help="Permit reviewed task/excerpts and bounded API spending")
    deck_parser.add_argument("--preferences", help="Optional explicit local preferences file")
    deck_parser.add_argument("--json", action="store_true")
    deck_parser.add_argument("--html", action="store_true", help="Readable five-card HTML on stdout; no server or publication")

    consider_parser = subparsers.add_parser("consider", help="Review one save against supplied authored conditions and active efforts (read-only)")
    consider_parser.add_argument("item_id", type=int)
    consider_parser.add_argument("--context", required=True, help="Dated version-1 context packet; does not fetch tickets or queues")
    consider_parser.add_argument("--db", default=db.DB_PATH)
    consider_parser.add_argument("--provider", choices=["local", "typesafe"], default="local")
    consider_parser.add_argument("--preview", action="store_true")
    consider_parser.add_argument("--allow-upload", action="store_true")
    consider_parser.add_argument("--html", action="store_true")

    eval_parser = subparsers.add_parser("useful-eval", help="Evaluate task retrieval on isolated synthetic cases")
    eval_parser.add_argument("--fixture", default=os.path.join(os.path.dirname(__file__), "tests", "fixtures", "useful-now.json"))
    eval_parser.add_argument("--provider", choices=["local", "typesafe"], default="local")
    eval_parser.add_argument("--allow-upload", action="store_true", help="Confirm approved fixture upload and capped API budget")

    routing_eval = subparsers.add_parser("intake-eval", help="Evaluate frozen synthetic routing and comparison cases; no live database access")
    routing_eval.add_argument("--live", action="store_true")
    routing_eval.add_argument("--allow-upload", action="store_true")
    routing_eval.add_argument("--output", help="Save the full evaluation, including synthetic requests and responses")

    kit_parser = subparsers.add_parser("kit", help="Assemble advisory evidence; assess requirement and source-pair conflicts without certifying compatibility")
    kit_parser.add_argument("task")
    kit_parser.add_argument("--requirement", action="append", required=True, help="Repeat for 2–5 explicit requirements")
    kit_parser.add_argument("--db", default=db.DB_PATH)
    kit_parser.add_argument("--scan-limit", type=int, default=20, help="Bound examined rows; at most 190 pairs and 10 requests, narrow if exceeded")
    kit_parser.add_argument("--before-id", type=int)
    kit_parser.add_argument("--workstream")
    kit_parser.add_argument("--limit", type=int, default=5)
    kit_parser.add_argument("--preview", action="store_true")
    kit_parser.add_argument("--allow-upload", action="store_true")
    kit_parser.add_argument("--answers", help="Offline request-hash-bound response envelopes (JSON)")
    kit_parser.add_argument("--conflicts", help="Authored conflict observations (JSON); never semantic rejection")
    kit_parser.add_argument("--json", action="store_true")
    batch_consider = subparsers.add_parser("consider-batch", help="Bounded independent save reviews against one dated context packet")
    batch_consider.add_argument("item_ids", nargs="+", type=int)
    batch_consider.add_argument("--context", required=True)
    batch_consider.add_argument("--db", default=db.DB_PATH)
    batch_consider.add_argument("--preview", action="store_true")
    batch_consider.add_argument("--allow-upload", action="store_true")
    batch_consider.add_argument("--answers")
    batch_consider.add_argument("--max-calls", type=int, default=20)
    batch_consider.add_argument("--json", action="store_true")

    conditions_parser = subparsers.add_parser("conditions", help="Save future-use conditions and check explicit project events; no polling or lifecycle changes")
    condition_actions = conditions_parser.add_subparsers(dest="action", required=True)
    condition_add = condition_actions.add_parser("add")
    condition_add.add_argument("id")
    condition_add.add_argument("--workstream", required=True)
    condition_add.add_argument("--title", required=True)
    condition_add.add_argument("--when", required=True)
    condition_add.add_argument("--author", required=True)
    condition_list = condition_actions.add_parser("list")
    condition_check = condition_actions.add_parser("check")
    condition_check.add_argument("id")
    condition_check.add_argument("item_id", type=int)
    condition_check.add_argument("--event", required=True, help="JSON project event with id, project_slug, and explicit evidence")
    condition_check.add_argument("--live", action="store_true", help="Without this flag print the exact request only")
    condition_check.add_argument("--allow-upload", action="store_true")
    condition_bind = condition_actions.add_parser("bind", help="Explicitly approve an expiring item/condition binding")
    condition_bind.add_argument("id")
    condition_bind.add_argument("item_id", type=int)
    condition_bind.add_argument("--author", required=True)
    condition_bind.add_argument("--expires-at", required=True)
    condition_event = condition_actions.add_parser("event", help="Check a dated project event against approved bindings")
    condition_event.add_argument("--event", required=True)
    condition_event.add_argument("--preview", action="store_true")
    condition_event.add_argument("--answers")
    condition_event.add_argument("--allow-upload", action="store_true")
    condition_event.add_argument("--apply", action="store_true", help="Explicitly persist local transitions; upload or replay alone remains read-only")
    condition_event.add_argument("--json", action="store_true")
    for condition_parser in (condition_add, condition_list, condition_check, condition_bind, condition_event):
        condition_parser.add_argument("--db", default=db.DB_PATH)

    advisory_parser = subparsers.add_parser("advisory", help="Ingest or manually resolve external advisory holds")
    advisory_actions = advisory_parser.add_subparsers(dest="action", required=True)
    advisory_ingest = advisory_actions.add_parser("ingest", help="Ingest a local version-1 advisory packet")
    advisory_ingest.add_argument("packet", help="JSON packet path; no network fetch is performed")
    advisory_resolve = advisory_actions.add_parser("resolve", help="Manually resolve one local advisory hold")
    advisory_resolve.add_argument("advisory_id", type=int)
    advisory_resolve.add_argument("--note", help="Local resolution note")
    for advisory_action in (advisory_ingest, advisory_resolve):
        advisory_action.add_argument("--db", default=db.DB_PATH)

    triage_parser = subparsers.add_parser("triage", help="Read-only intent/destination review of pending intake (JSON)")
    triage_parser.add_argument("item_ids", nargs="*", type=int, help="Optional saved item IDs; defaults to pending intake")
    triage_parser.add_argument("--held", action="store_true", help="Show the persistent uncertainty/no-fit queue without a model call")
    triage_parser.add_argument("--batched", action="store_true", help="Pack independent items without dropping any destinations")
    triage_parser.add_argument("--answers", help="Offline request-hash-bound response envelopes; requires --batched")
    triage_parser.add_argument("--json", action="store_true", help="JSON output for --batched")
    triage_parser.add_argument("--limit", type=int, default=1, help="Pending items, 1–20; default 1 keeps all project judgments within the payload cap")
    compare_parser = subparsers.add_parser("compare", help="Compare two saved resources without merging or deleting (JSON)")
    compare_parser.add_argument("item_ids", nargs=2, type=int)
    compare_parser.add_argument("--task", default="Compare documented purpose and capabilities", help="Task whose constraints matter for the comparison")
    for review_parser in (triage_parser, compare_parser):
        review_parser.add_argument("--db", default=db.DB_PATH, help="Existing database; opened read-only")
        review_parser.add_argument("--provider", choices=["local", "typesafe"], default="local")
        review_parser.add_argument("--preview", action="store_true", help="Print exact payload without sending it")
        review_parser.add_argument("--allow-upload", action="store_true", help="Use only after approving this payload and bounded API spend")

    # list
    list_parser = subparsers.add_parser("list", help="List items")
    list_parser.add_argument("--source", choices=sorted(set(source_choices) | {"add", "github", "mymind", "x", "x-like", "readwise"}))
    list_parser.add_argument("--workstream")
    list_parser.add_argument("--intent", choices=["build-with", "reference", "explore", "non-technical"])
    list_parser.add_argument("--status", choices=["new", "queued", "reviewed", "tried", "adopted", "rejected", "stale", "interest", "pending"])
    list_parser.add_argument("--unrouted", action="store_true")
    list_parser.add_argument("--limit", type=int, default=20)

    # stats
    subparsers.add_parser("stats", help="Show processing health")

    # baseline
    baseline_parser = subparsers.add_parser(
        "baseline",
        help="Build a read-only human-label evaluation set and receipt",
    )
    baseline_parser.add_argument("--size", type=int, default=30)
    baseline_parser.add_argument("--seed", type=int, default=20260713)
    baseline_parser.add_argument("--output-dir", default="runs")

    # mark
    mark_parser = subparsers.add_parser("mark", help="Set status on items")
    mark_parser.add_argument("status", choices=["reviewed", "tried", "adopted", "rejected", "queued", "stale"])
    mark_parser.add_argument("item_ids", nargs="+", type=int, help="Item IDs to update")
    mark_parser.add_argument("--note", help="Status note")

    # sweep
    sweep_parser = subparsers.add_parser("sweep", help="Auto-archive stale items")
    sweep_parser.add_argument("--queued-days", type=int, default=21, help="Days before queued items go stale (default: 21)")
    sweep_parser.add_argument("--reviewed-days", type=int, default=45, help="Days before reviewed items go stale (default: 45)")

    # reading
    subparsers.add_parser("reading", help="Show items flagged for human reading")

    # workstreams
    ws_parser = subparsers.add_parser("workstreams", help="List or add workstreams")
    ws_parser.add_argument("action", nargs="?", default="list", choices=["list", "add"])
    ws_parser.add_argument("--slug")
    ws_parser.add_argument("--name")
    ws_parser.add_argument("--path")

    # discover
    discover_parser = subparsers.add_parser("discover", help="Discover new workstreams")
    discover_parser.add_argument("--dry-run", action="store_true", help="Show without registering")
    discover_parser.add_argument("--skip-github", action="store_true", help="Skip GitHub API")
    discover_parser.add_argument("--show-all", action="store_true", help="Show non-recommended too")

    # regenerate
    subparsers.add_parser("regenerate", help="Regenerate all backlog files from DB state")

    # add
    add_parser = subparsers.add_parser("add", help="Manually add URLs to Beacon")
    add_parser.add_argument("urls", nargs="+", help="One or more URLs to add")
    add_parser.add_argument("--title", help="Title (default: URL)")
    add_parser.add_argument("--source", choices=source_choices + ["manual"], help="Override auto-detected source")
    add_parser.add_argument("--author", help="Override auto-detected author")

    # backfill
    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Backfill a workstream using already-classified active items",
    )
    backfill_parser.add_argument("workstream", help="Workstream slug to backfill")
    backfill_parser.add_argument("--limit", type=int, default=100, help="Max candidate items to review")
    backfill_parser.add_argument("--batch-size", type=int, help="Override classification batch size")
    backfill_parser.add_argument("--min-confidence", type=float, default=0.6, help="Minimum confidence to assign")
    backfill_parser.add_argument("--dry-run", action="store_true", help="Preview assignments without writing")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "init":
        target = os.path.join(settings.ROOT, "config.json")
        try:
            if os.path.exists(target):
                raise FileExistsError(f"{target} already exists; edit it instead. beacon init never overwrites a config.")
            if args.owner and args.project:
                owner, about, projects = args.owner, args.about, [setup.parse_project(p) for p in args.project]
            elif sys.stdin.isatty():
                owner, about, projects = setup.ask()
            else:
                raise ValueError("Pass --owner and at least one --project, or run init in a terminal.")
            config = setup.build_config(owner, about, projects)
            setup.write(target, config)
        except (ValueError, FileExistsError) as error:
            print(f"init: {error}", file=sys.stderr)
            raise SystemExit(2)
        print(setup.next_steps(config))
        return

    if args.command == "import-mymind":
        try:
            print(json.dumps(mymind.import_export(args.path, args.db, args.preview), indent=2))
        except (ValueError, OSError, sqlite3.Error) as error:
            # Export records and SQL exception details can contain private saved text.
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else "Cannot read export or write destination database."
            print(f"import-mymind: {message}", file=sys.stderr)
            raise SystemExit(2) from None
        return

    if args.command == "import-x":
        try:
            print(json.dumps(x_handoff.import_handoff(args.path, args.db, args.preview), indent=2))
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"import-x: {_redacted_import_error(error)}", file=sys.stderr)
            raise SystemExit(2) from None
        return

    if args.command == "import-saves":
        try:
            print(json.dumps(saves_handoff.import_saves(args.path, args.db, args.preview), indent=2))
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"import-saves: {_redacted_import_error(error)}", file=sys.stderr)
            raise SystemExit(2) from None
        return

    if args.command == "sources":
        print(json.dumps(source_status.coverage(load_config(), args.db), indent=2))
        return

    if args.command == "match":
        try:
            config = load_config()
            projects, practices, gaps = save_match.catalog_from_config(config)
            result = save_match.match_catalog(
                args.db, projects, practices, write=args.write, repo_root=os.path.dirname(CONFIG_PATH),
            )
            result["gaps"] = gaps
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"match: {_redacted_import_error(error)}", file=sys.stderr)
            raise SystemExit(2) from None
        return

    if args.command == "useful":
        cmd_useful(args)
        return
    if args.command == "kit":
        cmd_kit(args)
        return
    if args.command == "consider-batch":
        cmd_consider_batch(args)
        return
    if args.command == "search" and args.meaning:
        try:
            cmd_search(args)
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"search: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        return
    if args.command == "search" and (
        any(getattr(args, option) is not None for option in ("scan_limit", "before_id", "workstream", "db"))
        or args.preview or args.allow_upload or args.json
    ):
        print("search: Meaning-search options require --meaning.", file=sys.stderr)
        raise SystemExit(2)
    if args.command == "consider":
        cmd_consider(args)
        return
    if args.command == "deck":
        cmd_deck(args)
        return
    if args.command == "useful-eval":
        cmd_useful_eval(args)
        return
    if args.command == "conditions":
        cmd_conditions(args)
        return
    if args.command == "advisory":
        cmd_advisory(args)
        return
    if args.command == "curate":
        try:
            cmd_curate(args)
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"curate: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        return
    if args.command == "intake-eval":
        result = intake_eval.evaluate(live=args.live, allow_upload=args.allow_upload)
        if args.output:
            from pathlib import Path
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x") as handle:
                json.dump(result, handle, indent=2, allow_nan=False)
        print(json.dumps({key: value for key, value in result.items() if key not in {"cases", "pairs"}}, indent=2))
        return
    if args.command == "review":
        try:
            if args.show:
                print(json.dumps(review_actions.inspect(args.db, args.target), indent=2, ensure_ascii=False, allow_nan=False))
            else:
                review_actions.interactive(args.db, args.target)
        except (ValueError, OSError, sqlite3.Error) as error:
            print(f"review: {error}", file=sys.stderr)
            raise SystemExit(2) from None
        return
    if args.command in {"triage", "compare"}:
        cmd_review(args)
        return

    if args.command == 'follow-up' and args.dry_run:
        cmd_follow_up(args)
        return

    # Activation records live in the database it is pointed at, like every command with --db; the rest use DB_PATH.
    with db.using(args.db if args.command in {"activate", "activity", "activity-close", "activity-answer"} else db.DB_PATH):
        _dispatch(args)


def _dispatch(args):
    config = load_config()
    db.init_db()
    db.seed_workstreams(config["workstreams"])
    db.seed_workstreams(config.get("areas", []), kind="area")

    commands = {
        "run": cmd_run,
        "sync": cmd_sync,
        "classify": cmd_classify,
        "route": cmd_route,
        "route-top": cmd_route_top,
        "route-tags": cmd_route_tags,
        "quick-intake": cmd_quick_intake,
        "queue": cmd_queue,
        "brief": cmd_brief,
        "read": cmd_read,
        "stack": cmd_stack,
        "gist": cmd_gist,
        "pile": cmd_pile,
        "pick": cmd_pick,
        "calls": cmd_calls,
        "serve": cmd_serve,
        "briefs": cmd_briefs,
        "follow-up": cmd_follow_up,
        "brief-status": cmd_brief_status,
        "brief-done": cmd_brief_done,
        "brief-mac": cmd_brief_mac,
        "library": cmd_library,
        "vectors": cmd_vectors,
        "keep": cmd_keep,
        "references": cmd_references,
        "autopilot": cmd_autopilot,
        "handled": cmd_handled,
        "unhandle": cmd_unhandle,
        "activate": cmd_activate,
        "activity": cmd_activity,
        "activity-close": cmd_activity_close,
        "activity-answer": cmd_activity_answer,
        "mcp": cmd_mcp,
        "areas": cmd_areas,
        "vocab": cmd_vocab,
        "search": cmd_search,
        "list": cmd_list,
        "stats": cmd_stats,
        "baseline": cmd_baseline,
        "mark": cmd_mark,
        "sweep": cmd_sweep,
        "reading": cmd_reading,
        "workstreams": cmd_workstreams,
        "discover": cmd_discover,
        "regenerate": cmd_regenerate,
        "backfill": cmd_backfill,
        "add": cmd_add,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
