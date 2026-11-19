---
name: beacon
description: Bookmark & Star Intake System — sync, classify, and route X bookmarks and GitHub stars to project backlogs. Use when user says "check beacon", "beacon backlog", "what's in beacon", "process beacon", "triage beacon", or invokes /beacon.
user_invocable: true
---

# /beacon

Beacon syncs X bookmarks and GitHub stars into a local SQLite DB, AI-classifies them against active workstreams, and routes actionable items to each project's backlog.

## Behavior

### No arguments (or "check beacon")

Find and display the relevant beacon backlog for the current project:

1. Check if `docs/beacon-backlog.md` exists in the current working directory — if yes, read and display it
2. If not, infer the project name from the current directory basename and look up the backlog path:
   ```bash
   python3 ~/Projects/beacon/beacon.py workstreams
   ```
   Then read the matching workstream's backlog file.
3. If no match, run `python3 ~/Projects/beacon/beacon.py list --workstream <dirname> --status queued --limit 10` to show recent items from the DB directly.
4. Summarize the most actionable items — group by intent (build-with first, then reference, then explore). Skip non-technical items.
5. Add processing summary: `X queued | Y reviewed | Z tried — N approaching stale`

### With subcommand

Run the beacon CLI directly:

```bash
python3 ~/Projects/beacon/beacon.py <subcommand> [args]
```

Available subcommands:
- `run` — full pipeline: sync + classify + route + sweep
- `sync [--source x|github]` — fetch new items
- `classify` — AI-classify unclassified items
- `route [--dry-run]` — push items to backlogs
- `search "<query>"` — search across all items
- `list [--workstream X] [--source X] [--intent X] [--status X] [--unrouted]`
- `stats` — processing health by status and workstream
- `mark <status> <id> [<id>...] [--note "..."]` — set item status
- `sweep [--queued-days N] [--reviewed-days N]` — auto-archive stale items
- `backfill <workstream> [--dry-run] [--limit N]` — populate a newly added workstream from already-classified active items
- `regenerate` — rewrite all backlog files from DB state

### Process beacon

When user says "process beacon", "triage beacon", or "clean up backlog":

1. Run `python3 ~/Projects/beacon/beacon.py list --status queued --workstream <current> --limit 20`
2. Present items to user grouped by intent
3. For each item or batch, ask: review, reject, or skip
4. Execute `python3 ~/Projects/beacon/beacon.py mark <status> <id> [--note "..."]`

### Triage judgment (when recommending dispositions)

**The default disposition is `reviewed`, not `rejected`.** Every item here was bookmarked or starred by the user — the curation already happened. Rejecting overrides a signal the user already gave; it needs a nameable reason they'd agree with in one line.

Legitimate reject reasons (closed list):
- Superseded — something covering the same ground was `adopted` or `tried` (name it in the note)
- Duplicate of another item in the DB
- Tied to a workstream the user has explicitly abandoned or sunset
- Already tried and it failed (mark `tried` with the failure note instead when possible)

**NOT reject reasons** (the surface-cue traps — a real Opus failure mode, 2026-era correction rule):
- Announcement-only or launch-tweet format — announcements often mark tools worth a later look
- Unfamiliar author, low engagement, tweet-style brevity
- "Looks like marketing" — the user bookmarked it through the marketing
- Age — old and queued means unprocessed, not worthless (that's what `stale` sweep is for)
- You personally can't see the use — absence of an obvious use is what `reviewed` is for

Test before any reject recommendation: write the one-line note first. If the note would be a surface cue from the trap list, or you can't write one, recommend `reviewed`. Calibration check: rejects should be a small minority of any triage pass — if you're recommending reject on more than ~1 in 5, stop and re-check against the trap list before presenting.

### Quick actions

```
/beacon reject 15 "superseded by dspy"
/beacon reviewed 23
/beacon tried 15 "tested, works well"
/beacon adopted 23
```

These map to:
```bash
python3 ~/Projects/beacon/beacon.py mark <status> <id> [--note "reason"]
```

## Item Lifecycle

| Status | Meaning |
|--------|---------|
| `new` | Synced, not yet classified |
| `queued` | Classified + routed, awaiting review |
| `reviewed` | The user has seen it, keeping it active |
| `tried` | Actually tested/used the item |
| `adopted` | Incorporated into a project (terminal) |
| `rejected` | Not useful, dismissed (terminal) |
| `stale` | Auto-archived after N days untouched |

## Examples

```
/beacon                          # Show this project's beacon backlog
/beacon run                      # Sync, classify, route, sweep
/beacon search "dspy"            # Find items matching "dspy"
/beacon stats                    # Processing health overview
/beacon mark reviewed 15 23      # Mark items as reviewed
/beacon reject 31 "not useful"   # Quick reject with note
/beacon sweep                    # Archive stale items
/beacon regenerate               # Rewrite all backlog files
```
