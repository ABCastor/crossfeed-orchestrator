#!/usr/bin/env bash
# Pi worker: final text on stdout, diagnostics and receipt on stderr.
# Usage: pi-agent.sh [run] --lane ID|--model provider/id --prompt-file FILE
#   --dir DIR --effort low|medium|high|xhigh|max --mode ro|rw
#   --idle S --wall S --kill-after S --events FILE --last FILE
# Python owns the idle-timeout watchdog, process group and lease, not this launcher.
set -euo pipefail
command -v python3 >/dev/null || { echo 'pi-agent: python3 is required' >&2; exit 127; }
exec python3 "$(cd "${BASH_SOURCE[0]%/*}" && pwd)/pi_runner.py" "$@"
