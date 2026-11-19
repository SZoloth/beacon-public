# Use saved resources together

## Build an advisory task kit

Supply two to five explicit requirements. Preview the exact bounded requests before approving an upload.

```sh
python3 beacon.py kit "Build an offline reader" \
  --requirement "Restore the exact sentence after restart" \
  --requirement "Read cached text offline" \
  --scan-limit 20 --db /path/to/disposable.db --preview --json
```

Without `--preview`, `--answers` or `--allow-upload`, the command returns unassessed evidence and gaps without making a model call. `--allow-upload` requires separate approval for source disclosure and API spend, plus an approved process key. No command discovers credentials.

The kit selects at most five complementary resources from a recent eligible window. `--before-id` pages older records. `--workstream` restricts the window. The result retains source excerpts, character offsets, full-source fingerprints and counts of unsearched items. Each resource receives independent support and conflict judgments for every requirement. High support and high conflict can coexist. The provisional support threshold is 0.8, not a calibrated reliability guarantee.

The command also asks whether explicit constraints prevent each unordered pair of examined sources from working together for the task. Different replacement implementations are alternatives, not automatic conflicts. A stated dependency with incompatible mandatory formats or runtime versions can establish a provisional conflict. Missing evidence does not.

Conflict probabilities at least 0.8 are `provisional_conflict`. Values from 0.2 up to 0.8 are `uncertain`. Lower values are `unestablished`, never compatible. Missing judgments are `unassessed`. These thresholds need evaluation on representative data. Low support does not imply conflict. Neither conflict judgments nor authored observations reject resources or change the support-based selection.

The kit does not promise a safe, complete, compatible or executable workflow. Read the selected excerpts and the displayed requirement conflicts and source-pair conflicts or uncertainties. Preserve known conflicts with `--conflicts /path/to/conflicts.json`.

```json
[{"item_id": 17, "requirement_index": 1, "note": "This resource explicitly requires an internet connection."}]
```

Requirement indexes start at zero. These authored observations remain separate from model judgments and cannot be overwritten by them. `--json` retains every examined source, requirement cell and `pair_cells` entry, including sources outside the displayed kit. Requirement cells retain `support_probability` and `assessment`, and add `conflict_probability` and `conflict_assessment`. Pair cells contain two `item_ids`, `conflict_probability` and `conflict_assessment`.

Preview includes every request upfront in replay order. Pair requests contain the union of both sources for every included pair, including pairs that cross requirement-request batches. All requests use the existing 24 KB encoded-byte limit. The command allows at most 190 pairs, equivalent to 20 examined rows, and 10 total requests. It fails before any provider call if either ceiling is exceeded. Narrow `--scan-limit` and preview again. Long evidence or five requirements can reach the request ceiling with fewer than 20 rows.

The policy is `task-kit-conflicts-v2`. Replays from the old support-only policy are invalid. No mode mutates the database, routes resources or authorizes execution.

## Resurface saves from a project event

Create the authored condition with the existing `conditions add` command. Explicitly approve each saved resource binding before checking an event.

```sh
python3 beacon.py conditions add offline-launch --workstream reader \
  --title "Offline launch" --when "The reader ships offline mode" \
  --author "Ada" --db /path/to/disposable.db
python3 beacon.py conditions bind offline-launch 17 --author "Ada" \
  --expires-at 2026-10-01T00:00:00Z --db /path/to/disposable.db
python3 beacon.py conditions event --event /path/to/event.json \
  --db /path/to/disposable.db --preview --json
```

Supply a dated event with explicit evidence.

```json
{"id":"reader-offline-launch","project_slug":"reader","summary":"Offline reading shipped in the supplied project revision.","observed_at":"2026-09-21T12:00:00Z","expires_at":"2026-09-28T12:00:00Z"}
```

The event command evaluates approved bindings for that project, not the whole catalog. Default execution is read-only and unassessed. An approved `--allow-upload` run or explicit `--answers` replay returns read-only assessments. Add `--apply` to explicitly persist local condition transitions and suggestions. Upload approval alone never authorizes that mutation. Neither mode changes item lifecycle, routes a backlog, schedules work or sends notifications.

Duplicate and out-of-order events are skipped. Future or expired events are rejected. Changed source or condition snapshots require binding approval again. Reversions hide prior active suggestions. Expired bindings and event evidence do not appear in the active suggestion list. Request and transition identities include model and policy. Each binding update rechecks source, condition and approval inside its write transaction. A failed later binding can leave earlier valid transitions applied; repeat the same event to skip those safely.

## Review independent items in batches

```sh
python3 beacon.py triage 17 18 19 --batched --preview --json --db /path/to/disposable.db
python3 beacon.py consider-batch 17 18 19 --context /path/to/context.json \
  --preview --json --db /path/to/disposable.db
```

`consider-batch` uses the same dated context format as [save review](save-review.md). Both commands offer text and JSON output, exact previews and offline answer replay. Each item retains its own result. All active destinations stay in intake requests, including wide configurations. Batches split before the existing 24 KB request limit. An oversized singleton fails before upload; destinations are never silently dropped.

Normal `classify --provider typesafe` batches independent advisory decisions. `--typed-batch-size` lowers the maximum from 20. Per-item transactions reject stale source results. Automatic semantic routing stays on the previously evaluated singleton request shape, even if a larger batch size is requested. Legacy summaries remain downstream of acceptance. `summary_generation_calls` counts their external invocation attempts separately; their tokens and cost are unavailable and excluded from typed cost.

Configured TypeSafe curator passes use batched save review. Shared usage is counted once in the outer result. Each item is retained as a local hold, with source/context freshness checked before persistence. Model and policy changes invalidate curator reuse. No scheduler is added.

## Replay without a provider

Save one response envelope for each request in preview order. Copy its corresponding `request_hashes` entry into `request_sha256`.

```json
[{"request_sha256":"hash from the exact preview","response":{"model":"jev-1.13.0","answers":{"question_id":{"type":"noul","noul":0.9}},"usage":{"input_tokens":0,"output_tokens":0}}}]
```

Use `--answers /path/to/answers.json` instead of `--preview` or `--allow-upload`. Answers must exactly match every requested question and type. Choice responses also need their complete probability distribution and confidence. A changed request, source fingerprint or model invalidates the replay. Replay proves the consumer behavior, not model accuracy. A condition-event replay changes local condition state only with `--apply`; the kit, triage and consider commands remain read-only.

Run the isolated checks with `python3 -m unittest discover -s tests -v`. The new tests exercise actual CLI preview/replay with temporary databases, source edits, conflicting excerpts, injection text, missing metadata, requirement gaps, event expiry/reversion and full destination coverage. Synthetic and mocked judgments do not establish representative quality, latency or cost savings.
