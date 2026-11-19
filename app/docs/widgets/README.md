# Beacon widgets — October 10, 2026

Two widget extensions share the same views and Beacon’s unchanged Theme. Today’s picks uses Triage’s existing five-pick selection; small shows one headline and medium shows three. From your library shows one picture and rotates every three hours. Both deep-link destinations are registered and handled in BeaconApp.swift, including finding a save outside the current shelf or search.

iPhone uses `group.com.example.beacon`: Triage and Library loads publish a bounded JSON snapshot with small JPEGs. Mac uses the plan’s server fallback at `http://127.0.0.1:8765`, with a last-successful local cache, because its ad hoc signing cannot grant an App Group.

## Renders

ImageRenderer renders the shipping views in light appearance at 2× resolution. The interior picture is a local sample illustration; it is not a private Library save. Eight PNGs cover every required family on both platforms, plus two empty states.

| Family | iPhone | Mac |
| --- | --- | --- |
| Picks, small | [PNG](iphone-picks-small.png) | [PNG](mac-picks-small.png) |
| Picks, medium | [PNG](iphone-picks-medium.png) | [PNG](mac-picks-medium.png) |
| Library, medium | [PNG](iphone-library-medium.png) | [PNG](mac-library-medium.png) |
| Library, large | [PNG](iphone-library-large.png) | [PNG](mac-library-large.png) |

Regenerate from the repository root with `zsh app/one/render-widgets.sh`. This also checks JSON persistence, three-hour rotation and valid/invalid deep links.

## Validation

- Mac: build and embedded-extension signature verification pass. The ordinary build script hit the sandbox’s Swift macro subprocess restriction; the successful retry used `OTHER_SWIFT_FLAGS='$(inherited) -disable-sandbox'`, with derived data at `app/one/build/dd`. The session sandbox stayed active.
- iPhone: the requested signed build is blocked by inaccessible Xcode account credentials and provisioning profiles. An unsigned retry is blocked by the asset compiler’s inaccessible simulator runtimes. App and widget Swift sources separately pass iPhone SDK type-checking. A signed iPhone build and App Group access remain unverified.
- Swift tests: all six pass, including a regression test that first failed when snapshots received the entire queue instead of Triage’s picks. Compiler caches and `TMPDIR` were placed under `app/one/build`; the default sandbox temporary location caused the existing cache test to read an empty file.
- Snapshot, rotation and link-parser checks pass; rendered layouts were inspected. Live home-screen placement, server fetching from WidgetKit and tap-to-open behavior were not tested.

Build logs are retained in `app/one/build/widget-validation/`. `build-mac.sh --build-only` builds without installing outside the worktree. No commits, pushes, merges, installs or Apple account changes were made. The optional lock-screen widget was skipped.
