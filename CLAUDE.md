# Beacon

Bookmark & Star Intake System. Syncs X bookmarks and GitHub stars into SQLite, AI-classifies them, and routes to project backlogs.

## Architecture
- Python 3 stdlib only (no pip deps)
- `hosted/` is the exception: a TypeScript Cloudflare Workers app for the hosted beta (`cd hosted && pnpm install --frozen-lockfile && pnpm test`)
- Long-running processes (the MCP server registered in each Claude Code profile's `.claude.json` and in `~/.codex/config.toml`, and the `com.example.beacon-serve` launchd job) start through `scripts/python`. It runs a uv-managed Python through the link `~/.local/share/beacon/python`, pinned to one patch release, because Homebrew upgrades delete the old Python and broke running servers on 2026-11-19 (`No module named '_strptime'`). `scripts/bump-python` runs monthly (launchd `com.example.beacon-python-bump`, log `~/Library/Logs/beacon-python-bump.log`): it installs the newest 3.14 patch, runs the tests on it and moves the link only if they pass; a failure sends a macOS notification
- SQLite with FTS5 for search
- `claude --print --model sonnet` for classification
- Every headless `claude` run goes through `lib/claude_accounts.py`: config `claude_accounts` lists the subscriptions in order (`default`, then a `CLAUDE_CONFIG_DIR` such as `~/.claude-second`). One that reports a usage limit hands the call to the next and is tried last for 6 hours (`.claude-limits.json` beside the database)
- `bird` CLI for X bookmarks, `gh api` for GitHub stars

## Commands
```bash
beacon run                    # full pipeline
beacon sync [--source x|github|readwise|readwise-notes|mymind|granola-digests|keep-local|wiki|omnisearch|last30days]  # no flag syncs every enabled source
beacon vocab [--json] | set TERM TARGET | unset TERM TARGET | propose [--preview]  # the owner's tags mapped to projects and areas; see docs/owner-notes.md
beacon import-x PATH [--preview] [--db PATH]  # local x-handoff-v1 file, no network
beacon import-saves PATH [--preview] [--db PATH]  # local x-saves-v1 or JSONL handoff, no network
beacon sources [--db PATH]  # live sources and file handoffs; does not fetch
beacon match [--db PATH] [--write]  # project and practice recommendations; does not route or execute
beacon classify
beacon route [--dry-run]
beacon search "query"
beacon list [--workstream X] [--source X] [--intent X] [--unrouted]
beacon stats
beacon workstreams [add --slug X --name X --path X]
beacon discover [--dry-run] [--skip-github] [--show-all]
beacon backfill <workstream> [--dry-run] [--limit N]
beacon pile decide [--limit N]   # Haiku decides do/read/reference/pass for untriaged saves
beacon pile release [--per N]   # queue a few Do saves per destination, file references
beacon pick score --axis X [--rescore] | prepare [--axis X] | round | summary | holdout  # pick-two taste rounds; see docs/pick.md
beacon calls session | check | undo | summary | rank | redecide  # check Beacon's keep/pass calls; see docs/calls.md
```

## Tests
```bash
scripts/test                 # every test, one class per process, several at a time (~70 s vs ~5 min serially)
scripts/test test_pick       # one or more modules
```
Same discovery as `python -m unittest discover -s tests`; exit code 0 only when every class passes.

## Landing a PR
```bash
scripts/land 42                # main in, roadmap merged row by row, tests (+ hosted tests), push, squash-merge
scripts/land 43 --after 41 42  # a PR stacked on PRs that already squash-merged
```
It works in its own worktree and stops on any other conflict, a failing test or a failed push.

## Health check
```bash
scripts/doctor               # launchd jobs, phone server, Keychain sign-ins, pinned Python, git sync, hosted deploy, public release
```
The Monday job runs it last with `--notify`. Exit 1 means a FAIL line needs fixing; warn lines don't fail it.

## Publishing the public repo
```bash
scripts/publish-public --dry-run   # export origin/main, scan, stranger-Python tests; shows what would ship
scripts/publish-public             # the same, then one commit to <owner>/beacon-public
```
It stops on personal text, on new or changed binary files (look at them, then `--binaries-checked`) and on any failing test.

## Installing the apps
```bash
app/one/build-mac.sh          # Mac app into ~/Applications
scripts/install-phone         # iPhone app on the paired phone, with this Mac's Tailscale address built in
```
Build the phone app only through `scripts/install-phone`: a plain build has no server address and the phone can't reach the Mac.

## Key Files
- `beacon.py` — CLI entry point
- `lib/db.py` — schema, queries
- `lib/fetch.py` — source fetchers
- `lib/classify.py` — AI classification
- `lib/route.py` — backlog routing
- `lib/search.py` — FTS5 search
- `lib/discover.py` — workstream discovery
- `config.json` — workstream definitions

## Conventions
- `docs/ROADMAP.md` is the one roadmap, idea bank and backlog. Update its Shipped, Roadmap and Idea bank sections by hand in the same change that ships, starts or defers something; the backlog block between the `beacon:backlog` markers is regenerated from the database, so never edit it.
- All dates stored as ISO 8601 UTC
- Code, comments and tests say "the owner", never the owner's name: it lives only in `config.json`, and `tests/test_owner_name_stays_out.py` fails otherwise, so the public export stays clean
- Items deduplicated by (source, external_id)
- Classification batches 15 items per call
- Active backlogs (`beacon-backlog.md`) are **regenerated from DB state**, not append-only
- Archive files (`beacon-backlog-archive.md`) preserve rejected/stale item history
- Items have a lifecycle: new → queued → reviewed → tried → adopted/rejected (stale is automatic)
- `status` lives on the `items` table, not per-workstream
- Sweep runs at end of `beacon run` — queued > 21d and reviewed > 45d go stale
- `backfill` is the safe way to populate a newly added workstream from already-classified non-terminal items
