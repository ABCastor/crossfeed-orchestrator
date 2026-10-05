#!/usr/bin/env bash
# claude-agent.sh - run one headless Claude Code agent on a folder and return its final message.
#
# Usage:
#   claude-agent.sh --prompt "<task>" [--dir <workdir>] [--model opus]
#                   [--role <role>] [--effort high] [--permission-mode acceptEdits]
#                   [--read-only] [--timeout S] [--idle-timeout S]
#                   [--kill-after S] [--last <file>] [--dry-run]
#
# The model: the models switched on in the Crossfeed console decide. --model is passed as given
# while that model is on; when it is off, the nearest model that is on runs instead (cheaper side
# first) and stderr says so; with none on the run is refused (exit 5). No --model runs "opus"
# (CLAUDE_WRAPPER_DEFAULT in fleetctl.py names the same default), or its stand-in when opus is off.
# --dry-run prints the exact claude command this run would start (the prompt left out) and exits 0:
# no slot is taken and nothing is sent.
#
# --timeout has no default: absent means no wall-clock limit.
# --idle-timeout defaults to 0 (opt-in killing only); --kill-after defaults to 30 seconds.
# Prints the agent's final message to stdout. Diagnostics go to stderr.
# Exit codes: 0 success; 1 supervision setup failure; 2 usage or bad workdir;
# 4 empty output; 124 wall-clock kill; 125 idle kill; 127 claude missing;
# every other Claude exit code is passed through.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -euo pipefail
if [ "$("$(dirname "$0")/fleetctl.py" switch claude 2>/dev/null)" = off ]; then
  echo "claude-agent: the claude pool is switched off by hand (fleetctl.py switch claude auto)" >&2; exit 5
fi

DIR="$PWD"; PROMPT=""; PROMPT_FILE=""; MODEL="opus"; EFFORT=""; ROLE="default"
PERMISSION_MODE="acceptEdits"; TOOLS="default"
TIMEOUT=""
IDLE_TIMEOUT="0"
KILL_AFTER="30"
LAST=""
DRY_RUN=0

_usage_error() {
  echo "claude-agent: $1" >&2
  exit 2
}

_require_value() {
  [ "$#" -ge 2 ] || _usage_error "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt)          _require_value "$@"; PROMPT="$2"; shift 2;;
    --prompt-file)     _require_value "$@"; PROMPT_FILE="$2"; shift 2;;
    --dir)             _require_value "$@"; DIR="$2"; shift 2;;
    --model)           _require_value "$@"; MODEL="$2"; shift 2;;
    --effort)          _require_value "$@"; EFFORT="$2"; shift 2;;
    --role)            _require_value "$@"; ROLE="$2"; shift 2;;
    --permission-mode) _require_value "$@"; PERMISSION_MODE="$2"; shift 2;;
    --tools)           _require_value "$@"; TOOLS="$2"; shift 2;;
    --read-only)       PERMISSION_MODE="plan"; TOOLS="Read,Grep,Glob"; shift;;
    --timeout)         _require_value "$@"; TIMEOUT="$2"; shift 2;;
    --idle-timeout)    _require_value "$@"; IDLE_TIMEOUT="$2"; shift 2;;
    --kill-after)      _require_value "$@"; KILL_AFTER="$2"; shift 2;;
    --last)            _require_value "$@"; LAST="$2"; shift 2;;
    --dry-run)         DRY_RUN=1; shift;;
    -h|--help)         sed -n '2,22p' "$0"; exit 0;;
    *)                  _usage_error "unknown arg: $1";;
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

# Low means one run at a time on this pool. The gate holds the pool's slot for this process and
# frees it when the process ends; a second run waits (FLEET_POOL_WAIT_S, default 900s), then
# refuses with exit 5. A gate that cannot run at all never blocks the dispatch. A dry run starts
# nothing, so it takes no slot and never waits.
if [ "$DRY_RUN" != 1 ]; then
  _gate_rc=0; "$(dirname "$0")/fleetctl.py" pool-slot claude --pid $$ || _gate_rc=$?
  [ "$_gate_rc" != 5 ] || exit 5
fi

# The models switched on in the console are the operator's hand: a task that asks for a model
# that is off gets the nearest one that is on (cheaper side first), and says so; with none on the
# provider is off (exit 5). Reading never blocks a run: only "none on" stops it, and an unreadable
# answer or one that is not a plain model id leaves the task's model as it was.
# A stand-in is said twice on stderr: here, and again as the very last line once the run is over,
# in one fixed form (STAND_IN_NOTICE) so the agent that reports the run names the model that ran.
# A shared receipt also reaches stdout, the worker, saved results and the run ledger.
STAND_IN_NOTICE=""
_pick_rc=0; _pick="$("$(dirname "$0")/fleetctl.py" model-run claude "$MODEL" --explain 2>/dev/null)" || _pick_rc=$?
if [ "$_pick_rc" = 5 ]; then
  echo "claude-agent: no model is switched on for claude in the Crossfeed console (fleetctl.py model-toggle claude <model> on)" >&2; exit 5
fi
_picked=""; _asked=""; _why=""
read -r _picked _asked _why <<<"$_pick" || true
case "$_picked" in ''|*[!A-Za-z0-9._:/-]*) _picked="";; esac
case "$_asked" in ''|-|*[!A-Za-z0-9._:/-]*) _asked="";; esac
if [ -n "$_picked" ] && [ "$_picked" != "$MODEL" ]; then
  if [ -n "$MODEL" ]; then
    echo "claude-agent: model $_picked, the nearest one switched on in the Crossfeed console ($MODEL is off)" >&2
  else
    echo "claude-agent: model $_picked, the nearest one switched on in the Crossfeed console" >&2
  fi
  case "$_why" in
    retired)  _because="retired by its provider";;
    unlisted) _because="not a model Crossfeed lists, and some models are switched off in the console";;
    *)        _because="switched off in the console";;
  esac
  STAND_IN_NOTICE="Crossfeed: this run used $_picked, not ${_asked:-${MODEL:-the default model}} (${_because}). Say $_picked when you report it."
  MODEL="$_picked"
fi
# Said last, after everything else this wrapper prints, but only for a run that started claude:
# a run refused before launch used no model at all.
LAUNCHED=0
_stand_in_notice() {
  [ -n "$STAND_IN_NOTICE" ] && [ "$LAUNCHED" = 1 ] && echo "$STAND_IN_NOTICE" >&2
  return 0
}

if [ "$DRY_RUN" != 1 ]; then
  command -v claude >/dev/null 2>&1 || { echo "claude-agent: claude binary not on PATH" >&2; exit 127; }
fi
[ -d "$DIR" ] || _usage_error "work directory not found: $DIR"

# A static scanner cannot see `timeout ./claude-agent.sh`, so detect the exact
# layered-cap failure at runtime and make the outer wall clock explicit.
_parent_comm="$(ps -o comm= -p "$PPID" 2>/dev/null | tr -d '[:space:]' || true)"
case "${_parent_comm##*/}" in
  timeout|gtimeout)
    echo "claude-agent: WARNING - launched under an outer '${_parent_comm##*/}' wall-clock cap." >&2
    echo "claude-agent: that outer cap will kill this run regardless of progress, and the idle watchdog cannot prevent it." >&2
    echo "claude-agent: remove the outer wrapper; pass --timeout to this script if you genuinely need a hard budget." >&2
    ;;
esac

args=( -p --output-format stream-json --verbose --no-session-persistence --permission-mode "$PERMISSION_MODE" --tools "$TOOLS" )
effort_args=( effort "$MODEL" "$ROLE" --harness claude --explain )
[ -z "$EFFORT" ] || effort_args+=( --level "$EFFORT" )
[ -z "$STAND_IN_NOTICE" ] || effort_args+=( --stand-in )
effort_row="$("$(dirname "$0")/fleetctl.py" "${effort_args[@]}")" || _usage_error "cannot resolve thinking effort"
IFS=$'\t' read -r EFFORT _effort_model _effort_why <<<"$effort_row"
echo "claude-agent: effort $EFFORT: $_effort_why" >&2
[ -n "$MODEL" ] && args+=( --model "$MODEL" )
if [ "$EFFORT" != provider-default ]; then
  [ -n "$EFFORT" ] || _usage_error "empty thinking effort"
  args+=( --effort "$EFFORT" )
fi

if [ "$DRY_RUN" = 1 ]; then
  # The very array the run below would start claude with, so the two cannot drift apart.
  echo "claude-agent: dry run, nothing started. The command:" >&2
  printf '%q ' claude "${args[@]}" --
  printf '%s\n' '<prompt>'
  [ -z "$STAND_IN_NOTICE" ] || echo "${STAND_IN_NOTICE/this run used/this run would use}" >&2
  exit 0
fi

crossfeed_prepare claude

RUNOUT="$(mktemp "${TMPDIR:-/tmp}/claude-agent.runout.XXXXXX")"
EVENTS="$RUNOUT"
# Native stream-json carries model identity and progress. Capture stderr as well: both streams
# contribute to the watchdog, so a quiet result channel cannot turn idleness into a wall clock.
RUNERR="$(mktemp "${TMPDIR:-/tmp}/claude-agent.runerr.XXXXXX")"
RUNERR_PIPE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/claude-agent.stderr-pipe.XXXXXX")"
RUNERR_PIPE="$RUNERR_PIPE_DIR/stderr.fifo"
mkfifo "$RUNERR_PIPE"
KILL_STATE="$(mktemp "${TMPDIR:-/tmp}/claude-agent.kill.XXXXXX")"
CHILD_PID=""
CHILD_PGID=""
WATCHDOG_PID=""
TEE_PID=""
STDERR_STREAM_RC=0
CHILD_COMPLETED=0
PENDING_SIGNAL_NAME=""
PENDING_SIGNAL_CODE=""

_stop_stderr_stream() {
  if [ -n "${TEE_PID:-}" ]; then
    kill "$TEE_PID" 2>/dev/null || true
    wait "$TEE_PID" 2>/dev/null || true
    TEE_PID=""
  fi
}

_cleanup() {
  local status=$?
  # Only the wrapper's own shell cleans up. With bash 5 a background subshell (the watchdog,
  # a job just forked) can run this inherited EXIT trap and delete the run's files early.
  [ "${BASHPID:-$$}" = "$$" ] || return 0
  crossfeed_finish "$status" || status=8
  _stop_stderr_stream
  [ -n "${RUNERR_PIPE:-}" ] && rm -f "$RUNERR_PIPE"
  [ -n "${RUNERR_PIPE_DIR:-}" ] && rmdir "$RUNERR_PIPE_DIR" 2>/dev/null || true
  [ -n "${RUNERR:-}" ] && rm -f "$RUNERR"
  [ -n "${RUNOUT:-}" ] && rm -f "$RUNOUT"
  [ -n "${KILL_STATE:-}" ] && rm -f "$KILL_STATE"
  _stand_in_notice
  trap - EXIT
  exit "$status"
}
trap _cleanup EXIT

if stat -f '%z:%m' "$0" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
elif stat -c '%s:%Y' "$0" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
else
  echo "claude-agent: neither BSD nor GNU stat interface is available" >&2
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
  _file_signature "$RUNOUT"
  printf '|'
  _file_signature "$RUNERR"
}

# Keep the historical function name, but sample the whole process group. The
# leader can be idle while a tool descendant is the active worker.
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
  kill -0 -- "-$CHILD_PGID" 2>/dev/null
}

_child_alive() {
  [ -n "${CHILD_PID:-}" ] && kill -0 "$CHILD_PID" 2>/dev/null
}

_wait_for_group_completion() {
  local announced=0
  while _group_alive; do
    if [ "$announced" -eq 0 ]; then
      echo "claude-agent: Claude leader exited $rc, but process group $CHILD_PGID still has live descendants; keeping supervision active until the group exits." >&2
      announced=1
    fi
    [ -s "$KILL_STATE" ] && return 0
    sleep 1
  done
}

_wait_for_stderr_stream() {
  [ -n "${TEE_PID:-}" ] || return 0
  set +e
  wait "$TEE_PID"
  STDERR_STREAM_RC=$?
  set -e
  TEE_PID=""
  if [ "$STDERR_STREAM_RC" -ne 0 ]; then
    echo "claude-agent: STDERR STREAM FAILURE: tee exited $STDERR_STREAM_RC; progress may be incomplete." >&2
  fi
}

_now() {
  date +%s
}

_print_kill_message() {
  local label="$1" elapsed="$2" detail="$3"
  echo "claude-agent: ${label} LIMIT FIRED after ${elapsed}s (${detail})." >&2
  echo "claude-agent: working directory: $DIR" >&2
  echo "claude-agent: killing Claude process group $CHILD_PGID; partial edits may be on disk." >&2
  echo "claude-agent: recovery: inspect git status before deciding whether to rerun the prompt." >&2
}

_terminate_group() {
  local term_started now since_term
  kill -TERM -- "-$CHILD_PGID" 2>/dev/null || true
  term_started="$(_now)"

  while _group_alive; do
    now="$(_now)"
    since_term=$((now - term_started))
    if [ "$since_term" -ge "$KILL_AFTER" ]; then
      echo "claude-agent: KILL-AFTER LIMIT FIRED after ${since_term}s; sending SIGKILL to process group $CHILD_PGID." >&2
      kill -KILL -- "-$CHILD_PGID" 2>/dev/null || true
      return
    fi
    sleep 1
  done
}

# The watchdog is stopped with SIGKILL, never TERM: a TERM that lands while bash is still forking
# the watchdog makes bash 5 run the wrapper's EXIT trap in it, deleting this run's files.
_watchdog() {
  local silence_interval="${CROSSFEED_TEST_SILENCE_INTERVAL_S:-600}" reported_silence=0
  [[ "$silence_interval" =~ ^[1-9][0-9]*$ ]] || silence_interval=600
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
    # Reporting never changes the child-output/CPU progress clock.
    if [ "$idle_for" -lt "$reported_silence" ]; then reported_silence=0; fi
    if [ "$((idle_for - reported_silence))" -ge "$silence_interval" ]; then
      echo "claude-agent: silent for $((idle_for / 60)) min (${idle_for}s); still running" >&2
      reported_silence="$idle_for"
    fi
    # Only an explicit positive --idle-timeout opts into termination.
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
    echo "claude-agent: EXTERNAL ${signal_name} RECEIVED after ${elapsed}s; no configured limit fired." >&2
    echo "claude-agent: working directory: $DIR" >&2
    echo "claude-agent: killing Claude process group $CHILD_PGID; partial edits may be on disk." >&2
    _terminate_group
    _wait_for_stderr_stream
    exit "$signal_code"
  fi
  return 0
}

# Job control makes the background Claude process a process-group leader. The
# watchdog signals that group, so shell/tool descendants cannot outlive a kill.
RUN_STARTED="$(_now)"
trap '_handle_signal HUP 129' HUP
trap '_handle_signal INT 130' INT
trap '_handle_signal TERM 143' TERM

if [ -n "$PENDING_SIGNAL_CODE" ]; then
  trap - HUP INT TERM
  exit "$PENDING_SIGNAL_CODE"
fi

# Keep tee in the wrapper's group, not Claude's group. The watchdog samples the
# same RUNERR bytes that are streamed live, and the wrapper explicitly reaps tee.
tee "$RUNERR" <"$RUNERR_PIPE" >&2 &
TEE_PID=$!

LAUNCHED=1
CROSSFEED_STARTED=1
set -m
(cd "$DIR" && exec claude "${args[@]}" -- "$PROMPT" </dev/null >"$RUNOUT" 2>"$RUNERR_PIPE") &
CHILD_PID=$!
if [ -n "$PENDING_SIGNAL_CODE" ]; then
  _handle_signal "$PENDING_SIGNAL_NAME" "$PENDING_SIGNAL_CODE"
fi

rc=0
if ! CHILD_PGID="$(_read_child_pgid "$CHILD_PID")"; then
  CHILD_PGID="$CHILD_PID"
  if _child_alive; then
    echo "claude-agent: PROCESS-GROUP DISCOVERY WARNING: ps did not return a PGID; supervising live child group $CHILD_PGID via the set -m PID=PGID invariant." >&2
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

# Every writer in Claude's group is now gone, so tee receives EOF, drains the
# sampled file to the caller, and is reaped before any result is interpreted.
_wait_for_stderr_stream
if [ "$STDERR_STREAM_RC" -ne 0 ] && [ "$rc" -eq 0 ]; then
  rc=1
fi

case "$(sed -n '1p' "$KILL_STATE")" in
  wall) rc=124;;
  idle) rc=125;;
esac

if [ "$rc" -ne 0 ]; then
  echo "claude-agent: claude exited $rc. output tail:" >&2
  tail -5 "$RUNOUT" >&2 || true
  exit "$rc"
fi

# Guard: an empty final message is never success. Claude can exit 0 having
# written nothing, so do not create an exists-but-empty --last deliverable.
if [ ! -s "$RUNOUT" ]; then
  echo "claude-agent: EMPTY OUTPUT with exit 0 - no model output (check auth, quota, or prompt)" >&2
  exit 4
fi

answer="$(python3 "$CROSSFEED_IDENTITY_SCRIPT" answer --path "$RUNOUT")" || exit 4
[ -n "$answer" ] || { echo "claude-agent: no final result in event stream" >&2; exit 4; }
printf '%s\n' "$answer"
if [ -n "$LAST" ]; then
  # Atomic publish, same as codex-agent.sh: a plain cp lets a reader observe a half-written
  # deliverable, and a failed copy leaves the caller's --last holding a previous run's answer.
  printf '%s\n' "$answer" >"$LAST.part.$$" && mv -f "$LAST.part.$$" "$LAST"
fi
exit 0

}
main "$@"; exit $?
