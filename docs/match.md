# Match a save to a project or a way of working

`beacon match` reads saves already in the database and compares them with the active workstreams in `config.json`, plus the optional `practices` list. It prints a recommendation. It does not classify, route, edit a backlog, open a pull request, change a bot profile, or send a message. `--write` stores the same recommendation and still does none of those.

A recommendation is not an approved task. Filing one still takes classify and route. The next action is one step a person can do later. Match does not take that step.

## Commands

```bash
python3 beacon.py match --db /path/to/beacon.db
python3 beacon.py match --db /path/to/beacon.db --write
```

Each recommendation has the target kind (`project` or `practice`), slug, name, disposition (`apply` or `reference`), confidence, the configured evidence it used, a citation (`source`, `external_id`, `url`), an explanation, and a next action. The explanation names the target and the evidence, and says what that project is. The next action names one concrete step and says it is a recommendation only. Nothing is changed, opened, or sent.

A save with text and no configured evidence is `no_fit`. Its next action is: leave the save unmatched. An empty project list and an empty practice list is `insufficient_context`.

A save can match more than one target. The pair of item and target is stored once. Running match again replaces those rows with the same explanations.

## What can match

A project matches only from its own config:

- a keyword in that workstream
- a `Helped by` phrase of at least two words
- the project name, when the name is at least five characters
- a GitHub star whose repo id is that project’s backlog path

`apply` is used when the save names the project and also hits a keyword, a `Helped by` phrase, or a bullet from that project's `beacon-context.md`, or when the GitHub repo id matches, and the description is not reference notes. A phrase without the project name stays `reference`. The threshold is not lowered. Parked workstreams (`active: false`) are skipped. Areas stay on the existing area path.

`beacon classify` now sees the same project description, not only the slug and name. A unique `Helped by` phrase of at least three words is preclassified as `reference` (or `build-with` when the save also names the project and is a tool). A two-word phrase such as "writing tools" or "web design" stays a match recommendation and is not assigned on its own. Two projects hitting at once still go to the model. The model must return an empty workstream list when nothing fits. An assignment is stored only at confidence 0.6 or higher, and only for a project that was offered. `non-technical` is stored as no useful match. This floor is the legacy classifier's. It does not change Jev's 0.85 routing policy or `route-top`'s 0.5 floor.

`beacon route` still files an assignment that classify already made. The regenerated backlog repeats that item's next step under **Next**. Writing the line does not perform the step, open a task, or send anything.

`beacon-context.md` sits beside `backlog_path`. Beacon's own file is `docs/beacon-context.md`, with phrases taken from `THEORY.MD`. If the configured path is missing, match reports `loaded: false` and that path. It does not invent phrases for the other repos. On the machine that has those repos, add the file next to each backlog. The private sample's 20 reference hits and 0 apply hits were keyword hits that did not also name the project.

A practice matches only phrases in its `evidence` list. The three configured practices are coding verification, skill organization, and routing correction evals. Their disposition is `reference`. Adding one looks like this:

```json
{
  "slug": "routing-correction-evals",
  "name": "Routing correction evals",
  "evidence": ["routing evaluation", "routing correction"],
  "application": "Add a frozen routing case when the save names a miss. Do not change live routing from the save.",
  "disposition": "reference"
}
```

`beacon match` lists projects that have no keyword, no `Helped by` phrase, and no usable name under `gaps`. It does not guess a destination for them.

Projects are matched from the descriptions already in `config.json`. A save that says "bot fleet" and none of an assistant project's configured phrases (say "personal assistant agents" and "agent memory", plus its name) does not match it.

## What does not match

Empty text, a title-only Readwise row, a Beacon echo that is not also a bookmark, and a like-only row abstain. They are not routed. A save with text and no configured evidence is `no_fit`. An empty project list and an empty practice list is `insufficient_context`.

## Before and after

These are synthetic saves, the same shape as the tests.

A save that names Folio and "local-first sync":

- Before: "Review against Folio. Suggested application: local-first sync."
- After: Folio fits because the save contains "local-first sync", and Folio is a local-first reading app. Next: write one note on applying "local-first sync" from that save, then stop. Nothing is changed, opened, or sent.

A save that says "local-first sync" and does not name Folio:

- Before: the same "Review against Folio" line, marked reference only because the name was missing.
- After: keep it as a Folio reference for "local-first sync". Open it the next time you work on Folio. Do not open a task.

A save about a game score, with no configured evidence:

- Before: `no_fit`, with no next step.
- After: `no_fit`. Next: no useful match. Leave the save unmatched. Nothing was filed.

A GitHub star whose repo id is the configured project:

- Before: "Review against" that project, using the repo id as the suggested application.
- After: compare the repo with the current work and write down one difference. Do not open a pull request.

## Tests

`tests/test_match.py` covers a Folio project hit, a phrase that stays reference, a practice-only hit, no-fit, no-text, title-only, echo, like-only, two targets for one save, a second run, a GitHub repo id, and the live source list staying intact. X, GitHub, and Readwise stay live. mymind stays a file handoff. `tests/test_classify.py` covers a three-word Helped by assignment, a two-word phrase that is not assigned, and weak, unknown, and non-technical model assignments that are not stored. `tests/test_roadmap.py` checks that the backlog names the next step and does not start it.
