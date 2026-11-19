# Review a save before it becomes another obligation

`consider` reads one eligible saved item and a small, explicitly supplied context
packet. It does not fetch tickets, import reading queues or change any records.

```sh
python3 beacon.py consider 123 --context /private/current-context.json
python3 beacon.py consider 123 --context /private/current-context.json --html
python3 beacon.py consider 123 --context /private/current-context.json --provider typesafe --preview
```

Local mode shows source evidence and leaves judgments unassessed. `--html` prints
a static, escaped review card to stdout. It is not a public page or a server.
The existing five-card command also accepts `--html`:

```sh
python3 beacon.py deck "Build local backups" --since 2026-09-19T00:00:00-06:00 --until 2026-09-20T00:00:00-06:00 --html
```

## Context packet

Use version 1, a task and arrays of at most eight conditions and eight efforts.
Every entry needs a unique ID within its kind, project, action owner, source
reference, timezone-aware observation date and expiry. Conditions supply an
authored `when` fragment. Efforts supply the actual open-ticket or reading-queue
`summary`. Unknown fields are rejected. Example, entirely invented:

```json
{
  "version": 1,
  "task": "Build a local SQLite backup and restore workflow",
  "conditions": [{
    "id": "local-backup",
    "project": "invented-reader",
    "owner": "Synthetic owner",
    "source_ref": "synthetic:authored-condition",
    "observed_at": "2026-09-20T00:00:00Z",
    "expires_at": "2026-09-21T00:00:00Z",
    "when": "you need a local SQLite backup with an integrity check."
  }],
  "efforts": [{
    "id": "backup-ticket",
    "project": "invented-reader",
    "owner": "Synthetic owner",
    "source_ref": "synthetic:ticket",
    "observed_at": "2026-09-20T00:00:00Z",
    "expires_at": "2026-09-21T00:00:00Z",
    "summary": "Implement local SQLite backups and verify integrity."
  }]
}
```

Dates are evidence, not permission. Entries older than seven days, expired or
observed in the future are excluded visibly. The command never independently
verifies supplied ownership or source assertions. Recheck current owner/source
before an action. Do not fabricate fresh dates to bypass exclusion.

## Optional judgments

After inspecting the preview and approving its exact data and spending scope,
`--provider typesafe --allow-upload` makes at most one bounded request. It uses
the existing server-side `TYPESAFE_API_KEY`; never put a key in the packet. Private
ticket or reading data needs its own upload approval. IDs, owners, references,
URLs and timestamps stay local; source and condition text are not sanitized.

TypeSafe scores support for each authored future-use condition. A score of at
least 0.8, at least 0.15 above the next candidate, proposes the literal template
“You will want this when …”. Otherwise the save remains a hold. These are
provisional display thresholds, not calibrated automation. The command does not
claim that the condition is already met; the existing `conditions check` workflow
still owns explicit event evaluation. Nothing auto-routes from this advice.

Collision suggestions reuse the existing duplicate/alternative/distinct rubric,
show the saved excerpt and supplied effort summary, and retain the action owner.
Uncertainty stays unassessed. Overlapping effort is not proof that projects should
merge. No copy, merge, deletion, notification or background schedule is performed.
Full saved source changes, packet changes and expiry during a request invalidate
the result. This is an explicit review command, not a new on-save hook.

## Checks

Run `python3 -m unittest discover -s tests`. The synthetic contracts check the
actual CLI and database bytes, snapshot freshness and changes, conflicting
condition fits, HTML escaping, and rejection of conflicting output options before
any upload. Model accuracy, reduced interruptions and usefulness in a real
morning routine remain unmeasured. Overnight activation is a separate gate.
