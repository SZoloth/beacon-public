# Owner notes

The tags and notes the owner adds in mymind, Readwise and Granola say what a save is for: a prompt, an idea, a pointer to a repo or a product. Beacon keeps them in `owner_notes`, apart from anything a model wrote, and shows them to agents as the strongest signal of intent.

## Which tags and notes are kept

| Source | Sync | What becomes an owner note |
| --- | --- | --- |
| mymind | `beacon sync --source mymind` runs `objects list --jsonl` through the mymind CLI in `paths.mymind_cli` | Every note on a card, and every tag whose `flags` has bit 8 (the owner added it). Tags with only bit 2 come from mymind's AI and are dropped. Tags with no `flags` are dropped too. |
| Readwise | `beacon sync --source readwise-notes` pages `readwise-list-highlights` through the CLI in `paths.readwise_cli` | A highlight's note and tags, with the highlighted passage kept as context, and the book's tags and document note. A book's own notes sit on one save: the X post a tweet book copies, else the book's save. |
| Granola | `beacon sync --source granola-digests` reads the daily digests in `paths.granola_digests` | Each idea row with a project in "Belongs to". These rows are a model's summary of what the owner said, so they are stored with origin `owner_digest`. `## Needs you` and rows marked `none` are skipped. |

A record that copies an X post Beacon already has (by status id) attaches its notes to that post, and the post keeps its status. `beacon sync` reads X before the note sources, and each note sync ends by moving notes from a save of its own to the X post it copies once Beacon has that post. Readwise reads only recent highlights, so without that pass an older note would stay on the duplicate. Everything else becomes a save under the source's own name with status `interest`, so no stage that sends new saves to a model (classify, areas, pile, curator) picks it up. A Granola meeting title can name people. Those saves reach agents through their notes, not through classification. Readwise and Granola import only records with at least one owner note or tag. mymind imports every card, unless Beacon already has the X post it copies. A card with no notes reaches agents only while the last sync listed it (`listed` in its `raw_json`, set by `owner_notes._mark_listed`), so a card deleted in mymind, or an old save whose X post Beacon now has, drops out of agent search. These cards stay out of the app's picture feed and out of `project_references`: mymind's summary and its AI tags are searchable and returned as `summary` and `mymind_tags`, but they are what a model wrote, not the owner filing the card anywhere.

mymind syncs daily (launchd `com.example.beacon-mymind`) as well as in the Monday run. When mymind starts sending a top-level field Beacon doesn't know, the sync records it and `scripts/doctor` warns `mymind fields` until the field is added to `MYMIND_FIELDS` in `lib/owner_notes.py`, with a decision to read or ignore it. An export imported with `beacon import-mymind` adds its notes and tags to a card the sync already wrote and never replaces the card itself.

Each sync mirrors removals. mymind and Granola list everything on every run, so a note that is gone from the source is removed. The mymind sync never removes a note that `beacon import-mymind` read from an export. Readwise reads only highlights updated since the last run. Each highlight that comes back replaces its own notes, and `--full` rereads everything and removes the rest. Saves are never deleted.

An unset path turns its sync off, and the weekly run prints that it skipped it.

## Matching notes to projects

Notes are matched when an agent asks, not when they are synced. A project made after the note still finds it without a new sync.

- `project_references` lists the project's filed saves first, then noted saves (`filed: "noted"`). A save is noted when one of its notes or tags names the project's slug, name or a configured alias as whole words: `pencil review`, `pencil-review` and `Pencil_Review` all match, and `cos` does not match `cosmos`. A Granola row also matches on its "Belongs to" projects. With a task, every save must share the task's words, by the same threshold as filed saves. A save naming the project may share them in its title, text or takeaway, and comes first. A save that doesn't name the project comes in when its notes share them. A slug that is not a configured project still finds the saves that name it.
- Each save carries `owner_notes` (`source`, `kind`, `origin`, `body`) and a plain reason, such as `Your mymind tag "pencil review" names this project`.
- Area names (`home`, `style`, `food`) are ordinary words, so only a tag names an area. A note saying "a home page idea" does not reach the `home` area. Project names still match note text.
- A save the owner rejected in Beacon is left out. A stale one still comes back, because going stale is automatic.
- `get_save` and `search_library` mode `words` reach noted saves even when they are not in the picture feed, and word search matches note text. A rejected save stays out.
- A save in a list (search, `project_references`) shows its first 5 notes and `owner_notes_more`, the count left out. `get_save` shows them all.
- When notes attach to an X post, the classifier sees them in that post's `<item>` as a labeled, escaped field. The typed weekly classifier (`intake.py`) does not see them yet.

Client work follows the rule in `lib/activate.py`. A client project sees every save that names it. Every other project never sees a noted save that is filed under client work, or whose notes name a client or a client project. A tool call carries no project, so `search_library` and `find_similar` never list such a save, in any mode. `get_save` returns it when asked for by id, and inside a project only when the project is client work.

## The owner's vocabulary

Owner tags rarely say a project's name. A tag like "stews" means the Food area, and "showcase" means the personal site. `owner_vocabulary` is an authority file for this: each owner term (a tag in the same plain form, so `Pencil-Review` and `pencil review` are one term) points at one or more project or area slugs.

- The owner's rows always win. An owner row for a term hides every model row for it. An owner row with target `none` unlinks the term, even when the term says a project's name.
- A model row (`model_proposed`) comes from `beacon vocab propose`. It sends `claude --print` (through `lib/ai.py`, like classification) the unresolved terms with their save counts, and each active project's and area's slug, name and description. It never sends a save's title or text. Client projects are never offered, so only the owner can file a term under client work. A term that names a client may be sent: it is a word, and the owner allows it. Answers below 0.7 confidence, or for a slug that was not offered, are dropped, and the term stays unresolved.
- Each term carries evidence: where the owner's other tags on its saves already point, through the owner's rows, model rows and literal names. The line reads `north star (30 saves; 22 also filed under home, 1 under websites)`. Only offered places count.
- Unresolved terms are not stored. They are computed: owner tags that name no project or area and have no row. `beacon vocab` lists every term with its saves, how it resolves (literal, owner, model, none) and where, unresolved ones first.
- Each sent term is recorded against a hash of the offered slugs and the places its evidence points. `beacon run` and the weekly job ask again only about terms not yet asked against that, so adding a project, or a co-tag getting filed, gives a still-unresolved term another chance, with no re-import.
- `project_references` matches a tag, or a note that is only a term, through the vocabulary. The reason says who filed it: `Your mymind tag "stews" (Beacon files it under Food; a model proposed this)` or `(you filed this term under Food)`. `get_save` adds `tag_targets`, where each owner tag points and how.

```
beacon vocab [--json]
beacon vocab set TERM TARGET      # TARGET is a slug or none
beacon vocab unset TERM TARGET
beacon vocab propose [--preview]  # --preview prints the exact prompt and calls nothing
```
