# Import mymind saves

Use mymind's account export and save it locally. The [official export instructions](https://mymind.helpscoutdocs.com/article/18-can-i-export) describe `cards.csv` for notes, quotes, bookmarks and highlights, with images and other saved files exported separately. They do not publish a CSV column schema. The [official app listing](https://apps.apple.com/us/app/mymind-extend-your-mind/id1520332347) advertises Connections/MCP, but a bulk synchronization API contract has not been verified for this importer. Beacon uses explicit local CSV imports.

```bash
python3 beacon.py import-mymind /path/to/export --preview
python3 beacon.py import-mymind /path/to/export/cards.csv
python3 beacon.py import-mymind /path/to/export/cards.csv --db /path/to/test-beacon.db
python3 beacon.py list --source mymind
```

Preview parses and validates the complete file, then prints counts and mapped field names. It opens no database and prints no saved content. Import creates a destination database when necessary, without seeding workstreams, calling models, fetching URLs or files, uploading content, routing backlogs, or enabling a scheduled sync. Rows use source `mymind`. Run normal classification/review separately when you choose to process them. Existing classification can involve external model calls, so the import command never invokes it.

CSV must be UTF-8, optionally with a BOM. Quoted multiline content is supported. A directory must contain `cards.csv` directly. Archives and separately exported image/PDF/file contents are not imported. File references and other unrecognized CSV fields remain in `raw_json.record`; they are never opened or fetched.

## Column mapping

Headers ignore case and normalize spaces, underscores and hyphens. At least one title, body or URL column is required, and every row must contain one of those values. If an export uses different headers, rename columns in a local copy. Multiple headers mapped to the same field are rejected except body columns. Distinct nonempty body values are joined with two newlines in the alias order below, preserving their original whitespace. All original field values, including unmapped fields and the untrimmed text, are retained in `raw_json.record`.

| Beacon field | Accepted headers |
| --- | --- |
| External ID | id, card id, card uuid, uuid, external id |
| Title | title, name |
| Body | content, text, note, notes, quote, highlight, description |
| URL | url, link, source url, source link, bookmark url |
| Author | author, creator |
| Source tags | tags, tag |
| Created time | created, created at, created date, date created |
| Saved time | saved, saved at, saved date, date saved, added at, added, date added, date |
| Updated time | updated, updated at, updated date, date updated, modified at |

Dates accept ISO 8601 with an explicit timezone, converted to UTC. Date-only `YYYY-MM-DD` values use midnight UTC to represent that date. Datetimes without a timezone are rejected. Missing dates are not guessed from the import time. An existing date is retained if a refreshed export omits it. Source updated time is stored as normalized `raw_json.updated_at`; Beacon's `synced_at` records the local import time.

Tags accept a JSON array of strings or comma, semicolon or pipe separated text. Trimmed source tags are recorded in `raw_json.source_tags` and added to Beacon's shared tags. Original spelling and separators remain in `raw_json.record`. Tags removed in a later export remain in Beacon's merged tags because existing classifier tags have no ownership distinction. The current source tag list in `raw_json.source_tags` stays authoritative. A tag containing a delimiter must use a JSON string array to preserve its boundaries.

## Repeat imports and limits

Original card IDs are used when present. Without an ID, Beacon uses `sha256:` plus a hash of the URL and normalized saved timestamp when both exist. Otherwise it hashes the URL, title and body. Moving the export or reordering its rows does not change these IDs. The fallback cannot distinguish identical saves, and editing identity fields can create a new item. Stable card IDs are preferred. Unknown fields and tags do not participate in fallback identity.

Duplicate identical records are coalesced. Conflicting rows with the same identity, duplicate headers, invalid dates/tags, malformed CSV and rows with the wrong number of columns reject the complete import before database access. Item and tag writes occur together in one transaction; database failures roll them all back and return an error.

Refreshes preserve Beacon's lifecycle, classification fields, project assignments and classifier tags. Checkouts with intake review triggers still apply those triggers when reviewed content changes. Import does not mirror deletions, remove old items, or provide live synchronization. It requires a new account export for subsequent updates. These tests use synthetic exports; actual account coverage and field mapping require checking your export locally. Keep exports and resulting databases private.
