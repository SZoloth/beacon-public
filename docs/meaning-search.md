# Meaning search

`beacon search "query" --meaning` can find saves whose wording differs from the
query. It examines a bounded window and asks TypeSafe for provisional meaning
judgments. It does not change items, workstreams or statuses, initialize a database,
or run the intake pipeline. Ordinary keyword search remains unchanged.

Start with `beacon search "preserve a coherent copy without pausing transactions" --meaning --preview --db /path/to/beacon.db`.

Use `--preview` to print the exact UTF-8-bounded requests without a key or
network call. A live semantic run requires both `--allow-upload` and an
approved `TYPESAFE_API_KEY`; the request includes only the query, item IDs,
titles, and first-800-character excerpts. It never sends URLs, authors, raw
JSON, statuses, labels, or local fingerprints.

The window is controlled with `--scan-limit` (1–100, default 40), `--limit` (1–20, default 20),
`--before-id`, and an optional active `--workstream`. Results are provisional
and apply only to the examined window. Use the reported cursor to inspect an
older window. The command rechecks the full source snapshot before each call
and at release; any source, lifecycle, workstream, or window change rejects
the whole result. Earlier calls may already have incurred provider spend when
a later call fails.

The window orders eligible saves by descending item ID, not keyword relevance.
Rejected and stale saves are excluded. `--before-id` is exclusive. Counts disclose
older items and items at or above that cursor that this call did not examine.
The next cursor follows the last examined item, even when there are no matches.
Relevant text after the first 800 body characters, title characters after 240,
and saves outside the window are not judged. This is not a full-library index.

Each invocation plans at most five requests before sending any, with at most
20 items and 24,000 UTF-8 bytes per request. Large Unicode inputs can exceed this
budget even below 100 saves; narrow the window if planning rejects it. No retries
or partial rankings follow a failure. Full bodies are hashed one row at a time
for freshness; one exceptionally large saved body still affects local memory.

Probabilities at least 0.8 are provisional matches; at most 0.2 are nonmatches.
Values between them stay visibly uncertain. The console shows saved links and
uncertain evidence; `--json` retains all probabilities, fingerprints, usage,
coverage and display-overflow counts. These thresholds do not authorize routing.

## Verification

Run `python3 -m unittest discover -s tests -v` for local checks.
Run `python3 tests/eval_meaning_search.py` to preview the six frozen invented cases
without credentials or upload. Live evaluation additionally requires `--live
--allow-upload`, a process key and a fresh output path supplied with `--output`.
The evaluator caps itself at 12 attempted calls and preserves unknown usage on failure.

The [recorded September 20 results](../tests/fixtures/meaning-search-results-2026-09-20.json)
contain seven live calls over 40 eligible invented saves. Five intended matches
were found, 33 decoys excluded and two decoys held as uncertain. The older keyword
candidate stage included only two of those five intended saves. The no-match case
returned no match. Two cases were designated calibration and four held out before
the run; after inspection all are calibration. A separate model agreed with the
authored labels before Jev ran, but this is not human ground truth or representative
library accuracy. No prompt or threshold was tuned after seeing the results.
