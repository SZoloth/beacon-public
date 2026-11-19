# Review intake and compare saved resources

`triage` and `compare` return JSON without changing Beacon's database, summaries, routing or lifecycle. The opt-in classification integration below persists routing decisions and holds. TypeSafe supplies narrow semantic judgments; code controls execution.

## Review intake

```sh
python3 beacon.py triage
python3 beacon.py triage --provider typesafe --preview
```

By default, review one newest unclassified `new` item against the database's active workstreams. Supply saved item IDs after `triage` to select particular records, including records beyond the default first page. `--limit` accepts 1–20 for the default pending selection; explicit IDs select those records instead. Rejected, stale and missing records fail explicitly.

Existing exact GitHub matches retain their deterministic intent and destination without a model call. Other items receive separate questions: one Choice for intent and one Noul (probability of yes) for each active workstream. Multiple destinations and no destination are valid. The response retains a deterministic baseline over the same capped snapshot and leaves the existing generated summary unchanged. This baseline omits author context and is not a replay of the full production classifier.

The default local mode lists unresolved items in `review_ids`; it does not invent semantic answers. With TypeSafe enabled, uncertain intent, ambiguous destination fit, and conflicting judgments also produce `assessment: review`. Strong unsupported fits produce `no_fit`, not an arbitrary destination. Thresholds are provisional: intent needs winning probability ≥0.8 and confidence ≥0.5; destination fit ≥0.8 is a suggestion, ≤0.2 is a negative, and intermediate values need review. Confidence measures distribution concentration, not correctness.

`triage` remains advisory. `classify --provider typesafe` and `run --provider typesafe` use the separate persistent routing gate described below; they never consume arbitrary triage output.

### September 29 real-data changes

The first real-bookmark run (65 items) routed nothing and held 47 for review. Two causes were fixed:

- **Project context.** Jev only saw slugs and names ("toolkit", "Atlas"), so it could not tell projects apart. Each workstream in `config.json` now has a one-sentence `description` (capped at 240 characters). Each fit question carries its own project inline as `{"project": ..., "question": "...`project`..."}` instead of indexing `workstreams[i]` in a shared list; TypeSafe documents Jev 1.13 as weak at that kind of indirection. On the same 65 items, agent-tooling posts stopped matching the reverse-engineering and Slack projects, and `agent-workflows` became the clear top fit.
- **No-fit short circuit.** When every destination fit is ≤0.2, the item is `no_fit` even if its intent is uncertain. Nothing could be routed, so it is not worth human review. Held items dropped from 47 to 24 of 65.

**Top-K routing (`beacon route-top`).** On 10,963 real items only one fit score reached policy v2's 0.85 bar, so the author chose ranking over gating on September 30. `route-top` gives each project its highest-scoring held items (fit ≥ `--floor`, default 0.5; intent not non-technical/unclear), up to `--k` (default 10) active at once, then runs the normal router. Routed items count against the cap until they are reviewed, rejected or swept stale, which frees the slot for the next-best item. Use `--dry-run` to preview. No model calls; it reads stored judgments. Policy v2 automatic routing inside `classify` is unchanged.

`classify --provider typesafe --limit` now accepts up to 1,000 items per run (processed 20 at a time). Measured cost is about $0.0002 per item. Write a new description when adding a workstream; without one, fit quality degrades to name matching.

## Compare a pair

Use two saved IDs from a Beacon listing or task shortlist; replace the example IDs below with those records.

```sh
python3 beacon.py compare 123 456 --task "Search notes offline"
python3 beacon.py compare 123 456 --task "Search notes offline" --provider typesafe --preview
```

The semantic choices are `duplicate`, `alternative`, `distinct` and `insufficient`. Overlapping purpose with different offline/cloud constraints can be an alternative; shared names alone do not establish a duplicate. Local mode returns `unassessed`, not a guessed relationship. A low-confidence or insufficient judgment remains for review.

Both source IDs, URLs, exact excerpts and excerpt offsets remain in the local output. No merge, deletion, recommendation to replace an adopted tool, or source claim verification follows from a label. Comparisons concern the supplied excerpts, not entire linked pages.

## Upload and usage boundaries

Both commands accept `--db` for an existing database and default to zero network calls. `--preview` prints the exact request locally without sending it. Keep that output private if its contents are private.

Triage sends item IDs, capped titles and excerpts, plus active workstream slugs, names and up to four keywords each. Comparison sends the task and the two items' IDs, capped titles and excerpts. Neither sends authors, source URLs, raw source JSON, generated summaries, backlog paths or credentials in its payload. Workstream names and excerpts can still be sensitive.

An approved live invocation adds `--provider typesafe --allow-upload` and loads `TYPESAFE_API_KEY` from the [shared personal-development Keychain item](useful-now.md#shared-personal-development-key). Both commands reuse the existing pinned-model adapter: one request, no retries, no redirects, 20-second timeout, 24 KB request cap. Exact-only or empty triage makes no request. The default of one item fits the current 31-workstream configuration; larger batches may exceed the cap and fail before upload. Reduce the selection rather than silently dropping projects.

Advisory results retain raw judgments, request hash, elapsed time, `project: beacon`, returned token usage and an estimated charge. They say `automation_allowed: false` and `quality: unmeasured`; that label concerns the individual private input, not the separate synthetic benchmark. The integration evaluation used invented examples only. No private uploads, key copies or purchases occurred.

## Verification

```sh
python3 -m unittest discover -s tests -p test_review.py -v
python3 -m unittest discover -s tests -v
```

Offline tests cover independent destinations, none-fit, uncertainty, conflicting intent, exact-match bypass, invalid response rejection, source preservation, payload limits and CLI read-only behavior. Mocked comparisons include different capabilities and similarly named unrelated resources; those tests verify the application contract, **not the model's semantic accuracy**. Injection-like source text is treated as untrusted in the request; model resistance is not established by these tests.

The [initial September 20 verification record](../runs/2026-09-20-intake-review/verification.json) records six read-only local/preview checks against the existing database. The later [routing v1](../runs/2026-09-20-routing/live-v1.json) and [fresh held-out v2](../runs/2026-09-20-routing/live-v2.json) reports retain live requests and responses. V1 routed none of six relevant items. After calibrating policy v2, a new ten-item holdout produced five correct routes, zero wrong routes and one missed relevant item. Four pair comparisons in v1 matched their frozen labels. These small invented samples do not establish accuracy on the author's real intake.

## Automatic routing integration

The legacy classifier remains the default. After approving the selected payload for upload, use the shared environment key with:

```sh
python3 beacon.py classify --provider typesafe --allow-upload --limit 1 --routing-evaluation runs/2026-09-20-routing/live-v2.json
python3 beacon.py route --dry-run
python3 beacon.py triage --held
```

`run` accepts the same flags and calls the existing router after classification. `classify` alone does not write backlog files. Without `--routing-evaluation`, only exact configured matches can be approved; semantic results remain held. The evidence file must match the pinned model, frozen fixture hash and `typed-routing-v2` policy, with zero errors/false routes and at least four correct held-out routes.

Policy v2 requires fit ≥0.85, other destinations ≤0.10, intent probability ≥0.90 and confidence ≥0.80. Ambiguous/no-fit results remain in `intake_reviews` and are excluded from both classifiers and routing. Unchanged held items make no repeated calls. Changed new/unclassified sources can be reevaluated; changed previously classified items require explicit review. No terminal item is resurrected; inactive destinations, stale snapshots and existing assignments fail closed. File delivery is acknowledged only after the backlog write succeeds, so a failed write remains retryable.

The tests replay v2's actual responses through temporary SQLite state and real temporary backlog files: five routed entries with preserved summaries and no writes to personal project backlogs. Private-data calibration and deliberate activation of personal intake remain separate from this integration test.

Complete held items with `python3 beacon.py review intake:ITEM_ID`. Choose an explicit intent and complete destination set, file the historical report, or press Enter to leave it held. This local human decision requires no model and does not write backlog files. Read the [human review guide](human-review.md) for freshness, correction and filing limits.

## Future-use conditions

Save a condition explicitly, then supply a project event with `id`, `project_slug` and evidence in `summary` or `body`:

```sh
python3 beacon.py conditions add backup-ready --workstream PROJECT --title "Revisit backup guide" --when "The project needs SQLite backups" --author Ada
python3 beacon.py conditions check backup-ready ITEM_ID --event project-event.json
python3 beacon.py conditions list
```

`check` previews the exact request by default; add `--live --allow-upload` only for an approved payload. This creates local suggestions, not notifications or lifecycle changes. Identical condition/item/event snapshots skip inference. Met→not-met becomes reverted; stale/rejected items stay protected, concurrent source/state changes are discarded, and repeated events do not duplicate suggestions. No background poller was added.

Run `python3 tests/eval_conditions.py --output /tmp/conditions-eval.json` for a frozen offline contract replay (new output path required). Its explicit `--live --allow-upload` mode produced all three expected states with three real calls and no calls on repeated events; see [live conditions](../runs/2026-09-20-routing/conditions-live.json).

API references checked September 20: [Choice](https://docs.typesafe.ai/primitives/choice), [HTTP API](https://docs.typesafe.ai/api), [classification with confidence](https://docs.typesafe.ai/cookbooks/classification_using_confidence).
