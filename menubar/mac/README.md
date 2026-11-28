# Crossfeed menu bar for macOS

Crossfeed shows a status item with the console's palette and type. It reads `fleetctl glance --json` (schema 1; the stored reading with `--no-refresh`, then the refreshed one) and lets the operator save spend levels through fleetctl. The app does not read provider logs or credentials or call provider endpoints. Linux keeps the Go AppIndicator tray in `menubar/main.go`.

```
bash menubar/build.sh /absolute/build-dir      # Crossfeed.app, ad hoc signed (needs the Swift toolchain, no Xcode project)
swift test                                     # in this directory: decoding and presenter tests
build-dir/Crossfeed.app/Contents/MacOS/Crossfeed -python /usr/bin/python3 -fleetctl /absolute/path/fleetctl.py
```

## Round 11: B+ Tiles

The menu is a strip of tabs over a sunk panel; every tab is a header and rounded tiles (design: `design-r11` B+ Tiles, the tokens copied from its `theme.js`). Colour, size and motion are not tuned per app.

| Part | What it does |
|---|---|
| Overview | The alert line (what runs out first, or "Nothing runs out before it refills"; a click opens that provider), one tile per provider with its tightest window and the deficit or surplus word (an API provider shows its spend against its budget), a held-resets count (🎟️ 2), the subscriptions with ½ for a split plan and "You pay a month ≈ $X" from `subscription_spend.usd.operator`, tokens today, **Status** (the incidents the providers report; there while there is one, or pinned), then the links |
| Provider tab | Header (name, plan from the overlay plan name: "Claude Ultra" reads "Ultra"), Updated, then **Limits**, **Resets**, **Tokens** + **Spend policy** (half tiles, side by side), **Credits**, **Status**, **Links** |
| API tab | OpenRouter, Gemini API and Crossfeed Chat each have a tab (ids `openrouter-free`, `gemini-metered`, `chatgpt-work`; any other pool that is not read by percent and carries an `api` block gets one too). Tiles: **Spend**, **Balance** + **Activity** (half tiles), **Models**, **Status**, **Links**. See "API tabs" below |
| Pace bar | Teal fill; terracotta diagonal stripes from the pace tick to the fill's end (deficit); green diagonal stripes, same angle and period, from the fill's end to the tick (surplus); flush, full height; a 2 pt tick |
| Limits | "58% used" (or "42% left"), the window named top right ("Weekly · resets Tue 14:00"), the bar, "▲ 17% deficit" and "Runs out in 2d 1h". A 5-hour row exists only when the provider reports one |
| Resets | Titled "Resets"; each row 🎟️ name · Next · until 10 Dec · **Use**. Use asks ("Use the weekly reset? This can't be undone." and what it clears), Use runs `fleetctl use-reset claude --key KEY --yes --json` once, no retry, and the row says "Used. Left: n" or the plain failure. Only Claude's resets can be used |
| Tokens | Today's number and a sparkline from the pool's `tokens`. What the chart plots and over how many days (7, 14 or 30) is the operator's choice (right-click the tile, or Settings, Tiles): Tokens, Cost or Requests where the pool has them |
| Status | The provider's incident in words ("Minor issue", the text), or "No incidents", or "Status not reported" when no feed exists. Movable and hideable like every tile; last before Links by default. A click opens the provider's status page |
| Links | "Usage and cost ↗", "Status ↗" (provider tabs), "Billing ↗" (API tabs, when a page is known). The footer's Open console is the one way to the console |
| Clicks | Every tile answers a click: Tokens and Activity, Spend, Models and Limits rows go to the console's usage page at that provider; Balance to the billing page when known; Status to the provider's status page; an Overview provider tile to its tab; a plugin tile to its first link |
| Plugin tiles | `plugins` in the glance, drawn with the same parts (bar = the pace bar, value, sparkline, text, https link). Shown only where you add them (Settings, Tiles, Add tile). No plugin code runs in the menu |

**Drag**: press a tile and move past 7 pt; it lifts (opaque, scale 1.02, a shadow), every legal order is a state, the others blend between the states nearest the pointer, release settles on the nearest in 200 ms (150 ms under Reduce Motion), Escape puts it back. Two half tiles side by side move as a pair; a half tile alone spans the row. Overview's provider tiles reorder among themselves (that is the tab order); the other tiles move around the group. Tabs drag in the strip. The keyboard and VoiceOver path is "Move up" and "Move down" (context menu and accessibility actions).

**Settings** (the sliders icon): *General* (Appearance: System, Light or Dark; Show Used or Left; Open on: Last used, Overview or a tab; Refresh when opened, and every Off, 1, 5 or 15 minutes; Notify; the monthly budget of each API pool that reports money), *Tiles* (per tab, the tiles that are on with a grip and a switch; Add tile lists what is off and every plugin tile; the chart's metric and range), *Tabs* (show, hide, order), *Menu bar* (the item in the bar, with a live preview of it).

**Opening**: the menu opens on the tab Settings, General, Open on names. Last used (the default) is the tab left open last time, kept in `last_tab` and written on every tab switch, so it survives a restart. A tab that is hidden or gone falls back to Overview. **Refreshing**: with "When opened" on (the default), a reading older than a minute is read again in the background when the menu opens: the numbers on screen are the cached ones at once, the refresh icon turns while fleetctl reads, and the new numbers replace them in place. Nothing waits for it. The interval timer does the same every 5 minutes (Off, 1, 5 or 15).

**Appearance** pins the menu, its Settings and the rows under it to light or dark; System follows the Mac.

**panel.json** is the one preferences store for Show, the tab order, hidden tabs, every tab's tiles, and the choices both the Mac and the web panel read: `appearance`, `menubar.icon` (`crossfeed`, `provider` or `none`), `refresh` (`on_open`, `every_minutes`), `open_on`, `last_tab`, `budgets` (pool to USD a month) and `tile_options` (`"<tab>/<tile>": {"metric", "days"}`). The file is `~/.config/orchestrator/panel.json` (`XDG_CONFIG_HOME`, or `FLEET_PANEL_PREFS`), mode 600, atomic replace, unknown keys kept, also those inside `menubar` and `refresh`. The menu reads it again each time it opens, so a change made in the web panel shows. The first run moves the old UserDefaults tab order, hidden tabs, Claude block order and refresh interval into it and reads only the file after that. The Menu bar page's other choices (provider, window, count, text, add, reset style) and the notification switches stay in app preferences.

## API tabs

A pool billed per token, by prepaid credit or through a relay carries an `api` block in the glance (all fields optional): `balance_usd`, `credits_usd`, `key_limit_usd`, `key_remaining_usd`, `spend` (`today_usd`, `week_usd`, `month_usd`), `budget` (`monthly_usd`, `source`), `days` (up to 30, oldest first: `date`, `usd`, `requests`, `tokens`), `top_models`, `source` (`openrouter-api`, `admin-cost-api` or `local-meter`) and `observed_at`.

| Tile | What it draws |
|---|---|
| Spend | This month's spend with the pace bar against the monthly budget (the operator's, set in Settings, General; else the reading's): deficit and surplus stripes, "Runs out in 2d 9h", then Today, Week, Month and where the figures come from ("Measured by Crossfeed" for the local meter). Without a budget the bar is the key limit, else the prepaid credit; without those, figures only. A relay that reports runs and no money counts runs |
| Balance | Credit left and key limit left, each with a bar. A click opens the billing page when one is known |
| Models | The top four models by spend (by runs when no money is reported), with a bar and the request count |
| Activity | Today's figure and the sparkline: Cost by default, or Tokens or Requests, over 7, 14 or 30 days |
| Status | As on every provider tab |

A budget is the operator's and only shown here; nothing stops at it. With ten or more tabs the strip keeps only the selected tab's name under its icon; the full names stay in the accessibility labels.

Review images: `CrossfeedSnapshot --fixture Fixtures/glance-round12.json --now 2026-11-27T10:02:00Z --zone Europe/Rome --fonts Resources/Fonts --tab claude --bare --out claude.png` (also `--appearance dark`, `--mode left`, `--confirm`, `--middrag --drag-index 2 --drag-dy -80`, `--settings --settings-page 0|1|2|3 --settings-tab ID [--add-open]` (0 General, 1 Tiles, 2 Tabs, 3 Menu bar), `--icon crossfeed|provider|none`, `--logo-strips-out PNG` (every logo in colour and in one colour on a light and a dark bar), `--panel panel.json`, `--side-by-side mockup.png`). `Fixtures/glance-round11*.json` are the design's 27 Nov live-reading sample with the `limit_resets` keys, `tokens`, `usd`, and one plugin tile in the `-plugins` file. `Fixtures/glance-round12c.json` is that sample with Claude's weekly at 96% in deficit, `resets_held: 1` on the weekly and Fable windows and `reset_capacity_percent: 100` on the pool (`glance-round12c-held-only.json` also removes every other provider's pace, so the Overview line is about the held reset). Round 12c: a window with a held reset never says "runs out" (tiles, Overview line, notices, bar tooltip): it says "🎟️ 1 held" and a notice offers Use. The header carries the Castor theme switch beside refresh and settings (`--theme system|light|dark` draws its icon state); Settings > General has Resets: Ask first / Use when used up (panel.json `auto_use_resets`; the spending is fleetctl's). `Fixtures/glance-round12.json` is the same sample with an `api` block on OpenRouter (the contract's example), Gemini API (the local meter) and Crossfeed Chat (run counts only); those blocks are written to the contract's shape, not read from an account.

The type is the system face, as the design's `-apple-system`; the bundled Commissioner and Martian Mono files are still registered for the Menu bar page's older rows. Sections below describe the first menu; where they differ (the card layout, the dial, the status link) this section is current.

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

Settings, Menu bar has a live preview of the item as the bar draws it, then these choices, saved in app preferences (`crossfeed.bar.*`, tolerant of values it does not know; the icon also in panel.json):

| Choice | Options |
|---|---|
| Show | Most pressed (default) or one provider that has a reading; a provider with none falls back to the most pressed and the text names what it shows |
| Window | Tightest (default), 5h, Week, Month |
| Count | Used (default) or Left (`Gemini 5h 41% left`; the tank fills with what remains) |
| Icon | Provider logo (default): the logo of the provider the text is about, the tightest one; Crossfeed: the two tanks (left: 5-hour or session, right: weekly; empty outline when not running); or None. Colour marks (Claude, Gemini, Antigravity) keep their colour, which reads well at 15 pt on a light and a dark bar; the marks that are one colour already (OpenAI, Copilot, OpenCode, OpenRouter) take the bar's ink. Saved in panel.json as `menubar.icon` |
| Text | `5h 59%` (default), `Gemini 5h 59%`, `59%`, or off. With the icon off too, the tanks come back: the item is never empty |
| Add | Nothing, reset (`· 2h 13m`), pace (`· 46% over`) or today's estimated cost (`· US$12.02 today`); a value nobody reports prints nothing |
| Resets | Countdown (default) or the clock time in the viewer's time zone, after the number. The row shows only while Add is Reset, which is the one place it changes |
| Notify | (Settings, General.) Off by default; two switches. *Running low*: once when a window reaches 90% used and once when it is used up. *Reset*: once when a window that was over 50% used starts over. Turning one on asks macOS for permission; what was told is saved per window, so a relaunch repeats nothing, and the first reading of a window is only a baseline |

The VoiceOver label says the whole sentence ("Gemini 5-hour: 59% used · Resets in 2h 13m", or "Codex used up · resets Sun 29 Nov 12:04"). The status item has no hover tooltip: a tooltip window can outlive a repaint and stay on screen. A pool whose models are all archived or withdrawn (`retired_on`) never leads the bar. Count applies to the bar item only; the card always reads in used terms because its pace stripe does. The status item has a stable autosave name and is restored after a display change, a new Space or wake.

## Layout

- `Sources/CrossfeedCore`: schema 1 decoding and the presenter (every string, bar position and colour decision), Foundation only.
- `Sources/CrossfeedUI`: the SwiftUI views and the menu bar icon. `UsageProgressBar.swift` is CodexBar's, adapted.
- `Sources/Crossfeed`: the app (status item, NSMenu, one fleetctl call per interval, on open when the reading is over a minute old, and from the refresh button; each one shows the stored reading first, then lets fleetctl read the providers).
- `Sources/CrossfeedSnapshot`: draws the real menu view into a PNG with `NSHostingView`, offscreen: `CrossfeedSnapshot --fixture Fixtures/glance-today-priced.json --now 2026-11-25T09:53:57Z --fonts Resources/Fonts --tabs claude,codex --side-by-side codexbar.png --out out.png`.
- `Fixtures/`: glance JSON from a no-refresh capture. `glance-round6.json` adds synthetic provider extras, model cost and attribution samples to illustrate the round-6 contract; those fields are not live account data.
- `Resources/ProviderLogos/`: seven real provider marks from Lobe Icons (MIT; see THIRD_PARTY_NOTICES.md).
- `Resources/Fonts`: static instances of the console's Commissioner and Martian Mono (OFL), made with fontTools' `instancer` from `scripts/console-assets/fonts/*.woff2`.

## Menu behaviour

The panel has one height for every provider tab and the settings pane (the tallest of them, held to the screen height minus 150 pt; "Other" scrolls instead of setting it) and the content scrolls inside. A tab switch is a short crossfade with a few points of slide, none under Reduce Motion. The status item is repainted only when its content changes and never while the menu is open or a click is landing. The Claude card lists every window the glance has, one line each: a 5-hour window that is not running (`active: false`) shows its note in place of a bar, and extra usage shows what is left with its renewal or expiry on the right.

The card is one hosted SwiftUI view; the three footer actions (Open console, About, Quit) are native menu items with system hover, keyboard navigation and dismissal. The hosting view routes tile drag and drop, dial clicks and status links using the frames SwiftUI reports. Reorder by dragging a tile onto another tile (or use the tile's accessibility actions); the app saves the order in preferences without changing the selected tab. `Crossfeed -selftest -fixture FILE -tab agy` builds the menu without a status item and checks the controls it draws: every Settings control is clicked the way the host routes a click, and each choice is read back from a second store on the same file, which is what the next launch reads.
