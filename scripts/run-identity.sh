#!/usr/bin/env bash
# Sourced by wrappers: one model receipt protocol, no changes to watchdog ownership.
CROSSFEED_IDENTITY_SCRIPT="$(cd "${BASH_SOURCE[0]%/*}" && pwd)/run_identity.py"
CROSSFEED_REQUESTED="${MODEL:-${MODEL_KEY:-}}"
[ -n "$CROSSFEED_REQUESTED" ] || CROSSFEED_REQUESTED="${MODEL_KEY:-${LANE:-}}"
CROSSFEED_REQUESTED_ROLE="${EFFORT_ROLE:-${ROLE:-}}"
CROSSFEED_IDENTITY=""
CROSSFEED_STARTED=0
CROSSFEED_LAST="${LAST:-}"

crossfeed_prepare() {
  CROSSFEED_IDENTITY="$(mktemp "${TMPDIR:-/tmp}/crossfeed-identity.XXXXXX")"
  local selected="${MODEL:-}"
  # Codex's configured default is resolved by model-run even when it needs no stand-in.
  [ -n "$selected" ] || selected="${_asked:-}"
  local effort
  case "$1" in
    codex) effort="${REASONING:-}";;
    claude|agy) effort="${EFFORT:-provider-default}";;
    opencode) effort="${VARIANT:-provider-default}";;
    copilot) effort="service-chosen";;
    pi) effort="${THINKING:-${EFFORT:-medium}}";;
    *) effort="provider-default";;
  esac
  CROSSFEED_RESOLVED_EFFORT="$effort"
  python3 "$CROSSFEED_IDENTITY_SCRIPT" begin --path "$CROSSFEED_IDENTITY" \
    --wrapper "$1" --requested "$CROSSFEED_REQUESTED" --selected "$selected" \
    --lane "${LANE:-}" --role "$CROSSFEED_REQUESTED_ROLE" --effort "$effort"
  PROMPT="$(python3 "$CROSSFEED_IDENTITY_SCRIPT" prompt --path "$CROSSFEED_IDENTITY")"$'\n'"$PROMPT"
}

crossfeed_finish() {
  local status="$1"
  if [ "$CROSSFEED_STARTED" = 1 ] && [ -n "$CROSSFEED_IDENTITY" ]; then
    local options=( finish --path "$CROSSFEED_IDENTITY" --returncode "$status" )
    [ -z "${EVENTS:-}" ] || options+=( --events "$EVENTS" )
    [ -z "${oc_data_home:-}" ] || options+=( --database "$oc_data_home/opencode/opencode.db" )
    [ -z "$CROSSFEED_LAST" ] || options+=( --last "$CROSSFEED_LAST" )
    # --last and stdout retain the original schema JSON; identity uses stderr/sidecar.
    if [ -n "${SCHEMA:-}" ]; then
      options+=( --schema )
      options+=( --result-file "$LAST" )
    fi
    python3 "$CROSSFEED_IDENTITY_SCRIPT" "${options[@]}" || {
      echo 'Crossfeed: model receipt/ledger failed; do not claim this run was accounted.' >&2
      return 8
    }
  fi
  # Run-private scratch, outside the owner's folders; no durable data is deleted.
  [ -z "$CROSSFEED_IDENTITY" ] || unlink "$CROSSFEED_IDENTITY"
}
