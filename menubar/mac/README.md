# Crossfeed menu bar for macOS

A status item whose menu looks and behaves like CodexBar's, restyled with the console's palette and type. It shows what `fleetctl glance --json --no-refresh` says (schema 1, including `subscription_spend`) and nothing else: no provider readers, cookies, tokens or network calls. Linux keeps the Go AppIndicator tray in `menubar/main.go`.

```
bash menubar/build.sh /absolute/build-dir      # Crossfeed.app, ad hoc signed (needs the Swift toolchain, no Xcode project)
swift test                                     # in this directory: decoding and presenter tests
build-dir/Crossfeed.app/Contents/MacOS/Crossfeed -python /usr/bin/python3 -fleetctl /absolute/path/fleetctl.py
```

## What is on screen

| CodexBar region | Crossfeed |
|---|---|
| Menu bar icon with usage | Two tanks (the Crossfeed mark) filled to the two most pressed pools, plus the leading pool's percentage; terracotta when that pool is in deficit |
| Provider switcher: icon over title, filled when selected, mini usage bar | Claude, Codex, Copilot, AGY (all Antigravity pools), OpenCode, Other (pools with no percentage window); SF Symbols instead of vendor logos; opens on the pool in deepest deficit and remembers the last tab |
| Card header: name, "Updated …", plan | Pool label, "Updated … ago" from the reading's age, the router's state (Abundant, Conserve …) |
| Labelled usage rows: title, bar, "% used", "Resets in …", pace line | Same, one row per window; the countdown is computed from `reset_at` each time the menu is drawn; the pace line is fleetctl's own words |
| Pace stripe on the bar | Ported: a stripe at the point an even burn would have reached. Added: in deficit the fill is teal up to the stripe and terracotta beyond it; in surplus the stretch between fill and stripe is olive |
| Extra usage card | **Your share**: the operator's part of this month's subscriptions |
| Cost card | **Subscriptions**: this month's total per currency and how many plans have no price yet |
| Add account, Usage Dashboard, Status Page | **Open console**, **Reload view** (no accounts exist in the app by design) |
| Settings, About, Quit | About Crossfeed, Quit Crossfeed |

An unread pool (for example an Antigravity pool with no usable server) shows its name, "Not read" and fleetctl's reason instead of a bar; a pool with an old reading and a failed refresh keeps the bar and says why.

## Layout

- `Sources/CrossfeedCore`: schema 1 decoding and the presenter (every string, bar position and colour decision), Foundation only.
- `Sources/CrossfeedUI`: the SwiftUI views and the menu bar icon. `UsageProgressBar.swift` is CodexBar's, adapted.
- `Sources/Crossfeed`: the app (status item, NSMenu, one fleetctl call per minute and on open).
- `Sources/CrossfeedSnapshot`: draws the real menu view into a PNG with `NSHostingView`, offscreen: `CrossfeedSnapshot --fixture Fixtures/glance-today-priced.json --now 2026-11-25T09:53:57Z --fonts Resources/Fonts --tabs claude,codex --side-by-side codexbar.png --out out.png`.
- `Fixtures/`: glance JSON in today's real shape (taken from a no-refresh `fleetctl glance`, quota numbers only).
- `Resources/Fonts`: static instances of the console's Commissioner and Martian Mono (OFL), made with fontTools' `instancer` from `scripts/console-assets/fonts/*.woff2`.

## Menu behaviour

The card is one hosted SwiftUI view; the four actions are native menu items, as in CodexBar, so they keep the system's hover, keyboard and dismissal. The menu's tracking loop hands clicks to the hosting view, so tile clicks are routed with the frames SwiftUI reports (`MenuHostingView` in `Sources/Crossfeed/main.swift`). `Crossfeed -selftest -fixture FILE` builds the menu without a status item and prints the result.
