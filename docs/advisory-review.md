# External advisory review holds

Beacon accepts explicit, local JSON packets for advisory review. It does not
fetch the referenced source, call a model, route work, notify anyone, or grant
permission to act.

```sh
python3 beacon.py advisory ingest /path/to/advisories.json
python3 beacon.py triage --held
python3 beacon.py advisory resolve 12 --note "Checked manually with the action owner."
```

Use a version-1 packet with one or more entries:

```json
{
  "version": 1,
  "advisories": [{
    "source_owner": "Synthetic source owner",
    "action_owner": "Synthetic action owner",
    "source_ref": "synthetic:advisory/1",
    "current_version": "v1",
    "advisory_reason": "Verify the invented safety assumption before action.",
    "observed_at": "2026-09-20T00:00:00Z",
    "expires_at": "2026-09-21T00:00:00Z",
    "severity": "urgent",
    "risk_type": "safety"
  }]
}
```

`source_owner`, `action_owner`, `source_ref` and `current_version` form the
deterministic identity. Identical replays leave the existing hold unchanged.
Changed content under the same identity is rejected atomically; use a new
`current_version` for a new assessment. Expired holds remain visible and marked `expired`; they
are never silently deleted. `material` and `urgent` safety or financial holds
remain in the queue until a person explicitly resolves them. A resolved hold
does not re-open when the same version is ingested again; publish a new
`current_version` for a new review item.

The ingest receipt reports separate event and deduplicated counts. All packet
values are treated as assertions: recheck the source and current owner before
any action.

```sh
python3 -m unittest discover -s tests -p 'test_advisory.py' -v
python3 -m unittest discover -s tests -v
```

The tests use invented packets and temporary databases only. They do not
measure real-world safety, financial accuracy, or reduced interruptions.

Mosaic's **Save review holds…** exports this contract explicitly. It includes
uncertain item IDs and fixed review labels, not titles, descriptions, the brief,
images or preferences. A review version is stable until its inputs change or a
new review begins. These general warning holds expire after seven days but stay
visible. Import the saved file with the command above; neither app syncs it in
the background. Source and action owner remain Mosaic, not Beacon or Shipyard.
