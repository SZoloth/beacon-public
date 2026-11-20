# Calls: check Beacon's keep/pass calls

Beacon's pile run made 9,819 keep/pass calls on Oct 13 from about a hundred of your own decisions. A Calls session shows you ten of those saves, one at a time, with Beacon's call hidden, and asks "Keep or pass?". Each answer either confirms the call or changes it. Either way it becomes one of your own decisions, so later model calls learn from it. Spec: [docs/plans/2026-11-19-pick-calls.md](plans/2026-11-19-pick-calls.md). The code is in `lib/calls.py`.

## Commands

```bash
beacon calls session [--size 10] [--new]   # the open session's cards; --new starts another ("10 more")
beacon calls check --session S --card C --answer keep|pass|unsure [--destination D] [--chip X]
beacon calls undo --change ID              # put one change back
beacon calls summary [--session S]         # the end-of-session portrait
beacon calls rank                          # nightly: suspect scores and flagged neighbours (EmbeddingGemma, minutes)
beacon calls redecide --group G [--max-cost 5]  # only after you accept the offer
```

## Groups

Beacon's calls fall into three groups, and each group gets its own estimate. False passes and false keeps are never pooled.

| Group | Calls | On Oct 13 |
| --- | --- | --- |
| `readable` | passes whose text is 40 characters or longer | 7,065 |
| `near-empty` | passes with text under 40 characters (a bare link or picture) | 1,837 |
| `keeps` | do, read and reference | 917 |

A call can be checked while it still stands, which means nothing else has decided the save since Beacon called it. You haven't marked it in Triage or changed it in a check. A pass is still `new` or `stale`, or autopilot rejected it. A keep is still `new`, `queued` or `stale`, or it was filed as a reference.

## A session

A session has 10 cards in random order, of two kinds you can't tell apart:

- **5 random cards**: 3 readable passes, 1 near-empty pass and 1 keep, each sampled uniformly from the group. Only these feed the estimate.
- **5 suspect cards**: the calls most likely wrong. A call's suspect score is the share of its 8 nearest decisions of yours (`gists.nearest`, EmbeddingGemma 2) that went the other way. Passes flagged by your recent changes come first, then the highest scores. The top suspect is chosen first, but cards are shuffled, so it isn't always card 1. Shuffling keeps the kinds blind.
- **1 repeat** replaces a suspect once you have a check at least 7 days old. It asks that card again to measure whether you agree with yourself. Repeats never feed the estimate.

When a group's upper bound falls under 5%, it is closed: its random slots go to suspects. Without a ranking (before the first `calls rank`), random cards from the open groups fill the suspect slots.

`session` returns the newest session while any of its cards is unanswered, so opening the app twice doesn't start two sessions. `--new` (or `?new=1`) starts another. A session also carries up to two Pick pairs, one matched and one repeat, from the current Pick round. A pair is served only when the round was built by the current scorer (`fresh` in `GET /pick/round`). A session never builds a round.

## A check

`keep` or `pass` is your answer, and `unsure` means can't tell. Answering the same card again replaces the earlier answer and undoes its change, so Back is just answering again.

- **Agreement** writes a `call_checks` row and changes nothing else.
- **You keep a pass**: the save goes to `destination`, or else to the suggested destination. That is Beacon's own destination for it, else the one its kept neighbours share. The save is filed as a reference (status `adopted`, note `reference`). With no destination anywhere, it becomes a `read` and stays `new`.
- **You pass a keep**: the call becomes `pass` and the save is `rejected` with the note "Passed in a check".
- **Can't tell** is stored with `unsure`, changes nothing, and counts in neither the estimate nor your examples.

A change writes a `call_overrides` row holding the old call, status, status note, status time and routing. `undo` restores all of them and voids the check, so it stops counting everywhere.

Every check that stands, confirmations included, is one of your own decisions in `gists.examples()`. Gists, `pile decide` and hand-offs then show it under similar saves. The newest decision on a save wins.

## Estimate

Each group's random checks give a Wilson 90% interval (z = 1.6449). Can't-tell answers are left out. Below 10 random checks the range is null and `enough` is false ("not enough checks yet"). For example, 2 changes in 30 checks gives 2.2% to 18.3%.

## Nightly ranking

`beacon calls rank` scores every standing, unchecked call and stores the score in `call_suspects`. It then flags the 8 nearest standing passes of each change made since the last run (`call_flags`), and those lead the next session. It runs EmbeddingGemma on this Mac and takes minutes, so it never runs on a request. If the model fails, the last ranking stays.

Nothing is re-decided on its own. When a group's lower bound rises above 15%, the summary's `offer` names the group, how many calls would be re-decided and the cost (Haiku, about $0.20 per 1,000). `beacon calls redecide` or `POST /calls/redecide` runs it after you accept. Each changed call is a `call_overrides` row with source `redecide`, so `undo` works on it too. The offer stays hidden until a newer random check in that group.

## Phone and Mac

`beacon serve` answers these directly, with the same access key as every other route:

- `GET /calls/session[?size=10&new=1]` returns `{session_id, cards:[{card_id, position, item:{id, source, author, text, url, media, quoted?, repo?, link?}, answer}], chips, pairs}`. Cards carry no call, takeaway, kind or group. `answer` is your answer so far (`keep`, `pass`, `unsure`) or null. `chips` lists the reasons you can tap on a change; each belongs to one taste axis. `pairs` are Pick pair objects plus `round_id` and `question`; record them with `POST /pick`.
- `POST /calls/check` takes `{session_id, card_id, answer, destination?, chip?}` and returns `{agreed, unsure, beacon_outcome (keep|pass), beacon_call (do|read|reference|pass), beacon_reason, suggested_destination, change_id}`. `beacon_reason` is in the second person and is null when Beacon stored no takeaway.
- `POST /calls/undo` takes `{change_id}` and returns `{restored}`. It is false when the change was already undone.
- `GET /calls/summary[?session=S]` returns `{session_id, session:{checked, changed, unsure}, changes, groups:[{name, label, checked_random, changed_random, low, high, enough, closed, standing}], flagged_next, repeats:{checked, same}, axes:[{axis, question, decisive, needed, lean, state, said}], offer, redecide}`. Without `session`, it describes the newest session.
- `POST /calls/redecide` takes `{group}` and returns `{started, running}` at once; the summary's `redecide` shows progress and the result.

## Axes in the summary

An axis has a `lean` only from 8 decisive picks. `needed` is the number of picks still missing: up to 8 for a lean, then up to 15 for confirmed (docs/pick.md). `said` counts the chips you tapped on changes that still stand. It is shown apart from the picks you chose.
