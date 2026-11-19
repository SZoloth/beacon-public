# Pick: learn taste from deliberate choices

Pick shows two of your own saves and asks which one you would rather have saved, 20 times a round. Every answer is recorded as your deliberate choice, including the negatives that saves alone never give. Spec: [Pick: a pick-two taste mode for Beacon](https://claude.ai/artifact/VvcDvT6p1Xg5679K1gBU2N). The Pick tab in the Mac and iPhone app is the way to answer; this page covers the backend it calls.

## Commands

```bash
python3 beacon.py pick score --axis working --limit 600   # Haiku scores saves on one axis, 15 per call
python3 beacon.py pick prepare [--axis working]           # build the next round (default: least settled axis)
python3 beacon.py pick round                              # current round and your picks so far, as JSON
python3 beacon.py pick summary                            # per-axis lean, for you to read
python3 beacon.py pick record --round 3 --position 1 --choice left [--why "real numbers"]  # what POST /pick does
python3 beacon.py pick undo --round 3                     # what POST /pick/undo does
python3 beacon.py pick holdout [--preview] [--max-cost 3]  # do picks as examples help? reads only
```

Scoring costs about the same as `pile decide`: roughly $0.20 per 1,000 saves per axis. That is an estimate from the pile run, not a measurement of this prompt. `prepare` runs EmbeddingGemma on this Mac, as `gist` does, so it takes a few minutes and sends nothing anywhere.

## Axes

Each axis is a line of the taste map v1. Haiku scores a save 1 toward the keep pole and 0 toward the other. When the axis doesn't fit (jokes, sports, a bare link), the save is stored with no score and is never paired.

| Axis | Question | Map line |
| --- | --- | --- |
| working | Shows the working, or tells you about it? | Keep 1 |
| why | A principle with a worked example, or an abstract essay? | Keep 2 vs probable pass 1 |
| restraint | Quiet and typographic, or loud and maximalist? (Library saves only) | Keep 3 vs probable pass 4 |
| tonight | Runnable by you tonight, or a someday project? | Keep 4 |
| method | A method, or hype? | Probable passes 2 and 5 |
| do-read | Would you build from it, or read it? | Beacon's do/read call |

## What a round holds

Up to 20 pairs, at least 10, in random order with random sides:

- **repeat** (up to 2): an earlier decided pair with the sides swapped, so you can see whether you agree with yourself.
- **foil** (up to 4): a scored save against a same-topic X like you never bookmarked, or against a save the pile run passed. Beacon's guess is the save.
- **cross** (up to 2): two saves filed to different projects or areas.
- **matched** (the rest): two same-topic saves whose axis scores differ by at least 0.5. Saves you haven't seen in Pick come first.

If the embedding model fails, no round is built. A round without topic matching would measure topic, not taste.

## Picks

`choice` is `left`, `right`, `neither` (both are a pass) or `skip` (can't tell). `why` is empty or one of the round's three chips, tapped by you. Beacon never writes an inferred reason. Recording the same position again changes the pick, and Back removes the last one. Every row has origin `you-deliberate`.

## Phone and Mac

`beacon serve` answers these directly, with the same access key as `/mark`:

- `GET /pick/round` returns the current round with cards (id, author, text, takeaway, url, has_media) and your picks so far, or `round_id: null` with the reason. `preparing` is true while a round is being built.
- `POST /pick` takes `{round_id, position, choice, why?, shown_at?}` and returns `{ok, next_position}`. `next_position` is null once the round is done.
- `POST /pick/undo` takes `{round_id}` and returns `{position}`, the pick it removed.
- `POST /pick/prepare` takes `{axis?}` and returns `{started, preparing}` at once. Building a round takes a few minutes, so poll `GET /pick/round` until `preparing` is false. If the build failed, the round's `reason` says why, for example that the axis needs `beacon pick score` first. Only one build runs at a time; a second call returns `started: false`.
- `GET /pick/summary` returns the per-axis lean.

Every route needs the access key (`Authorization: Bearer <key>`) unless the request comes from this Mac by a local name. POSTs must send `Content-Type: application/json`. A bad request gets 400 with `{error}`.

## Summary states

Only matched and repeat picks where Beacon had a guess count as decisive.

- `inferred`: fewer than 8 decisive picks.
- `confirmed` or `overturned`: at least 15 picks, at least 80% one way, and repeats at least 70% consistent.
- `leaning`, `leaning against` or `split` otherwise.

The summary is for you to read. It is never passed to a model: `lib/preferences.py` says Beacon keeps no learned user profile.

## Picks as examples

Your picks are shown to Haiku as examples, the same way your other decisions are. When `gist`, `pile decide` or a hand-off shows Haiku a save, Haiku also sees your decisions on the most similar saves (EmbeddingGemma 2 neighbours, `gists.examples()`). A pick adds one of two lines there:

- "chose it over a similar save": the save you picked.
- "turned it down next to a similar save": the other one, and both saves when you answered neither.

A skip adds nothing. A save you picked as often as you turned it down is left out. A save you also kept or passed elsewhere shows both, as in "kept, and turned it down next to a similar save". These are your own choices on single saves, shown next to similar saves. They are not a summary of your taste, so no learned profile reaches a model.

## Holdout check

`beacon pick holdout` measures whether your picks help as examples. It holds out 30% of your decisive picks (left or right; `--share`, `--seed`). Repeats of one pair stay on the same side of the split. Haiku then predicts each held-out pick twice. The first time, Haiku sees your other decisions on similar saves but no picks. The second time, it also sees the picks that were not held out. Each save shows its 4 nearest decisions. The command only reads the database.

The output gives agreement for each version (`without_picks`, `with_picks`), how many picks only one version got right (`only_with_right`, `only_without_right`), and Beacon's own axis guess as a baseline. It also reports `cost_usd`, `tokens` and `calls`. Both versions judge the same picks in the same batches of 10. A failed batch is dropped from both, so the comparison stays fair.

- The check needs at least 34 decisive picks, so that 10 can be held out. With fewer, it says how many there are and makes no calls. `--preview` shows the split without embedding or calling a model.
- Spending stops before a batch would push the total past `--max-cost` (default $3). Each batch makes two Haiku calls of about 10 pairs.
- Read a small difference with care. With 10 to 20 held-out picks, a gap of one or two answers is noise. Look at the two `only_*` counts: they are the picks where the versions disagreed.
