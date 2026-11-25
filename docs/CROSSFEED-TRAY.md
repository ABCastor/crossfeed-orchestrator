# Crossfeed tray

Crossfeed reads the existing quota cache once a minute. It does not poll providers or start their tools. On macOS, provider tiles switch pools and each window gets its own bar with a pace marker; on Linux, the tightest window is on each pool's line and the submenu holds the others. The full console controls routing and billing: **Settings → Subscriptions**.

## Glance JSON — schema 1

`python3 scripts/fleetctl.py glance --json --no-refresh` (`--format json` is equivalent) returns `{schema: 1, title, pools, readings, subscription_spend}`. Each `pools` entry has `pool` (stable key), `name` (short label), `measured` (quota reader configured), `used_percent` (integer 0–100 or null), `resets` (relative text or empty), `reset_at` (ISO timestamp or null), `pace` (null or an object with `words`), `limits` (individual windows with the same percentage/clock/pace fields and a `title`), `gauge_error` (null or evidence of a failed read), and `stale_age_s` (null or seconds). A null percentage is not 0% used. Readers tolerate additional fields but reject an unknown schema number.

`subscription_spend` contains `monthly` (one object per currency: `{currency, total, operator}`), `unpriced`, `unsplit`, and `unavailable`. Amounts are decimal strings; `operator: null` means some priced plans in that currency have no share. Totals exclude unpriced plans, don't convert currencies and divide annual prices by twelve. No payer labels leave the signed-in console. With `--no-refresh` the tray invokes no providers.

## Private subscriptions

The signed-in console creates `subscriptions.json` next to the access overlay (default `~/.config/orchestrator/subscriptions.json`, mode 600) on the first Save. It learns plan names from the overlay, not the price anyone pays. Enter a plan's price, currency, period, renewal and your percent share. Enter 100 if you pay it all; leave a field blank if unknown. The other payer's label is optional and the console shows the remaining share. Antigravity's separate quotas count as one bill. Retired subscriptions do not contribute to the total.

Weekly service-value estimates multiply each pool's declared weekly USD-equivalent capacity by a fresh, unscoped weekly usage percentage. They are not charges; the console shows the whole plan's recorded monthly price alongside each pool, without dividing it among windows or converting currencies. The ledger cannot change routing, pool settings or paid spending limits.

## Build and run

macOS builds with the Swift toolchain (Command Line Tools or Xcode); Linux builds with Go 1.23+. From the repository, run `bash menubar/build.sh /absolute/build-dir`. This creates `Crossfeed.app` on macOS or a `Crossfeed` binary on Linux. Build assets do not need provider credentials.

- **macOS login:** `bash menubar/install-macos.sh /absolute/build-dir/Crossfeed.app /absolute/path/to/fleetctl.py /absolute/path/to/python3`. This copies to `~/Applications`, registers a per-user LaunchAgent and runs Crossfeed now and at future logins. Use the deployed orchestrator's `fleetctl.py`, not a temporary checkout. The menu opens `~/Applications/Crossfeed Orchestrator.app` when present, otherwise the local console URL.
- **Linux desktop:** `/absolute/build-dir/Crossfeed -fleetctl /absolute/path/to/fleetctl.py`. Requires a D-Bus session and an AppIndicator (StatusNotifierItem) compatible panel. Add that command to the desktop session's autostart if desired. **Open console** uses `xdg-open` at `http://127.0.0.1:8768/`; start the signed console with `fleetctl.py console` and use its sign-in link once.
- **Headless Linux:** use `fleetctl.py glance` and the signed loopback console; no tray is required.

The tray only displays cached state; keep the orchestrator's normal quota refresh job running. Development check: `cd menubar && go test ./...`.

## From a phone or iPad

The signed local console shows the read-only view at `/phone`. To reach only this view from your tailnet, put `{"operator_login":"<your exact Tailscale login>"}` in a private mode-600 `~/.config/orchestrator/phone.json`. Run `bash scripts/serve-phone.sh "$HOME/.config/orchestrator/phone.json"` in a terminal on the orchestrator host. Open the HTTPS `/crossfeed` URL printed by Tailscale Serve on the phone or iPad, signed into the same tailnet and login. The script requires an idle Tailscale HTTPS port (8443, or `CROSSFEED_PHONE_PORT`); on macOS, where the Tailscale app cannot reach a user's Unix socket, set `CROSSFEED_PHONE_LOOPBACK=<port>` to proxy to 127.0.0.1 instead, supports no public Funnel, and removes the foreground route when it stops.

This route goes to a private Unix socket, not to the console's TCP port. Tailscale Serve strips a client-supplied identity header and forwards the real `Tailscale-User-Login`; the app accepts exactly one header equal to the configured operator login. Keep the socket private to this local user and the Tailscale service. Missing identities (including tagged clients with no user identity) and other logins get no data. There is no write endpoint, provider refresh, billing edit or signed-console session on the tailnet view.

On macOS, `menubar/install-macos.sh` also upgrades an installed Crossfeed in place, keeps a backup, and prints the `--rollback` command.
