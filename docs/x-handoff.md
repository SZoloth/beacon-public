# Import X bookmarks and likes

The connector, or a person, reads bookmarks and likes with the sanctioned X tools and writes an `x-handoff-v1` file. Beacon imports that file locally. `bird` on a machine that has it remains the live bookmark sync. The two paths share one post id through `store_tweet`.

`beacon import-x` does not page X. It does not accept a token. It is not scheduled.

A bookmark is an intentional save. A like is weaker interest. A like is not a command, and it is not an endorsement. Likes stay out of classification, triage, routing, and the library. A later bookmark for the same post updates that same row and makes it a save.

`saved_at` and `liked_at` are stored only when the file contains them. X often omits them. Beacon does not copy `fetched_at`, `created_at`, or the import time into those fields.

## Commands

```bash
python3 beacon.py import-x /path/to/handoff.json --preview
python3 beacon.py import-x /path/to/handoff.json
python3 beacon.py import-x /path/to/handoff.json --db /path/to/test-beacon.db
python3 beacon.py list --source x
python3 beacon.py list --source x-like
```

Preview reads the whole file and prints counts. It does not open a database. The counts do not include post text, ids, or URLs.

Import creates the destination database when it is missing. It does not seed workstreams, classify, route, fetch URLs, or update sync state. `beacon sync --source x-like` is rejected. Likes are not a sync source.

## Scheduled X sync on macOS

The weekly job uses Keychain credentials when both `BEACON_X_AUTH_TOKEN` and `BEACON_X_CT0` are present in `with-secret`. Add them with `with-secret --add BEACON_X_AUTH_TOKEN` and `with-secret --add BEACON_X_CT0`. Use the hidden prompt for each value. Beacon passes them only to the bookmark fetch, without putting them in command arguments or logs. If either entry is missing, `bird` tries its usual browser-based authentication.

`scripts/x-keys` stores both from the x.com sign-in bird already uses (Safari, then Chrome, then Firefox) without printing them; run it again when the weekly log says the Keychain sign-in stopped working.

## File

The file is a JSON array of posts, or an object with `format` set to `x-handoff-v1` and a `posts` array.

Each post requires `post_id`, `url`, `text`, `created_at`, `source_types`, and `fetched_at`.

`post_id` is a string of digits. `url` is `https://x.com/<user>/status/<id>` or `https://twitter.com/<user>/status/<id>`. A suffix such as `/photo/1` is allowed. The status id matches `post_id`. `text` is a string and may be empty. `created_at` and `fetched_at` are ISO 8601 timestamps with a timezone. `source_types` is a non-empty list of unique names, each either `bookmark` or `like`.

`fetched_at` is when the connector or bird read the post. It is not when someone saved or liked it.

Optional fields are `saved_at`, `liked_at`, `source_ranks`, `author.username`, `author.name`, `media`, `public_metrics`, and any other JSON. A missing time is null. `source_ranks` maps those signal names to integer page indexes. A rank is not a time. Beacon keeps the original post object at `raw_json.handoff`.

A bad type, a bad date, an empty `source_types`, an unknown signal name, or a URL that does not match `post_id` rejects the whole file. Beacon does that before it opens the database.

Duplicate `post_id` values in one file merge when `text` and `created_at` match. Beacon unions the signals and ranks. A conflicting `text` or `created_at` rejects the file before the database opens.

## Rows

One post is one row, keyed by post id.

A bookmark uses source `x`. The first insert sets status `new`. A like with no bookmark uses source `x-like` and status `interest`. The role comes from the signals. It is not a second stored flag.

`items.saved_at` comes only from the bookmark signal's `acted_at`. A later import with no bookmark time leaves a stored `saved_at` in place. A like never writes `saved_at`.

`items.created_at` is the post time from `created_at`. When that column is already non-empty, a later import keeps it.

`raw_json.x_signals` stores `bookmark` and `like`. Each signal has `rank`, `retrieved_at`, and `acted_at`. Imports merge those signals. A new read does not drop the other signal, and a null rank or acted time does not erase a value already stored.

`raw_json.x_explanation` is one sentence. A bookmark alone is a save eligible for classification and routing. A bookmark that was also liked says the bookmark is the save and the like is extra interest. A like alone is weaker interest and stays out of classification, triage, routing, and the library.

A source `x` row keeps the fields the library preview reads. Those are `author` with `name`, `likeCount` taken from the public `public_metrics.like_count`, `media` with `type`, `url`, `previewUrl`, `width`, and `height`, and `createdAt`. `preview_image_url` is stored as `previewUrl`. `likeCount` is that public count.

An `x-like` row becomes source `x` when a bookmark arrives. If status is still `interest`, it becomes `new`. The row id stays the same. An `x` row that later gains a like stays source `x`. Its status, classification, and workstream rows stay as they are. A bookmark stored before these signals existed is still a bookmark. A like for that post does not change its source, and an empty like text or an empty media list does not erase the saved text or picture.

Importing the same file again adds no rows. Each existing row counts as refreshed.

When `bird` rewrites `raw_json`, it starts from the tweet it just read, then puts `x_signals` and `x_explanation` back on that object. A resync does not drop a like already stored for that post.

## Tests

`tests/test_x_handoff.py` uses synthetic posts. Keep real exports, and the databases they produce, private.
