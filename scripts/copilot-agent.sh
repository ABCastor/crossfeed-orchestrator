#!/usr/bin/env bash
# copilot-agent.sh: use GitHub Copilot Student's only honest route, official CLI Auto.
#
# This wrapper is intentionally read-only and single-agent. Student has 200 monthly AI
# credits and no named-model selection; use OpenCode Go, Codex, or Claude for real fan-out.
#
# Usage:
#   copilot-agent.sh --prompt "<brief review>" [--dir <workdir>]
#                     [--timeout S] [--idle-timeout S] [--kill-after S]
#                     [--events <file.jsonl>] [--last <file>]
#
# --timeout has no default: absent means no wall-clock limit.
# --idle-timeout defaults to 2400 seconds; --kill-after defaults to 30 seconds.
# Exit codes: 0 success; 1 supervision/setup or passed-through roster failure;
# 2 usage/write refusal; 4 invalid JSON; 5 missing/failing result; 6 empty answer;
# 124 wall-clock kill; 125 idle kill; 127 missing copilot/jq;
# every other Copilot exit code is passed through.
set -euo pipefail
if [ "$("$(dirname "$0")/fleetctl.py" switch github-copilot-student 2>/dev/null)" = off ]; then
  echo "copilot-agent: the github-copilot-student pool is switched off by hand (fleetctl.py switch github-copilot-student auto)" >&2; exit 5
fi
# Low means one run at a time on this pool. The gate holds the pool's slot for this process and
# frees it when the process ends; a second run waits (FLEET_POOL_WAIT_S, default 900s), then
# refuses with exit 5. A gate that cannot run at all never blocks the dispatch.
_gate_rc=0; "$(dirname "$0")/fleetctl.py" pool-slot github-copilot-student --pid $$ || _gate_rc=$?
[ "$_gate_rc" != 5 ] || exit 5

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$PWD"; PROMPT=""; PROMPT_FILE=""; MODEL="auto"; CREDITS="30"
TIMEOUT=""
IDLE_TIMEOUT="2400"
KILL_AFTER="30"
EVENTS=""; LAST=""

_usage_error() {
  echo "copilot-agent: $1" >&2
  exit 2
}

_require_value() {
  [ "$#" -ge 2 ] || _usage_error "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt)       _require_value "$@"; PROMPT="$2"; shift 2;;
    --prompt-file)  _require_value "$@"; PROMPT_FILE="$2"; shift 2;;
    --dir)          _require_value "$@"; DIR="$2"; shift 2;;
    --read-only)    shift;;
    --timeout)      _require_value "$@"; TIMEOUT="$2"; shift 2;;
    --idle-timeout) _require_value "$@"; IDLE_TIMEOUT="$2"; shift 2;;
    --kill-after)   _require_value "$@"; KILL_AFTER="$2"; shift 2;;
    --events)       _require_value "$@"; EVENTS="$2"; shift 2;;
    --last)         _require_value "$@"; LAST="$2"; shift 2;;
    --write)        _usage_error "Student Auto is observer-only; use OpenCode Go or Codex for writes";;
    # fanout.sh passes --model auto for every lane. Student is Auto-only by policy, so accept
    # exactly that and still refuse a named model, which is the rule this wrapper exists to hold.
    --model)        _require_value "$@"; [ "$2" = "auto" ] || _usage_error "Copilot Student is Auto-only; named models are not admitted (got: $2)"; shift 2;;
    -h|--help)      sed -n '2,16p' "$0"; exit 0;;
    *)               _usage_error "unknown arg: $1";;
  esac
done

source "${BASH_SOURCE[0]%/*}/run-identity.sh"

_is_seconds() {
  case "$1" in
    ''|*[!0-9]*) return 1;;
    *) return 0;;
  esac
}

if [ -n "$TIMEOUT" ] && ! _is_seconds "$TIMEOUT"; then
  _usage_error "--timeout must be a non-negative whole number of seconds"
fi
_is_seconds "$IDLE_TIMEOUT" || _usage_error "--idle-timeout must be a non-negative whole number of seconds"
_is_seconds "$KILL_AFTER" || _usage_error "--kill-after must be a non-negative whole number of seconds"

if [ -n "$PROMPT_FILE" ] && [ -z "$PROMPT" ]; then
  [ -f "$PROMPT_FILE" ] || _usage_error "prompt-file not found: $PROMPT_FILE"
  PROMPT="$(<"$PROMPT_FILE")"
fi

[ -n "$PROMPT" ] || _usage_error "--prompt or --prompt-file is required"
[ -d "$DIR" ] || _usage_error "work directory not found: $DIR"
command -v copilot >/dev/null 2>&1 || { echo "copilot-agent: copilot binary not on PATH" >&2; exit 127; }
command -v jq >/dev/null 2>&1 || { echo "copilot-agent: jq is required" >&2; exit 127; }

_parent_comm="$(ps -o comm= -p "$PPID" 2>/dev/null | tr -d '[:space:]' || true)"
case "${_parent_comm##*/}" in
  timeout|gtimeout)
    echo "copilot-agent: WARNING - launched under an outer '${_parent_comm##*/}' wall-clock cap." >&2
    echo "copilot-agent: that outer cap will kill this run regardless of progress, and the idle watchdog cannot prevent it." >&2
    echo "copilot-agent: remove the outer wrapper; pass --timeout to this script if you genuinely need a hard budget." >&2
    ;;
esac

"$HERE/roster.sh" check copilot "$MODEL"
echo "copilot-agent: effort service-chosen: the service chooses the level" >&2

tmp_events=""
if [ -z "$EVENTS" ]; then
  tmp_events="$(mktemp "${TMPDIR:-/tmp}/copilot-agent.events.XXXXXX")"
  EVENTS="$tmp_events"
fi
stderr_file="$(mktemp "${TMPDIR:-/tmp}/copilot-agent.stderr.XXXXXX")"
answer_file="$(mktemp "${TMPDIR:-/tmp}/copilot-agent.answer.XXXXXX")"
KILL_STATE="$(mktemp "${TMPDIR:-/tmp}/copilot-agent.kill.XXXXXX")"
CHILD_PID=""
CHILD_PGID=""
WATCHDOG_PID=""
CHILD_COMPLETED=0
PENDING_SIGNAL_NAME=""
PENDING_SIGNAL_CODE=""

_cleanup() {
  local status=$?
  # Only the wrapper's own shell cleans up. With bash 5 a background subshell (the watchdog,
  # a job just forked) can run this inherited EXIT trap and delete the run's files early.
  [ "${BASHPID:-$$}" = "$$" ] || return 0
  crossfeed_finish "$status" || status=8
  [ -n "$tmp_events" ] && rm -f "$tmp_events"
  rm -f "$stderr_file" "$answer_file" "$KILL_STATE"
  trap - EXIT
  exit "$status"
}
trap _cleanup EXIT

crossfeed_prepare copilot

args=(
  -C "$DIR"
  -p "$PROMPT"
  --model auto
  --max-ai-credits "$CREDITS"
  --mode interactive
  --deny-tool=shell
  --deny-tool=write
  --no-ask-user
  --no-custom-instructions
  --disable-builtin-mcps
  --no-remote
  --no-remote-export
  --output-format json
  --log-level error
  --disallow-temp-dir
)

if stat -f '%z:%m' "$0" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
elif stat -c '%s:%Y' "$0" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
else
  echo "copilot-agent: neither BSD nor GNU stat interface is available" >&2
  exit 1
fi

_file_signature() {
  if [ ! -e "$1" ]; then
    printf 'missing'
  elif [ "$STAT_STYLE" = "bsd" ]; then
    stat -f '%z:%m' "$1" 2>/dev/null || printf 'unreadable'
  else
    stat -c '%s:%Y' "$1" 2>/dev/null || printf 'unreadable'
  fi
}

_output_signature() {
  _file_signature "$EVENTS"
  printf '|'
  _file_signature "$stderr_file"
}

# Keep the historical function name, but sample every member of the CLI's
# process group so a waiting leader cannot hide a CPU-active descendant.
_child_cpu_ticks() {
  { ps -A -o pgid=,time= 2>/dev/null || true; } | awk -v target="$CHILD_PGID" '
    function digits(value) { return value ~ /^[0-9]+$/ }
    function ticks(raw, whole, fraction, decimal, days, day_fields,
                   field_count, fields, hours, minutes, seconds) {
      whole = raw
      fraction = 0
      if (index(whole, ".") > 0) {
        split(whole, decimal, ".")
        if (!digits(decimal[2])) return -1
        whole = decimal[1]
        fraction = substr(decimal[2] "00", 1, 2) + 0
      }
      days = 0
      if (index(whole, "-") > 0) {
        split(whole, day_fields, "-")
        if (!digits(day_fields[1]) || day_fields[2] == "") return -1
        days = day_fields[1] + 0
        whole = day_fields[2]
      }
      field_count = split(whole, fields, ":")
      hours = 0
      if (field_count == 3) {
        hours = fields[1]; minutes = fields[2]; seconds = fields[3]
      } else if (field_count == 2) {
        minutes = fields[1]; seconds = fields[2]
      } else {
        return -1
      }
      if (!digits(hours) || !digits(minutes) || !digits(seconds)) return -1
      return ((((days * 24) + hours) * 60 + minutes) * 60 + seconds) * 100 + fraction
    }
    ($1 + 0) == (target + 0) {
      value = ticks($2)
      if (value < 0) invalid = 1
      else { total += value; found = 1 }
    }
    END {
      if (found && !invalid) printf "%.0f\n", total
      else print "unavailable"
    }
  '
}

_read_child_pgid() {
  local child="$1" pgid="" attempts=0
  while [ "$attempts" -lt 20 ]; do
    pgid="$(ps -o pgid= -p "$child" 2>/dev/null | tr -d '[:space:]' || true)"
    case "$pgid" in
      ''|*[!0-9]*) ;;
      *) printf '%s' "$pgid"; return 0;;
    esac
    attempts=$((attempts + 1))
    sleep 0.05
  done
  return 1
}

_group_alive() {
  # Guard the empty case: called before the PGID is known, `kill -0 -- "-"` fails silently and the
  # caller concludes the group is dead while it is very much alive.
  [ -n "${CHILD_PGID:-}" ] || return 1
  kill -0 -- "-$CHILD_PGID" 2>/dev/null
}

_child_alive() {
  [ -n "${CHILD_PID:-}" ] && kill -0 "$CHILD_PID" 2>/dev/null
}

_wait_for_group_completion() {
  local announced=0
  while _group_alive; do
    if [ "$announced" -eq 0 ]; then
      echo "copilot-agent: Copilot leader exited $rc, but process group $CHILD_PGID still has live descendants; keeping supervision active until the group exits." >&2
      announced=1
    fi
    [ -s "$KILL_STATE" ] && return 0
    sleep 1
  done
}

_now() {
  date +%s
}

_print_kill_message() {
  local label="$1" elapsed="$2" detail="$3"
  echo "copilot-agent: ${label} LIMIT FIRED after ${elapsed}s (${detail})." >&2
  echo "copilot-agent: working directory: $DIR" >&2
  echo "copilot-agent: killing Copilot process group $CHILD_PGID; this observer produced no trusted final result." >&2
  echo "copilot-agent: recovery: inspect the JSON event stream and stderr before rerunning the review." >&2
}

_terminate_group() {
  local term_started now since_term
  kill -TERM -- "-$CHILD_PGID" 2>/dev/null || true
  term_started="$(_now)"

  while _group_alive; do
    now="$(_now)"
    since_term=$((now - term_started))
    if [ "$since_term" -ge "$KILL_AFTER" ]; then
      echo "copilot-agent: KILL-AFTER LIMIT FIRED after ${since_term}s; sending SIGKILL to process group $CHILD_PGID." >&2
      kill -KILL -- "-$CHILD_PGID" 2>/dev/null || true
      return
    fi
    sleep 1
  done
}

# The watchdog is stopped with SIGKILL, never TERM: a TERM that lands while bash is still forking
# the watchdog makes bash 5 run the wrapper's EXIT trap in it, deleting this run's files.
_watchdog() {
  local started last_activity now elapsed idle_for
  local last_output current_output last_cpu current_cpu

  started="$(_now)"
  last_activity="$started"
  last_output="$(_output_signature)"
  last_cpu="$(_child_cpu_ticks)"

  while _group_alive; do
    sleep 1
    _group_alive || return 0
    now="$(_now)"
    elapsed=$((now - started))

    if [ -n "$TIMEOUT" ] && [ "$elapsed" -ge "$TIMEOUT" ]; then
      printf 'wall\n' >"$KILL_STATE"
      _print_kill_message "WALL-CLOCK" "$elapsed" "configured --timeout ${TIMEOUT}s"
      _terminate_group
      return 0
    fi

    current_output="$(_output_signature)"
    current_cpu="$(_child_cpu_ticks)"
    if [ "$current_output" != "$last_output" ] || [ "$current_cpu" != "$last_cpu" ]; then
      last_activity="$now"
    fi
    last_output="$current_output"
    last_cpu="$current_cpu"

    idle_for=$((now - last_activity))
    # --idle-timeout 0 DISABLES idle supervision. Without this, 0 meant "kill the instant nothing
    # has changed", so anyone trying to turn supervision off got instant kills instead. Keeping the
    # off-switch honest matters: it is the one lever for anyone who would rather risk an unbounded
    # hang than any false kill, and a reviewer argued exactly that position.
    if [ "$IDLE_TIMEOUT" -gt 0 ] && [ "$idle_for" -ge "$IDLE_TIMEOUT" ]; then
      printf 'idle\n' >"$KILL_STATE"
      _print_kill_message "IDLE" "$elapsed" "no output or CPU progress for ${idle_for}s; configured --idle-timeout ${IDLE_TIMEOUT}s"
      _terminate_group
      return 0
    fi
  done
}

_handle_signal() {
  local signal_name="$1" signal_code="$2" now elapsed
  PENDING_SIGNAL_NAME="$signal_name"
  PENDING_SIGNAL_CODE="$signal_code"
  [ -n "$WATCHDOG_PID" ] && kill -s KILL "$WATCHDOG_PID" 2>/dev/null || true

  if [ -z "$CHILD_PGID" ] && _child_alive; then
    CHILD_PGID="$CHILD_PID"
  fi
  if [ -n "$CHILD_PGID" ] && _group_alive; then
    trap - HUP INT TERM
    now="$(_now)"
    elapsed=$((now - RUN_STARTED))
    echo "copilot-agent: EXTERNAL ${signal_name} RECEIVED after ${elapsed}s; no configured limit fired." >&2
    echo "copilot-agent: working directory: $DIR" >&2
    echo "copilot-agent: killing Copilot process group $CHILD_PGID; no trusted final result exists." >&2
    _terminate_group
    exit "$signal_code"
  fi
  return 0
}

# Copilot remains observer-only; only the supervision mechanics change. Job
# control gives the CLI and any descendants a distinct process group.
RUN_STARTED="$(_now)"
trap '_handle_signal HUP 129' HUP
trap '_handle_signal INT 130' INT
trap '_handle_signal TERM 143' TERM

if [ -n "$PENDING_SIGNAL_CODE" ]; then
  trap - HUP INT TERM
  exit "$PENDING_SIGNAL_CODE"
fi

CROSSFEED_STARTED=1
set -m
env -u COPILOT_GITHUB_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
  copilot "${args[@]}" </dev/null >"$EVENTS" 2>"$stderr_file" &
CHILD_PID=$!
if [ -n "$PENDING_SIGNAL_CODE" ]; then
  _handle_signal "$PENDING_SIGNAL_NAME" "$PENDING_SIGNAL_CODE"
fi

rc=0
if ! CHILD_PGID="$(_read_child_pgid "$CHILD_PID")"; then
  CHILD_PGID="$CHILD_PID"
  if _child_alive; then
    echo "copilot-agent: PROCESS-GROUP DISCOVERY WARNING: ps did not return a PGID; supervising live child group $CHILD_PGID via the set -m PID=PGID invariant." >&2
  else
    set +e
    wait "$CHILD_PID"
    rc=$?
    set -e
    CHILD_COMPLETED=1
  fi
fi
set +m

if [ "$CHILD_COMPLETED" -eq 0 ] || _group_alive; then
  _watchdog &
  WATCHDOG_PID=$!

  if [ "$CHILD_COMPLETED" -eq 0 ]; then
    set +e
    wait "$CHILD_PID"
    rc=$?
    set -e
  fi

  if [ ! -s "$KILL_STATE" ]; then
    _wait_for_group_completion
  fi

  if [ -s "$KILL_STATE" ]; then
    set +e
    wait "$WATCHDOG_PID"
    set -e
  else
    kill -s KILL "$WATCHDOG_PID" 2>/dev/null || true
    set +e
    wait "$WATCHDOG_PID" 2>/dev/null
    set -e
  fi
fi
WATCHDOG_PID=""
CHILD_PID=""

case "$(sed -n '1p' "$KILL_STATE")" in
  wall) rc=124;;
  idle) rc=125;;
esac

if [ "$rc" -ne 0 ]; then
  echo "copilot-agent: copilot exited $rc. stderr tail:" >&2
  tail -8 "$stderr_file" >&2 || true
  exit "$rc"
fi

jq -e . "$EVENTS" >/dev/null 2>&1 || { echo "copilot-agent: invalid JSON event stream" >&2; exit 4; }
result_rc="$(jq -r 'select(.type == "result") | .exitCode' "$EVENTS" | tail -1)"
[ "$result_rc" = "0" ] || { echo "copilot-agent: missing or failing result event" >&2; exit 5; }
jq -rs -r '[.[] | select(.type == "assistant.message") | .data.content // empty] | last // empty' "$EVENTS" >"$answer_file"
[ -s "$answer_file" ] || { echo "copilot-agent: empty final model output" >&2; exit 6; }

chosen="$(jq -r 'select(.type == "session.auto_mode_resolved") | .data.chosenModel // empty' "$EVENTS" | tail -1)"
echo "copilot-agent: Auto routed this call to ${chosen:-unknown}; that is not a named Student entitlement" >&2
cat "$answer_file"
[ -n "$LAST" ] && cp "$answer_file" "$LAST"
exit 0
