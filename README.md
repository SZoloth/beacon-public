# Beacon

Beacon turns the things you save (X bookmarks, GitHub stars, Readwise highlights) into a short queue per project. It keeps one SQLite database on your Mac. Claude reads each save, decides which of your projects it would help, and writes it into that project's `beacon-backlog.md`. Long reads go to a reading inbox, and the rest stays out of your way.

It is a personal tool that is being opened up. Expect rough edges and file an issue when you hit one.

## What you need

- A Mac. Linux runs the core commands; the apps and launchd jobs are Mac-only.
- Python 3.9 or newer. The `python3` that comes with Apple's command line tools works (run `xcode-select --install` if `python3 --version` asks for them). Beacon uses only the standard library, so there is nothing to `pip install`.
- [Claude Code](https://claude.com/claude-code), signed in. Beacon calls `claude --print` to classify saves, so it runs on your Claude plan.
- Optional: the GitHub CLI `gh`, signed in, for your GitHub stars; a Readwise access token in `READWISE_ACCESS_TOKEN` for Readwise.

## First run

```bash
git clone https://github.com/SZoloth/beacon-public ~/beacon
cd ~/beacon
python3 beacon.py init
```

`init` asks for your name, one line about what you read, and your projects: a name, the folder, and a sentence on what kind of save would help it. It writes `config.json` and never overwrites one. To script it: `python3 beacon.py init --owner Ada --project "Recipe app|~/code/recipes|An iPhone recipe app; helped by SwiftUI techniques and recipe parsing."`

Keep your first save and find it again:

```bash
python3 beacon.py keep --url https://www.sqlite.org/fts5.html   # any web page; it skips the queue
python3 beacon.py search fts5
```

`keep` may say the picture index isn't built. That index is optional; word search works without it.

Let Claude Code search your library from any project:

```bash
claude mcp add --scope user beacon -- python3 ~/beacon/beacon.py mcp
```

Start a new Claude Code session and ask it to search your Beacon library. It sees `search_library`, `project_references`, `get_save` and the rest; `claude mcp list` shows whether the server connected.

You can also copy `config.example.json` to `config.json` and edit it by hand. What the fields mean:

- **`owner`**: your name and one line about what you read. Prompts use them.
- **`workstreams`**: your projects. Each needs a `slug`, a `name`, a `backlog_path` (where its `beacon-backlog.md` goes, usually the project's `docs/` folder) and a `description`. Write the description well: say what the project is and what kind of save would help it. Classification matches saves against these sentences, and a project it can't understand gets nothing.
- **`paths.live_db`**: the full path to `beacon.db` in this checkout. Only the database at this path may write backlog files and the reading inbox. Any other copy (a worktree, a test) prints `skipped: not the live database` and leaves your files alone.
- **`paths.reading_inbox`**: a markdown file where reads are appended. Leave it out and reads stay unrouted.
- **`career`** (optional, on a project): `true` makes plans and agent hand-offs for that project prepare and practice only, never contact anyone.
- **`client_work`** (optional): work Beacon must never hand saves to or let an agent touch. `slugs` lists client projects, `folders` the folders client files live in (`~` allowed), `names` is a regular expression that matches client and codenames, and `words` is a lowercase list at least one of which every name contains (a fast first pass). Leave it out and nothing counts as client work.

Then bring saves in, classify them and route them:

```bash
python3 beacon.py sync --source github      # your GitHub stars
python3 beacon.py import-x bookmarks.json   # X bookmarks from an x-handoff-v1 file (docs/x-handoff.md)
python3 beacon.py classify                  # Claude reads new saves, 15 per call
python3 beacon.py route --dry-run           # see what would land where
python3 beacon.py route                     # write the backlogs and the reading inbox
python3 beacon.py list --unrouted           # what didn't fit any project
```

`python3 beacon.py run` does sync, classify, route and the stale sweep in one go.

### X bookmarks

X has no free bookmarks API. Export your bookmarks to the `x-handoff-v1` format described in [docs/x-handoff.md](docs/x-handoff.md) and import that file. The author's own setup syncs X live with `bird`, which reads your browser's X session. It isn't in the example config because it depends on cookies and may break X's terms; add `"x": {"method": "bird"}` under `sources` only if you understand that.

## Everyday commands

```bash
python3 beacon.py search "local-first sync"   # full-text search over every save
python3 beacon.py stats                       # counts by status and source
python3 beacon.py list --workstream my-app    # one project's items
python3 beacon.py gist                        # plain takeaway and a do / read / skip suggestion per queued save
python3 beacon.py mcp                         # serve your library to coding agents over MCP
```

Items move through new, queued, reviewed, tried, then adopted or rejected. Queued items go stale after 21 days and reviewed ones after 45. Backlog files are regenerated from the database on every route, so edit status with `beacon mark`, not by hand.

`python3 beacon.py --help` lists everything. [AGENTS.md](AGENTS.md) is the full command reference and the rules agents follow in this repo.

## What isn't ready for other people yet

- **Taste.** On the author's library, triage agrees with them about 84% of the time because it is shown their past decisions on similar saves. A new install has no decisions yet, so expect generic suggestions until you have made a few dozen. `beacon pick` (see [docs/pick.md](docs/pick.md)) is a pick-two flow meant to shorten that; it has no app screen yet.
- **The Mac and iPhone apps** (`app/`) are built with the author's Apple team and bundle ids. Change those in `app/one/project.yml` before building. The phone app talks to `beacon serve` on your Mac over Tailscale; enter your Mac's address in Settings › Beacon.
- **Weekly jobs** (`scripts/weekly.sh`) use a TypeSafe scoring key the author has. `beacon run` works without it.

## Tests

```bash
python3 -m unittest discover tests   # about 800 tests; a few that time background processes can fail on a busy Mac
```

## License

MIT. See [LICENSE](LICENSE).
