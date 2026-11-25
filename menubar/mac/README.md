# Crossfeed menu bar for macOS

A status item whose menu looks and behaves like CodexBar's, restyled with the console's palette and type. It reads `fleetctl glance --json --no-refresh` (schema 1, including `subscription_spend`) and lets the operator save pool spend levels through fleetctl. No provider readers, cookies, tokens or network calls live in the app. Linux keeps the Go AppIndicator tray in `menubar/main.go`.

```
bash menubar/build.sh /absolute/build-dir      # Crossfeed.app, ad hoc signed (needs the Swift toolchain, no Xcode project)
swift test                                     # in this directory: decoding and presenter tests
build-dir/Crossfeed.app/Contents/MacOS/Crossfeed -python /usr/bin/python3 -fleetctl /absolute/path/fleetctl.py
```

## What is on screen

| CodexBar region | Crossfeed |
|---|---|
| Menu bar icon with usage | Two tanks (the Crossfeed mark) filled to the two most pressed pools, plus the leading pool's percentage; terracotta when that pool is in deficit |
| Provider switcher: icon over title, filled when selected, mini usage bar | Claude, Codex, Copilot, AGY (Google AI Pro), OpenCode, Other (pools with no percentage window); bundled provider logos; opens on the pool in deepest deficit and remembers the last tab |
| Card header: name, "Updated …", plan | Pool label, "Updated … ago" from the reading's age, the router's state (Abundant, Conserve …) |
| Labelled usage rows: title, bar, "% used", "Resets in …", pace line | Same, one row per window; the countdown is computed from `reset_at` each time the menu is drawn; the pace line is fleetctl's own words |
| Pace stripe on the bar | Ported: a stripe at the point an even burn would have reached. Added: in deficit the fill is teal up to the stripe and terracotta beyond it; in surplus the stretch between fill and stripe is olive |
| Extra usage card | **Your share**: the operator's part of this month's subscriptions |
| Cost card | **Subscriptions**: this month's total per currency and how many plans have no price yet |
| Add account, Usage Dashboard, Status Page | **Open console**, **Reload view** (no accounts exist in the app by design) |
| Settings, About, Quit | Spend policy per pool, About Crossfeed, Quit Crossfeed |

An unread pool shows "Not read" and fleetctl's reason instead of a bar. Google AI Pro uses the same usage rows as Claude: Gemini weekly and 5-hour share the Gemini pool's policy; Claude/GPT and Argon each have a separate policy, with model names under each allowance. These settings control existing pools, not individual reset clocks, and do not change routing semantics.

**Spend policy**: Off · Low · Normal · High · Ignore estimates. The selected segment and attribution line are terracotta for the operator and teal for an agent. A pool with no recorded change is neutral. The clicked value is tentative until `fleetctl level POOL LEVEL --owner --reason "Crossfeed menu bar"` commits the locked, audited change. The menu reads attribution back via glance (`level_control.actor` and `when`). Refusal restores the last selection and shows a reason. Fixture mode never saves.

## Layout

- `Sources/CrossfeedCore`: schema 1 decoding and the presenter (every string, bar position and colour decision), Foundation only.
- `Sources/CrossfeedUI`: the SwiftUI views and the menu bar icon. `UsageProgressBar.swift` is CodexBar's, adapted.
- `Sources/Crossfeed`: the app (status item, NSMenu, one fleetctl call per minute and on open).
- `Sources/CrossfeedSnapshot`: draws the real menu view into a PNG with `NSHostingView`, offscreen: `CrossfeedSnapshot --fixture Fixtures/glance-today-priced.json --now 2026-11-25T09:53:57Z --fonts Resources/Fonts --tabs claude,codex --side-by-side codexbar.png --out out.png`.
- `Fixtures/`: glance JSON from a no-refresh capture. `glance-today.json` adds example attribution times and public roster model names for the settings preview, not private audit history.
- `Resources/ProviderLogos/`: seven real provider marks from Lobe Icons (MIT; see THIRD_PARTY_NOTICES.md).
- `Resources/Fonts`: static instances of the console's Commissioner and Martian Mono (OFL), made with fontTools' `instancer` from `scripts/console-assets/fonts/*.woff2`.

## Menu behaviour

The card is one hosted SwiftUI view; the four actions are native menu items, as in CodexBar, so they keep the system's hover, keyboard and dismissal. The menu's tracking loop hands clicks to the hosting view, so tile and policy segment clicks are routed with the frames SwiftUI reports (`MenuHostingView` in `Sources/Crossfeed/main.swift`). `Crossfeed -selftest -fixture FILE` builds the menu without a status item and prints the result.
