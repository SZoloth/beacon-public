# Five-card curator preview

Read a bounded day's saves without syncing, classifying, routing or changing any
item's status. Local mode shows recent candidates, explicitly **unassessed**:

```sh
python3 beacon.py deck "Build local SQLite backups" \
  --since 2026-09-19T00:00:00-06:00 --until 2026-09-20T00:00:00-06:00
```

Use timezone-aware timestamps. The start is inclusive; the end is exclusive.
Eligible statuses are new, queued and reviewed. The timestamp is saved_at,
falling back to created_at then synced_at. Invalid/missing timestamps do not
enter the window. Read at most eight recent candidates by default (maximum 20);
the result reports eligible but unexamined items. It is not a whole-library scan.

Add `--provider typesafe --preview` to inspect the exact external payload without
making a request. A model run additionally requires `--allow-upload` and the
approved key in the child process's `TYPESAFE_API_KEY`. The shared key stays in
Keychain, not a repo or browser. Personal source text requires explicit upload
approval; the verification fixture below is entirely invented.

The model scores usefulness for your stated task and independently chooses try,
pattern, ignore or uncertain. Code reuses Beacon's existing provisional display
thresholds, validates all responses, and shows at most five useful try/pattern
cards. A card contains the actual saved excerpt and URL—not a generated promise.
Conflicting/uncertain judgments become review IDs. Ignore means not useful for
this task, not deletion. Extra eligible matches and unexamined candidates have
separate counts. A full-source change during the request discards the deck.

This is an explicit preview command, not an overnight service. Nothing is
scheduled, notified, auto-routed or removed. Confidence is not approval or proof
of quality. Cross-product taste learning is not implemented. Explicit local
review holds use the separate [advisory queue](advisory-review.md).

## Explicit shared preferences

Add `--preferences /path/to/reference-preferences.json` to use the same authored
profile that Mosaic loads. Nothing is discovered or inferred automatically.
Both products require this exact version-1 shape (the values below are invented):

```json
{
  "version": 1,
  "id": "synthetic-proof",
  "revision": 1,
  "purpose": "reference-selection",
  "consumers": ["beacon", "mosaic"],
  "confirmed": true,
  "expires_at": "2026-10-20T00:00:00Z",
  "statements": ["Prefer concrete authored experiments with explicit limits."]
}
```

Keep the file local and supply it explicitly to each consumer. It may contain
one to four statements, at most 300 characters each, within an 8 KiB file.
Use a UTC timestamp with seconds. Expired, unconfirmed, wrong-purpose or
wrong-consumer profiles fail closed. `confirmed` records an assertion, not an
authentication or approval mechanism. Increment the revision when changing an
authored preference; do not extend expiry without reconfirming it.

Plain-text and HTML output disclose the applied profile. Payload preview shows
the exact combined task and statements; profile IDs and expiry stay local.
Beacon rechecks the file and expiry after a model call and discards changed
inputs. Local mode remains unassessed. No real owner preference profile, learning,
private-data upload, notification or scheduled curator was activated.

Add `--html` for an escaped, static five-card page on stdout; do not combine it
with JSON or payload preview. It keeps uncertainty, ignored-but-retained,
additional and unexamined counts visible. [Save review](save-review.md) adds the
separate authored future-use and supplied-effort collision workflow.

## Verify without personal data

```sh
python3 -m unittest discover -s tests -p test_deck.py
python3 tests/eval_deck.py --output /tmp/beacon-deck-offline.json
```

Use a new report path on each run. `--live --allow-upload` performs exactly one
bounded API call over the same frozen six-item synthetic fixture. The report
retains raw request/response, expected outcomes, uncertainty, usage, latency and
the actual CLI output. A passing smoke fixture is not representative usefulness
or a speedup estimate. No personal morning routine has been activated.
