#!/usr/bin/env bash
# codex-agent.sh - run ONE headless Codex agent on a folder and return its final message.
#
# Layer-2 (headless-agent): drives the OFFICIAL `codex` binary in non-interactive
# `exec` mode, authenticating as the ChatGPT subscription via ~/.codex/auth.json
# (OAuth, auth_mode=ChatGPT). The model stays inside the product it was sold in.
# This is the path the June-2026 research found PERMITTED across vendors. It is
# NOT model-extraction/proxy (layer-3), which is what gets accounts banned.
#
# Usage:
#   codex-agent.sh --prompt "<task>" [--dir <workdir>] [--sandbox MODE]
#                  [--model gpt-6-astra] [--role <role>] [--reasoning xhigh] [--schema <file.json>]
#                  [-c KEY=VALUE ...] [--timeout S] [--idle-timeout S] [--kill-after S]
#                  [--events <file.jsonl>] [--last <file.txt>] [--log <file>] [--dry-run]
#
#   The model: the models switched on in the Crossfeed console decide. --model is passed as given
#   while that model is on; when it is off, the nearest model that is on runs instead (cheaper side
#   first) and stderr says so; with none on the run is refused (exit 5). No --model resolves the
#   configured model or the roster model default. Every run pins its roster or caller effort.
#   --dry-run prints the exact codex command this run would start (the prompt left out) and exits 0:
#   no slot is taken, no directory is created, nothing is sent.
#
#   MODE = read-only | workspace-write (default) | danger-full-access
#   --timeout has no default: absent means no wall-clock limit.
#   --idle-timeout defaults to 0 (opt-in killing only); --kill-after defaults to 30 seconds.
#
# Silence can be healthy server-side reasoning. Default supervision reports silence every
# ten minutes; only an explicit --idle-timeout opts into killing a silent process group.
# Output growth or process-group CPU progress resets the silence diagnostic.
# Owner 2026-10-04: no wasted work.
#
# Prints the agent's FINAL message to stdout. Diagnostics go to stderr.
# Exit codes: 0 success; 2 usage; 4 exited 0 with a blank deliverable; 124 wall-clock kill; 125 idle kill;
# 127 codex binary missing; every other codex exit code is passed through.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -euo pipefail
if [ "$("$(dirname "$0")/fleetctl.py" switch codex 2>/dev/null)" = off ]; then
  echo "codex-agent: the codex pool is switched off by hand (fleetctl.py switch codex auto)" >&2; exit 5
fi

# DIR is where Codex WRITES, not just an anchor (unlike Claude Code's cwd): codex exec gets it
# via -C below and drops task files there. Default $PWD keeps the pin on the caller's repo;
# for throwaway work pass --dir ~/.codex/scratch - never bare ~ (it clutters home).
# Convention + why: references/usage.md section 3, "Codex writes INTO its working folder".
DIR="$PWD"; PROMPT=""; PROMPT_FILE=""; SANDBOX="workspace-write"; MODEL=""; SCHEMA=""
TIMEOUT=""; IDLE_TIMEOUT="0"; KILL_AFTER="30"
EVENTS=""; LAST=""; REASONING=""; ROLE="default"; LOG=""; DRY_RUN=0

_usage_error() {
  echo "codex-agent: $1" >&2
  exit 2
}

_require_value() {
  [ "$#" -ge 2 ] || _usage_error "$1 requires a value"
}

CONFIGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt)       _require_value "$@"; PROMPT="$2"; shift 2;;
    --prompt-file)  _require_value "$@"; PROMPT_FILE="$2"; shift 2;;
    --dir)          _require_value "$@"; DIR="$2"; shift 2;;
    --sandbox)      _require_value "$@"; SANDBOX="$2"; shift 2;;
    --model)        _require_value "$@"; MODEL="$2"; shift 2;;
    --reasoning)    _require_value "$@"; REASONING="$2"; shift 2;;
    --role)         _require_value "$@"; ROLE="$2"; shift 2;;
    --schema)       _require_value "$@"; SCHEMA="$2"; shift 2;;
    --timeout)      _require_value "$@"; TIMEOUT="$2"; shift 2;;
    --idle-timeout) _require_value "$@"; IDLE_TIMEOUT="$2"; shift 2;;
    --kill-after)   _require_value "$@"; KILL_AFTER="$2"; shift 2;;
    --events)       _require_value "$@"; EVENTS="$2"; shift 2;;
    --last)         _require_value "$@"; LAST="$2"; shift 2;;
    --log)          _require_value "$@"; LOG="$2"; shift 2;;
    -c|--config)    _require_value "$@"; CONFIGS+=( "$2" ); shift 2;;
    --dry-run)      DRY_RUN=1; shift;;
    -h|--help)      sed -n '2,37p' "$0"; exit 0;;
    *)              _usage_error "unknown arg: $1";;
  esac
done

# -m/--model takes precedence over config, but config takes precedence over
# saved defaults. Resolve it before asking the console for a stand-in.
if [ -z "$MODEL" ]; then
  for _cfg in ${CONFIGS+"${CONFIGS[@]}"}; do
    if [[ "$_cfg" =~ ^[[:space:]]*model[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      MODEL="${BASH_REMATCH[1]}"; MODEL="${MODEL%%#*}"
      MODEL="${MODEL//\"/}"; MODEL="${MODEL//\'/}"; MODEL="${MODEL//[[:space:]]/}"
    fi
  done
fi
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

# --prompt-file: read the prompt from a file (used by ShellAgentProposer bridge).
if [ -n "$PROMPT_FILE" ] && [ -z "$PROMPT" ]; then
  [ -f "$PROMPT_FILE" ] || _usage_error "prompt-file not found: $PROMPT_FILE"
  PROMPT="$(cat "$PROMPT_FILE")"
fi

[ -n "$PROMPT" ] || _usage_error "--prompt or --prompt-file is required"

# Low means one run at a time on this pool. The gate holds the pool's slot for this process and
# frees it when the process ends; a second run waits (FLEET_POOL_WAIT_S, default 900s), then
# refuses with exit 5. A gate that cannot run at all never blocks the dispatch. A dry run starts
# nothing, so it takes no slot and never waits.
if [ "$DRY_RUN" != 1 ]; then
  _gate_rc=0; "$(dirname "$0")/fleetctl.py" pool-slot codex --pid $$ || _gate_rc=$?
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
_pick_rc=0; _pick="$("$(dirname "$0")/fleetctl.py" model-run codex "$MODEL" --explain 2>/dev/null)" || _pick_rc=$?
if [ "$_pick_rc" = 5 ]; then
  echo "codex-agent: no model is switched on for codex in the Crossfeed console (fleetctl.py model-toggle codex <model> on)" >&2; exit 5
fi
_picked=""; _asked=""; _why=""
read -r _picked _asked _why <<<"$_pick" || true
case "$_picked" in ''|*[!A-Za-z0-9._:/-]*) _picked="";; esac
case "$_asked" in ''|-|*[!A-Za-z0-9._:/-]*) _asked="";; esac
if [ -n "$_picked" ] && [ "$_picked" != "$MODEL" ]; then
  if [ -n "$MODEL" ]; then
    echo "codex-agent: model $_picked, the nearest one switched on in the Crossfeed console ($MODEL is off)" >&2
  else
    echo "codex-agent: model $_picked, the nearest one switched on in the Crossfeed console" >&2
  fi
  case "$_why" in
    retired)  _because="retired by its provider";;
    unlisted) _because="not a model Crossfeed lists, and some models are switched off in the console";;
    *)        _because="switched off in the console";;
  esac
  STAND_IN_NOTICE="Crossfeed: this run used $_picked, not ${_asked:-${MODEL:-the default model}} (${_because}). Say $_picked when you report it."
  MODEL="$_picked"
fi
# Said last, after everything else this wrapper prints, but only for a run that started codex:
# a run refused before launch used no model at all.
LAUNCHED=0
_stand_in_notice() {
  [ -n "$STAND_IN_NOTICE" ] && [ "$LAUNCHED" = 1 ] && echo "$STAND_IN_NOTICE" >&2
  return 0
}

if [ "$DRY_RUN" != 1 ]; then
  command -v codex >/dev/null 2>&1 || { echo "codex-agent: codex binary not on PATH" >&2; exit 127; }
fi

# Dispatch banner - lane facts every dispatch must know.
echo "codex-agent [lane]: Codex REFUSES adversarial-security work (exploit/leak reproduction) - keep that on Claude (gpt-lane-refuses-adversarial-security)." >&2
echo "codex-agent [lane]: Codex LACKS Claude-side plumbing (autocommit, snapshots, destructive guards, ctx%) - see roster codex-lane notes before relying on harness features (codex-capability-map)." >&2
# OUTER-CAP DETECTION. A file scanner cannot see call sites, so `timeout 2400 ./codex-agent.sh ...`
# sits outside any static check. That form is not hypothetical: it is the incident this wrapper was
# rebuilt around, where the caller asked for 40 minutes and a hidden inner default killed the run
# at 601s. If our parent is a timeout binary, say so loudly, because the outer cap overrides the
# watchdog and will kill this run on a clock that knows nothing about whether it is making progress.
_parent_comm="$(ps -o comm= -p "$PPID" 2>/dev/null | tr -d '[:space:]' || true)"
case "${_parent_comm##*/}" in
  timeout|gtimeout)
    echo "codex-agent: WARNING - launched under an outer '${_parent_comm##*/}' wall-clock cap." >&2
    echo "codex-agent: that outer cap will kill this run regardless of progress, and the idle watchdog cannot prevent it." >&2
    echo "codex-agent: remove the outer wrapper; pass --timeout to this script if you genuinely need a hard budget." >&2
    ;;
esac

# Creating the caller's workspace silently turns a mistyped, deleted, or renamed worktree path into
# a fresh empty directory, and the agent then does its work somewhere nobody is looking. Still
# create it (callers rely on that), but refuse a path whose PARENT is missing, which is what a real
# typo looks like, and say so loudly when a directory had to be created.
if [ "$DRY_RUN" != 1 ] && [ ! -d "$DIR" ]; then
  _parent_dir="$(dirname "$DIR")"
  [ -d "$_parent_dir" ] || { echo "codex-agent: refusing to create '$DIR': its parent '$_parent_dir' does not exist (mistyped path?)" >&2; exit 2; }
  echo "codex-agent: NOTE - working directory '$DIR' did not exist and was created. If you expected an existing worktree, this run is writing to the wrong place." >&2
  mkdir -p "$DIR"
fi

# The final message ALWAYS lands in a run-private file first. Writing straight into the caller's
# --last means a run that exits 0 without producing a message leaves the PREVIOUS run's answer in
# place, and the wrapper then prints stale bytes as this run's deliverable. Reproduced during the
# audit with a fake codex that only ran `exit 0`.
RUN_LAST="$(mktemp "${TMPDIR:-/tmp}/codex-agent.runlast.XXXXXX")"
_tmp_last=""
if [ -z "$LAST" ]; then
  _tmp_last="$(mktemp "${TMPDIR:-/tmp}/codex-agent.last.XXXXXX")"
  LAST="$_tmp_last"
fi

# Full run output (banner + stderr) goes here. If --log is given, keep it for the caller;
# otherwise use a temp file. Never delete the caller's --log file.
_tmp_events=""
_tmp_runout=""
if [ -n "$LOG" ]; then
  RUNOUT="$LOG"
else
  _tmp_runout="$(mktemp "${TMPDIR:-/tmp}/codex-agent.runout.XXXXXX")"
  RUNOUT="$_tmp_runout"
fi
KILL_STATE="$(mktemp "${TMPDIR:-/tmp}/codex-agent.kill.XXXXXX")"

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
  [ -n "$_tmp_events" ] && rm -f "$_tmp_events"
  [ -n "$_tmp_runout" ] && rm -f "$_tmp_runout"
  [ -n "$_tmp_last" ] && rm -f "$_tmp_last"
  [ -n "${RUN_LAST:-}" ] && rm -f "$RUN_LAST"
  [ -n "${KILL_STATE:-}" ] && rm -f "$KILL_STATE"
  _stand_in_notice
  trap - EXIT
  exit "$status"
}
trap _cleanup EXIT

# Resolve after model stand-ins, so evidence belongs to the model that will run.
if [ -z "$REASONING" ]; then
  for _cfg in ${CONFIGS+"${CONFIGS[@]}"}; do
    if [[ "$_cfg" =~ ^[[:space:]]*model_reasoning_effort[[:space:]]*=[[:space:]]*(.*)$ ]]; then
      REASONING="${BASH_REMATCH[1]}"; REASONING="${REASONING%%#*}"
      REASONING="${REASONING//\"/}"; REASONING="${REASONING//\'/}"; REASONING="${REASONING//[[:space:]]/}"
    fi
  done
fi
effort_args=( effort "$MODEL" "$ROLE" --harness codex --explain )
[ -z "$REASONING" ] || effort_args+=( --level "$REASONING" )
[ -z "$STAND_IN_NOTICE" ] || effort_args+=( --stand-in )
effort_row="$("$(dirname "$0")/fleetctl.py" "${effort_args[@]}")" || _usage_error "cannot resolve reasoning effort"
IFS=$'\t' read -r REASONING _effort_model _effort_why <<<"$effort_row"
[ -n "$REASONING" ] && [ "$REASONING" != provider-default ] || _usage_error "Codex requires a resolved reasoning effort"
[ -n "$MODEL" ] || MODEL="$_effort_model"
echo "codex-agent: effort $REASONING: $_effort_why" >&2

args=( exec --skip-git-repo-check -C "$DIR" -s "$SANDBOX" -o "$RUN_LAST" --color never )
[ -n "$MODEL" ] && args+=( -m "$MODEL" )
[ -n "$SCHEMA" ] && args+=( --output-schema "$SCHEMA" )
[ -n "$EVENTS" ] && args+=( --json )
for _cfg in ${CONFIGS+"${CONFIGS[@]}"}; do args+=( -c "$_cfg" ); done
args+=( -c "model_reasoning_effort=\"$REASONING\"" )

# The local overlay names tool-source directories and their live MCP registrations.
# Append denials after caller configs so a worker cannot accidentally enable the tool it builds.
mcp_denials="$("$(dirname "$0")/fleetctl.py" codex-mcp-denials "$DIR")" || _usage_error "cannot resolve worker MCP denials"
while IFS= read -r _mcp_server; do
  [ -n "$_mcp_server" ] || continue
  args+=( -c "mcp_servers.$_mcp_server.enabled=false" )
  echo "codex-agent: live MCP tool $_mcp_server disabled for this worker directory" >&2
done <<<"$mcp_denials"

if [ "$DRY_RUN" = 1 ]; then
  # The very array the run below would start codex with, so the two cannot drift apart.
  echo "codex-agent: dry run, nothing started. The command:" >&2
  printf '%q ' codex "${args[@]}"
  printf '%s\n' '<prompt>'
  [ -z "$STAND_IN_NOTICE" ] || echo "${STAND_IN_NOTICE/this run used/this run would use}" >&2
  exit 0
fi

if [ -z "$EVENTS" ]; then
  _tmp_events="$(mktemp "${TMPDIR:-/tmp}/codex-model-events.XXXXXX")"
  EVENTS="$_tmp_events"
  args+=( --json )
fi
crossfeed_prepare codex

if stat -f '%z:%m' "$0" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
elif stat -c '%s:%Y' "$0" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
else
  echo "codex-agent: neither BSD nor GNU stat interface is available" >&2
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
  _file_signature "$RUN_LAST"
  if [ -n "$EVENTS" ]; then
    printf '|'
    _file_signature "$EVENTS"
  fi
}

# Keep the historical function name, but sample the whole process group. The
# CLI leader commonly waits while a tool descendant does the real CPU work.
# One ps+awk pass keeps the one-second poll cheap and works with macOS/BSD ps.
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
  kill -0 -- "-$1" 2>/dev/null
}

_child_alive() {
  [ -n "${CHILD_PID:-}" ] && kill -0 "$CHILD_PID" 2>/dev/null
}

_wait_for_group_completion() {
  local announced=0
  while _group_alive "$CHILD_PGID"; do
    if [ "$announced" -eq 0 ]; then
      echo "codex-agent: Codex leader exited $rc, but process group $CHILD_PGID still has live descendants; keeping supervision active until the group exits." >&2
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
  echo "codex-agent: ${label} LIMIT FIRED after ${elapsed}s (${detail})." >&2
  echo "codex-agent: working directory: $DIR" >&2
  echo "codex-agent: killing Codex process group $CHILD_PGID; partial edits may be on disk." >&2
  echo "codex-agent: recovery: inspect git status first. 'codex exec resume --last' is BEST-EFFORT only:" >&2
  echo "codex-agent:   it resumes the globally most recent session, so if any other codex run overlapped this one" >&2
  echo "codex-agent:   it will continue the WRONG task. Resume by explicit session id when runs overlap." >&2
}

_terminate_group() {
  local term_started now since_term
  kill -TERM -- "-$CHILD_PGID" 2>/dev/null || true
  term_started="$(_now)"

  while _group_alive "$CHILD_PGID"; do
    now="$(_now)"
    since_term=$((now - term_started))
    if [ "$since_term" -ge "$KILL_AFTER" ]; then
      echo "codex-agent: KILL-AFTER LIMIT FIRED after ${since_term}s; sending SIGKILL to process group $CHILD_PGID." >&2
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
  last_cpu="$(_child_cpu_ticks "$CHILD_PID")"

  while _group_alive "$CHILD_PGID"; do
    sleep 1
    _group_alive "$CHILD_PGID" || return 0
    now="$(_now)"
    elapsed=$((now - started))

    if [ -n "$TIMEOUT" ] && [ "$elapsed" -ge "$TIMEOUT" ]; then
      printf 'wall\n' >"$KILL_STATE"
      _print_kill_message "WALL-CLOCK" "$elapsed" "configured --timeout ${TIMEOUT}s"
      _terminate_group
      return 0
    fi

    current_output="$(_output_signature)"
    current_cpu="$(_child_cpu_ticks "$CHILD_PID")"
    if [ "$current_output" != "$last_output" ] || [ "$current_cpu" != "$last_cpu" ]; then
      last_activity="$now"
    fi
    last_output="$current_output"
    last_cpu="$current_cpu"

    idle_for=$((now - last_activity))
    # Reporting never changes the child-output/CPU progress clock.
    if [ "$idle_for" -lt "$reported_silence" ]; then reported_silence=0; fi
    if [ "$((idle_for - reported_silence))" -ge "$silence_interval" ]; then
      echo "codex-agent: silent for $((idle_for / 60)) min (${idle_for}s); still running" >&2
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

  # Between `command &` and PGID sampling, set -m already guarantees PID=PGID.
  # Use that invariant only while the child still exists. If it has already
  # exited, leave it for the normal wait path so its real status is not lost.
  if [ -z "$CHILD_PGID" ] && _child_alive; then
    CHILD_PGID="$CHILD_PID"
  fi
  if [ -n "$CHILD_PGID" ] && _group_alive "$CHILD_PGID"; then
    trap - HUP INT TERM
    now="$(_now)"
    elapsed=$((now - RUN_STARTED))
    echo "codex-agent: EXTERNAL ${signal_name} RECEIVED after ${elapsed}s; no configured limit fired." >&2
    echo "codex-agent: working directory: $DIR" >&2
    echo "codex-agent: killing Codex process group $CHILD_PGID; partial edits may be on disk." >&2
    echo "codex-agent: recovery: inspect git status first. 'codex exec resume --last' is BEST-EFFORT only:" >&2
  echo "codex-agent:   it resumes the globally most recent session, so if any other codex run overlapped this one" >&2
  echo "codex-agent:   it will continue the WRONG task. Resume by explicit session id when runs overlap." >&2
    _terminate_group
    exit "$signal_code"
  fi
  # No launch state yet, or the child completed before the signal was handled.
  # The post-launch checkpoint or normal wait path will resolve it safely.
  return 0
}

# Job control gives the child a distinct process group. The watchdog always
# signals that entire group so grandchildren cannot outlive a timed-out run.
RUN_STARTED="$(_now)"
trap '_handle_signal HUP 129' HUP
trap '_handle_signal INT 130' INT
trap '_handle_signal TERM 143' TERM

# A signal before launch has nothing to clean up. Exit only after all three
# tolerant traps are installed, so a signal in the launch window is recorded.
if [ -n "$PENDING_SIGNAL_CODE" ]; then
  trap - HUP INT TERM
  exit "$PENDING_SIGNAL_CODE"
fi

LAUNCHED=1
CROSSFEED_STARTED=1
set -m
if [ -n "$EVENTS" ]; then
  codex "${args[@]}" "$PROMPT" </dev/null >"$EVENTS" 2>"$RUNOUT" &
else
  codex "${args[@]}" "$PROMPT" </dev/null >"$RUNOUT" 2>&1 &
fi
CHILD_PID=$!
if [ -n "$PENDING_SIGNAL_CODE" ]; then
  _handle_signal "$PENDING_SIGNAL_NAME" "$PENDING_SIGNAL_CODE"
fi

rc=0
if ! CHILD_PGID="$(_read_child_pgid "$CHILD_PID")"; then
  # set -m established PID=PGID at launch even if the leader exited before ps
  # observed it. Keep that group identity so any surviving descendants remain supervised.
  CHILD_PGID="$CHILD_PID"
  if _child_alive; then
    echo "codex-agent: PROCESS-GROUP DISCOVERY WARNING: ps did not return a PGID; supervising live child group $CHILD_PGID via the set -m PID=PGID invariant." >&2
  else
    set +e
    wait "$CHILD_PID"
    rc=$?
    set -e
    CHILD_COMPLETED=1
  fi
fi
set +m

if [ "$CHILD_COMPLETED" -eq 0 ] || _group_alive "$CHILD_PGID"; then
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

  # If a limit fired, the watchdog owns TERM-to-KILL escalation for every process
  # in the group. Otherwise stop it only after the complete group has exited.
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
  echo "codex-agent: codex exited $rc. output tail:" >&2
  tail -5 "$RUNOUT" >&2 || true
  exit "$rc"
fi

# Exit 0 is NOT proof of a deliverable. Codex can return 0 having written nothing, and a blank or
# whitespace-only answer must never be published as success: downstream, an empty digest reads as
# "the worker had nothing to report" rather than "the worker produced nothing".
if [ ! -f "$RUN_LAST" ] || ! grep -q '[^[:space:]]' "$RUN_LAST" 2>/dev/null; then
  echo "codex-agent: codex exited 0 but produced NO final message (blank deliverable). Not publishing it." >&2
  echo "codex-agent: run log retained at: $RUNOUT" >&2
  tail -5 "$RUNOUT" >&2 || true
  _tmp_runout=""   # keep the evidence; a failed run is exactly when the log is needed
  exit 4
fi

# Publish atomically, so a reader never observes a half-written deliverable and a failed run never
# leaves the previous run's answer sitting in the caller's --last.
cp "$RUN_LAST" "$LAST.part.$$" && mv -f "$LAST.part.$$" "$LAST"

# The final agent message is the deliverable.
[ -n "$SCHEMA" ] || cat "$LAST"
exit 0

}
main "$@"; exit $?
