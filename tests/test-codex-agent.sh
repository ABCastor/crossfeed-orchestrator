#!/usr/bin/env bash
# Deterministic tests for scripts/codex-agent.sh. The real codex binary is never used.
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Works whether this file sits beside scripts/ (dev tree) or in tests/ (installed skill).
[ -d "$SCRIPT_DIR/scripts" ] || SCRIPT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER="${WRAPPER_UNDER_TEST:-$SCRIPT_DIR/scripts/codex-agent.sh}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-agent-test.XXXXXX")"
# The wrapper reads the fleet's runtime (level, pool slot, the console's model choice). A private one
# keeps the operator's live settings out of these cases, and these cases out of his pool slot.
export FLEET_STATE_DIR="$TEST_ROOT/fleet-state"
export ACCESS_OVERLAY="$SCRIPT_DIR/tests/fixtures/access-overlay.test.json"
export CODEX_HOME="$TEST_ROOT/codex-home"
mkdir -p "$CODEX_HOME"
printf 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "low"\n' >"$CODEX_HOME/config.toml"
FAKE_BIN="$TEST_ROOT/bin"
CLOCK_BIN="$TEST_ROOT/clock-bin"
EMPTY_BIN="$TEST_ROOT/empty-bin"
mkdir -p "$FAKE_BIN" "$CLOCK_BIN" "$EMPTY_BIN"

cleanup() {
  local pid_file pid
  for pid_file in "$TEST_ROOT"/*/*-pid; do
    [ -f "$pid_file" ] || continue
    pid="$(sed -n '1p' "$pid_file" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) kill -KILL "$pid" 2>/dev/null || true;;
    esac
  done
  case "$TEST_ROOT" in
    "${TMPDIR:-/tmp}"/codex-agent-test.*) rm -rf "$TEST_ROOT";;
  esac
}
trap cleanup EXIT

cat >"$FAKE_BIN/codex" <<'FAKE_CODEX'
#!/bin/bash
set -u

if [ -n "${FAKE_ARGS_FILE:-}" ]; then
  printf '%s\n' "$@" >"$FAKE_ARGS_FILE"
fi

last_file=""
prompt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -C|-s|-o|--color|-m|-c|--output-schema)
      [ "$#" -ge 2 ] || exit 91
      [ "$1" = "-o" ] && last_file="$2"
      shift 2
      ;;
    exec|--skip-git-repo-check|--json) shift;;
    *) prompt="$1"; shift;;
  esac
done

# Preserve behavioral modes after the wrapper adds its identity briefing.
prompt="${prompt##*=== CROSSFEED TASK ===$'\n'}"
finish() {
  [ -n "$last_file" ] || exit 92
  printf 'final:%s\n' "$prompt" >"$last_file"
}

case "$prompt" in
  output-alive)
    i=1
    while [ "$i" -le 6 ]; do
      printf 'progress %s\n' "$i"
      sleep 1
      i=$((i + 1))
    done
    finish
    ;;
  idle-silent)
    sleep 10
    finish
    ;;
  no-default-wall-cap)
    sleep 5
    finish
    ;;
  wall-clock)
    sleep 10
    finish
    ;;
  ignore-term)
    trap '' TERM
    sleep 10
    finish
    ;;
  descendant)
    (
      trap '' TERM HUP INT
      while :; do sleep 1; done
    ) &
    descendant_pid=$!
    printf '%s\n' "$descendant_pid" >"${FAKE_DESCENDANT_PID_FILE:?}"
    wait "$descendant_pid"
    ;;
  nonzero)
    echo 'intentional fake codex failure' >&2
    exit 42
    ;;
  instant-42)
    echo 'instant fake codex diagnostic' >&2
    exit 42
    ;;
  cancel-setup)
    printf '%s\n' "$$" >"${FAKE_TARGET_PID_FILE:?}"
    while :; do sleep 1; done
    ;;
  cpu-alive)
    printf '%s\n' "$$" >"${FAKE_CPU_PID_FILE:?}"
    end=$((SECONDS + 5))
    while [ "$SECONDS" -lt "$end" ]; do :; done
    finish
    ;;
  descendant-cpu)
    printf '%s\n' "$$" >"${FAKE_CPU_GROUP_PGID_FILE:?}"
    (
      end=$((SECONDS + 5))
      while [ "$SECONDS" -lt "$end" ]; do :; done
    ) &
    wait "$!"
    finish
    ;;
  orphan-descendant)
    (
      trap '' HUP
      exec </dev/null >/dev/null 2>&1
      sleep 3
      printf 'done\n' >"${FAKE_DESCENDANT_DONE_FILE:?}"
    ) &
    printf '%s\n' "$!" >"${FAKE_DESCENDANT_PID_FILE:?}"
    finish
    ;;
  interface)
    echo '{"type":"fake-event"}'
    echo 'fake diagnostic' >&2
    printf '%s\n' '{"answer":"final:interface"}' >"$last_file"
    ;;
  *)
    finish
    ;;
esac
FAKE_CODEX

# The managed execution sandbox blocks the host ps command. This shim implements
# exactly the two portable ps queries made by the wrapper. PGID=PID is the real
# invariant created by `set -m`; signals and descendant checks still hit the kernel.
cat >"$FAKE_BIN/ps" <<'FAKE_PS'
#!/bin/bash
set -u
field=""
pid=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) field="$2"; shift 2;;
    -p) pid="$2"; shift 2;;
    *) shift;;
  esac
done

case "$field" in
  pgid=)
    if [ -n "${FAKE_PGID_MARKER:-}" ]; then
      printf 'probe\n' >"$FAKE_PGID_MARKER"
    fi
    if [ "${FAKE_PGID_MODE:-ok}" = "miss" ]; then
      exit 1
    fi
    printf ' %s\n' "$pid"
    ;;
  time=)
    if [ -n "${FAKE_CPU_PID_FILE:-}" ] && [ -f "$FAKE_CPU_PID_FILE" ] &&
       [ "$(cat "$FAKE_CPU_PID_FILE")" = "$pid" ]; then
      counter=0
      [ -f "${FAKE_PS_CPU_COUNTER:?}" ] && counter="$(cat "$FAKE_PS_CPU_COUNTER")"
      counter=$((counter + 1))
      printf '%s\n' "$counter" >"$FAKE_PS_CPU_COUNTER"
      printf '00:00:%02d.00\n' "$counter"
    else
      printf '00:00:00.00\n'
    fi
    ;;
  pgid=,time=)
    pgid_file="${FAKE_CPU_GROUP_PGID_FILE:-${FAKE_CPU_PID_FILE:-}}"
    [ -n "$pgid_file" ] && [ -f "$pgid_file" ] || exit 0
    pgid="$(cat "$pgid_file")"
    counter=0
    [ -f "${FAKE_PS_CPU_COUNTER:?}" ] && counter="$(cat "$FAKE_PS_CPU_COUNTER")"
    counter=$((counter + 1))
    printf '%s\n' "$counter" >"$FAKE_PS_CPU_COUNTER"
    if [ -n "${FAKE_CPU_GROUP_PGID_FILE:-}" ]; then
      printf '%s %s\n' "$pgid" '1-02:03:04.05'
    fi
    printf '%s 00:00:%02d.00\n' "$pgid" "$counter"
    ;;
  *) exit 2;;
esac
FAKE_PS

# A virtual clock makes a restored hidden 600-second wall default observable in
# one real second. Correct code has no wall limit and therefore ignores the jump.
cat >"$CLOCK_BIN/date" <<'FAKE_DATE'
#!/bin/bash
set -u
[ "${1:-}" = "+%s" ] || exit 2
state="${FAKE_DATE_STATE:?}"
if [ -f "$state" ]; then
  value="$(cat "$state")"
  value=$((value + 601))
else
  value=100000
fi
printf '%s\n' "$value" >"$state"
printf '%s\n' "$value"
FAKE_DATE

chmod +x "$FAKE_BIN/codex" "$FAKE_BIN/ps" "$CLOCK_BIN/date"

BASE_PATH="$FAKE_BIN:/usr/bin:/bin:/usr/sbin:/sbin"
PASS_COUNT=0
FAIL_COUNT=0
RUN_COUNT=0
DETAIL=""

fail() {
  DETAIL="$1"
  return 1
}

contains() {
  grep -F -- "$2" "$1" >/dev/null 2>&1
}

invoke() {
  local stdout_file="$1" stderr_file="$2"
  shift 2
  PATH="$BASE_PATH" /bin/bash "$WRAPPER" "$@" >"$stdout_file" 2>"$stderr_file"
}

wait_for_file() {
  local path="$1" attempts=0
  while [ ! -s "$path" ] && [ "$attempts" -lt 100 ]; do
    sleep 0.02
    attempts=$((attempts + 1))
  done
  [ -s "$path" ]
}

assert_dead() {
  local pid_file="$1" label="$2" pid attempts=0
  [ -s "$pid_file" ] || { fail "$label PID was not recorded"; return 1; }
  pid="$(sed -n '1p' "$pid_file")"
  while kill -0 "$pid" 2>/dev/null && [ "$attempts" -lt 30 ]; do
    sleep 0.1
    attempts=$((attempts + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    fail "$label process $pid survived immediate cancellation"
    return 1
  fi
}

test_output_liveness() {
  local case_dir="$TEST_ROOT/output-alive" rc
  mkdir -p "$case_dir"
  invoke "$case_dir/stdout" "$case_dir/stderr" --prompt output-alive --dir "$case_dir/work" --idle-timeout 3 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected rc 0, got $rc"; return 1; }
  [ "$(cat "$case_dir/stdout")" = 'final:output-alive' ] || { fail "stdout must contain only the final message"; return 1; }
  contains "$case_dir/stderr" 'Crossfeed model receipt:' || { fail "stderr receipt missing"; return 1; }
  ! contains "$case_dir/stderr" 'LIMIT FIRED' || { fail "active output was killed"; return 1; }
}

test_idle_kill() {
  local case_dir="$TEST_ROOT/idle" rc
  mkdir -p "$case_dir"
  invoke "$case_dir/stdout" "$case_dir/stderr" --prompt idle-silent --dir "$case_dir/work" --idle-timeout 3 --kill-after 1
  rc=$?
  [ "$rc" -eq 125 ] || { fail "expected rc 125, got $rc"; return 1; }
  contains "$case_dir/stderr" 'IDLE LIMIT FIRED' || { fail "idle label missing"; return 1; }
  contains "$case_dir/stderr" 'working directory:' || { fail "working directory missing"; return 1; }
  contains "$case_dir/stderr" 'partial edits may be on disk' || { fail "partial-edit warning missing"; return 1; }
  contains "$case_dir/stderr" 'codex exec resume --last' || { fail "resume command missing"; return 1; }
}

test_no_default_wall_cap() {
  local case_dir="$TEST_ROOT/no-default" rc
  mkdir -p "$case_dir"
  FAKE_DATE_STATE="$case_dir/date-state" PATH="$CLOCK_BIN:$BASE_PATH" \
    /bin/bash "$WRAPPER" --prompt no-default-wall-cap --dir "$case_dir/work" \
    --idle-timeout 999999 --kill-after 1 >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected rc 0 with no --timeout, got $rc"; return 1; }
  contains "$case_dir/stdout" 'final:no-default-wall-cap' || { fail "five-second run did not complete"; return 1; }
  ! contains "$case_dir/stderr" 'WALL-CLOCK LIMIT FIRED' || { fail "hidden wall cap fired"; return 1; }
}

test_wall_clock_kill() {
  local case_dir="$TEST_ROOT/wall" rc
  mkdir -p "$case_dir"
  invoke "$case_dir/stdout" "$case_dir/stderr" --prompt wall-clock --dir "$case_dir/work" \
    --timeout 3 --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 124 ] || { fail "expected rc 124, got $rc"; return 1; }
  contains "$case_dir/stderr" 'WALL-CLOCK LIMIT FIRED' || { fail "wall-clock label missing"; return 1; }
  contains "$case_dir/stderr" 'configured --timeout 3s' || { fail "wall-clock detail missing"; return 1; }
}

test_sigkill_escalation() {
  local case_dir="$TEST_ROOT/sigkill" rc started ended duration
  mkdir -p "$case_dir"
  started="$(/bin/date +%s)"
  invoke "$case_dir/stdout" "$case_dir/stderr" --prompt ignore-term --dir "$case_dir/work" \
    --timeout 3 --idle-timeout 20 --kill-after 2
  rc=$?
  ended="$(/bin/date +%s)"
  duration=$((ended - started))
  [ "$rc" -eq 124 ] || { fail "expected rc 124, got $rc"; return 1; }
  [ "$duration" -le 8 ] || { fail "TERM-ignoring run was not bounded: ${duration}s"; return 1; }
  contains "$case_dir/stderr" 'KILL-AFTER LIMIT FIRED' || { fail "SIGKILL escalation missing"; return 1; }
}

test_process_group_cleanup() {
  local case_dir="$TEST_ROOT/process-group" pid_file rc descendant_pid attempts
  case_dir="$TEST_ROOT/process-group"
  pid_file="$case_dir/descendant-pid"
  mkdir -p "$case_dir"
  FAKE_DESCENDANT_PID_FILE="$pid_file" PATH="$BASE_PATH" /bin/bash "$WRAPPER" \
    --prompt descendant --dir "$case_dir/work" --timeout 2 --idle-timeout 20 --kill-after 1 \
    >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 124 ] || { fail "expected rc 124, got $rc"; return 1; }
  [ -s "$pid_file" ] || { fail "fake descendant PID was not recorded"; return 1; }
  descendant_pid="$(cat "$pid_file")"
  attempts=0
  while kill -0 "$descendant_pid" 2>/dev/null && [ "$attempts" -lt 3 ]; do
    sleep 1
    attempts=$((attempts + 1))
  done
  if kill -0 "$descendant_pid" 2>/dev/null; then
    kill -KILL "$descendant_pid" 2>/dev/null || true
    fail "descendant $descendant_pid survived process-group kill"
    return 1
  fi
  contains "$case_dir/stderr" 'KILL-AFTER LIMIT FIRED' || { fail "descendant did not require group escalation"; return 1; }
}

test_nonzero_passthrough() {
  local case_dir="$TEST_ROOT/nonzero" rc
  mkdir -p "$case_dir"
  invoke "$case_dir/stdout" "$case_dir/stderr" --prompt nonzero --dir "$case_dir/work" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 42 ] || { fail "expected fake codex rc 42, got $rc"; return 1; }
  contains "$case_dir/stderr" 'codex exited 42' || { fail "non-zero diagnostic missing"; return 1; }
  ! contains "$case_dir/stderr" 'LIMIT FIRED' || { fail "ordinary codex failure mislabeled as a limit"; return 1; }
}

test_fast_exit_passthrough() {
  local case_dir="$TEST_ROOT/fast-exit" rc
  mkdir -p "$case_dir"
  FAKE_PGID_MODE=miss invoke "$case_dir/stdout" "$case_dir/stderr" \
    --prompt instant-42 --dir "$case_dir/work" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 42 ] || { fail "instant child expected rc 42, got $rc"; return 1; }
  contains "$case_dir/stderr" 'instant fake codex diagnostic' || { fail "instant child diagnostic was discarded"; return 1; }
  contains "$case_dir/stderr" 'codex exited 42' || { fail "wrapper exit diagnostic missing"; return 1; }
  ! contains "$case_dir/stderr" 'PROCESS-GROUP DISCOVERY FAILURE' || { fail "completed child was mislabeled as discovery failure"; return 1; }
}

test_immediate_signal_cleanup() {
  local case_dir="$TEST_ROOT/immediate-signal" pid_file marker wrapper_pid rc attempts=0
  mkdir -p "$case_dir"
  pid_file="$case_dir/target-pid"
  marker="$case_dir/pgid-probe"
  FAKE_TARGET_PID_FILE="$pid_file" FAKE_PGID_MODE=miss FAKE_PGID_MARKER="$marker" \
    PATH="$BASE_PATH" /bin/bash "$WRAPPER" --prompt cancel-setup --dir "$case_dir/work" \
      --idle-timeout 20 --kill-after 1 >"$case_dir/stdout" 2>"$case_dir/stderr" &
  wrapper_pid=$!

  if ! wait_for_file "$pid_file" || ! wait_for_file "$marker"; then
    kill -KILL "$wrapper_pid" 2>/dev/null || true
    wait "$wrapper_pid" 2>/dev/null || true
    fail "launch-window fixture did not reach PGID discovery"
    return 1
  fi

  kill -TERM "$wrapper_pid" 2>/dev/null || true
  while kill -0 "$wrapper_pid" 2>/dev/null && [ "$attempts" -lt 50 ]; do
    sleep 0.1
    attempts=$((attempts + 1))
  done
  if kill -0 "$wrapper_pid" 2>/dev/null; then
    kill -KILL "$wrapper_pid" 2>/dev/null || true
    wait "$wrapper_pid" 2>/dev/null || true
    fail "wrapper did not terminate after immediate TERM"
    return 1
  fi
  wait "$wrapper_pid"
  rc=$?
  [ "$rc" -eq 143 ] || { fail "immediate TERM expected rc 143, got $rc"; return 1; }
  assert_dead "$pid_file" "Codex child" || return 1
  contains "$case_dir/stderr" 'EXTERNAL TERM RECEIVED' || { fail "pre-launch TERM trap did not run"; return 1; }
}

test_cpu_liveness() {
  local case_dir="$TEST_ROOT/cpu" rc
  case_dir="$TEST_ROOT/cpu"
  mkdir -p "$case_dir"
  FAKE_CPU_PID_FILE="$case_dir/cpu-pid" FAKE_PS_CPU_COUNTER="$case_dir/cpu-counter" \
    PATH="$BASE_PATH" /bin/bash "$WRAPPER" --prompt cpu-alive --dir "$case_dir/work" \
    --idle-timeout 2 --kill-after 1 >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected CPU-active silent run to survive, got rc $rc"; return 1; }
  contains "$case_dir/stdout" 'final:cpu-alive' || { fail "CPU-active final message missing"; return 1; }
  ! contains "$case_dir/stderr" 'IDLE LIMIT FIRED' || { fail "CPU progress did not reset idle timer"; return 1; }
}

test_descendant_cpu_liveness() {
  local case_dir="$TEST_ROOT/descendant-cpu" rc
  mkdir -p "$case_dir"
  FAKE_CPU_GROUP_PGID_FILE="$case_dir/group-pgid" FAKE_PS_CPU_COUNTER="$case_dir/cpu-counter" \
    PATH="$BASE_PATH" /bin/bash "$WRAPPER" --prompt descendant-cpu --dir "$case_dir/work" \
    --idle-timeout 2 --kill-after 1 >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "quiet leader with CPU-active descendant was killed, rc $rc"; return 1; }
  contains "$case_dir/stdout" 'final:descendant-cpu' || { fail "descendant-CPU final message missing"; return 1; }
  ! contains "$case_dir/stderr" 'IDLE LIMIT FIRED' || { fail "watchdog ignored aggregate process-group CPU"; return 1; }
}

test_group_completion() {
  local case_dir="$TEST_ROOT/group-completion" rc
  mkdir -p "$case_dir"
  FAKE_DESCENDANT_PID_FILE="$case_dir/descendant-pid" \
    FAKE_DESCENDANT_DONE_FILE="$case_dir/descendant-done" \
    PATH="$BASE_PATH" /bin/bash "$WRAPPER" --prompt orphan-descendant --dir "$case_dir/work" \
    --idle-timeout 20 --kill-after 1 >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "leader status changed while waiting for descendants, rc $rc"; return 1; }
  [ -s "$case_dir/descendant-done" ] || { fail "wrapper returned before the descendant finished"; return 1; }
  assert_dead "$case_dir/descendant-pid" "Codex orphan descendant" || return 1
  contains "$case_dir/stderr" 'still has live descendants' || { fail "continued group supervision was silent"; return 1; }
}

test_interface_and_routing() {
  local case_dir="$TEST_ROOT/interface" rc args_file
  mkdir -p "$case_dir/work"
  printf 'interface\n' >"$case_dir/prompt.txt"
  printf '{}\n' >"$case_dir/schema.json"
  args_file="$case_dir/args"
  FAKE_ARGS_FILE="$args_file" PATH="$BASE_PATH" /bin/bash "$WRAPPER" \
    --prompt-file "$case_dir/prompt.txt" --dir "$case_dir/work" --sandbox read-only \
    --model fake-model --reasoning high --schema "$case_dir/schema.json" \
    --events "$case_dir/events.jsonl" --last "$case_dir/last.txt" --log "$case_dir/run.log" \
    --idle-timeout 10 >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "interface run expected rc 0, got $rc"; return 1; }
  contains "$case_dir/stdout" 'final:interface' || { fail "stdout is not the final message"; return 1; }
  contains "$case_dir/events.jsonl" 'fake-event' || { fail "events were not routed"; return 1; }
  contains "$case_dir/run.log" 'fake diagnostic' || { fail "diagnostics were not logged"; return 1; }
  contains "$args_file" 'fake-model' || { fail "model option was not forwarded"; return 1; }
  contains "$args_file" 'model_reasoning_effort="high"' || { fail "reasoning option was not forwarded"; return 1; }
  contains "$args_file" "$case_dir/schema.json" || { fail "schema option was not forwarded"; return 1; }
  contains "$args_file" '--json' || { fail "events did not enable JSON mode"; return 1; }
}

test_roster_effort() {
  local d="$TEST_ROOT/effort" rc
  mkdir -p "$d/work"
  FAKE_ARGS_FILE="$d/default-args" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --model gpt-6.1-sol
  rc=$?
  [ "$rc" -eq 0 ] || { fail "roster default exited $rc"; return 1; }
  contains "$d/default-args" 'model_reasoning_effort="medium"' || { fail "saved low leaked instead of roster medium"; return 1; }
  contains "$d/err" 'effort medium: roster' || { fail "roster effort source not announced"; return 1; }
  FAKE_ARGS_FILE="$d/builder-args" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --model gpt-6.1-sol --role builder
  [ "$?" -eq 0 ] && contains "$d/builder-args" 'model_reasoning_effort="high"' || { fail "builder role not applied"; return 1; }
  FAKE_ARGS_FILE="$d/override-args" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --model gpt-6.1-sol --role builder --reasoning low -c 'model_reasoning_effort="max"'
  [ "$?" -eq 0 ] || { fail "caller override refused"; return 1; }
  [ "$(grep 'model_reasoning_effort=' "$d/override-args" | tail -1)" = 'model_reasoning_effort="low"' ] || { fail "config option overrode explicit reasoning"; return 1; }
  contains "$d/err" 'explicit caller level' || { fail "caller effort source not announced"; return 1; }
  FAKE_ARGS_FILE="$d/toml-args" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --model gpt-6.1-sol -c 'model_reasoning_effort = "max"'
  [ "$?" -eq 0 ] && [ "$(grep 'model_reasoning_effort=' "$d/toml-args" | tail -1)" = 'model_reasoning_effort="max"' ] || { fail "TOML whitespace lost caller effort"; return 1; }
  # An unknown model now legitimately gets a current stand-in because older
  # models are off. Remove effort from the model that will actually run instead.
  python3 - "$ACCESS_OVERLAY" "$d/missing-effort.json" <<'PY'
import json, sys
with open(sys.argv[1]) as source:
    roster = json.load(source)
del roster['effort']['gpt-6.1-sol']
with open(sys.argv[2], 'w') as target:
    json.dump(roster, target)
PY
  ACCESS_OVERLAY="$d/missing-effort.json" FAKE_ARGS_FILE="$d/missing-args" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --model gpt-6.1-sol
  [ "$?" -eq 2 ] && [ ! -e "$d/missing-args" ] || { fail "missing effort silently launched Codex"; return 1; }
  contains "$d/err" 'missing effort data for gpt-6.1-sol' || { fail "missing effort diagnostic absent"; return 1; }
}

test_usage_exit() {
  local case_dir="$TEST_ROOT/usage" rc
  mkdir -p "$case_dir"
  invoke "$case_dir/stdout" "$case_dir/stderr" --bogus
  rc=$?
  [ "$rc" -eq 2 ] || { fail "unknown option expected rc 2, got $rc"; return 1; }
  contains "$case_dir/stderr" 'unknown arg' || { fail "usage diagnostic missing"; return 1; }
}

test_missing_binary_exit() {
  local case_dir="$TEST_ROOT/missing" rc
  mkdir -p "$case_dir"
  PATH="$EMPTY_BIN" /bin/bash "$WRAPPER" --prompt quick >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 127 ] || { fail "missing codex expected rc 127, got $rc"; return 1; }
  contains "$case_dir/stderr" 'codex binary not on PATH' || { fail "missing-binary diagnostic absent"; return 1; }
}

test_help_contract() {
  local case_dir="$TEST_ROOT/help" rc
  mkdir -p "$case_dir"
  PATH="/usr/bin:/bin" /bin/bash "$WRAPPER" -h >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "help expected rc 0, got $rc"; return 1; }
  contains "$case_dir/stdout" '--idle-timeout S' || { fail "idle control missing from help"; return 1; }
  contains "$case_dir/stdout" '124 wall-clock kill' || { fail "exit-code contract missing from help"; return 1; }
}

selected() {
  local wanted="$1" item
  [ "$#" -ge 1 ] || return 1
  [ "$SELECT_ALL" -eq 1 ] && return 0
  for item in "${SELECTED_TESTS[@]}"; do
    [ "$item" = "$wanted" ] && return 0
  done
  return 1
}

run_test() {
  local name="$1" function_name="$2"
  selected "$name" || return 0
  RUN_COUNT=$((RUN_COUNT + 1))
  DETAIL=""
  if "$function_name"; then
    PASS_COUNT=$((PASS_COUNT + 1))
    printf 'PASS %s\n' "$name"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf 'FAIL %s: %s\n' "$name" "${DETAIL:-test returned non-zero}"
  fi
}

if [ "$#" -eq 0 ]; then
  SELECT_ALL=1
  SELECTED_TESTS=()
else
  SELECT_ALL=0
  SELECTED_TESTS=("$@")
fi

run_test output_liveness test_output_liveness
run_test idle_kill test_idle_kill
run_test no_default_wall_cap test_no_default_wall_cap
run_test wall_clock_kill test_wall_clock_kill
run_test sigkill_escalation test_sigkill_escalation
run_test process_group_cleanup test_process_group_cleanup
run_test nonzero_passthrough test_nonzero_passthrough
run_test fast_exit_passthrough test_fast_exit_passthrough
run_test immediate_signal_cleanup test_immediate_signal_cleanup
run_test cpu_liveness test_cpu_liveness
run_test descendant_cpu_liveness test_descendant_cpu_liveness
run_test group_completion test_group_completion
run_test interface_and_routing test_interface_and_routing
run_test roster_effort test_roster_effort
run_test usage_exit test_usage_exit
run_test missing_binary_exit test_missing_binary_exit
run_test help_contract test_help_contract

if [ "$RUN_COUNT" -eq 0 ]; then
  printf 'FAIL no matching tests selected\n'
  exit 2
fi

printf 'RESULT: %s passed, %s failed, %s total\n' "$PASS_COUNT" "$FAIL_COUNT" "$RUN_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
