# Beacon Triage

A native macOS app for clearing Beacon's saved items, laid out like Mail: stacks on the left, the current stack's items in the middle, the selected item in full on the right.


## Use

Each middle-column row is one saved item: what it actually offers (not its hook), what kind of thing it is, who posted it, and why Beacon suggests Do, Read or Reject for its project. Every row comes prefilled with that suggestion, so you only change the ones you disagree with. Nothing is saved until you apply the stack.

| Action | Key | What happens |
| --- | --- | --- |
| Move between items | ↑ ↓ | |
| Set the row to Do | D | on Apply: status `reviewed`; the project's agent plans it |
| Set the row to Read | R | on Apply: saved to Readwise Reader, tagged with the project; status `adopted` |
| Set the row to Reject | X | on Apply: status `rejected` |
| Go to the next item | Return or Next | setting a choice keeps you on the item, so you see it change |
| Open the original post | Space | |
| Apply the whole stack | ⌘Return | saves every row's choice in one write |
| Reject All | button | rejects every item in the stack |
| Undo the last Apply | ⌘Z | back to `queued` (a Reader save stays in Reader) |

The right pane shows the post as it appeared at its source, and under the decision buttons it says what the selected choice will do. "Repeats …" marks a near-duplicate of another row. Clearing an item frees its project's slot (25 per project), and the next Sync or weekly run refills it.

The weekly run builds the stacks and suggestions: `beacon stack` (Sonnet names themes; Jev assigns items and flags near-duplicates), then `beacon gist` (one Sonnet call per 20 new items reads the full post and writes the takeaway and suggestion). Items without a theme go in "Everything else".

## Briefs


About 20 seconds after your last Do for a project, one agent reads that project once and plans every item you marked. A "Planning…" row shows while it runs, usually 1–2 minutes. The Briefs tab then shows one plan per project: where to start, any overlaps between items, and a ranked brief per item.

- **Verdict**: do it, try it, reference only, or skip.
- **Next step**: one concrete action you can take in one sitting.
- **Details**: why it fits this project (citing files the agent actually read), the steps, the files involved, and the risks.
- **Claude Code**: opens a new Claude Code session in that project's folder with the task already typed in. Press Enter there to start.
- **Codex**: opens a new Codex thread with the task (and the project's path) typed in. Codex links can't choose a folder, so switch the workspace chip to the project before pressing Enter.
- **Copy** (clipboard icon): the task prompt, for pasting anywhere else.
- **Start on Mac** (phone only, in place of Claude Code and Codex, whose links only open on the Mac): puts the brief under "Start on the Mac" at the top of Briefs. Tap again to take it off.
- **Done**: you acted on it. The item is marked used and its slot frees up.
- **Dismiss**: hide the brief.

Sending moves the item to *tried* (in progress) and removes the brief. Nothing runs until you press Enter in Claude or Codex.

After the run, Jev checks each brief's claims against the saved post (and the README, for GitHub repos) and lists any it couldn't confirm. Agents only read; they never edit a project. Career projects (`"career": true` in config) get preparation advice only, never outreach. If a plan fails, the weekly run retries it. Details: `lib/briefs.py`.

## How it works

The app reads `python3 beacon.py queue` (queued items with their Jev match scores and stacks) and writes each decision with `python3 beacon.py mark` or `read`, so Python stays the only writer and backlog files regenerate exactly as they do from the terminal. Writes run in order in the background; the next card appears immediately. A failed write shows a red toast.

It looks for Beacon at `~/Projects/beacon`. To point it elsewhere (for example a scratch copy):

```bash
defaults write com.example.beacon-triage beaconRepoPath /path/to/beacon
```

## Phone

`beacon serve` (always on via the launchd job `com.example.beacon-serve`, log in `logs/serve.log`) offers the app's actions over HTTP on port 8765: `GET /queue`, `GET /briefs`, `GET /library`, `POST /mark`, `/read`, `/brief-status`, `/brief-done`, `/brief-mac`, `/add`, `/references` and `/plan`. Reads are answered inside the server (about 0.1 s); writes run the same `beacon.py` command the Mac app runs. The job's plist must set `ProcessType` to `Interactive`: without it launchd runs the server at background priority, and a 0.3 s request took 4-75 s whenever the Mac was busy. Large answers are gzipped for the phone. It answers only this Mac and devices on your Tailscale network, so the phone reaches it at `http://<Mac's Tailscale IP>:8765` with Tailscale on and the Mac awake. `GET /status` names the database and checkout it serves.

**Access key.** Being on the tailnet is not enough: anything but this Mac asking for itself by `127.0.0.1` or `localhost` must send `Authorization: Bearer <key>`. The server makes the key on first start in `~/.config/beacon/serve-key` (owner-only; override with `BEACON_SERVE_KEY_FILE`, or set `BEACON_SERVE_KEY`). `beacon serve --show-key` prints it; paste it into Settings › Beacon › Access key on the phone. Requests must also use a known Host (this Mac's loopback or tailnet address, a `*.ts.net` name, or names in `BEACON_SERVE_HOSTS`), carry no Origin from another site, and POST as `application/json`. Pictures under `/media/` load without the key (their names are content hashes only the authenticated library lists). To rotate the key, delete the file and restart the server. Rebuild the phone app before restarting the server on this version, or the phone gets "Beacon needs its access key" until it has one.

## Build

```bash
./scripts/build-app.sh
```

This builds a release binary, renders the icon, and installs `Beacon Triage.app` in `~/Applications`. Requires Xcode 16+ (Swift 6) and macOS 14+.
