#!/usr/bin/env bash
# Text-only Crossfeed Chat worker; Python supervises the HTTP child and quota lease.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -euo pipefail
command -v python3 >/dev/null || { echo 'chatgpt-agent: python3 is required' >&2; exit 127; }
HERE="$(cd "${BASH_SOURCE[0]%/*}" && pwd)"
for dependency in chatgpt_runner.py chatgpt_queue.py chatgpt_transport.py chatgpt_catalog.py chatgpt_pro.py run_identity.py fleetctl.py; do
  [ -f "$HERE/$dependency" ] || { echo 'chatgpt-agent: required helper missing' >&2; exit 127; }
done
exec python3 "$HERE/chatgpt_runner.py" "$@"

}
main "$@"; exit $?
