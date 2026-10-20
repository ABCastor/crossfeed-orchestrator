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

# ---- vault guard (20 Oct 2026; rule and why: vault_guard.py, BUSINESS-RULES.md BR-VAULT-001) ----
# Every shell wrapper calls crossfeed_vault_guard once its brief is loaded and before it creates
# --dir, takes a lease, dry-runs or starts a model. A refusal exits 5 (prelaunch refusal).
# CROSSFEED_JAIL is the launch prefix (sandbox-exec + a profile denying writes under the protected
# roots); each wrapper starts its CLI with ${CROSSFEED_JAIL[@]+"${CROSSFEED_JAIL[@]}"} in front.
CROSSFEED_VAULT_GUARD="$(cd "${BASH_SOURCE[0]%/*}" && pwd)/vault_guard.py"
CROSSFEED_JAIL=()
crossfeed_vault_guard() {
  local wrapper="${0##*/}" name mode jail=1 profile rc
  case "$wrapper" in
    codex-agent.sh)      name=codex; mode=write; [ "${SANDBOX:-}" = read-only ] && mode=read-only
                         # Seatbelt cannot nest: Codex's own sandbox keeps writes to --dir.
                         [ "${SANDBOX:-}" = danger-full-access ] || jail=0;;
    claude-agent.sh)     name=claude; mode=write; [ "${PERMISSION_MODE:-}" = plan ] && mode=read-only;;
    agy-agent.sh)        name=agy; mode=write;;   # agy always runs --dangerously-skip-permissions
    opencode-agent.sh)   name=opencode; mode="${MODE:-read-only}";;
    copilot-agent.sh)    name=copilot; mode=read-only;;
    openrouter-agent.sh) name=openrouter; mode=read-only; jail=0;;   # tool-less: prompt in, text out
    *) return 0;;
  esac
  [ -f "$CROSSFEED_VAULT_GUARD" ] || { echo "vault-guard: refused: vault_guard.py is missing beside run-identity.sh" >&2; exit 5; }
  local opts=( check --wrapper "$name" --mode "$mode" )
  [ -z "${DIR:-}" ] || opts+=( --dir "$DIR" )
  [ -z "${LAST:-}" ] || opts+=( --out "$LAST" )
  [ -z "${EVENTS:-}" ] || opts+=( --out "$EVENTS" )
  [ -z "${LOG:-}" ] || opts+=( --out "$LOG" )
  if [ "$name" = codex ]; then
    local cfg
    for cfg in ${CONFIGS[@]+"${CONFIGS[@]}"}; do opts+=( --config "$cfg" ); done
  fi
  rc=0
  if [ -n "${PROMPT:-}" ]; then
    printf '%s' "$PROMPT" | python3 "$CROSSFEED_VAULT_GUARD" "${opts[@]}" || rc=$?
  elif [ -n "${PROMPT_FILE:-}" ]; then
    python3 "$CROSSFEED_VAULT_GUARD" "${opts[@]}" --prompt-file "$PROMPT_FILE" </dev/null || rc=$?
  else
    python3 "$CROSSFEED_VAULT_GUARD" "${opts[@]}" </dev/null || rc=$?
  fi
  [ "$rc" = 0 ] || exit 5
  [ "$jail" = 1 ] || return 0
  rc=0; profile="$(python3 "$CROSSFEED_VAULT_GUARD" profile)" || rc=$?
  [ "$rc" = 0 ] || exit 5
  if [ -n "$profile" ]; then
    CROSSFEED_JAIL=( /usr/bin/sandbox-exec -p "$profile" )
    echo "vault-guard: this worker cannot write under: $(python3 "$CROSSFEED_VAULT_GUARD" roots | paste -sd ',' -)" >&2
  fi
  return 0
}
