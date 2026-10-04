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

**CodexBar, if you accept a third-party reader.** [CodexBar](https://github.com/steipete/CodexBar) publishes its own platform-specific installation instructions. It reads quota through existing sessions or browser cookies; it is not a vendor tool. Crossfeed only reads its output. Pools that declare `"oracle": "codexbar"` show as MISSING in `doctor` until it is on PATH. This optional reader has not been run with Crossfeed on Linux for this export.

## The daily market refresh

`scripts/market-refresh.py` fetches public model data (prices, context sizes, benchmark priors) that routing uses as a prior. Run it once a day with the examples in `examples/linux/`:

- **systemd:** copy `crossfeed-market-refresh.service` and `.timer` to `~/.config/systemd/user/`, point `ExecStart` at your clone, then `systemctl --user daemon-reload && systemctl --user enable --now crossfeed-market-refresh.timer`.
- **cron:** add the line in `market-refresh.cron` with `crontab -e`.

## Running the tests

```bash
bash tests/run-all.sh
```

The runner discovers every suite, prints its verdict and ends with `ALL N SUITES GREEN`, or names failed suites and shows the tail of their output. Runtime depends on the platform.
