#!/bin/bash
# <xbar.title>Crossfeed limits</xbar.title>
# <xbar.desc>Every plan's limits and resets, from the orchestrator's own quota readings.</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.environment>[CROSSFEED_GLANCE_LEAD=claude]</swiftbar.environment>
#
# A SwiftBar or xbar plugin (the .5m. in the name is its refresh interval). It prints
# `fleetctl.py glance --format swiftbar`: the same runtime snapshot the console, brief and
# routing read, refreshed by the same TTLs, so the menu bar cannot disagree with them.
# Link it into the plugin folder; it finds fleetctl.py beside itself through the link.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
self="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$0" 2>/dev/null || echo "$0")"
fleetctl="${CROSSFEED_FLEETCTL:-$(dirname "$(dirname "$self")")/fleetctl.py}"
lead=()
[ -n "${CROSSFEED_GLANCE_LEAD:-}" ] && lead=(--lead "$CROSSFEED_GLANCE_LEAD")
if out="$(python3 "$fleetctl" glance --format swiftbar ${lead[@]+"${lead[@]}"} --plugin "$0" "$@" 2>&1)"; then
  printf '%s\n' "$out"
else
  echo "Limits ?"
  echo "---"
  printf '%s\n' "$out" | tail -3 | tr '|' '/'
  echo "Try again | refresh=true"
fi
