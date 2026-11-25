#!/usr/bin/env bash
# Run explicitly on the operator's Mac. Never run as root.
set -euo pipefail
if [[ $(uname -s) != Darwin || $# != 3 || $EUID == 0 || $1 != /* || $2 != /* || $3 != /* ]]; then
  echo 'Usage: bash menubar/install-macos.sh /path/to/Crossfeed.app /path/to/fleetctl.py /path/to/python3' >&2; exit 2
fi
src=$1 fleet=$2 python=$3
[[ -x "$src/Contents/MacOS/Crossfeed" && -f "$fleet" && -x "$python" ]] || { echo 'Missing Crossfeed, fleetctl or Python.' >&2; exit 2; }
app="$HOME/Applications/Crossfeed.app"
label=org.crossfeed.tray
agent="$HOME/Library/LaunchAgents/$label.plist"
if [[ -e "$app" || -L "$app" || -e "$agent" || -L "$agent" ]]; then
  echo 'Crossfeed or its LaunchAgent already exists. Stop and back it up before upgrading.' >&2; exit 2
fi
/usr/bin/codesign --verify --strict "$src"
mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents"
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
/usr/bin/plutil -lint "$agent"
/bin/launchctl bootstrap "gui/$UID" "$agent"
echo 'Crossfeed starts at login.'
