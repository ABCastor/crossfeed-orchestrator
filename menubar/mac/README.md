# Crossfeed menu bar for macOS

Crossfeed shows a status item with the console's palette and type. It reads `fleetctl glance --json --no-refresh` (schema 1) and lets the operator save spend levels through fleetctl. The app does not read provider logs or credentials or call provider endpoints. Linux keeps the Go AppIndicator tray in `menubar/main.go`.

```
bash menubar/build.sh /absolute/build-dir      # Crossfeed.app, ad hoc signed (needs the Swift toolchain, no Xcode project)
swift test                                     # in this directory: decoding and presenter tests
build-dir/Crossfeed.app/Contents/MacOS/Crossfeed -python /usr/bin/python3 -fleetctl /absolute/path/fleetctl.py
```

## What is on screen

| CodexBar region | Crossfeed |
|---|---|
| Menu bar icon with usage | The shown provider's logo plus the window and percent (`[Claude logo] Week 36%`); terracotta when that pool is in deficit. The optional two tanks (the Crossfeed mark) are that provider's 5-hour window on the left and its weekly window on the right, an empty outline when a window is not running. The gear in the card header opens **Menu bar** settings (see below) |
| Provider switcher: icon over title, filled when selected, mini usage bar | Claude, Codex, Copilot, AGY (Google AI Pro), OpenCode, Other (pools with no percentage window); bundled provider logos; opens on the pool in deepest deficit and remembers the last tab and drag order |
| Card header: name, "Updated …", plan | Pool label, "Updated … ago" from the reading's age, the router's state (Abundant, Conserve …) |
| Labelled usage rows: title, bar, "% used", "Resets in …", pace line | Same, one row per window; the countdown is computed from `reset_at` each time the menu is drawn; the pace line is fleetctl's own words |
| Pace stripe on the bar | Ported: a stripe at the point an even burn would have reached. Added: in deficit the fill is teal up to the stripe and terracotta beyond it; in surplus the stretch between fill and stripe is olive |
| Extra usage card | **Your share**: the operator's part of this month's subscriptions |
| Cost card | **Subscriptions**: this month's total per currency and how many plans have no price yet |
| Add account, Usage Dashboard, Status Page | **Open console**; a refresh button in the card header keeps the menu open (no accounts exist in the app by design) |
| Credits, Cost, Status Page | Provider credits and dates, usage cost estimate and tokens for today and 30 days, provider status link; OpenRouter's model spend is under its section in Other |
| Settings, About, Quit | Spend policy dial, About Crossfeed (console `#about`), Quit Crossfeed |

Google AI Pro uses the same usage rows as Claude: Gemini weekly and 5-hour, Claude/GPT weekly, and Weekly · Argon only each have their own reset line. The dial controls the whole subscription and has no selected stop while the pool levels differ (Mixed). The menu reports missing windows as unread rather than zero. Where model names are listed, archived models are omitted and each family stays together, with the highest costs first.

**Spend policy**: Off · Low · Normal · High · Max. Max writes the existing `forced` key and ignores quota estimates, not real caps. The rail, knob and attribution are terracotta for the operator and teal for an agent; a dial with no recorded change is neutral. The selected value is tentative until `fleetctl level TARGET LEVEL --owner --reason "Crossfeed menu bar"` commits the locked, audited change. A refusal restores the last selection and shows a reason. A successful write reads attribution back from glance. Fixture mode never saves.

Provider extras are shown only when fleetctl supplies the values; unknown credits, renewal dates and usage costs are never shown as zero. Open console and About use one menu-managed `fleetctl console --no-open` service; the signed localhost link stays in memory until handed to the browser. The menu app stops the service it started on quit.

## Menu bar settings

The gear in the card header opens a pane with a live preview and these choices, saved in app preferences (`crossfeed.bar.*`, tolerant of values it does not know):

| Choice | Options |
|---|---|
| Show | Most pressed (default) or one provider that has a reading; a provider with none falls back to the most pressed and the text names what it shows |
| Window | Tightest (default), 5h, Week, Month |
| Count | Used (default) or Left (`Gemini 5h 41% left`; the tank fills with what remains) |
| Icon | The provider's logo (default), the two tanks (left: 5-hour or session, right: weekly; empty outline when not running), or off |
| Text | `5h 59%` (default), `Gemini 5h 59%`, `59%`, or off. With the icon off too, the tanks come back: the item is never empty |
| Add | Nothing, reset (`· 2h 13m`), pace (`· 46% over`) or today's estimated cost (`· US$12.02 today`); a value nobody reports prints nothing |
| Resets | Countdown (default) or the clock time in the viewer's time zone, in the card and after the number |
| Notify | Off by default; two switches. *Running low*: once when a window reaches 90% used and once when it is used up. *Reset*: once when a window that was over 50% used starts over. Turning one on asks macOS for permission; what was told is saved per window, so a relaunch repeats nothing, and the first reading of a window is only a baseline |
| Refresh | 1, 5 (default) or 15 minutes in the background; opening the menu reads again when the last reading is over 30 s old and updates in place |

The VoiceOver label says the whole sentence ("Gemini 5-hour: 59% used · Resets in 2h 13m", or "Codex used up · resets Sun 29 Nov 12:04"). The status item has no hover tooltip: a tooltip window can outlive a repaint and stay on screen. A pool whose models are all archived or withdrawn (`retired_on`) never leads the bar. Count applies to the bar item only; the card always reads in used terms because its pace stripe does. The status item has a stable autosave name and is restored after a display change, a new Space or wake.

## Layout

- `Sources/CrossfeedCore`: schema 1 decoding and the presenter (every string, bar position and colour decision), Foundation only.
- `Sources/CrossfeedUI`: the SwiftUI views and the menu bar icon. `UsageProgressBar.swift` is CodexBar's, adapted.
- `Sources/Crossfeed`: the app (status item, NSMenu, one fleetctl call per 5 minutes by default, on open and from the refresh button).
- `Sources/CrossfeedSnapshot`: draws the real menu view into a PNG with `NSHostingView`, offscreen: `CrossfeedSnapshot --fixture Fixtures/glance-today-priced.json --now 2026-11-25T09:53:57Z --fonts Resources/Fonts --tabs claude,codex --side-by-side codexbar.png --out out.png`.
- `Fixtures/`: glance JSON from a no-refresh capture. `glance-round6.json` adds synthetic provider extras, model cost and attribution samples to illustrate the round-6 contract; those fields are not live account data.
- `Resources/ProviderLogos/`: seven real provider marks from Lobe Icons (MIT; see THIRD_PARTY_NOTICES.md).
- `Resources/Fonts`: static instances of the console's Commissioner and Martian Mono (OFL), made with fontTools' `instancer` from `scripts/console-assets/fonts/*.woff2`.

## Menu behaviour

The panel has one height for every provider tab and the settings pane (the tallest of them, held to the screen height minus 150 pt; "Other" scrolls instead of setting it) and the content scrolls inside. A tab switch is a short crossfade with a few points of slide, none under Reduce Motion. The status item is repainted only when its content changes and never while the menu is open or a click is landing. The Claude card lists every window the glance has, one line each: a 5-hour window that is not running (`active: false`) shows its note in place of a bar, and extra usage shows what is left with its renewal or expiry on the right.

The card is one hosted SwiftUI view; the three footer actions (Open console, About, Quit) are native menu items with system hover, keyboard navigation and dismissal. The hosting view routes tile drag and drop, dial clicks and status links using the frames SwiftUI reports. Reorder by dragging a tile onto another tile (or use the tile's accessibility actions); the app saves the order in preferences without changing the selected tab. `Crossfeed -selftest -fixture FILE -tab agy` builds the menu without a status item and checks the controls it draws.
