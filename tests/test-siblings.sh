#!/usr/bin/env bash
# Deterministic fake-only tests for claude-agent.sh, agy-agent.sh, and copilot-agent.sh.
# No real model CLI can be reached: PATH contains only the generated fakes plus system tools.
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Works whether this file sits beside scripts/ (dev tree) or in tests/ (installed skill).
[ -d "$SCRIPT_DIR/scripts" ] || SCRIPT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLAUDE_SOURCE="${CLAUDE_WRAPPER_UNDER_TEST:-$SCRIPT_DIR/scripts/claude-agent.sh}"
AGY_SOURCE="${AGY_WRAPPER_UNDER_TEST:-$SCRIPT_DIR/scripts/agy-agent.sh}"
COPILOT_SOURCE="${COPILOT_WRAPPER_UNDER_TEST:-$SCRIPT_DIR/scripts/copilot-agent.sh}"

TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sibling-agent-test.XXXXXX")"
FAKE_BIN="$TEST_ROOT/bin"
CLOCK_BIN="$TEST_ROOT/clock-bin"
EMPTY_BIN="$TEST_ROOT/empty-bin"
FIXTURE_DIR="$TEST_ROOT/wrappers"
TMP_AREA="$TEST_ROOT/tmp"
mkdir -p "$FAKE_BIN" "$CLOCK_BIN" "$EMPTY_BIN" "$FIXTURE_DIR" "$TMP_AREA"

cleanup() {
  local pid_file pid
  for pid_file in "$TEST_ROOT"/*/*-pid "$TEST_ROOT"/*/*-pid-file; do
    [ -f "$pid_file" ] || continue
    pid="$(sed -n '1p' "$pid_file" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) kill -KILL "$pid" 2>/dev/null || true;;
    esac
  done
  case "$TEST_ROOT" in
    "${TMPDIR:-/tmp}"/sibling-agent-test.*) rm -rf "$TEST_ROOT";;
  esac
}
trap cleanup EXIT

for source_file in "$CLAUDE_SOURCE" "$AGY_SOURCE" "$COPILOT_SOURCE"; do
  [ -f "$source_file" ] || { echo "FAIL wrapper not found: $source_file"; exit 2; }
done

cp "$CLAUDE_SOURCE" "$FIXTURE_DIR/claude-agent.sh"
cp "$AGY_SOURCE" "$FIXTURE_DIR/agy-agent.sh"
cp "$COPILOT_SOURCE" "$FIXTURE_DIR/copilot-agent.sh"
cp "$SCRIPT_DIR/scripts/run-identity.sh" "$SCRIPT_DIR/scripts/run_identity.py" "$FIXTURE_DIR/"
export FLEET_STATE_DIR="$TEST_ROOT/model-state"
chmod +x "$FIXTURE_DIR/claude-agent.sh" "$FIXTURE_DIR/agy-agent.sh" "$FIXTURE_DIR/copilot-agent.sh"
CLAUDE_WRAPPER="$FIXTURE_DIR/claude-agent.sh"
AGY_WRAPPER="$FIXTURE_DIR/agy-agent.sh"
COPILOT_WRAPPER="$FIXTURE_DIR/copilot-agent.sh"

REAL_PYTHON3="$(command -v python3 || true)"
REAL_JQ="$(command -v jq || true)"
[ -n "$REAL_PYTHON3" ] || { echo "FAIL python3 is required for the AGY PTY test"; exit 2; }
[ -n "$REAL_JQ" ] || { echo "FAIL jq is required for the Copilot JSON test"; exit 2; }
ln -s "$REAL_PYTHON3" "$FAKE_BIN/python3"
ln -s "$REAL_JQ" "$FAKE_BIN/jq"

cat >"$FIXTURE_DIR/roster.sh" <<'FAKE_ROSTER'
#!/bin/bash
set -u
case "${1:-}" in
  check|check-lane)
    exit "${FAKE_ROSTER_RC:-0}"
    ;;
  lane-json)
    [ "${FAKE_LANE_UNKNOWN:-0}" = "0" ] || { echo 'unknown lane' >&2; exit 9; }
    if [[ "$2" = model-* ]]; then
      printf '{"selector":"%s"}\n' "${2#model-}"
    else
      printf '%s\n' '{"selector":"fake-agy-model"}'
    fi
    ;;
  lookup)
    printf 'model-%s\n' "$3"
    ;;
  *) exit 9;;
esac
FAKE_ROSTER

cat >"$FIXTURE_DIR/fleetctl.py" <<'FAKE_FLEETCTL'
#!/bin/bash
set -u
case "${1:-}" in
  effort)
    [ "${FAKE_EFFORT_MISSING:-0}" = 0 ] || exit 2
    model="$2"; role="$3"; shift 3
    level="${FAKE_EFFORT_DEFAULT:-high}"; why="roster fake/$role"
    while [ "$#" -gt 0 ]; do
      case "$1" in --level) level="$2"; why='explicit caller level'; shift 2;; *) shift;; esac
    done
    printf '%s\t%s\t%s\n' "$level" "$model" "$why"
    ;;
  route)
    [ "${FAKE_ROUTE_FAIL:-0}" = "0" ] || exit 9
    printf '%s\n' fake-agy-lane
    ;;
  acquire)
    [ "${FAKE_LEASE_FAIL:-0}" = "0" ] || { echo 'fake lease refusal'; exit 9; }
    printf '%s\n' fake-lease-token
    ;;
  release) exit 0;;
  *) exit 9;;
esac
FAKE_FLEETCTL
chmod +x "$FIXTURE_DIR/roster.sh" "$FIXTURE_DIR/fleetctl.py"

cat >"$FAKE_BIN/claude" <<'FAKE_CLAUDE'
#!/bin/bash
set -u
if [ -n "${FAKE_ARGS_FILE:-}" ]; then printf '%s\n' "$@" >"$FAKE_ARGS_FILE"; fi
prompt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --) shift; [ "$#" -gt 0 ] && prompt="$1"; break;;
    *) shift;;
  esac
done

# The fake's behavioral mode is the task payload; recorded argv still proves worker awareness.
prompt="${prompt##*=== CROSSFEED TASK ===$'\n'}"
case "$prompt" in
  output-alive)
    i=1
    while [ "$i" -le 4 ]; do printf 'progress %s\n' "$i"; sleep 1; i=$((i + 1)); done
    printf 'final:%s\n' "$prompt"
    ;;
  cpu-alive)
    printf '%s\n' "$$" >"${FAKE_CPU_PID_FILE:?}"
    end=$((SECONDS + 4)); while [ "$SECONDS" -lt "$end" ]; do :; done
    printf 'final:%s\n' "$prompt"
    ;;
  descendant-cpu)
    printf '%s\n' "$$" >"${FAKE_CPU_GROUP_PGID_FILE:?}"
    (end=$((SECONDS + 4)); while [ "$SECONDS" -lt "$end" ]; do :; done) &
    wait "$!"
    printf 'final:%s\n' "$prompt"
    ;;
  orphan-descendant)
    (
      trap '' HUP
      exec </dev/null >/dev/null 2>&1
      sleep 3
      printf 'done\n' >"${FAKE_DESCENDANT_DONE_FILE:?}"
    ) &
    printf '%s\n' "$!" >"${FAKE_DESCENDANT_PID_FILE:?}"
    printf 'final:%s\n' "$prompt"
    ;;
  stderr-progress)
    printf 'live-stderr-progress\n' >&2
    sleep 3
    printf 'final:%s\n' "$prompt"
    ;;
  no-default-wall-cap)
    sleep 5; printf 'final:%s\n' "$prompt"
    ;;
  idle-silent|wall-clock)
    sleep 7; printf 'final:%s\n' "$prompt"
    ;;
  ignore-term)
    printf '%s\n' "$$" >"${FAKE_TARGET_PID_FILE:?}"
    trap '' TERM HUP INT
    sleep 7
    printf 'final:%s\n' "$prompt"
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
  empty) exit 0;;
  nonzero) echo 'intentional fake claude failure' >&2; exit 42;;
  instant-42) echo 'instant fake claude diagnostic' >&2; exit 42;;
  cancel-setup)
    printf '%s\n' "$$" >"${FAKE_TARGET_PID_FILE:?}"
    while :; do sleep 1; done
    ;;
  *) printf 'final:%s\n' "$prompt";;
esac
FAKE_CLAUDE

cat >"$FAKE_BIN/agy" <<'FAKE_AGY'
#!/bin/bash
set -u
if [ -n "${FAKE_ARGS_FILE:-}" ]; then printf '%s\n' "$@" >"$FAKE_ARGS_FILE"; fi
prompt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print) prompt="$2"; shift 2;;
    --add-dir|--model) shift 2;;
    *) shift;;
  esac
done

# The fake's behavioral mode is the task payload; recorded argv still proves worker awareness.
prompt="${prompt##*=== CROSSFEED TASK ===$'\n'}"
case "$prompt" in
  output-alive)
    i=1
    while [ "$i" -le 4 ]; do printf 'progress %s\n' "$i"; sleep 1; i=$((i + 1)); done
    printf 'final:%s\n' "$prompt"
    ;;
  cpu-alive)
    printf '%s\n' "$$" >"${FAKE_CPU_PID_FILE:?}"
    end=$((SECONDS + 4)); while [ "$SECONDS" -lt "$end" ]; do :; done
    printf 'final:%s\n' "$prompt"
    ;;
  descendant-cpu)
    printf '%s\n' "$$" >"${FAKE_CPU_GROUP_PGID_FILE:?}"
    (end=$((SECONDS + 4)); while [ "$SECONDS" -lt "$end" ]; do :; done) &
    wait "$!"
    printf 'final:%s\n' "$prompt"
    ;;
  orphan-descendant)
    # forkpty leader exit may send HUP before the child gets scheduled.
    # Inherit ignored HUP so this fixture survives to exercise supervision.
    trap '' HUP
    (
      exec </dev/null >/dev/null 2>&1
      sleep 3
      printf 'done\n' >"${FAKE_DESCENDANT_DONE_FILE:?}"
    ) &
    printf '%s\n' "$!" >"${FAKE_DESCENDANT_PID_FILE:?}"
    printf 'final:%s\n' "$prompt"
    ;;
  no-default-wall-cap)
    sleep 5; printf 'final:%s\n' "$prompt"
    ;;
  idle-silent|wall-clock)
    sleep 7; printf 'final:%s\n' "$prompt"
    ;;
  ignore-term)
    printf '%s\n' "$$" >"${FAKE_TARGET_PID_FILE:?}"
    trap '' TERM HUP INT
    sleep 7
    printf 'final:%s\n' "$prompt"
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
  empty) exit 0;;
  nonzero) echo 'intentional fake agy failure' >&2; exit 42;;
  instant-42) echo 'instant fake agy diagnostic' >&2; exit 42;;
  cancel-setup)
    printf '%s\n' "$$" >"${FAKE_TARGET_PID_FILE:?}"
    while :; do sleep 1; done
    ;;
  tty-stdin)
    [ -t 0 ] || { echo 'fake agy lost its PTY stdin' >&2; exit 43; }
    printf 'final:%s\n' "$prompt"
    ;;
  *) printf 'final:%s\n' "$prompt";;
esac
FAKE_AGY

cat >"$FAKE_BIN/copilot" <<'FAKE_COPILOT'
#!/bin/bash
set -u
if [ -n "${FAKE_ARGS_FILE:-}" ]; then printf '%s\n' "$@" >"$FAKE_ARGS_FILE"; fi
if [ -n "${FAKE_TOKEN_STATE:-}" ]; then
  printf 'COPILOT_GITHUB_TOKEN=%s\n' "${COPILOT_GITHUB_TOKEN-unset}" >"$FAKE_TOKEN_STATE"
  printf 'GH_TOKEN=%s\n' "${GH_TOKEN-unset}" >>"$FAKE_TOKEN_STATE"
  printf 'GITHUB_TOKEN=%s\n' "${GITHUB_TOKEN-unset}" >>"$FAKE_TOKEN_STATE"
fi
prompt=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p) prompt="$2"; shift 2;;
    -C|--model|--max-ai-credits|--mode|--log-level|--output-format) shift 2;;
    *) shift;;
  esac
done

success() {
  printf '%s\n' '{"type":"session.auto_mode_resolved","data":{"chosenModel":"fake-auto"}}'
  printf '{"type":"assistant.message","data":{"content":"final:%s"}}\n' "$1"
  printf '%s\n' '{"type":"result","exitCode":0}'
}

# The fake's behavioral mode is the task payload; recorded argv still proves worker awareness.
prompt="${prompt##*=== CROSSFEED TASK ===$'\n'}"
case "$prompt" in
  output-alive)
    i=1
    while [ "$i" -le 4 ]; do
      printf '{"type":"assistant.message","data":{"content":"progress-%s"}}\n' "$i"
      sleep 1
      i=$((i + 1))
    done
    success "$prompt"
    ;;
  cpu-alive)
    printf '%s\n' "$$" >"${FAKE_CPU_PID_FILE:?}"
    end=$((SECONDS + 4)); while [ "$SECONDS" -lt "$end" ]; do :; done
    success "$prompt"
    ;;
  descendant-cpu)
    printf '%s\n' "$$" >"${FAKE_CPU_GROUP_PGID_FILE:?}"
    (end=$((SECONDS + 4)); while [ "$SECONDS" -lt "$end" ]; do :; done) &
    wait "$!"
    success "$prompt"
    ;;
  orphan-descendant)
    (
      trap '' HUP
      exec </dev/null >/dev/null 2>&1
      sleep 3
      printf 'done\n' >"${FAKE_DESCENDANT_DONE_FILE:?}"
    ) &
    printf '%s\n' "$!" >"${FAKE_DESCENDANT_PID_FILE:?}"
    success "$prompt"
    ;;
  no-default-wall-cap)
    sleep 5; success "$prompt"
    ;;
  idle-silent|wall-clock)
    sleep 7; success "$prompt"
    ;;
  ignore-term)
    printf '%s\n' "$$" >"${FAKE_TARGET_PID_FILE:?}"
    trap '' TERM HUP INT
    sleep 7
    success "$prompt"
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
  invalid-json) printf '%s\n' 'not-json';;
  bad-result) printf '%s\n' '{"type":"result","exitCode":9}';;
  empty) printf '%s\n' '{"type":"result","exitCode":0}';;
  nonzero) echo 'intentional fake copilot failure' >&2; exit 42;;
  instant-42) echo 'instant fake copilot diagnostic' >&2; exit 42;;
  cancel-setup)
    printf '%s\n' "$$" >"${FAKE_TARGET_PID_FILE:?}"
    while :; do sleep 1; done
    ;;
  *) success "$prompt";;
esac
FAKE_COPILOT

# The execution sandbox blocks host ps queries. This fake supplies only the
# wrapper's three portable queries; real kernel process-group signals still run.
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
  comm=)
    if [ "${FAKE_PARENT_TIMEOUT:-0}" = "1" ]; then printf '%s\n' '/fake/bin/gtimeout'
    else printf '%s\n' bash
    fi
    ;;
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
       [ "$(sed -n '1p' "$FAKE_CPU_PID_FILE")" = "$pid" ]; then
      counter=0
      [ -f "${FAKE_PS_CPU_COUNTER:?}" ] && counter="$(sed -n '1p' "$FAKE_PS_CPU_COUNTER")"
      counter=$((counter + 1))
      printf '%s\n' "$counter" >"$FAKE_PS_CPU_COUNTER"
      printf '00:00:%02d.00\n' "$counter"
    else
      printf '%s\n' '00:00:00.00'
    fi
    ;;
  pgid=,time=)
    pgid_file="${FAKE_CPU_GROUP_PGID_FILE:-${FAKE_CPU_PID_FILE:-}}"
    [ -n "$pgid_file" ] && [ -f "$pgid_file" ] || exit 0
    pgid="$(sed -n '1p' "$pgid_file")"
    counter=0
    [ -f "${FAKE_PS_CPU_COUNTER:?}" ] && counter="$(sed -n '1p' "$FAKE_PS_CPU_COUNTER")"
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

cat >"$FAKE_BIN/tee" <<'FAKE_TEE'
#!/bin/bash
set -u
[ -z "${FAKE_TEE_PID_FILE:-}" ] || printf '%s\n' "$$" >"$FAKE_TEE_PID_FILE"
exec /usr/bin/tee "$@"
FAKE_TEE

# A virtual clock makes a restored hidden 600-second default fire in one real
# second. Correct wrappers ignore the jump because TIMEOUT is empty.
cat >"$CLOCK_BIN/date" <<'FAKE_DATE'
#!/bin/bash
set -u
[ "${1:-}" = "+%s" ] || exit 2
state="${FAKE_DATE_STATE:?}"
if [ -f "$state" ]; then
  value="$(sed -n '1p' "$state")"
  value=$((value + 601))
else
  value=100000
fi
printf '%s\n' "$value" >"$state"
printf '%s\n' "$value"
FAKE_DATE

chmod +x "$FAKE_BIN/claude" "$FAKE_BIN/agy" "$FAKE_BIN/copilot" "$FAKE_BIN/ps" "$FAKE_BIN/tee" "$CLOCK_BIN/date"

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

new_case() {
  local case_dir="$TEST_ROOT/$1"
  mkdir -p "$case_dir/home" "$case_dir/work"
  printf '%s' "$case_dir"
}

invoke() {
  local wrapper="$1" case_dir="$2" stdout_file="$3" stderr_file="$4"
  shift 4
  HOME="$case_dir/home" TMPDIR="$TMP_AREA" PATH="$BASE_PATH" \
    /bin/bash "$wrapper" "$@" >"$stdout_file" 2>"$stderr_file"
}

wait_for_file() {
  local path="$1" attempts=0
  while [ ! -s "$path" ] && [ "$attempts" -lt 100 ]; do
    sleep 0.02
    attempts=$((attempts + 1))
  done
  [ -s "$path" ]
}

wait_for_contains() {
  local path="$1" needle="$2" attempts=0
  while ! grep -F -- "$needle" "$path" >/dev/null 2>&1 && [ "$attempts" -lt 100 ]; do
    sleep 0.02
    attempts=$((attempts + 1))
  done
  grep -F -- "$needle" "$path" >/dev/null 2>&1
}

assert_dead() {
  local pid_file="$1" label="$2" pid attempts=0
  [ -s "$pid_file" ] || { fail "$label PID was not recorded"; return 1; }
  pid="$(sed -n '1p' "$pid_file")"
  while kill -0 "$pid" 2>/dev/null && [ "$attempts" -lt 15 ]; do
    sleep 0.2
    attempts=$((attempts + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    fail "$label process $pid survived the wrapper's group kill"
    return 1
  fi
}

common_output_liveness() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-output")"
  invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt output-alive --dir "$case_dir/work" --idle-timeout 3 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected rc 0, got $rc"; return 1; }
  local expected='final:output-alive'
  # The older plain-text fakes include each progress line in their native response.
  # Copilot exposes terminal text separately in its JSON event stream.
  if [ "$label" != copilot ]; then
    expected=$'progress 1\nprogress 2\nprogress 3\nprogress 4\nfinal:output-alive'
  fi
  [ "$(cat "$case_dir/stdout")" = "$expected" ] || { fail "stdout must contain exactly the native response"; return 1; }
  contains "$case_dir/stderr" 'Crossfeed model receipt:' || { fail "stderr receipt missing"; return 1; }
  ! contains "$case_dir/stderr" 'LIMIT FIRED' || { fail "growing output was killed"; return 1; }
}

common_cpu_liveness() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-cpu")"
  FAKE_CPU_PID_FILE="$case_dir/cpu-pid" FAKE_PS_CPU_COUNTER="$case_dir/cpu-counter" \
    invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
      --prompt cpu-alive --dir "$case_dir/work" --idle-timeout 2 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected CPU-active rc 0, got $rc"; return 1; }
  contains "$case_dir/stdout" 'final:cpu-alive' || { fail "CPU-active final output missing"; return 1; }
  ! contains "$case_dir/stderr" 'IDLE LIMIT FIRED' || { fail "CPU progress did not reset idle timer"; return 1; }
}

common_descendant_cpu_liveness() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-descendant-cpu")"
  FAKE_CPU_GROUP_PGID_FILE="$case_dir/group-pgid" FAKE_PS_CPU_COUNTER="$case_dir/cpu-counter" \
    invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
      --prompt descendant-cpu --dir "$case_dir/work" --idle-timeout 2 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "$label quiet leader with CPU-active descendant was killed, rc $rc"; return 1; }
  contains "$case_dir/stdout" 'final:descendant-cpu' || { fail "$label descendant-CPU final output missing"; return 1; }
  ! contains "$case_dir/stderr" 'IDLE LIMIT FIRED' || { fail "$label watchdog ignored aggregate group CPU"; return 1; }
}

common_group_completion() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-group-completion")"
  FAKE_DESCENDANT_PID_FILE="$case_dir/descendant-pid" \
    FAKE_DESCENDANT_DONE_FILE="$case_dir/descendant-done" \
    invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
      --prompt orphan-descendant --dir "$case_dir/work" --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "$label leader status changed while waiting for descendants, rc $rc"; return 1; }
  [ -s "$case_dir/descendant-done" ] || { fail "$label wrapper returned before its descendant finished"; return 1; }
  assert_dead "$case_dir/descendant-pid" "$label orphan descendant" || return 1
  contains "$case_dir/stderr" 'still has live descendants' || { fail "$label continued group supervision was silent"; return 1; }
}

common_no_default_wall_cap() {
  local label="$1" wrapper="$2" case_dir rc started ended duration
  case_dir="$(new_case "$label-no-default")"
  started="$(/bin/date +%s)"
  FAKE_DATE_STATE="$case_dir/date-state" HOME="$case_dir/home" TMPDIR="$TMP_AREA" \
    PATH="$CLOCK_BIN:$BASE_PATH" /bin/bash "$wrapper" \
      --prompt no-default-wall-cap --dir "$case_dir/work" --idle-timeout 999999 --kill-after 1 \
      >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  ended="$(/bin/date +%s)"
  duration=$((ended - started))
  [ "$rc" -eq 0 ] || { fail "expected rc 0 without --timeout, got $rc"; return 1; }
  [ "$duration" -ge 4 ] || { fail "fake five-second run did not actually run for five seconds"; return 1; }
  contains "$case_dir/stdout" 'final:no-default-wall-cap' || { fail "five-second final output missing"; return 1; }
  ! contains "$case_dir/stderr" 'WALL-CLOCK LIMIT FIRED' || { fail "hidden wall cap fired"; return 1; }
}

common_idle_kill() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-idle")"
  invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt idle-silent --dir "$case_dir/work" --idle-timeout 1 --kill-after 1
  rc=$?
  [ "$rc" -eq 125 ] || { fail "expected rc 125, got $rc"; return 1; }
  contains "$case_dir/stderr" 'IDLE LIMIT FIRED after ' || { fail "idle label or elapsed seconds missing"; return 1; }
  contains "$case_dir/stderr" 'configured --idle-timeout 1s' || { fail "idle configuration missing"; return 1; }
  contains "$case_dir/stderr" 'working directory:' || { fail "working directory missing"; return 1; }
}

common_wall_kill() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-wall")"
  invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt wall-clock --dir "$case_dir/work" --timeout 1 --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 124 ] || { fail "expected rc 124, got $rc"; return 1; }
  contains "$case_dir/stderr" 'WALL-CLOCK LIMIT FIRED after ' || { fail "wall label or elapsed seconds missing"; return 1; }
  contains "$case_dir/stderr" 'configured --timeout 1s' || { fail "wall configuration missing"; return 1; }
  contains "$case_dir/stderr" 'working directory:' || { fail "working directory missing"; return 1; }
}

common_sigkill_escalation() {
  local label="$1" wrapper="$2" case_dir rc started ended duration pid_file
  case_dir="$(new_case "$label-sigkill")"
  pid_file="$case_dir/target-pid"
  started="$(/bin/date +%s)"
  FAKE_TARGET_PID_FILE="$pid_file" invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt ignore-term --dir "$case_dir/work" --timeout 1 --idle-timeout 20 --kill-after 1
  rc=$?
  ended="$(/bin/date +%s)"
  duration=$((ended - started))
  [ "$rc" -eq 124 ] || { fail "expected rc 124, got $rc"; return 1; }
  [ "$duration" -le 5 ] || { fail "TERM-ignoring child was not bounded: ${duration}s"; return 1; }
  contains "$case_dir/stderr" 'KILL-AFTER LIMIT FIRED' || { fail "SIGKILL escalation diagnostic missing"; return 1; }
  assert_dead "$pid_file" "$label TERM-ignoring child"
}

common_process_group_cleanup() {
  local label="$1" wrapper="$2" case_dir rc pid_file
  case_dir="$(new_case "$label-process-group")"
  pid_file="$case_dir/descendant-pid"
  FAKE_DESCENDANT_PID_FILE="$pid_file" invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt descendant --dir "$case_dir/work" --timeout 1 --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 124 ] || { fail "expected rc 124, got $rc"; return 1; }
  contains "$case_dir/stderr" 'process group' || { fail "process-group diagnostic missing"; return 1; }
  assert_dead "$pid_file" "$label descendant"
}

common_outer_warning() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-outer")"
  FAKE_PARENT_TIMEOUT=1 invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt quick --dir "$case_dir/work" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 0 ] || { fail "outer-warning run expected rc 0, got $rc"; return 1; }
  contains "$case_dir/stderr" "launched under an outer 'gtimeout' wall-clock cap" || { fail "outer-cap warning missing"; return 1; }
}

common_passthrough() {
  local label="$1" wrapper="$2" case_dir rc
  case_dir="$(new_case "$label-passthrough")"
  invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt nonzero --dir "$case_dir/work" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 42 ] || { fail "expected child rc 42 passthrough, got $rc"; return 1; }
  ! contains "$case_dir/stderr" 'LIMIT FIRED' || { fail "ordinary child failure mislabeled as limit"; return 1; }
}

common_fast_exit_passthrough() {
  local label="$1" wrapper="$2" diagnostic="$3" case_dir rc
  case_dir="$(new_case "$label-fast-exit")"
  FAKE_PGID_MODE=miss invoke "$wrapper" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt instant-42 --dir "$case_dir/work" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 42 ] || { fail "$label instant child expected rc 42, got $rc"; return 1; }
  contains "$case_dir/stderr" "$diagnostic" || { fail "$label instant child diagnostic was discarded"; return 1; }
  ! contains "$case_dir/stderr" 'PROCESS-GROUP DISCOVERY FAILURE' || { fail "$label completed child was mislabeled as discovery failure"; return 1; }
}

common_immediate_signal_cleanup() {
  local label="$1" wrapper="$2" case_dir pid_file marker wrapper_pid rc attempts=0
  case_dir="$(new_case "$label-immediate-signal")"
  pid_file="$case_dir/target-pid"
  marker="$case_dir/pgid-probe"
  FAKE_TARGET_PID_FILE="$pid_file" FAKE_PGID_MODE=miss FAKE_PGID_MARKER="$marker" \
    HOME="$case_dir/home" TMPDIR="$TMP_AREA" PATH="$BASE_PATH" \
    /bin/bash "$wrapper" --prompt cancel-setup --dir "$case_dir/work" \
      --idle-timeout 20 --kill-after 1 >"$case_dir/stdout" 2>"$case_dir/stderr" &
  wrapper_pid=$!

  if ! wait_for_file "$pid_file" || ! wait_for_file "$marker"; then
    kill -KILL "$wrapper_pid" 2>/dev/null || true
    wait "$wrapper_pid" 2>/dev/null || true
    fail "$label launch-window fixture did not reach PGID discovery"
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
    fail "$label wrapper did not terminate after immediate TERM"
    return 1
  fi
  wait "$wrapper_pid"
  rc=$?
  [ "$rc" -eq 143 ] || { fail "$label immediate TERM expected rc 143, got $rc"; return 1; }
  assert_dead "$pid_file" "$label child" || return 1
  contains "$case_dir/stderr" 'EXTERNAL TERM RECEIVED' || { fail "$label pre-launch TERM trap did not run"; return 1; }
}

test_sibling_fast_exit_passthrough() {
  common_fast_exit_passthrough claude "$CLAUDE_WRAPPER" 'instant fake claude diagnostic' || return 1
  common_fast_exit_passthrough agy "$AGY_WRAPPER" 'instant fake agy diagnostic' || return 1
  common_fast_exit_passthrough copilot "$COPILOT_WRAPPER" 'instant fake copilot diagnostic'
}

test_sibling_immediate_signal_cleanup() {
  common_immediate_signal_cleanup claude "$CLAUDE_WRAPPER" || return 1
  common_immediate_signal_cleanup agy "$AGY_WRAPPER" || return 1
  common_immediate_signal_cleanup copilot "$COPILOT_WRAPPER"
}

test_agy_tty_bridge_stdin() {
  local case_dir transcript rc
  case_dir="$(new_case agy-tty-stdin)"
  transcript="$case_dir/typescript"
  [ -x /usr/bin/script ] || { fail "/usr/bin/script is required for the real-TTY regression"; return 1; }

  "$REAL_PYTHON3" - "$AGY_WRAPPER" "$case_dir" "$BASE_PATH" "$TMP_AREA" "$transcript" <<'PY'
import os
import signal
import subprocess
import sys

wrapper, case_dir, base_path, tmp_area, transcript = sys.argv[1:]
stdout_path = os.path.join(case_dir, "stdout")
stderr_path = os.path.join(case_dir, "stderr")
inner = [
    "/usr/bin/env",
    "HOME=" + os.path.join(case_dir, "home"),
    "TMPDIR=" + tmp_area,
    "PATH=" + base_path,
    "/bin/bash", wrapper,
    "--prompt", "tty-stdin",
    "--dir", os.path.join(case_dir, "work"),
    "--idle-timeout", "20",
    "--kill-after", "1",
]
# BSD script (macOS) takes the command after the file; util-linux script (Linux) takes it as one
# string after -c, and needs -e to return the command's exit status.
probe = subprocess.run(["/usr/bin/script", "--version"], capture_output=True, text=True)
if "util-linux" in (probe.stdout + probe.stderr):
    import shlex
    cmd = ["/usr/bin/script", "-q", "-e", "-c", " ".join(shlex.quote(a) for a in inner), transcript]
else:
    cmd = ["/usr/bin/script", "-q", transcript] + inner
with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=out,
        stderr=err,
        start_new_session=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(b"make-the-controlling-tty-readable\n")
    proc.stdin.flush()
    proc.stdin.close()
    try:
        rc = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        rc = 99
sys.exit(rc)
PY
  rc=$?
  [ "$rc" -eq 0 ] || { fail "TTY-backed AGY wrapper did not complete, rc $rc"; return 1; }
  contains "$case_dir/stdout" 'final:tty-stdin' || { fail "AGY did not retain PTY stdin while bridge stdin was detached"; return 1; }
}

test_claude_output_liveness() { common_output_liveness claude "$CLAUDE_WRAPPER"; }
test_claude_cpu_liveness() { common_cpu_liveness claude "$CLAUDE_WRAPPER"; }
test_claude_descendant_cpu_liveness() { common_descendant_cpu_liveness claude "$CLAUDE_WRAPPER"; }
test_claude_group_completion() { common_group_completion claude "$CLAUDE_WRAPPER"; }
test_claude_no_default_wall_cap() { common_no_default_wall_cap claude "$CLAUDE_WRAPPER"; }
test_claude_idle_kill() { common_idle_kill claude "$CLAUDE_WRAPPER"; }
test_claude_wall_kill() { common_wall_kill claude "$CLAUDE_WRAPPER"; }
test_claude_sigkill_escalation() { common_sigkill_escalation claude "$CLAUDE_WRAPPER"; }
test_claude_process_group_cleanup() { common_process_group_cleanup claude "$CLAUDE_WRAPPER"; }
test_claude_outer_warning() { common_outer_warning claude "$CLAUDE_WRAPPER"; }
test_claude_passthrough() { common_passthrough claude "$CLAUDE_WRAPPER"; }

test_claude_usage_exit() {
  local case_dir rc
  case_dir="$(new_case claude-usage)"
  invoke "$CLAUDE_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --write
  rc=$?
  [ "$rc" -eq 2 ] || { fail "expected usage rc 2, got $rc"; return 1; }
}

test_claude_empty_exit() {
  local case_dir rc
  case_dir="$(new_case claude-empty)"
  invoke "$CLAUDE_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt empty --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 4 ] || { fail "expected empty-output rc 4, got $rc"; return 1; }
  contains "$case_dir/stderr" 'EMPTY OUTPUT' || { fail "empty-output diagnostic missing"; return 1; }
}

test_claude_missing_exit() {
  local case_dir rc
  case_dir="$(new_case claude-missing)"
  HOME="$case_dir/home" PATH="$EMPTY_BIN:/usr/bin:/bin" /bin/bash "$CLAUDE_WRAPPER" --prompt quick \
    >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 127 ] || { fail "expected missing-binary rc 127, got $rc"; return 1; }
}

test_claude_interface() {
  local case_dir rc
  case_dir="$(new_case claude-interface)"
  FAKE_ARGS_FILE="$case_dir/args" invoke "$CLAUDE_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt quick --dir "$case_dir/work" --read-only --last "$case_dir/last" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 0 ] || { fail "interface run expected rc 0, got $rc"; return 1; }
  contains "$case_dir/args" 'plan' || { fail "read-only permission mode not forwarded"; return 1; }
  contains "$case_dir/args" 'Read,Grep,Glob' || { fail "read-only tool set not forwarded"; return 1; }
  contains "$case_dir/last" 'final:quick' || { fail "--last output missing"; return 1; }
}

test_claude_help_contract() {
  local case_dir rc
  case_dir="$(new_case claude-help)"
  PATH="/usr/bin:/bin" /bin/bash "$CLAUDE_WRAPPER" -h >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "help expected rc 0, got $rc"; return 1; }
  contains "$case_dir/stdout" '124 wall-clock kill' || { fail "wall exit missing from header"; return 1; }
  contains "$case_dir/stdout" '125 idle kill' || { fail "idle exit missing from header"; return 1; }
  contains "$case_dir/stdout" '4 empty output' || { fail "Claude special exit missing from header"; return 1; }
}

test_claude_stderr_streaming() {
  local case_dir wrapper_pid rc progress_count
  case_dir="$(new_case claude-stderr-streaming)"
  FAKE_TEE_PID_FILE="$case_dir/tee-pid" HOME="$case_dir/home" TMPDIR="$TMP_AREA" PATH="$BASE_PATH" \
    /bin/bash "$CLAUDE_WRAPPER" --prompt stderr-progress --dir "$case_dir/work" \
      --idle-timeout 20 --kill-after 1 >"$case_dir/stdout" 2>"$case_dir/stderr" &
  wrapper_pid=$!

  if ! wait_for_contains "$case_dir/stderr" 'live-stderr-progress'; then
    kill -TERM "$wrapper_pid" 2>/dev/null || true
    wait "$wrapper_pid" 2>/dev/null || true
    fail "Claude stderr progress was not visible while the run was live"
    return 1
  fi
  kill -0 "$wrapper_pid" 2>/dev/null || { fail "stderr appeared only after the wrapper exited"; return 1; }

  wait "$wrapper_pid"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "stderr-streaming run expected rc 0, got $rc"; return 1; }
  progress_count="$(grep -c 'live-stderr-progress' "$case_dir/stderr")"
  [ "$progress_count" -eq 1 ] || { fail "stderr progress was duplicated or lost: count $progress_count"; return 1; }
  assert_dead "$case_dir/tee-pid" "Claude stderr tee" || return 1
}

test_agy_output_liveness() { common_output_liveness agy "$AGY_WRAPPER"; }
test_agy_cpu_liveness() { common_cpu_liveness agy "$AGY_WRAPPER"; }
test_agy_descendant_cpu_liveness() { common_descendant_cpu_liveness agy "$AGY_WRAPPER"; }
test_agy_group_completion() { common_group_completion agy "$AGY_WRAPPER"; }
test_agy_no_default_wall_cap() { common_no_default_wall_cap agy "$AGY_WRAPPER"; }
test_agy_idle_kill() { common_idle_kill agy "$AGY_WRAPPER"; }
test_agy_wall_kill() { common_wall_kill agy "$AGY_WRAPPER"; }
test_agy_sigkill_escalation() { common_sigkill_escalation agy "$AGY_WRAPPER"; }
test_agy_process_group_cleanup() { common_process_group_cleanup agy "$AGY_WRAPPER"; }
test_agy_outer_warning() { common_outer_warning agy "$AGY_WRAPPER"; }
test_agy_passthrough() { common_passthrough agy "$AGY_WRAPPER"; }

test_agy_usage_exit() {
  local case_dir rc
  case_dir="$(new_case agy-usage)"
  invoke "$AGY_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --bogus
  rc=$?
  [ "$rc" -eq 2 ] || { fail "expected usage rc 2, got $rc"; return 1; }
}

test_agy_missing_exit() {
  local case_dir rc
  case_dir="$(new_case agy-missing)"
  HOME="$case_dir/home" PATH="$EMPTY_BIN:/usr/bin:/bin" /bin/bash "$AGY_WRAPPER" --prompt quick \
    >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 127 ] || { fail "expected missing-binary rc 127, got $rc"; return 1; }
}

test_agy_quota_exit() {
  local case_dir rc
  case_dir="$(new_case agy-quota)"
  mkdir -p "$case_dir/home/.gemini/antigravity-cli"
  printf '%s\n' 'RESOURCE_EXHAUSTED fake marker' >"$case_dir/home/.gemini/antigravity-cli/cli.log"
  invoke "$AGY_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt empty --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 3 ] || { fail "expected quota rc 3, got $rc"; return 1; }
  contains "$case_dir/stderr" 'AGY QUOTA EXHAUSTED' || { fail "quota diagnostic missing"; return 1; }
}

test_agy_empty_exit() {
  local case_dir rc
  case_dir="$(new_case agy-empty)"
  invoke "$AGY_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt empty --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 4 ] || { fail "expected empty-output rc 4, got $rc"; return 1; }
}

test_agy_route_exit() {
  local case_dir rc
  case_dir="$(new_case agy-route)"
  FAKE_ROUTE_FAIL=1 invoke "$AGY_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt quick --dir "$case_dir/work" --role reviewer
  rc=$?
  [ "$rc" -eq 5 ] || { fail "expected no-route rc 5, got $rc"; return 1; }
}

test_agy_interface() {
  local case_dir rc
  case_dir="$(new_case agy-interface)"
  FAKE_ARGS_FILE="$case_dir/args" invoke "$AGY_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
    --prompt quick --dir "$case_dir/work" --model fake-model --sandbox \
    --raw "$case_dir/raw" --last "$case_dir/last" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 0 ] || { fail "interface run expected rc 0, got $rc"; return 1; }
  contains "$case_dir/args" '--add-dir' || { fail "workspace grant not forwarded"; return 1; }
  contains "$case_dir/args" '--sandbox' || { fail "sandbox flag not forwarded"; return 1; }
  contains "$case_dir/raw" 'final:quick' || { fail "raw PTY capture missing"; return 1; }
  contains "$case_dir/last" 'final:quick' || { fail "--last output missing"; return 1; }
}

test_agy_help_contract() {
  local case_dir rc
  case_dir="$(new_case agy-help)"
  PATH="/usr/bin:/bin" /bin/bash "$AGY_WRAPPER" -h >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "help expected rc 0, got $rc"; return 1; }
  contains "$case_dir/stdout" '124 wall-clock kill' || { fail "wall exit missing from header"; return 1; }
  contains "$case_dir/stdout" '125 idle kill' || { fail "idle exit missing from header"; return 1; }
  contains "$case_dir/stdout" '3 quota exhausted' || { fail "AGY quota exit missing from header"; return 1; }
  contains "$case_dir/stdout" '5 no route or lease' || { fail "AGY route exit missing from header"; return 1; }
}

test_copilot_output_liveness() { common_output_liveness copilot "$COPILOT_WRAPPER"; }
test_copilot_cpu_liveness() { common_cpu_liveness copilot "$COPILOT_WRAPPER"; }
test_copilot_descendant_cpu_liveness() { common_descendant_cpu_liveness copilot "$COPILOT_WRAPPER"; }
test_copilot_group_completion() { common_group_completion copilot "$COPILOT_WRAPPER"; }
test_copilot_no_default_wall_cap() { common_no_default_wall_cap copilot "$COPILOT_WRAPPER"; }
test_copilot_idle_kill() { common_idle_kill copilot "$COPILOT_WRAPPER"; }
test_copilot_wall_kill() { common_wall_kill copilot "$COPILOT_WRAPPER"; }
test_copilot_sigkill_escalation() { common_sigkill_escalation copilot "$COPILOT_WRAPPER"; }
test_copilot_process_group_cleanup() { common_process_group_cleanup copilot "$COPILOT_WRAPPER"; }
test_copilot_outer_warning() { common_outer_warning copilot "$COPILOT_WRAPPER"; }
test_copilot_passthrough() { common_passthrough copilot "$COPILOT_WRAPPER"; }

test_copilot_write_exit() {
  local case_dir rc
  case_dir="$(new_case copilot-write)"
  invoke "$COPILOT_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --write
  rc=$?
  [ "$rc" -eq 2 ] || { fail "expected write-refusal rc 2, got $rc"; return 1; }
}

test_copilot_missing_exit() {
  local case_dir rc
  case_dir="$(new_case copilot-missing)"
  HOME="$case_dir/home" PATH="$EMPTY_BIN:/usr/bin:/bin" /bin/bash "$COPILOT_WRAPPER" --prompt quick \
    >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 127 ] || { fail "expected missing-binary rc 127, got $rc"; return 1; }
}

test_copilot_invalid_json_exit() {
  local case_dir rc
  case_dir="$(new_case copilot-invalid)"
  invoke "$COPILOT_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt invalid-json --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 4 ] || { fail "expected invalid-JSON rc 4, got $rc"; return 1; }
}

test_copilot_result_exit() {
  local case_dir rc
  case_dir="$(new_case copilot-result)"
  invoke "$COPILOT_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt bad-result --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 5 ] || { fail "expected result rc 5, got $rc"; return 1; }
}

test_copilot_empty_exit() {
  local case_dir rc
  case_dir="$(new_case copilot-empty)"
  invoke "$COPILOT_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt empty --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 6 ] || { fail "expected empty-answer rc 6, got $rc"; return 1; }
}

test_copilot_roster_passthrough() {
  local case_dir rc
  case_dir="$(new_case copilot-roster)"
  FAKE_ROSTER_RC=9 invoke "$COPILOT_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt quick --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 9 ] || { fail "expected roster rc 9 passthrough, got $rc"; return 1; }
}

test_copilot_interface() {
  local case_dir rc
  case_dir="$(new_case copilot-interface)"
  COPILOT_GITHUB_TOKEN=secret GH_TOKEN=secret GITHUB_TOKEN=secret \
    FAKE_ARGS_FILE="$case_dir/args" FAKE_TOKEN_STATE="$case_dir/tokens" \
    invoke "$COPILOT_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" \
      --prompt quick --dir "$case_dir/work" --events "$case_dir/events" --last "$case_dir/last" --idle-timeout 20
  rc=$?
  [ "$rc" -eq 0 ] || { fail "interface run expected rc 0, got $rc"; return 1; }
  contains "$case_dir/args" '--deny-tool=shell' || { fail "shell denial missing"; return 1; }
  contains "$case_dir/args" '--deny-tool=write' || { fail "write denial missing"; return 1; }
  contains "$case_dir/args" '--no-remote' || { fail "remote denial missing"; return 1; }
  contains "$case_dir/tokens" 'COPILOT_GITHUB_TOKEN=unset' || { fail "Copilot token was not unset"; return 1; }
  contains "$case_dir/tokens" 'GH_TOKEN=unset' || { fail "GH token was not unset"; return 1; }
  contains "$case_dir/tokens" 'GITHUB_TOKEN=unset' || { fail "GitHub token was not unset"; return 1; }
  contains "$case_dir/last" 'final:quick' || { fail "--last output missing"; return 1; }
}

test_copilot_service_chosen_effort() {
  local case_dir rc
  case_dir="$(new_case copilot-service-effort)"
  invoke "$COPILOT_WRAPPER" "$case_dir" "$case_dir/stdout" "$case_dir/stderr" --prompt quick --dir "$case_dir/work"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected success, got $rc"; return 1; }
  [ "$(grep -c 'effort service-chosen: the service chooses the level' "$case_dir/stderr")" -eq 1 ] || { fail "service effort notice must appear exactly once"; return 1; }
  contains "$case_dir/stdout" 'final:quick' || { fail "effort notice changed deliverable"; return 1; }
}

test_copilot_help_contract() {
  local case_dir rc
  case_dir="$(new_case copilot-help)"
  PATH="/usr/bin:/bin" /bin/bash "$COPILOT_WRAPPER" -h >"$case_dir/stdout" 2>"$case_dir/stderr"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "help expected rc 0, got $rc"; return 1; }
  contains "$case_dir/stdout" '124 wall-clock kill' || { fail "wall exit missing from header"; return 1; }
  contains "$case_dir/stdout" '125 idle kill' || { fail "idle exit missing from header"; return 1; }
  contains "$case_dir/stdout" '4 invalid JSON' || { fail "Copilot JSON exit missing from header"; return 1; }
  contains "$case_dir/stdout" '6 empty answer' || { fail "Copilot empty exit missing from header"; return 1; }
}

selected() {
  local wanted="$1" item
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

run_test claude_output_liveness test_claude_output_liveness
run_test claude_cpu_liveness test_claude_cpu_liveness
run_test claude_descendant_cpu_liveness test_claude_descendant_cpu_liveness
run_test claude_group_completion test_claude_group_completion
run_test claude_no_default_wall_cap test_claude_no_default_wall_cap
run_test claude_idle_kill test_claude_idle_kill
run_test claude_wall_kill test_claude_wall_kill
run_test claude_sigkill_escalation test_claude_sigkill_escalation
run_test claude_process_group_cleanup test_claude_process_group_cleanup
run_test claude_outer_warning test_claude_outer_warning
run_test claude_usage_exit test_claude_usage_exit
run_test claude_empty_exit test_claude_empty_exit
run_test claude_missing_exit test_claude_missing_exit
run_test claude_passthrough test_claude_passthrough
run_test claude_interface test_claude_interface
run_test claude_help_contract test_claude_help_contract
run_test claude_stderr_streaming test_claude_stderr_streaming

run_test sibling_fast_exit_passthrough test_sibling_fast_exit_passthrough
run_test sibling_immediate_signal_cleanup test_sibling_immediate_signal_cleanup

run_test agy_output_liveness test_agy_output_liveness
run_test agy_cpu_liveness test_agy_cpu_liveness
run_test agy_descendant_cpu_liveness test_agy_descendant_cpu_liveness
run_test agy_group_completion test_agy_group_completion
run_test agy_no_default_wall_cap test_agy_no_default_wall_cap
run_test agy_idle_kill test_agy_idle_kill
run_test agy_wall_kill test_agy_wall_kill
run_test agy_sigkill_escalation test_agy_sigkill_escalation
run_test agy_process_group_cleanup test_agy_process_group_cleanup
run_test agy_outer_warning test_agy_outer_warning
run_test agy_usage_exit test_agy_usage_exit
run_test agy_missing_exit test_agy_missing_exit
run_test agy_quota_exit test_agy_quota_exit
run_test agy_empty_exit test_agy_empty_exit
run_test agy_route_exit test_agy_route_exit
run_test agy_passthrough test_agy_passthrough
run_test agy_interface test_agy_interface
run_test agy_help_contract test_agy_help_contract
run_test agy_tty_bridge_stdin test_agy_tty_bridge_stdin

test_roster_effort() {
  local d="$TEST_ROOT/effort" wrapper rc
  mkdir -p "$d/work"
  for wrapper in "$CLAUDE_WRAPPER" "$AGY_WRAPPER"; do
    FAKE_EFFORT_DEFAULT=medium FAKE_ARGS_FILE="$d/args" PATH="$BASE_PATH" /bin/bash "$wrapper" --prompt quick --dir "$d/work" --role probe >"$d/out" 2>"$d/err"
    rc=$?
    [ "$rc" -eq 0 ] && contains "$d/args" '--effort' && contains "$d/args" 'medium' || { fail "$(basename "$wrapper") failed roster medium, rc $rc"; return 1; }
    contains "$d/err" 'effort medium: roster' || { fail "effort source not announced"; return 1; }
    FAKE_EFFORT_DEFAULT=medium FAKE_ARGS_FILE="$d/args" PATH="$BASE_PATH" /bin/bash "$wrapper" --prompt quick --dir "$d/work" --role probe --effort low >"$d/out" 2>"$d/err"
    [ "$?" -eq 0 ] && contains "$d/args" 'low' && contains "$d/err" 'explicit caller level' || { fail "caller effort override lost"; return 1; }
    FAKE_EFFORT_MISSING=1 FAKE_ARGS_FILE="$d/$(basename "$wrapper")-missing-args" PATH="$BASE_PATH" /bin/bash "$wrapper" --prompt quick --dir "$d/work" >"$d/out" 2>"$d/err"
    [ "$?" -eq 2 ] && [ ! -e "$d/$(basename "$wrapper")-missing-args" ] || { fail "missing effort launched $(basename "$wrapper")"; return 1; }
  done
}
run_test roster_effort test_roster_effort

run_test copilot_output_liveness test_copilot_output_liveness
run_test copilot_cpu_liveness test_copilot_cpu_liveness
run_test copilot_descendant_cpu_liveness test_copilot_descendant_cpu_liveness
run_test copilot_group_completion test_copilot_group_completion
run_test copilot_no_default_wall_cap test_copilot_no_default_wall_cap
run_test copilot_idle_kill test_copilot_idle_kill
run_test copilot_wall_kill test_copilot_wall_kill
run_test copilot_sigkill_escalation test_copilot_sigkill_escalation
run_test copilot_process_group_cleanup test_copilot_process_group_cleanup
run_test copilot_outer_warning test_copilot_outer_warning
run_test copilot_write_exit test_copilot_write_exit
run_test copilot_missing_exit test_copilot_missing_exit
run_test copilot_invalid_json_exit test_copilot_invalid_json_exit
run_test copilot_result_exit test_copilot_result_exit
run_test copilot_empty_exit test_copilot_empty_exit
run_test copilot_roster_passthrough test_copilot_roster_passthrough
run_test copilot_passthrough test_copilot_passthrough
run_test copilot_interface test_copilot_interface
run_test copilot_service_chosen_effort test_copilot_service_chosen_effort
run_test copilot_help_contract test_copilot_help_contract

if [ "$RUN_COUNT" -eq 0 ]; then
  printf 'FAIL no matching tests selected\n'
  exit 2
fi

printf 'RESULT: %s passed, %s failed, %s total\n' "$PASS_COUNT" "$FAIL_COUNT" "$RUN_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
