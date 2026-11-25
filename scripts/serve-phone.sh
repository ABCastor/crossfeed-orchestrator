#!/usr/bin/env bash
# Foreground Tailscale Serve route for the read-only phone page. Never uses Funnel.
# Bash parses the complete body before starting, so an install cannot change it in flight.
main() {
set -euo pipefail
if [[ $# != 1 || $1 != /* ]]; then
  echo 'Usage: bash scripts/serve-phone.sh /absolute/path/to/phone.json' >&2; exit 2
fi
config=$1
python=${CROSSFEED_PYTHON:-python3}
port=${CROSSFEED_PHONE_PORT:-8443}
[[ $port =~ ^[0-9]+$ ]] || { echo 'CROSSFEED_PHONE_PORT must be a number.' >&2; exit 2; }
base=$(cd -- "$(dirname -- "$0")" && pwd)
# Do not place Crossfeed on a listener already in use, particularly a Funnel.
if ! tailscale serve status --json | CROSSFEED_PHONE_PORT="$port" "$python" -c '
import json, os, sys
PORT = os.environ["CROSSFEED_PHONE_PORT"]

def occupied(node):
    if isinstance(node, dict):
        return any(k == PORT or k.endswith(":" + PORT) or occupied(v) for k, v in node.items())
    if isinstance(node, list):
        return any(map(occupied, node))
    return False

try:
    raise SystemExit(1 if occupied(json.load(sys.stdin)) else 0)
except ValueError:
    raise SystemExit(1)
'; then
  echo "Cannot verify that tailnet HTTPS port $port is unused. No route was changed." >&2
  exit 1
fi
# Keep this path short enough for the macOS Unix socket pathname limit.
private=$(mktemp -d /tmp/crossfeed-phone.XXXXXXXX)
chmod 700 "$private"
socket="$private/phone.sock"
"$python" -B "$base/phone_view.py" --config "$config" --socket "$socket" &
backend=$!
cleanup() {
  kill -TERM "$backend" 2>/dev/null || true
  wait "$backend" 2>/dev/null || true
  rmdir "$private" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for ((i=0; i<50; i++)); do
  [[ -S "$socket" ]] && break
  kill -0 "$backend" 2>/dev/null || break
  sleep 0.1
done
[[ -S "$socket" ]] || { echo 'Crossfeed phone view could not start.' >&2; exit 1; }
# Tailscale Serve adds Tailscale-User-Login and strips a client-supplied value.
# The target is not reachable over TCP, and the signed console is never a target.
tailscale serve --https="$port" --set-path=/crossfeed "unix:$socket"
}

main "$@"; exit $?
