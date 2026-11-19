# Complete a held review

Use Beacon's existing command interface to make a local decision about saved evidence. The command does not call a model, sync sources or write project backlog files.

1. Run `python3 beacon.py triage --held` to see intake holds and saved review IDs.
2. Run `python3 beacon.py review intake:123` with the displayed item ID. For other reports use `save:123` or `advisory:123` with that report's ID.
3. Choose `classify`, `file` or press Enter to leave the evidence held. Only actions available for that record appear.
4. For classification, enter an explicit intent and the complete destination set. Enter comma-separated active slugs or `none` to retain the save without a project destination.
5. Supply a note and type `yes` to save. Reopen the same target to inspect or correct an intake decision.

Replace example IDs with the displayed record. Add `--db /absolute/path/beacon.db` to choose an existing database. The default uses Beacon's current local database. Add `--show` for a read-only JSON snapshot. Keep this output local when sources, project paths or review notes are private.

## What each decision means

`classify` records your explicit intent and destination set. It preserves saved words, summary, reading status and lifecycle. Changed assignments become pending in the existing router. The command does not change backlog files; removed assignments can remain in previously generated files until the ordinary router regenerates them. `unclear` is not an approval option. Leave uncertain evidence held instead.

`file` acknowledges that exact historical report. It does not certify a model judgment, fix a failed call, verify a collision or complete a suggested action. The underlying failed or uncertain report stays intact. An advisory is marked resolved as a read acknowledgment, not as proof that its action happened. Its original version and expiry remain unchanged.

Press Enter at the action or destination prompt to leave the record untouched. Any final confirmation other than `yes` also leaves it untouched. Closing the command during a prompt makes no change. If a command stops after confirming, reopen the record to inspect the saved state before retrying.

## Freshness and history

The decision is bound to the displayed source, lifecycle, saved hold and relevant routing state. A concurrent change rejects the save and asks you to reopen. Missing sources, inactive destinations and rejected or stale items cannot be classified here. You can file their historical reports without reviving them.

Each saved decision retains its displayed evidence and note. Repeating the same confirmed snapshot returns its earlier receipt without applying it again. Reopening cannot repeatedly file unchanged evidence. A changed source or other bound evidence reopens a filed hold. An intake record can still be classified after filing or corrected after classification.

History begins with decisions made through this command. It cannot recover older machine assessments that were overwritten before this feature existed.

## Verification

Run `python3 -m unittest discover -s tests -p test_review_actions.py` for the focused offline checks. The full September 21 suite passes 150 tests. Real CLI interaction was exercised against an invented temporary database, not the author's inbox. No upload, automatic personal routing or recurring curator was enabled.
