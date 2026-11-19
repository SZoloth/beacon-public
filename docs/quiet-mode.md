# Quiet Mode: one bounded local curator pass

Beacon can run a bounded, run-once local pass when `config.json` contains an
explicit `curator` object. It does not activate a scheduler. A scheduler may
invoke `python3 beacon.py curate --once`; an interrupted pass is held rather
than replayed because a remote request may have completed before the process
stopped.

```json
"curator": {
  "context_path": "/private/explicit-context.json",
  "advisory_inbox": "/private/shared-advisories.json",
  "task": "Build a local SQLite backup workflow",
  "window": {"since": "2026-09-19T00:00:00Z", "until": "2026-09-21T00:00:00Z"},
  "max_save_reviews": 20,
  "candidates": 8
}
```

The sync and manual-save paths run the bounded local future-use/collision
review for new or changed source revisions when this object is present. The
run-once command imports the advisory inbox using the existing immutable
version/dedup contract, records local save holds, and emits a read-only deck for
the configured half-open window. Missing context, malformed inboxes, stale
advisories and failed reviews remain visible as holds or failures; they are not
dropped.

An interrupted run is recorded as an interrupted/unknown-completion hold. A
matching window/config returns that failed held result with zero new calls; a
new input identity or an explicit future retry is required to run again.
Cached results retain their original phase details and failure reasons while
reporting zero calls for the current reuse.

No model upload, notification, route, merge, delete or schedule activation is
performed. The inbox and context are local paths and must be explicitly
configured. Tests use invented data and temporary databases.

Use `python3 beacon.py review save:ID` or `review advisory:ID` to acknowledge an exact saved report. Filing does not verify a condition, clear a provider failure, execute advice or schedule a retry. Changed evidence returns to review. Read the [human review guide](human-review.md).
