#!/usr/bin/env bash
# Install or upgrade the Crossfeed menu bar app on the operator's Mac. Run explicitly, never as root.
#
#   bash menubar/install-macos.sh /path/to/Crossfeed.app /path/to/fleetctl.py /path/to/python3
#   bash menubar/install-macos.sh --rollback /path/to/backup-directory
#
# A first install copies the app to ~/Applications and loads the LaunchAgent org.crossfeed.tray.
# An upgrade first backs up the installed app and its LaunchAgent plist to
# ~/.local/state/crossfeed-tray-backup/<time>/ (moved there, nothing is deleted), stops the agent by
# label, installs the new app and plist, starts it and checks that it is running; if it is not, it puts
# the backup back and says so. The roll-back
# command is printed at the end.
# CROSSFEED_TRAY_LABEL and LAUNCHCTL are test hooks: the tests give a fake launchctl and a label of their
# own, so they never touch the real agent.
set -euo pipefail
label=${CROSSFEED_TRAY_LABEL:-org.crossfeed.tray}
launchctl=${LAUNCHCTL:-/bin/launchctl}
app="$HOME/Applications/Crossfeed.app"
agent="$HOME/Library/LaunchAgents/$label.plist"
backups="$HOME/.local/state/crossfeed-tray-backup"
domain="gui/$UID"
usage() {
  echo 'Usage: bash menubar/install-macos.sh /path/to/Crossfeed.app /path/to/fleetctl.py /path/to/python3' >&2
  echo '       bash menubar/install-macos.sh --rollback /path/to/backup-directory' >&2
  exit 2
}
[[ $(uname -s) == Darwin && $EUID != 0 ]] || usage

stop_agent() { "$launchctl" bootout "$domain/$label" >/dev/null 2>&1 || true; }
start_agent() { "$launchctl" bootstrap "$domain" "$agent"; }
running() {  # up to ~6 s for the agent's process to appear
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if "$launchctl" print "$domain/$label" 2>/dev/null | grep -qE '^\s*pid = [0-9]+'; then return 0; fi
    sleep 0.5
  done
  return 1
}

restore() {  # restore "$1" (a backup directory): old app and old plist back in place, agent restarted
  local from=$1
  stop_agent
  # Nothing is deleted: the version being replaced moves into the backup directory.
  [[ ! -e $app ]] || mv -- "$app" "$from/replaced-$(date +%H%M%S).app"
  [[ ! -e $agent ]] || mv -- "$agent" "$from/replaced-$(date +%H%M%S).plist"
  if [[ -d $from/Crossfeed.app ]]; then /usr/bin/ditto "$from/Crossfeed.app" "$app"; fi
  if [[ -f $from/$label.plist ]]; then
    cp -p -- "$from/$label.plist" "$agent"
    start_agent
  fi
}

if [[ ${1:-} == --rollback ]]; then
  [[ $# == 2 && -d $2 && $2 == /* ]] || usage
  restore "$2"
  echo "Restored the Crossfeed app and LaunchAgent from $2."
  exit 0
fi

[[ $# == 3 && $1 == /* && $2 == /* && $3 == /* ]] || usage
src=$1 fleet=$2 python=$3
[[ -x "$src/Contents/MacOS/Crossfeed" && -f "$fleet" && -x "$python" ]] || { echo 'Missing Crossfeed, fleetctl or Python.' >&2; exit 2; }
/usr/bin/codesign --verify --strict "$src"
[[ ! -L $app && ! -L $agent ]] || { echo 'The installed app or its LaunchAgent is a symlink; remove it by hand first.' >&2; exit 2; }

mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents"
backup=
if [[ -e $app || -e $agent ]]; then
  mkdir -p -m 700 "$backups"
  backup=$(mktemp -d "$backups/$(date +%Y%m%d-%H%M%S).XXXX")
  stop_agent
  [[ ! -e $app ]] || mv -- "$app" "$backup/Crossfeed.app"
  [[ ! -e $agent ]] || mv -- "$agent" "$backup/$label.plist"
fi

/usr/bin/ditto "$src" "$app"
"$python" - "$agent" "$label" "$app/Contents/MacOS/Crossfeed" "$python" "$fleet" <<'PY'
import os
import plistlib
import sys
path, label, binary, python, fleet = sys.argv[1:]
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
    plistlib.dump({'Label': label, 'ProgramArguments': [binary, '-python', python, '-fleetctl', fleet],
                   'RunAtLoad': True}, handle)
PY
/usr/bin/plutil -lint "$agent" >/dev/null
start_agent
if running; then
  echo 'Crossfeed is running and starts at login.'
  [[ -z $backup ]] || echo "The previous version is saved in $backup. To undo: bash $0 --rollback $backup"
  exit 0
fi
if [[ -n $backup ]]; then
  restore "$backup"
  echo "The new Crossfeed did not start; the previous version is back (backup kept in $backup)." >&2
else
  stop_agent
  echo 'The new Crossfeed did not start. Its files are installed; check the agent with: launchctl print gui/$UID/'"$label" >&2
fi
exit 1
