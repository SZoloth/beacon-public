# Pinterest boards as a source

Phase 5 of [the Beacon + Mosaic plan](plans/2026-10-09-beacon-mosaic-merge.md). Pinterest runs in `beacon sync` and `beacon run` like X and GitHub. Items are deduplicated by (source, external ID), so syncing twice changes nothing.

Readwise Reader is not part of this source. `lib/readwise_sync.py` (the `readwise` source) already lists Reader tweets; see `docs/saves-handoff.md`.

```bash
python3 beacon.py sync --source pinterest
python3 beacon.py list --source pinterest
```

## Pinterest

Reads public board RSS (`https://www.pinterest.com/<user>/<board>.rss`). It needs no login and no Pinterest API approval.

Set the boards in `config.json`, as `user/board` or the board's page URL:

```json
"pinterest": { "method": "board_rss", "boards": ["yourname/kitchen", "yourname/travel"] }
```

With no boards listed, sync prints a one-line skip and does nothing.

- **Item:** source `pinterest`, external ID is the pin number, URL is `pinterest.com/pin/<id>/`, author is the board owner. The title is the pin's title, or its text, or `Pin <id>`. The body is the pin's text plus a `Pinterest board: <name>` line, so the classifier and Jev see which board it came from.
- **Picture:** the feed carries a 236-pixel thumbnail. Beacon stores it and also a `736x` URL for the same image (Pinterest serves each image at several widths under one path). Both are in `raw_json`; the larger one is the item's `media`.
- **Library:** pins with a picture show in the Library like X posts with media, and leave it when rejected. Databases made before this change rebuild the `in_library` column once on the next start (SQLite can't alter a generated column's expression), and a failure there only prints a line.
- **Limits:** public boards only (a secret board needs the Pinterest API). A board's feed carries only its most recent pins, so sync often enough that nothing scrolls out of it. A pin on two boards is one item, labelled with the board synced last. Removing a pin from a board doesn't remove it from Beacon.
- **Safety:** only `https` and `pinterest.com` hosts are fetched, a response over 2 MB is refused, and a feed that declares a DOCTYPE or entities is refused (the stdlib XML parser would expand them).
- **Failure:** one board failing prints a line and the others still sync. If every configured board fails, the sync raises, so `beacon sync` reports `pinterest` as failed, and the sync marker is left alone.

## Not verified here

Built and tested against a hand-written fixture, `tests/fixtures/pinterest-board.rss`. No real Pinterest feed was fetched, so these are unconfirmed until run on the Mac:

- that a real board feed has the shape the fixture assumes (an `<a><img></a>` then text in each description, a pin link in `<link>` and `<guid>`);
- that Pinterest answers a plain Python request with this user agent;
- that `736x` exists for the pins in your boards (the 236-pixel thumbnail is kept either way).
