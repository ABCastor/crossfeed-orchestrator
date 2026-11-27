# Running Crossfeed Orchestrator on Linux

Crossfeed uses Python 3 and bash, with no Python packages beyond the standard library. On Linux, configure quota readers for the tools installed on that machine.

## What has been run on Linux

Earlier release documentation reports container test runs on Debian 12 with Python 3.9 and 3.13. The current expanded suite has been run locally on macOS; it has not been rerun on Linux for this export. The checks workflow runs it on Linux and macOS after a push. Its fake CLIs exercise stalls, streams, ignored signals and price checks.

Not yet exercised on Linux: the real vendor CLIs signed in to real accounts, the Antigravity terminal bridge with the real `agy`, the console page in a Linux browser, and Alpine or any busybox system (the wrappers read `ps` output, and busybox `ps` differs). Report platform-specific failures with the suite output and tool versions.

## Requirements

- Python 3.9 or newer, bash, jq, and procps (`ps`). Debian and Ubuntu: `sudo apt install python3 jq procps`.
- The CLIs you want to route to, each signed in with its own login (see [roster admission](../skill/references/roster-admission.md)).
- For the test suite only: git, zip and unzip, and Node.js 18 or newer for the console's script test (skipped when `node` is missing).

## Install

```bash
git clone https://github.com/ABCastor/crossfeed-orchestrator ~/crossfeed-orchestrator
cd ~/crossfeed-orchestrator
mkdir -p ~/.config/orchestrator
cp examples/access-overlay.example.json ~/.config/orchestrator/access-overlay.json
python3 scripts/fleetctl.py doctor
```

Paths follow XDG: the overlay (your roster) lives in `~/.config/orchestrator/`, runtime state in `~/.local/state/orchestrator/`. Override them with `ACCESS_OVERLAY` and `FLEET_STATE_DIR`.

`python3 scripts/fleetctl.py console --no-open` prints the console's sign-in link instead of opening a browser, which is what you want over SSH (forward the port first: the console listens on 127.0.0.1 only).

## Quota readings

Without a reading, every pool shows UNKNOWN and routing runs at full quality: nothing breaks, the router is simply blind. `doctor` lists which pools have no source. Give each pool a `quota_refresh` block in the overlay, using whichever source you have.

**Codex, with no extra software.** Codex writes the rate-limit headers it receives into its own session files, and `scripts/oracle-codex-local.py` reads the newest one:

```json
"codex": {
  "quota_refresh": {
    "oracle": "command",
    "command": ["python3", "/path/to/crossfeed-orchestrator/scripts/oracle-codex-local.py"],
    "ttl_s": 900
  }
}
```

The reader is covered by synthetic tests; it has not been checked against a real Linux Codex install for this export. If it returns `{"available": false}`, run any Codex command once so a session file exists.

**Everything else: your own command, file or endpoint.** A `command` oracle runs a program that prints JSON; a `file` oracle reads JSON another process keeps fresh; an `http` oracle fetches it from a URL. The shape:

```json
{"windows": {"weekly": {"used_percent": 42, "reset_at": "2026-10-04T07:00:00Z", "window_minutes": 10080}}}
```

or `{"available": false, "reason": "not logged in"}` when there is nothing to report. `window_minutes` lets the router estimate a burn rate; `will_last_to_reset` and `eta_seconds` are used first when your source can compute them.

**Reading the Mac's numbers on the server.** The Mac serves its saved snapshots at `/crossfeed/readings.json` on the tailnet phone view, and the server adopts a pool's reading when it is newer than its own. What crosses is an allow-list, so the numbers the menu shows arrive with it: windows, reset clocks, `resets` (the glance's `limit_resets`), `tokens` and the account-level extras, and nothing else. Configure the puller with `<state>/readings-source.json` `{"url": "https://<mac>.<tailnet>.ts.net:8444/crossfeed/readings.json"}` or `FLEET_READINGS_URL`. A sleeping Mac leaves this machine's own readers in charge.

**CodexBar, if you accept a third-party reader.** [CodexBar](https://github.com/steipete/CodexBar) publishes its own platform-specific installation instructions. It reads quota through existing sessions or browser cookies; it is not a vendor tool. Crossfeed only reads its output. Pools that declare `"oracle": "codexbar"` show as MISSING in `doctor` until it is on PATH. This optional reader has not been run with Crossfeed on Linux for this export.

## The tray and the panel

Linux gets the same menu the Mac app draws, in two pieces: a tray icon that carries the one-line
title and the way in, and the tiles as a web page the console serves at `/panel`.

**The tray** (Go, AppIndicator through `fyne.io/systray`):

```bash
bash menubar/build.sh ~/crossfeed-build          # prints the built binary's path
~/crossfeed-build/Crossfeed -fleetctl "$PWD/scripts/fleetctl.py"
```

It needs a desktop session: a D-Bus session bus and a panel that speaks StatusNotifierItem (GNOME
needs the AppIndicator extension, KDE and XFCE have it). Add the command to the session's autostart
to keep it there. Building needs Go 1.23 or newer and the C headers `fyne.io/systray` uses:
`sudo apt install golang-go libgtk-3-dev libayatana-appindicator3-dev` on Debian and Ubuntu.

The tray starts its own console (`fleetctl.py console --no-open`), reads the sign-in link that
console prints, and keeps it in memory: never on disk, never in the menu. Its title is the same
short line the Mac menu bar shows (`Claude 12% · Codex 40%`), its menu has one line per provider
with the tightest window, and then:

| Item | Opens |
|---|---|
| Open panel | `<console>/login?key=…&next=/panel` in a `--app=` window of chromium or google-chrome when one is on PATH, else `xdg-open` |
| Usage and cost | `/usage`, signed in the same way |
| Settings | `/settings` |
| Quit | stops the tray and the console it started |

**The panel** is the console's own page, so it needs nothing extra: `fleetctl.py console` prints a
sign-in link, and `/panel` (or the link with `next=/panel`) draws the tiles. It is 310 px wide and
fine on a phone, follows `prefers-color-scheme`, loads no font or script from anywhere but the
console itself, and writes its tile order and choices to `~/.config/orchestrator/panel.json`, the
same file the Mac app reads. `fleetctl.py panel get` prints that file with defaults filled;
`fleetctl.py panel set --json '<doc>'` validates and writes it.

Tiles can come from plugins: see [PLUGINS.md](PLUGINS.md).

## Linux coverage

On a headless Debian 12 server (Python 3.12, Node 24, Go 1.23, no desktop session):

- The panel, plugin, console-panel and business suites pass under `python3 -m unittest`, and `node --test tests/panel.test.cjs` passes.
- Headless Chromium renders `/panel` (Overview, Claude, reset confirm, Settings, a drag in progress) at 310 px in light and dark from a real `/panel.json`.
- The tray passes `go vet ./...` and `go test ./...`, builds with `menubar/build.sh`, and runs; with no StatusNotifierWatcher on the session bus it logs `failed to register` and keeps running.

Not covered without a desktop: the tray icon in a real panel (GNOME with the AppIndicator extension, KDE, XFCE) and the `--app=` window it opens.

## The daily market refresh

`scripts/market-refresh.py` fetches public model data (prices, context sizes, benchmark priors) that routing uses as a prior. Run it once a day with the examples in `examples/linux/`:

- **systemd:** copy `crossfeed-market-refresh.service` and `.timer` to `~/.config/systemd/user/`, point `ExecStart` at your clone, then `systemctl --user daemon-reload && systemctl --user enable --now crossfeed-market-refresh.timer`.
- **cron:** add the line in `market-refresh.cron` with `crontab -e`.

The refresh also reads each installed tool's model list: `opencode models --standalone`, `agy models` and the Codex CLI's own cache (`~/.codex/models_cache.json`, or `$CODEX_HOME`; `CODEX_MODELS_CACHE` overrides the path). A model that Codex lists (`visibility: list`) and the roster does not yet route enters the candidate-only `codex-catalog` pool, unverified and with no roles; admit it as a Codex model card to route it. A cache older than 14 days counts as unknown and retires nothing. Give the scheduler a `PATH` that reaches these CLIs: a check that stays unknown on two runs in a row (for example "opencode not found on PATH") becomes the report's first line and the job exits non-zero. The consecutive count lives in `market/catalog-unknown-streak.json` under the state directory.

## Running the tests

```bash
bash tests/run-all.sh
```

The runner discovers every suite, prints its verdict and ends with `ALL N SUITES GREEN`, or names failed suites and shows the tail of their output. Runtime depends on the platform.
