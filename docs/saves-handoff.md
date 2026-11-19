# Import X bookmarks and Readwise history

`beacon sync` and `beacon run` fetch every source in `config.json`. `scripts/weekly.sh` is what launchd `com.example.beacon-weekly` and the Triage app Sync button run, on the existing Monday cadence. That script calls `beacon sync` once, so Readwise is included with x, github, keep-local, wiki, omnisearch, and last30days. One source failing does not skip the others. The script still exits 1, and its last line says a source failed.

Readwise has two intakes. `beacon import-saves` reads a file. `beacon sync --source readwise` lists Reader tweets with `GET https://readwise.io/api/v3/list/?category=tweet&withHtmlContent=true`, using the same `READWISE_ACCESS_TOKEN` as `beacon read`. A missing token imports any files already dropped in `handoffs/readwise`, then fails. It does not call Reader, and it does not mean the token was tested. `beacon sources` also does not call Reader. `readwise_auth_tested` in that report is false.

`beacon sources` prints that split without fetching. Live rows have a method. The Readwise row says `live: false` and shows the last Reader checkpoint when a database exists.

## Commands

```bash
python3 beacon.py import-saves /path/to/saves.jsonl --preview
python3 beacon.py import-saves /path/to/saves.jsonl
python3 beacon.py import-saves /path/to/saves.jsonl --db /path/to/test-beacon.db
python3 beacon.py sources --db /path/to/test-beacon.db
python3 beacon.py list --source x
python3 beacon.py list --source readwise
python3 beacon.py list --source x-like
python3 beacon.py sync
python3 beacon.py sync --source x
python3 beacon.py sync --source github
```

Preview reads the whole file and prints counts. It does not open a database. The counts do not include post text, ids, or URLs. `live` is false.

Import creates the destination database when it is missing. It does not seed workstreams, classify, route, fetch URLs, or call a model. It updates only the `readwise` sync checkpoint. X, GitHub, and the other checkpoints stay as they were.

## File

The file is JSONL (one post per line), a JSON array of posts, or an object with `format` set to `x-saves-v1` and a `posts` array. A single JSON object with `post_id` is one post.

Each post requires `post_id`, `url`, `text`, `created_at`, and `source_types`.

`post_id` is a string of digits. `url` is `https://x.com/<user>/status/<id>` or `https://twitter.com/<user>/status/<id>`. A suffix such as `/photo/1` is allowed. The status id matches `post_id`. `text` is a string and may be empty. Do not invent text when the connector only has a title. `created_at` is an ISO 8601 timestamp with a timezone, or null. Null stays empty. It is not the Reader time.

`source_types` is a non-empty unique list. Each name is `bookmark`, `like`, or `readwise_tweet`.

`readwise_tweet` requires a `readwise` object. A bookmark that also has a `readwise` object keeps both, even when `source_types` is only `bookmark`. The object has `reader_id` (string), `reader_saved_at` (ISO 8601 with a timezone, or null), and `reader_source` (string, or null). Null is stored as an empty string. It is not guessed. `reader_saved_at` is when Reader ingested the document. It is never an X bookmark time, and it is never copied into `items.saved_at` or `items.created_at`.

`text_is_title_only` is an optional boolean. True means the text is a title, not the post. Beacon does not infer that from length.

Optional `fetched_at`, `saved_at`, and `liked_at` are ISO 8601 with a timezone, or null. A missing time stays null. `fetched_at` is when the connector read a bookmark or like. It does not fill `saved_at` or `liked_at`.

A bad type, a bad date, an empty `source_types`, an unknown name, or a URL that does not match `post_id` rejects the whole file before the database opens. Duplicate `post_id` values merge when `created_at` agrees. Two different full texts reject the file. A title and a full text for the same id keep the full text.

## Rows

One post id is one row across `x`, `x-like`, and `readwise`.

A bookmark is source `x` and status `new`, including when Reader also has it. A like with no bookmark and no Readwise record is source `x-like` and status `interest`. A Readwise-only row is source `readwise`, not `x`, so it cannot stop the bird crawl.

A Readwise-only row whose text is only a title, or empty, is status `pending`. That means the post text is not here yet. It is not classified, routed, or matched. `list --status pending` shows it. A later file, or a Reader list response whose `html_content` is longer than the title, promotes that same row to `new`.

`reader_source` of `Beacon`, ignoring case, stays `interest`. That is the outbound `beacon read` echo, not a document waiting for text.

A Readwise-only row with post text, and not that echo, is status `new`. A later file that replaces a title with post text promotes the same row when status is still `interest`. A title does not replace a longer body. A bookmark that was already queued stays queued.

`raw_json.readwise` keeps `reader_id`, `reader_saved_at`, and `reader_source`. `raw_json.x_explanation` says whether the row is a save, a like, a title, an echo, or a Readwise post. A bird resync keeps the Readwise object.

The `readwise` checkpoint stores the newest `reader_saved_at` from files. An older file does not move it backward. Importing the same file again adds no rows.

`readwise-api` stores `updated_after` and `page_cursor`. A run that stops early keeps the cursor and retries that page. A finished pass clears the cursor and sets `updated_after` to the newest `updated_at`. The next run asks Reader for tweets updated after that. `list_ok` is true only after a list response parsed in that run. Each run also requests up to 50 `pending` documents by id, with `withHtmlContent=true`. A body longer than the title promotes the row. A title that comes back again stays `pending`.

`readwise-files` stores the sha256 of each file in `handoffs/readwise` that has already been imported. The directory is gitignored. Drop the historical JSONL there, or pass it to `import-saves` once, in the checkout the weekly job uses. That database is `beacon.db` next to `beacon.py`. Side effects that must hit the live library compare it with `~/Projects/beacon/beacon.db`. This environment did not import into that database and did not read the token.

## Coverage

The X bookmarks API is a recent window, not a full archive. Probes returned no next token after 149 and 196 bookmarks. Folder endpoints returned ids only, at most 20 per folder, with no page token. Some folder ids did not hydrate. Reader documents in the historical file are mostly titles. A later file can carry the post text. Those limits are properties of the connectors, not a second fetcher in Beacon.

## Tests

`tests/test_saves_handoff.py` and `tests/test_match.py` use synthetic posts. Keep real exports, and the databases they produce, private.
