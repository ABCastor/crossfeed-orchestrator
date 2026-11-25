#!/usr/bin/env bash
# The macOS tray installer: first install, upgrade with a backup, failed upgrade rolled back, explicit
# roll-back. A fake launchctl and a throw-away HOME stand in for the machine, so no real agent, app or
# login item is touched. macOS only (codesign, ditto and plutil are macOS tools).
set -uo pipefail
if [ "$(uname -s)" != Darwin ]; then echo "skip: the tray installer is macOS only"; exit 0; fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/crossfeed-install-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/state" "$TMP/home"
fail() { echo "FAIL: $*" >&2; exit 1; }

cat > "$TMP/bin/launchctl" <<'EOF'
#!/bin/bash
echo "$*" >> "$FAKE_STATE/log"
case "$1" in
  bootstrap) [ -e "$FAKE_STATE/refuse" ] || touch "$FAKE_STATE/alive" ;;
  bootout) rm -f "$FAKE_STATE/alive" ;;
  print) [ -e "$FAKE_STATE/alive" ] && echo "	pid = 4242" ;;
esac
exit 0
EOF
chmod +x "$TMP/bin/launchctl"

mkapp() {  # mkapp NAME MARKER: a signed stand-in bundle whose binary prints MARKER
  local dir="$TMP/$1/Crossfeed.app/Contents"
  mkdir -p "$dir/MacOS"
  printf '#!/bin/sh\necho %s\n' "$2" > "$dir/MacOS/Crossfeed"
  chmod +x "$dir/MacOS/Crossfeed"
  cp "$ROOT/menubar/Info.plist" "$dir/Info.plist"
  /usr/bin/codesign --force --sign - "$TMP/$1/Crossfeed.app" >/dev/null 2>&1 || fail "cannot sign $1"
}
mkapp v1 one; mkapp v2 two; mkapp v3 three
touch "$TMP/fleetctl.py"
PY="$(command -v python3)"
export HOME="$TMP/home" FAKE_STATE="$TMP/state" LAUNCHCTL="$TMP/bin/launchctl" CROSSFEED_TRAY_LABEL=org.crossfeed.tray.test
INSTALL="$ROOT/menubar/install-macos.sh"
APP="$HOME/Applications/Crossfeed.app"
AGENT="$HOME/Library/LaunchAgents/$CROSSFEED_TRAY_LABEL.plist"
installed() { "$APP/Contents/MacOS/Crossfeed"; }

# 1. first install
bash "$INSTALL" "$TMP/v1/Crossfeed.app" "$TMP/fleetctl.py" "$PY" >"$TMP/out1" 2>&1 || { cat "$TMP/out1"; fail "first install"; }
[ "$(installed)" = one ] || fail "first install did not put v1 in place"
/usr/bin/plutil -extract ProgramArguments.3 raw "$AGENT" | grep -qx -- "-fleetctl" || fail "plist arguments"
/usr/bin/plutil -extract ProgramArguments.4 raw "$AGENT" | grep -qx -- "$TMP/fleetctl.py" || fail "plist fleetctl path"
[ ! -d "$HOME/.local/state/crossfeed-tray-backup" ] || fail "a first install made a backup"
[ -e "$FAKE_STATE/alive" ] || fail "agent not started"

# 2. upgrade keeps a backup of the old app and plist and says how to undo
bash "$INSTALL" "$TMP/v2/Crossfeed.app" "$TMP/fleetctl.py" "$PY" >"$TMP/out2" 2>&1 || { cat "$TMP/out2"; fail "upgrade"; }
[ "$(installed)" = two ] || fail "upgrade did not put v2 in place"
BACKUP="$(find "$HOME/.local/state/crossfeed-tray-backup" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -n "$BACKUP" ] || fail "no backup directory"
[ -n "$(find "$HOME/.local/state/crossfeed-tray-backup" -mindepth 1 -maxdepth 1 -type d | sed -n 2p)" ] && fail "one upgrade made two backups"
[ "$("$BACKUP/Crossfeed.app/Contents/MacOS/Crossfeed")" = one ] || fail "backup is not the old app"
[ -f "$BACKUP/$CROSSFEED_TRAY_LABEL.plist" ] || fail "backup lacks the old plist"
grep -q -- "--rollback $BACKUP" "$TMP/out2" || fail "the undo command is not printed"
grep -q "^bootout" "$FAKE_STATE/log" || fail "the old agent was not stopped before the swap"

# 3. an upgrade whose agent never starts is rolled back and fails
touch "$FAKE_STATE/refuse"
if bash "$INSTALL" "$TMP/v3/Crossfeed.app" "$TMP/fleetctl.py" "$PY" >"$TMP/out3" 2>&1; then fail "a dead upgrade reported success"; fi
rm -f "$FAKE_STATE/refuse"
grep -q "previous version is back" "$TMP/out3" || fail "no roll-back message"
[ "$(find "$HOME/.local/state/crossfeed-tray-backup" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" = 2 ] || fail "the failed upgrade kept no backup of its own"
[ "$(installed)" = two ] || fail "the roll-back did not restore v2"

# 4. explicit roll-back to the first backup
bash "$INSTALL" --rollback "$BACKUP" >"$TMP/out4" 2>&1 || { cat "$TMP/out4"; fail "--rollback"; }
[ "$(installed)" = one ] || fail "--rollback did not restore v1"

# 5. refuses a bundle whose signature does not verify
printf 'tampered\n' >> "$TMP/v2/Crossfeed.app/Contents/MacOS/Crossfeed"
if bash "$INSTALL" "$TMP/v2/Crossfeed.app" "$TMP/fleetctl.py" "$PY" >"$TMP/out5" 2>&1; then fail "installed a bundle with a broken signature"; fi
[ "$(installed)" = one ] || fail "a refused install changed the installed app"
echo "ok: install, upgrade with backup, failed-upgrade roll-back, --rollback, signature refusal"
