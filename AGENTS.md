# Beacon

Bookmark & Star Intake System. Syncs X bookmarks and GitHub stars into SQLite, AI-classifies them, and routes to project backlogs.

## Architecture
- Python 3 stdlib only (no pip deps)
- SQLite with FTS5 for search
- `Codex --print --model sonnet` for classification
- `bird` CLI for X bookmarks, `gh api` for GitHub stars

## Commands
```bash
beacon run                    # full pipeline
beacon sync [--source x|github]
beacon import-mymind /path/to/cards.csv --preview  # local export validation, no DB/network; see docs/mymind-import.md
beacon import-x /path/to/x-handoff-v1.json --preview  # local X bookmark and like handoff, no network; see docs/x-handoff.md
beacon import-saves /path/to/saves.jsonl --preview  # X bookmarks plus Readwise history file; see docs/saves-handoff.md
beacon sync  # every source in config.json, including Reader when READWISE_ACCESS_TOKEN is set
beacon sources  # live sources and file handoffs; does not fetch
beacon match [--write]  # recommend configured projects and practices; does not route or execute; see docs/match.md
beacon sync --source x --full # bounded 1,200-item X backfill
beacon sync --source pinterest # public board RSS from config.json sources.pinterest.boards; see docs/pinterest-source.md
beacon classify
beacon route [--dry-run]
beacon route-top [--k 10] [--floor 0.5] [--dry-run]  # top Jev-scored held items per project
beacon queue                  # queued items as JSON (read by app/, the Beacon Triage Mac app)
beacon brief [--project SLUG] --allow-upload  # plan Do items: one read-only Sonnet run per project, Jev claim check
beacon read ITEM_ID            # send to Readwise Reader (status adopted, note read)
beacon stack --allow-upload    # group queued items into themed stacks (Sonnet names themes, Jev assigns + flags near-duplicates)
beacon serve [--port 8765] [--show-key]  # Triage actions over HTTP for the phone; Tailscale + localhost only, phone needs the access key (launchd: com.example.beacon-serve); see app/README.md
beacon gist                    # per queued item: plain takeaway, kind, and a Do/Read/Skip suggestion (Sonnet, new items only)
beacon briefs [--status ready] # project plans as JSON; beacon brief-status ITEM WORKSTREAM dismissed|dispatched; beacon brief-done ITEM WORKSTREAM dispatched adopted|tried (brief and save in one transaction); beacon brief-mac ITEM WORKSTREAM on|off
beacon baseline               # 30-item human-label set + run receipt
beacon search "query"
beacon search "query" --meaning --preview --db /path/to/beacon.db
beacon useful "current task"    # read-only local resource shortlist
beacon useful "current task" --provider typesafe --preview # exact payload, no upload
beacon useful-eval             # isolated synthetic baseline, no model calls
beacon deck "current task" --since <ISO> --until <ISO> # read-only five-card window; see docs/morning-deck.md
beacon triage [ITEM_ID ...]     # read-only intent/destination review; defaults to one pending item
beacon compare ITEM_ID ITEM_ID --task "current task" # read-only duplicate/alternative comparison
beacon triage --held             # persistent uncertainty/no-fit queue
beacon review intake:ID          # explicit local classify/file/leave-held interaction
beacon review save:ID --show     # read-only saved snapshot; also accepts advisory:ID
beacon intake-eval               # frozen routing evaluation; offline by default
beacon conditions --help         # explicit saved conditions and project-event checks
beacon kit "task" --requirement "first requirement" --requirement "second requirement" --preview
beacon conditions event --event event.json --preview # approved bindings only
beacon triage --batched --limit 5 --preview --json
beacon consider-batch 17 18 --context context.json --preview --json
beacon list [--workstream X] [--source X] [--intent X] [--unrouted]
beacon stats
beacon workstreams [add --slug X --name X --path X]
beacon discover [--dry-run] [--skip-github] [--show-all]
```

## Key Files
- `beacon.py` — CLI entry point
- `lib/db.py` — schema, queries
- `lib/fetch.py` — source fetchers
- `lib/pinterest.py` — Pinterest board RSS source
- `lib/classify.py` — AI classification
- `lib/route.py` — backlog routing
- `lib/search.py` — FTS5 search
- `lib/discover.py` — workstream discovery
- `lib/useful.py` — task-specific retrieval and opt-in TypeSafe reranking; see `docs/useful-now.md`
- `lib/review.py` — advisory triage and pair comparison; see `docs/intake-review.md`
- `lib/review_actions.py` — explicit snapshot-bound human decisions; see `docs/human-review.md`
- `lib/intake.py` — opt-in typed classification with persistent holds and a versioned evaluation gate for semantic routing
- `lib/conditions.py` — authored future-use conditions; explicit events only, no polling or lifecycle promotion
- `lib/task_kit.py`, `lib/resurface.py` — complementary evidence and approved project events; see `docs/task-kits-and-events.md`
- `config.json` — workstream definitions

## Conventions
- `docs/ROADMAP.md` is the one roadmap, idea bank and backlog. Update its Shipped, Roadmap and Idea bank sections by hand in the same change that ships, starts or defers something; the backlog block between the `beacon:backlog` markers is regenerated from the database, so never edit it.
- All dates stored as ISO 8601 UTC
- Items deduplicated by (source, external_id)
- Classification batches 15 items per call
- Active backlogs (`beacon-backlog.md`) are **regenerated from DB state**, not append-only
- Archive files (`beacon-backlog-archive.md`) preserve rejected/stale item history
- Items have a lifecycle: new → queued → reviewed → tried → adopted/rejected (stale is automatic)
- `status` lives on the `items` table, not per-workstream
- Sweep runs at end of `beacon run` — queued > 21d and reviewed > 45d go stale
- `app/` — Beacon Triage, native macOS triage app; see `app/README.md`. Weekly schedule: `scripts/weekly.sh` via launchd `com.example.beacon-weekly` (Mondays 8:00).
