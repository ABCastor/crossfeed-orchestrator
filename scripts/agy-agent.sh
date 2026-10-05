#!/usr/bin/env bash
# agy-agent.sh - run one headless Antigravity (agy) agent on a folder and return its final message.
#
# The official `agy --print` drops stdout when stdout is not a TTY. This wrapper
# therefore keeps the deliberate Python pty.spawn bridge and captures its PTY.
#
# Usage:
#   agy-agent.sh --prompt "<task>" [--dir <workdir>] [--model <m>] [--sandbox]
#                [--lane <lane>] [--role <role>] [--effort <level>] [--timeout S]
#                [--idle-timeout S] [--kill-after S]
#                [--last <file>] [--raw <file>]
#
# --timeout has no default: absent means no wall-clock limit.
# --idle-timeout defaults to 0 (opt-in killing only); --kill-after defaults to 30 seconds.
# Prints the agent's final message to stdout. Diagnostics go to stderr.
# Exit codes: 0 success; 1 supervision setup failure; 2 usage or lane config;
# 3 quota exhausted; 4 empty output; 5 no route or lease; 124 wall-clock kill;
# 125 idle kill; 127 agy missing; every other AGY/Python exit is passed through.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -euo pipefail

DIR="$PWD"; PROMPT=""; MODEL=""; SANDBOX=0; LAST=""; RAW=""; LANE=""; ROLE=""
EFFORT=""
TIMEOUT=""
IDLE_TIMEOUT="0"
KILL_AFTER="30"

_usage_error() {
  echo "agy-agent: $1" >&2
  exit 2
}

_require_value() {
  [ "$#" -ge 2 ] || _usage_error "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt)       _require_value "$@"; PROMPT="$2"; shift 2;;
    --dir)          _require_value "$@"; DIR="$2"; shift 2;;
    --model)        _require_value "$@"; MODEL="$2"; shift 2;;
    --lane)         _require_value "$@"; LANE="$2"; shift 2;;
    --role)         _require_value "$@"; ROLE="$2"; shift 2;;
    --effort)       _require_value "$@"; EFFORT="$2"; shift 2;;
    --sandbox)      SANDBOX=1; shift;;
    --timeout)      _require_value "$@"; TIMEOUT="$2"; shift 2;;
    --idle-timeout) _require_value "$@"; IDLE_TIMEOUT="$2"; shift 2;;
    --kill-after)   _require_value "$@"; KILL_AFTER="$2"; shift 2;;
    --last)         _require_value "$@"; LAST="$2"; shift 2;;
    --raw)          _require_value "$@"; RAW="$2"; shift 2;;
    -h|--help)      sed -n '2,18p' "$0"; exit 0;;
    *)               _usage_error "unknown arg: $1";;
  esac
done
CALLER_MODEL="$MODEL"
CALLER_LANE="$LANE"

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

[ -n "$PROMPT" ] || _usage_error "--prompt is required"

# A model named with --model is checked against the Crossfeed console's switches before agy
# starts: one that is off, retired, or on a provider set to Off is refused (exit 5), with what to
# name instead. A lane's own model is also checked when its lease is taken (acquire). A model the
# roster does not list is not refused, and a gate that cannot run never blocks the dispatch.
if [ -n "$MODEL" ]; then
  _gate_rc=0; _gate_msg="$("$(dirname "$0")/fleetctl.py" model-gate "$MODEL" --harness agy 2>&1 >/dev/null)" || _gate_rc=$?
  if [ "$_gate_rc" = 5 ]; then
    echo "agy-agent: ${_gate_msg#fleetctl: }" >&2
    exit 5
  fi
fi
if ! command -v agy >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/agy" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo "agy-agent: agy binary not found (install: curl -fsSL https://antigravity.google/cli/install.sh | bash)" >&2
    exit 127
  fi
fi
mkdir -p "$DIR"
HERE="$(cd "$(dirname "$0")" && pwd)"

_parent_comm="$(ps -o comm= -p "$PPID" 2>/dev/null | tr -d '[:space:]' || true)"
case "${_parent_comm##*/}" in
  timeout|gtimeout)
    echo "agy-agent: WARNING - launched under an outer '${_parent_comm##*/}' wall-clock cap." >&2
    echo "agy-agent: that outer cap will kill this run regardless of progress, and the idle watchdog cannot prevent it." >&2
    echo "agy-agent: remove the outer wrapper; pass --timeout to this script if you genuinely need a hard budget." >&2
    ;;
esac

lease_token=""
ptyfile=""
KILL_STATE=""
PTY_CHILD_STATE=""

_cleanup() {
  local status=$?
  # Only the wrapper's own shell cleans up. With bash 5 a background subshell (the watchdog,
  # a job just forked) can run this inherited EXIT trap and delete the run's files early.
  [ "${BASHPID:-$$}" = "$$" ] || return 0
  crossfeed_finish "$status" || status=8
  [ -n "${ptyfile:-}" ] && rm -f "$ptyfile"
  [ -n "${KILL_STATE:-}" ] && rm -f "$KILL_STATE"
  [ -n "${PTY_CHILD_STATE:-}" ] && rm -f "$PTY_CHILD_STATE"
  if [ -n "${lease_token:-}" ]; then
    "$HERE/fleetctl.py" release --token "$lease_token" >/dev/null 2>&1 || true
  fi
  trap - EXIT
  exit "$status"
}
trap _cleanup EXIT

# A roster lane resolves the model and takes a quota lease. With no wall limit,
# keep the pre-contract 600s lease TTL rather than passing an empty value; an
# explicit --timeout continues to size the lease exactly as it did before.
if [ -z "$LANE" ] && [ -z "$MODEL" ]; then
  LANE="$("$HERE/fleetctl.py" route --role "${ROLE:-default}" --mode read-only --harness agy)" || {
    echo "agy-agent: no eligible agy lane for role $ROLE" >&2
    exit 5
  }
fi

# An explicit model owns its own quota lane. A role is an effort selector here,
# never permission to reserve an unrelated model's pool. Gemini levels share a
# model and quota pool; a caller may use a supported level absent from auto-routing.
_model_lane() {
  local selector="$1" candidate level
  if candidate="$("$HERE/roster.sh" lookup agy "$selector" 2>/dev/null)" &&
     "$HERE/roster.sh" check-lane "$candidate" read-only >/dev/null 2>&1; then
    printf '%s\n' "$candidate"; return
  fi
  case "$selector" in
    gemini-*-low|gemini-*-medium|gemini-*-high|gemini-*-max)
      for level in medium high low max; do
        if candidate="$("$HERE/roster.sh" lookup agy "${selector%-*}-$level" 2>/dev/null)" &&
           "$HERE/roster.sh" check-lane "$candidate" read-only >/dev/null 2>&1; then
          printf '%s\n' "$candidate"; return
        fi
      done;;
  esac
  return 1
}
if [ -n "$MODEL" ] && [ -z "$LANE" ]; then
  LANE="$(_model_lane "$MODEL")" || _usage_error "no admitted AGY model family for $MODEL"
fi
if [ -n "$LANE" ]; then
  lane_json="$("$HERE/roster.sh" lane-json "$LANE")" || {
      echo "agy-agent: unknown lane: $LANE (see roster message above; try 'roster.sh list agy')" >&2
      exit 2
  }
  lane_model="$(jq -r '.selector // empty' <<<"$lane_json")"
  [ -n "$lane_model" ] || _usage_error "lane $LANE has no selector"
  "$HERE/roster.sh" check-lane "$LANE" read-only >/dev/null || _usage_error "AGY lane $LANE is not admitted"
  if [ -z "$MODEL" ]; then
    MODEL="$lane_model"
  elif [ "$MODEL" != "$lane_model" ]; then
    case "$MODEL" in
      gemini-*-low|gemini-*-medium|gemini-*-high|gemini-*-max)
        [ "${MODEL%-*}" = "${lane_model%-*}" ] || _usage_error "model $MODEL does not belong to lane $LANE";;
      *) _usage_error "model $MODEL does not belong to lane $LANE";;
    esac
  fi
fi

if [ -z "$MODEL" ]; then
  _usage_error "cannot resolve AGY model"
fi
# The suffix was AGY's level control before --effort existed here. Preserve a
# caller's named model/lane level, while automatically routed models use the table.
if [ -z "$EFFORT" ] && { [ -n "$CALLER_MODEL" ] || [ -n "$CALLER_LANE" ]; }; then
  case "$MODEL" in
    gemini-*-low|gemini-*-medium|gemini-*-high|gemini-*-max) EFFORT="${MODEL##*-}";;
  esac
fi
effort_args=( effort "$MODEL" "${ROLE:-default}" --harness agy --explain )
[ -z "$EFFORT" ] || effort_args+=( --level "$EFFORT" )
effort_row="$("$HERE/fleetctl.py" "${effort_args[@]}")" || _usage_error "cannot resolve AGY effort"
IFS=$'\t' read -r EFFORT _effort_model _effort_why <<<"$effort_row"
# Gemini's selector is the level control, and must agree with --effort.
case "$MODEL" in
  gemini-*-low|gemini-*-medium|gemini-*-high|gemini-*-max)
    _old_model="$MODEL"; MODEL="${MODEL%-*}-$EFFORT"
    if [ -n "$LANE" ] && [ "$MODEL" != "$_old_model" ]; then
      LANE="$(_model_lane "$MODEL")" || _usage_error "no admitted AGY model family for $MODEL"
    fi;;
esac
echo "agy-agent: effort $EFFORT: $_effort_why" >&2

if [ -n "$LANE" ]; then
  # The lease is a QUOTA reservation, not a wall clock. It used to default to the wrapper's old
  # hidden 600s cap; with no wall clock that fallback would quietly expire the lease mid-run and
  # let a second dispatch take the slot. Size it to the liveness bound instead, which is the real
  # upper bound on how long this run can sit before the watchdog ends it.
  # A lease is a SLOT RESERVATION, not a deadline. Sizing it to the idle window was wrong: a healthy
  # uncapped run that keeps emitting outlives its own lease and a second dispatch enters the lane.
  # Over-holding costs throughput on an abundant pool and dead holders are reaped anyway;
  # under-holding is a correctness bug. 4h was still outlivable by a healthy uncapped run.
  # The proper fix is parent-owned lease RENEWAL; until then this errs long deliberately.
  lease_ttl="${TIMEOUT:-86400}"
  if ! lease_token="$("$HERE/fleetctl.py" acquire --lane "$LANE" --ttl "$lease_ttl" 2>&1)"; then
    echo "agy-agent: lease refused for $LANE: $lease_token" >&2
    exit 5
  fi
fi

crossfeed_prepare agy

# --add-dir is the actual workspace grant. Merely changing directory is not.
agy_args=( agy --print "$PROMPT" --add-dir "$DIR" --dangerously-skip-permissions )
[ -n "$MODEL" ] && agy_args+=( --model "$MODEL" )
[ "$EFFORT" = provider-default ] || agy_args+=( --effort "$EFFORT" )
[ "$SANDBOX" = "1" ] && agy_args+=( --sandbox )

ptyfile="$(mktemp "${TMPDIR:-/tmp}/agy-agent.pty.XXXXXX")"
KILL_STATE="$(mktemp "${TMPDIR:-/tmp}/agy-agent.kill.XXXXXX")"
PTY_CHILD_STATE="$(mktemp "${TMPDIR:-/tmp}/agy-agent.child.XXXXXX")"
BRIDGE_PID=""
BRIDGE_PGID=""
PTY_CHILD_PID=""
CHILD_PGID=""
WATCHDOG_PID=""
BRIDGE_COMPLETED=0
PENDING_SIGNAL_NAME=""
PENDING_SIGNAL_CODE=""

if stat -f '%z:%m' "$0" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
elif stat -c '%s:%Y' "$0" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
else
  echo "agy-agent: neither BSD nor GNU stat interface is available" >&2
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
  _file_signature "$ptyfile"
}

# Keep the historical function name, but sample the full forkpty child group.
# AGY frequently waits in its leader while a tool descendant does the work.
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

_read_bridge_pgid() {
  local pgid="" attempts=0
  while [ "$attempts" -lt 20 ]; do
    pgid="$(ps -o pgid= -p "$BRIDGE_PID" 2>/dev/null | tr -d '[:space:]' || true)"
    case "$pgid" in
      ''|*[!0-9]*) ;;
      *) printf '%s' "$pgid"; return 0;;
    esac
    attempts=$((attempts + 1))
    sleep 0.05
  done
  return 1
}

_read_pty_child_pid() {
  local pid="" attempts=0
  while [ "$attempts" -lt 40 ]; do
    pid="$(sed -n '1p' "$PTY_CHILD_STATE" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) printf '%s' "$pid"; return 0;;
    esac
    attempts=$((attempts + 1))
    sleep 0.05
  done
  return 1
}

_read_pty_child_pid_once() {
  local pid=""
  pid="$(sed -n '1p' "$PTY_CHILD_STATE" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) return 1;;
    *) printf '%s' "$pid";;
  esac
}

_group_alive() {
  [ -n "${CHILD_PGID:-}" ] && kill -0 -- "-$CHILD_PGID" 2>/dev/null
}

_bridge_group_alive() {
  [ -n "${BRIDGE_PGID:-}" ] && kill -0 -- "-$BRIDGE_PGID" 2>/dev/null
}

_bridge_alive() {
  [ -n "${BRIDGE_PID:-}" ] && kill -0 "$BRIDGE_PID" 2>/dev/null
}

_wait_for_group_completion() {
  local announced=0
  while _group_alive; do
    if [ "$announced" -eq 0 ]; then
      echo "agy-agent: AGY leader and PTY bridge exited $rc, but process group $CHILD_PGID still has live descendants; keeping supervision active until the group exits." >&2
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
  echo "agy-agent: ${label} LIMIT FIRED after ${elapsed}s (${detail})." >&2
  echo "agy-agent: working directory: $DIR" >&2
  echo "agy-agent: killing AGY process group $CHILD_PGID behind PTY bridge group $BRIDGE_PGID; partial edits may be on disk." >&2
  echo "agy-agent: recovery: inspect git status and the raw PTY capture before deciding whether to rerun." >&2
}

_terminate_group() {
  local term_started now since_term
  # pty.spawn's forkpty child creates its own session. TERM must therefore go
  # to AGY's group, not merely to the Python bridge's distinct process group.
  [ -n "$CHILD_PGID" ] && kill -TERM -- "-$CHILD_PGID" 2>/dev/null || true
  [ -n "$BRIDGE_PGID" ] && kill -TERM -- "-$BRIDGE_PGID" 2>/dev/null || true
  term_started="$(_now)"

  while _group_alive || _bridge_group_alive; do
    now="$(_now)"
    since_term=$((now - term_started))
    if [ "$since_term" -ge "$KILL_AFTER" ]; then
      echo "agy-agent: KILL-AFTER LIMIT FIRED after ${since_term}s; sending SIGKILL to AGY process group $CHILD_PGID and PTY bridge group $BRIDGE_PGID." >&2
      kill -KILL -- "-$CHILD_PGID" 2>/dev/null || true
      kill -KILL -- "-$BRIDGE_PGID" 2>/dev/null || true
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
      echo "agy-agent: silent for $((idle_for / 60)) min (${idle_for}s); still running" >&2
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

  if [ -z "$CHILD_PGID" ]; then
    PTY_CHILD_PID="$(_read_pty_child_pid_once 2>/dev/null || true)"
    [ -n "$PTY_CHILD_PID" ] && CHILD_PGID="$PTY_CHILD_PID"
  fi
  if [ -z "$BRIDGE_PGID" ] && _bridge_alive; then
    BRIDGE_PGID="$BRIDGE_PID"
  fi

  if _group_alive || _bridge_group_alive; then
    trap - HUP INT TERM
    now="$(_now)"
    elapsed=$((now - RUN_STARTED))
    echo "agy-agent: EXTERNAL ${signal_name} RECEIVED after ${elapsed}s; no configured limit fired." >&2
    echo "agy-agent: working directory: $DIR" >&2
    if [ -n "$CHILD_PGID" ]; then
      echo "agy-agent: killing AGY process group $CHILD_PGID behind PTY bridge group $BRIDGE_PGID; partial edits may be on disk." >&2
    else
      echo "agy-agent: AGY child state is not published yet; terminating PTY bridge group $BRIDGE_PGID, whose signal handler owns any forkpty child." >&2
    fi
    _terminate_group
    exit "$signal_code"
  fi
  return 0
}

# Keep pty.spawn, but monkey-patch its fork hook so the shell supervisor learns
# the actual AGY PID. forkpty makes that PID the leader of a new session/group;
# recording it closes the otherwise-fatal gap where killing Python misses AGY.
_pty='import os, pty, signal, sys
state_path = sys.argv[1]
argv = sys.argv[2:]
original_fork = pty.fork
child_pid = 0
forwarded_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
def forward_signal(signum, _frame):
    if child_pid > 0:
        try:
            os.killpg(child_pid, signum)
        except ProcessLookupError:
            pass
    raise SystemExit(128 + signum)
for forwarded_signal in forwarded_signals:
    signal.signal(forwarded_signal, forward_signal)
def tracked_fork():
    global child_pid
    signal.pthread_sigmask(signal.SIG_BLOCK, forwarded_signals)
    pid, master_fd = original_fork()
    if pid > 0:
        child_pid = pid
        with open(state_path, "w") as handle:
            handle.write(str(pid))
    signal.pthread_sigmask(signal.SIG_UNBLOCK, forwarded_signals)
    return pid, master_fd
def eof_stdin(_fd):
    return b""
pty.fork = tracked_fork
sys.exit(os.waitstatus_to_exitcode(pty.spawn(argv, stdin_read=eof_stdin)))'

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
(cd "$DIR" && exec python3 -c "$_pty" "$PTY_CHILD_STATE" "${agy_args[@]}" </dev/null) >"$ptyfile" 2>&1 &
BRIDGE_PID=$!
if [ -n "$PENDING_SIGNAL_CODE" ]; then
  _handle_signal "$PENDING_SIGNAL_NAME" "$PENDING_SIGNAL_CODE"
fi

rc=0
if ! BRIDGE_PGID="$(_read_bridge_pgid)"; then
  if _bridge_alive; then
    BRIDGE_PGID="$BRIDGE_PID"
    echo "agy-agent: PROCESS-GROUP DISCOVERY WARNING: ps did not return the live bridge PGID; using the set -m PID=PGID invariant for bridge group $BRIDGE_PGID." >&2
  else
    set +e
    wait "$BRIDGE_PID"
    rc=$?
    set -e
    BRIDGE_COMPLETED=1
  fi
fi
if [ "$BRIDGE_COMPLETED" -eq 1 ]; then
  PTY_CHILD_PID="$(_read_pty_child_pid_once 2>/dev/null || true)"
  [ -n "$PTY_CHILD_PID" ] && CHILD_PGID="$PTY_CHILD_PID"
fi
if [ "$BRIDGE_COMPLETED" -eq 0 ]; then
  if ! PTY_CHILD_PID="$(_read_pty_child_pid)"; then
    echo "agy-agent: PROCESS-GROUP DISCOVERY FAILURE after 0s; no configured limit fired." >&2
    echo "agy-agent: working directory: $DIR" >&2
    echo "agy-agent: PTY bridge did not report AGY's process-group leader; terminating bridge group $BRIDGE_PGID." >&2
    kill -TERM -- "-$BRIDGE_PGID" 2>/dev/null || true
    wait "$BRIDGE_PID" 2>/dev/null || true
    set +m
    exit 1
  fi
  # POSIX forkpty/login_tty makes the PTY child a session and process-group leader.
  CHILD_PGID="$PTY_CHILD_PID"
fi
set +m

if [ "$BRIDGE_COMPLETED" -eq 0 ] || _group_alive; then
  _watchdog &
  WATCHDOG_PID=$!

  if [ "$BRIDGE_COMPLETED" -eq 0 ]; then
    set +e
    wait "$BRIDGE_PID"
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
BRIDGE_PID=""

case "$(sed -n '1p' "$KILL_STATE")" in
  wall) rc=124;;
  idle) rc=125;;
esac

[ -n "$RAW" ] && cp "$ptyfile" "$RAW"

# Strip ANSI CSI / OSC / charset escapes and carriage returns.
_clean() { sed -E 's/\x1b\[[0-9;?]*[A-Za-z]//g; s/\x1b\][0-9;]*(\x07|\x1b\\)?//g; s/\x1b[()][AB0]//g; s/\r//g' "$1"; }

if [ "$rc" -ne 0 ]; then
  echo "agy-agent: agy exited $rc. output tail:" >&2
  _clean "$ptyfile" | tail -6 >&2
  exit "$rc"
fi

# In --print mode the only stdout content is the final response. Trim only the
# leading/trailing blank lines introduced by the PTY path.
answer="$(_clean "$ptyfile" | awk '
  { line[NR] = $0 }
  END {
    start = 1; end = NR
    while (start <= end && line[start] ~ /^[[:space:]]*$/) start++
    while (end >= start && line[end] ~ /^[[:space:]]*$/) end--
    for (i = start; i <= end; i++) print line[i]
  }')"

# AGY can hide quota errors behind exit 0 plus empty PTY output. Preserve the
# distinct quota and generic-empty outcomes used by fleet callers.
if [ -z "$answer" ]; then
  cli_log="$(ls -t "$HOME"/.gemini/antigravity-cli/cli.log \
                   "$HOME"/.gemini/antigravity-cli/log/*.log 2>/dev/null | head -1 || true)"
  if [ -n "$cli_log" ] && grep -q 'RESOURCE_EXHAUSTED\|Individual quota reached' "$cli_log" 2>/dev/null; then
    echo "agy-agent: AGY QUOTA EXHAUSTED (429) - empty output; marker in $cli_log" >&2
    exit 3
  fi
  echo "agy-agent: EMPTY OUTPUT with exit 0 - no model output (check auth, quota, or prompt)" >&2
  exit 4
fi

printf '%s\n' "$answer"
[ -n "$LAST" ] && printf '%s\n' "$answer" >"$LAST"
exit 0

}
main "$@"; exit $?
