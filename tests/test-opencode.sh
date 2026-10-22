#!/usr/bin/env bash
# Deterministic tests for scripts/opencode-agent.sh. The real opencode binary is never used.
set -u
set -o pipefail
# Isolation is by HOME; a machine-wide XDG_* setting would outrank it (see run-all.sh).
unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Works whether this file sits beside scripts/ (dev tree) or in tests/ (installed skill).
[ -d "$SCRIPT_DIR/scripts" ] || SCRIPT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_WRAPPER="${WRAPPER_UNDER_TEST:-$SCRIPT_DIR/scripts/opencode-agent.sh}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/opencode-agent-test.XXXXXX")"
FIXTURE_DIR="$TEST_ROOT/fixture/scripts"
FAKE_BIN="$TEST_ROOT/bin"
CLOCK_BIN="$TEST_ROOT/clock-bin"
TEST_HOME="$TEST_ROOT/home"
TEST_TMP="$TEST_ROOT/tmp"
FLEET_STATE="$TEST_ROOT/fleet-state"
mkdir -p "$FIXTURE_DIR" "$FAKE_BIN" "$CLOCK_BIN" "$TEST_HOME" "$TEST_TMP" "$FLEET_STATE"
cp "$SOURCE_WRAPPER" "$FIXTURE_DIR/opencode-agent.sh"
cp "$SCRIPT_DIR/scripts/opencode_v2.py" "$FIXTURE_DIR/"
cp "$SCRIPT_DIR/scripts/run-identity.sh" "$SCRIPT_DIR/scripts/run_identity.py" "$SCRIPT_DIR/scripts/vault_guard.py" "$FIXTURE_DIR/"
export FLEET_STATE_DIR="$TEST_ROOT/model-state"
# Ordinary receipts must work even with an optional gateway configured but down.
# A supplied overlay still exercises the caller's reproducer; the default is
# entirely synthetic and never reaches the operator's keys or roster.
if [ -z "${ACCESS_OVERLAY:-}" ]; then
  export ACCESS_OVERLAY="$TEST_ROOT/access-overlay.json"
  printf 'fake-gateway-key\n' >"$TEST_ROOT/gateway-key"
  jq --arg key "$TEST_ROOT/gateway-key" '
    .chatgpt_gateway.lane_template.auth.key_file = $key |
    .chatgpt_gateway.lane_template.transport.api_base = "http://127.0.0.1:1/v1"
  ' "$SCRIPT_DIR/examples/access-overlay.example.json" >"$ACCESS_OVERLAY"
fi
chmod +x "$FIXTURE_DIR/opencode-agent.sh"
WRAPPER="$FIXTURE_DIR/opencode-agent.sh"

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
    "${TMPDIR:-/tmp}"/opencode-agent-test.*) rm -rf "$TEST_ROOT";;
  esac
}
trap cleanup EXIT

# Minimal roster surface used by the wrapper. Timeout and admitted modalities are
# varied per test, while selector/routing stay deterministic.
cat >"$FIXTURE_DIR/roster.sh" <<'FAKE_ROSTER'
#!/bin/bash
set -u
case "${1:-}" in
  resolve-lane|lookup)
    printf 'fake-lane\n'
    ;;
  lane-json)
    printf '{"harness":"opencode","selector":"fake/model","timeout_s":%s,"capabilities":{"input":%s}}\n' \
      "${FAKE_ROSTER_TIMEOUT:-999999}" "${FAKE_CAPABILITIES_JSON:-[\"text\",\"image\"]}"
    ;;
  check-lane)
    [ -z "${FAKE_CHECK_LANE_FILE:-}" ] || printf '%s\n' "$*" >"$FAKE_CHECK_LANE_FILE"
    exit "${FAKE_CHECK_LANE_RC:-0}"
    ;;
  *) exit 2;;
esac
FAKE_ROSTER

# Minimal fleet controller surface. It records lease TTL, route arguments, and
# telemetry return code so the tests can assert the wrapper's existing protocol.
cat >"$FIXTURE_DIR/fleetctl.py" <<'FAKE_FLEET'
#!/bin/bash
set -u
command_name="${1:-}"
shift || true
case "$command_name" in
  effort)
    [ "${FAKE_EFFORT_MISSING:-0}" = 0 ] || exit 2
    model="$1"; role="$2"; shift 2
    level="${FAKE_EFFORT_DEFAULT:-max}"; why="roster fake/$role"
    while [ "$#" -gt 0 ]; do
      case "$1" in --level) level="$2"; why='explicit caller level'; shift 2;; *) shift;; esac
    done
    [ -z "${FAKE_EFFORT_QUERY_FILE:-}" ] || printf '%s\n' "$role" >"$FAKE_EFFORT_QUERY_FILE"
    printf '%s\t%s\t%s\n' "$level" "$model" "$why"
    ;;
  route)
    [ -z "${FAKE_ROUTE_FILE:-}" ] || printf '%s\n' "$*" >"$FAKE_ROUTE_FILE"
    printf '%s\n' "${FAKE_ROUTE_LANE:-fake-lane}"
    ;;
  acquire)
    if [ "${FAKE_LEASE_CONTENTION:-0}" = "1" ]; then
      echo 'lane fake-lane has 1 active lease(s), cap is 1' >&2
      exit 1
    fi
    if [ "${FAKE_LEASE_ERROR:-0}" = "1" ]; then
      echo 'quota pool unavailable' >&2
      exit 1
    fi
    ttl=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --ttl) ttl="$2"; shift 2;;
        *) shift;;
      esac
    done
    [ -z "${FAKE_TTL_FILE:-}" ] || printf '%s\n' "$ttl" >"$FAKE_TTL_FILE"
    printf 'fake-token\n'
    ;;
  release)
    exit 0
    ;;
  record)
    returncode=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --returncode) returncode="$2"; shift 2;;
        *) shift;;
      esac
    done
    [ -z "${FAKE_RECORD_FILE:-}" ] || printf '%s\n' "$returncode" >"$FAKE_RECORD_FILE"
    if [ "${FAKE_TELEMETRY_FAIL:-0}" = "1" ]; then
      echo 'intentional telemetry failure' >&2
      exit 1
    fi
    ;;
  *) exit 2;;
esac
FAKE_FLEET

cat >"$FAKE_BIN/opencode" <<'FAKE_OPENCODE'
#!/bin/bash
set -u
for arg in "$@"; do
  case "$arg" in --pure|--dir|--variant) echo "v1 flag rejected: $arg" >&2; exit 2;; esac
done
[ -z "${FAKE_ARGS_FILE:-}" ] || printf '%s\n' "$@" >"$FAKE_ARGS_FILE"
[ -z "${FAKE_ENV_FILE:-}" ] || {
  printf 'cwd=%s\n' "$(pwd -P)"
  printf 'config=%s\n' "${OPENCODE_CONFIG_DIR:-}"
  printf 'project_config=%s\n' "${OPENCODE_DISABLE_PROJECT_CONFIG:-}"
  printf 'data_home=%s\n' "${XDG_DATA_HOME:-}"
  if [ -f "${XDG_DATA_HOME:-/nonexistent}/opencode/opencode.db" ]; then printf 'db=present\n'; fi
  if [ -f "${XDG_DATA_HOME:-/nonexistent}/opencode/auth.json" ]; then printf 'auth=present\n'; else printf 'auth=missing\n'; fi
} >"$FAKE_ENV_FILE"

prompt=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--" ]; then shift; prompt="${1:-}"; break; fi
  shift
done

success() {
  printf '{"type":"step_start","part":{"messageID":"final"}}\n'
  printf '{"type":"text","part":{"messageID":"final","text":"final:%s"}}\n' "$prompt"
}

# The fake's behavioral mode is the task payload; recorded argv still proves worker awareness.
prompt="${prompt##*=== CROSSFEED TASK ===$'\n'}"
case "$prompt" in
  output-alive)
    i=1
    while [ "$i" -le 5 ]; do
      printf '{"type":"progress","n":%s}\n' "$i"
      sleep 1
      i=$((i + 1))
    done
    success
    ;;
  cpu-alive)
    printf '%s\n' "$$" >"${FAKE_CPU_PID_FILE:?}"
    end=$((SECONDS + 5))
    while [ "$SECONDS" -lt "$end" ]; do :; done
    success
    ;;
  descendant-cpu)
    printf '%s\n' "$$" >"${FAKE_CPU_GROUP_PGID_FILE:?}"
    (
      end=$((SECONDS + 5))
      while [ "$SECONDS" -lt "$end" ]; do :; done
    ) &
    wait "$!"
    success
    ;;
  orphan-descendant)
    (
      trap '' HUP
      exec </dev/null >/dev/null 2>&1
      sleep 3
      printf 'done\n' >"${FAKE_DESCENDANT_DONE_FILE:?}"
    ) &
    printf '%s\n' "$!" >"${FAKE_DESCENDANT_PID_FILE:?}"
    success
    ;;
  no-silent-wall)
    sleep 5
    success
    ;;
  wall|roster-wall|idle-silent)
    sleep 10
    success
    ;;
  ignore-term)
    trap '' TERM
    sleep 10
    success
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
  invalid-json)
    printf 'not-json\n'
    ;;
  policy-probe)
    python3 - <<'POLICY_PROBE'
import fnmatch, json, os
from pathlib import Path
config = json.loads((Path(os.environ['OPENCODE_CONFIG_DIR']) / 'opencode.json').read_text())
agent = 'direct' if '--agent' not in __import__('sys').argv else 'plan'
agent = next(iter(config['agents']))
action = os.environ['FAKE_PROBE_ACTION']
effect = 'allow'
for rule in config['permissions'] + config['agents'][agent]['permissions']:
    if fnmatch.fnmatchcase(action, rule['action']) and fnmatch.fnmatchcase('*', rule['resource']):
        effect = rule['effect']
if effect == 'allow':
    Path(os.environ['FAKE_PROBE_MARKER']).write_text('operation permitted')
print(json.dumps({'type': 'step_start'}))
print(json.dumps({'type': 'text', 'part': {'text': effect}}))
POLICY_PROBE
    ;;
  auth-error|auth-error-nonzero)
    printf '{"type":"step_start"}\n{"type":"text","part":{"text":"must-not-publish"}}\n{"type":"error","error":{"type":"provider.auth","message":"expired"}}\n'
    [ "$prompt" != auth-error-nonzero ] || exit 1
    ;;
  real-v2)
    cat "${V2_FIXTURE:?}"
    ;;
  multi-text)
    printf '%s\n' '{"type":"step_start"}' '{"type":"text","part":{"text":"intermediate"}}' '{"type":"step_finish","part":{"reason":"tool-calls"}}' '{"type":"step_start"}' '{"type":"text","part":{"text":"final "}}' '{"type":"text","part":{"text":"answer"}}'
    ;;
  final-empty)
    printf '%s\n' '{"type":"step_start"}' '{"type":"text","part":{"text":"stale"}}' '{"type":"step_start"}'
    ;;
  session-error)
    printf '{"type":"error","error":{"type":"provider.failure","message":"fake"}}\n'
    printf '{"type":"text","part":{"text":"must-not-publish"}}\n'
    printf '{"type":"step_finish","part":{"reason":"stop"}}\n'
    ;;
  no-terminal)
    printf '{"type":"text","part":{"text":"unfinished"}}\n'
    ;;
  empty-output)
    printf '{"type":"step_finish","part":{"reason":"stop"}}\n'
    ;;
  nonzero)
    echo 'intentional fake OpenCode failure' >&2
    exit 42
    ;;
  *) success;;
esac
FAKE_OPENCODE

# The managed sandbox can block host ps. This implements exactly the wrapper's
# three ps query forms. Real process groups and negative-PGID signals still hit
# the kernel; only observation is deterministic.
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
    [ -z "${FAKE_PARENT_COMM:-}" ] || printf '%s\n' "$FAKE_PARENT_COMM"
    ;;
  pgid=)
    [ "${FAKE_PGID_MODE:-ok}" = "miss" ] || printf ' %s\n' "$pid"
    ;;
  time=)
    if [ -n "${FAKE_CPU_PID_FILE:-}" ] && [ -f "$FAKE_CPU_PID_FILE" ] &&
       [ "$(cat "$FAKE_CPU_PID_FILE")" = "$pid" ]; then
      counter=0
      [ ! -f "${FAKE_PS_CPU_COUNTER:?}" ] || counter="$(cat "$FAKE_PS_CPU_COUNTER")"
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
    [ ! -f "${FAKE_PS_CPU_COUNTER:?}" ] || counter="$(cat "$FAKE_PS_CPU_COUNTER")"
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

# The 601-second virtual jumps expose a reintroduced literal 600-second default
# in one real watchdog tick. Other date forms retain the host behavior.
cat >"$CLOCK_BIN/date" <<'FAKE_DATE'
#!/bin/bash
set -u
if [ "$#" -eq 1 ] && [ "$1" = "+%s" ]; then
  state="${FAKE_DATE_STATE:?}"
  if [ -f "$state" ]; then value="$(cat "$state")"; value=$((value + 601)); else value=100000; fi
  printf '%s\n' "$value" >"$state"
  printf '%s\n' "$value"
else
  /bin/date "$@"
fi
FAKE_DATE

cat >"$FAKE_BIN/verify-end" <<'FAKE_VERIFY'
#!/bin/bash
exit 0
FAKE_VERIFY

chmod +x "$FIXTURE_DIR/roster.sh" "$FIXTURE_DIR/fleetctl.py" \
  "$FAKE_BIN/opencode" "$FAKE_BIN/ps" "$FAKE_BIN/verify-end" "$CLOCK_BIN/date"

mkdir -p "$TEST_HOME/.config/opencode/fleet-worker" "$TEST_HOME/.local/share/opencode"
printf '{}\n' >"$TEST_HOME/.config/opencode/fleet-worker/opencode.jsonc"
printf '# fake lean context\n' >"$TEST_HOME/.config/opencode/fleet-worker/AGENTS.md"
printf '{}\n' >"$TEST_HOME/.local/share/opencode/auth.json"

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
  LAST_STDERR="$stderr_file"
  HOME="$TEST_HOME" TMPDIR="$TEST_TMP" FLEET_STATE_DIR="$FLEET_STATE" \
    AGENT_SYNC_VERIFY="$FAKE_BIN/verify-end" PATH="$BASE_PATH" \
    FAKE_ROSTER_TIMEOUT="${FAKE_ROSTER_TIMEOUT:-999999}" \
    FAKE_CAPABILITIES_JSON="${FAKE_CAPABILITIES_JSON:-[\"text\",\"image\"]}" \
    FAKE_PARENT_COMM="${FAKE_PARENT_COMM:-}" FAKE_CPU_PID_FILE="${FAKE_CPU_PID_FILE:-}" \
    FAKE_PS_CPU_COUNTER="${FAKE_PS_CPU_COUNTER:-}" FAKE_ARGS_FILE="${FAKE_ARGS_FILE:-}" \
    FAKE_ENV_FILE="${FAKE_ENV_FILE:-}" FAKE_ROUTE_FILE="${FAKE_ROUTE_FILE:-}" \
    FAKE_CHECK_LANE_FILE="${FAKE_CHECK_LANE_FILE:-}" FAKE_TTL_FILE="${FAKE_TTL_FILE:-}" \
    FAKE_RECORD_FILE="${FAKE_RECORD_FILE:-}" FAKE_LEASE_CONTENTION="${FAKE_LEASE_CONTENTION:-0}" \
    FAKE_LEASE_ERROR="${FAKE_LEASE_ERROR:-0}" FAKE_TELEMETRY_FAIL="${FAKE_TELEMETRY_FAIL:-0}" \
    FAKE_PGID_MODE="${FAKE_PGID_MODE:-ok}" \
    OPENCODE_LANE_WAIT_S="${OPENCODE_LANE_WAIT_S:-0}" \
    /bin/bash "$WRAPPER" "$@" >"$stdout_file" 2>"$stderr_file"
}

test_output_liveness() {
  local d="$TEST_ROOT/output-liveness" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt output-alive --dir "$d/work" --idle-timeout 2 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected output-active rc 0, got $rc"; return 1; }
  contains "$d/out" 'final:output-alive' || { fail "final output missing"; return 1; }
  ! contains "$d/err" 'IDLE LIMIT FIRED' || { fail "growing output was killed as idle"; return 1; }
}

test_cpu_liveness() {
  local d="$TEST_ROOT/cpu-liveness" rc
  mkdir -p "$d/work"
  FAKE_CPU_PID_FILE="$d/cpu-pid" FAKE_PS_CPU_COUNTER="$d/cpu-counter" \
    invoke "$d/out" "$d/err" --prompt cpu-alive --dir "$d/work" --idle-timeout 2 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected CPU-active rc 0, got $rc"; return 1; }
  ! contains "$d/err" 'IDLE LIMIT FIRED' || { fail "advancing CPU was killed as idle"; return 1; }
}

test_descendant_cpu_liveness() {
  local d="$TEST_ROOT/descendant-cpu" rc
  mkdir -p "$d/work"
  FAKE_CPU_GROUP_PGID_FILE="$d/group-pgid" FAKE_PS_CPU_COUNTER="$d/cpu-counter" \
    invoke "$d/out" "$d/err" --prompt descendant-cpu --dir "$d/work" --idle-timeout 2 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "quiet leader with CPU-active descendant was killed, rc $rc"; return 1; }
  [ "$(cat "$d/out")" = 'final:descendant-cpu' ] || { fail "stdout must contain only final output"; return 1; }
  contains "$d/err" 'Crossfeed model receipt:' || { fail "stderr receipt missing"; return 1; }
  ! contains "$d/err" 'IDLE LIMIT FIRED' || { fail "watchdog ignored aggregate process-group CPU"; return 1; }
}

test_group_completion() {
  local d="$TEST_ROOT/group-completion" rc pid
  mkdir -p "$d/work"
  FAKE_DESCENDANT_PID_FILE="$d/descendant-pid" FAKE_DESCENDANT_DONE_FILE="$d/descendant-done" \
    invoke "$d/out" "$d/err" --prompt orphan-descendant --dir "$d/work" --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 0 ] || { fail "leader status changed while waiting for descendants, rc $rc"; return 1; }
  [ -s "$d/descendant-done" ] || { fail "wrapper returned before the descendant finished"; return 1; }
  pid="$(cat "$d/descendant-pid")"
  kill -0 "$pid" 2>/dev/null && { fail "descendant $pid remained alive after wrapper return"; return 1; }
  contains "$d/err" 'still has live descendants' || { fail "continued group supervision was silent"; return 1; }
}

test_idle_kill() {
  local d="$TEST_ROOT/idle" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt idle-silent --dir "$d/work" --idle-timeout 2 --kill-after 1
  rc=$?
  [ "$rc" -eq 125 ] || { fail "expected idle rc 125, got $rc"; return 1; }
  contains "$d/err" 'IDLE LIMIT FIRED' || { fail "idle diagnostic missing"; return 1; }
  contains "$d/err" 'working directory:' || { fail "working directory missing"; return 1; }
  contains "$d/err" 'partial state and edits may be on disk' || { fail "partial-state warning missing"; return 1; }
}

test_no_silent_wall_cap() {
  local d="$TEST_ROOT/no-silent-wall" rc
  mkdir -p "$d/work"
  FAKE_DATE_STATE="$d/date-state" FAKE_ROSTER_TIMEOUT=999999 \
    HOME="$TEST_HOME" TMPDIR="$TEST_TMP" FLEET_STATE_DIR="$FLEET_STATE" \
    AGENT_SYNC_VERIFY="$FAKE_BIN/verify-end" PATH="$CLOCK_BIN:$BASE_PATH" \
    /bin/bash "$WRAPPER" --prompt no-silent-wall --dir "$d/work" --idle-timeout 999999 --kill-after 1 \
    >"$d/out" 2>"$d/err"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "expected configured roster budget to survive without literal cap, got $rc"; return 1; }
  contains "$d/out" 'final:no-silent-wall' || { fail "long fake did not finish"; return 1; }
  ! contains "$d/err" 'WALL-CLOCK LIMIT FIRED' || { fail "an unconfigured literal wall fired"; return 1; }
}

test_roster_wall_is_loud() {
  local d="$TEST_ROOT/roster-wall" rc
  mkdir -p "$d/work"
  FAKE_ROSTER_TIMEOUT=2 invoke "$d/out" "$d/err" --prompt roster-wall --dir "$d/work" --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 124 ] || { fail "expected roster wall rc 124, got $rc"; return 1; }
  contains "$d/err" 'WALL-CLOCK LIMIT FIRED' || { fail "roster wall label missing"; return 1; }
  contains "$d/err" 'roster lane fake-lane wall budget 2s' || { fail "roster wall source missing"; return 1; }
}

test_explicit_wall_kill() {
  local d="$TEST_ROOT/explicit-wall" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt wall --dir "$d/work" --timeout 2 --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 124 ] || { fail "expected explicit wall rc 124, got $rc"; return 1; }
  contains "$d/err" 'configured --timeout 2s' || { fail "explicit wall detail missing"; return 1; }
}

test_sigkill_escalation() {
  local d="$TEST_ROOT/sigkill" rc started ended duration
  mkdir -p "$d/work"
  started="$(/bin/date +%s)"
  invoke "$d/out" "$d/err" --prompt ignore-term --dir "$d/work" --timeout 2 --idle-timeout 20 --kill-after 1
  rc=$?
  ended="$(/bin/date +%s)"
  duration=$((ended - started))
  [ "$rc" -eq 124 ] || { fail "expected rc 124, got $rc"; return 1; }
  [ "$duration" -le 6 ] || { fail "TERM-ignoring child exceeded bound: ${duration}s"; return 1; }
  contains "$d/err" 'KILL-AFTER LIMIT FIRED' || { fail "SIGKILL escalation diagnostic missing"; return 1; }
}

test_process_group_cleanup() {
  local d="$TEST_ROOT/process-group" rc pid attempts
  mkdir -p "$d/work"
  FAKE_DESCENDANT_PID_FILE="$d/descendant-pid" invoke "$d/out" "$d/err" \
    --prompt descendant --dir "$d/work" --timeout 2 --idle-timeout 20 --kill-after 1
  rc=$?
  [ "$rc" -eq 124 ] || { fail "expected process-group wall rc 124, got $rc"; return 1; }
  [ -s "$d/descendant-pid" ] || { fail "descendant PID missing"; return 1; }
  pid="$(cat "$d/descendant-pid")"
  attempts=0
  while kill -0 "$pid" 2>/dev/null && [ "$attempts" -lt 3 ]; do sleep 1; attempts=$((attempts + 1)); done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    fail "descendant $pid survived group cleanup"
    return 1
  fi
}

test_outer_cap_warning() {
  local d="$TEST_ROOT/outer" rc
  mkdir -p "$d/work"
  FAKE_PARENT_COMM=gtimeout invoke "$d/out" "$d/err" --prompt quick --dir "$d/work"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "outer-warning run expected rc 0, got $rc"; return 1; }
  contains "$d/err" "launched under an outer 'gtimeout' wall-clock cap" || { fail "outer-cap warning missing"; return 1; }
  contains "$d/err" 'idle watchdog cannot prevent it' || { fail "outer-cap consequence missing"; return 1; }
}

test_lease_padding() {
  local d="$TEST_ROOT/lease-padding" rc ttl
  mkdir -p "$d/work"
  FAKE_ROSTER_TIMEOUT=4 FAKE_TTL_FILE="$d/ttl" invoke "$d/out" "$d/err" \
    --prompt quick --dir "$d/work" --timeout 9 --kill-after 3
  rc=$?
  [ "$rc" -eq 0 ] || { fail "lease-padding run expected rc 0, got $rc"; return 1; }
  ttl="$(cat "$d/ttl")"
  [ "$ttl" -eq 72 ] || { fail "expected max(4,9)+3+60 lease TTL 72, got $ttl"; return 1; }
}

# A child that finishes before ps can read its group is a normal run, not a discovery failure:
# set -m already made its PID its PGID, and `wait` still returns its status.
test_fast_child_pgid_miss() {
  local d="$TEST_ROOT/pgid-miss" rc
  mkdir -p "$d/work"
  FAKE_PGID_MODE=miss invoke "$d/out" "$d/err" --prompt quick --dir "$d/work"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "fast child with an unreadable PGID expected rc 0, got $rc"; return 1; }
  contains "$d/out" 'final:quick' || { fail "fast child's answer missing"; return 1; }
  ! contains "$d/err" 'DISCOVERY FAILURE' || { fail "completed child was mislabeled as discovery failure"; return 1; }
}

test_exit_3_modality() {
  local d="$TEST_ROOT/exit3" rc
  mkdir -p "$d/work"
  FAKE_CAPABILITIES_JSON='["text"]' invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --modality image
  rc=$?
  [ "$rc" -eq 3 ] || { fail "modality rejection expected 3, got $rc"; return 1; }
}

test_exit_4_lease_contention() {
  local d="$TEST_ROOT/exit4-lease" rc
  mkdir -p "$d/work"
  FAKE_LEASE_CONTENTION=1 OPENCODE_LANE_WAIT_S=0 invoke "$d/out" "$d/err" \
    --prompt quick --dir "$d/work" --lane fake-lane
  rc=$?
  [ "$rc" -eq 4 ] || { fail "lease contention expected 4, got $rc"; return 1; }
}

test_exit_4_invalid_json() {
  local d="$TEST_ROOT/exit4-json" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt invalid-json --dir "$d/work"
  rc=$?
  [ "$rc" -eq 4 ] || { fail "invalid JSON expected 4, got $rc"; return 1; }
}

test_exit_5_session_error() {
  local d="$TEST_ROOT/exit5" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt session-error --dir "$d/work"
  rc=$?
  [ "$rc" -eq 5 ] || { fail "session error expected 5, got $rc"; return 1; }
}

test_exit_6_no_terminal() {
  local d="$TEST_ROOT/exit6" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt no-terminal --dir "$d/work"
  rc=$?
  [ "$rc" -eq 6 ] || { fail "missing terminal step expected 6, got $rc"; return 1; }
}

test_exit_7_empty_output() {
  local d="$TEST_ROOT/exit7" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt empty-output --dir "$d/work"
  rc=$?
  [ "$rc" -eq 7 ] || { fail "empty output expected 7, got $rc"; return 1; }
}

test_exit_8_telemetry() {
  local d="$TEST_ROOT/exit8" rc
  mkdir -p "$d/work"
  FAKE_TELEMETRY_FAIL=1 invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --last "$d/last"
  rc=$?
  [ "$rc" -eq 8 ] || { fail "telemetry failure expected 8, got $rc"; return 1; }
  contains "$d/out" 'final:quick' || { fail "telemetry failure lost stdout deliverable"; return 1; }
  contains "$d/last" 'final:quick' || { fail "telemetry failure lost --last deliverable"; return 1; }
}

test_nonzero_passthrough() {
  local d="$TEST_ROOT/nonzero" rc
  mkdir -p "$d/work"
  invoke "$d/out" "$d/err" --prompt nonzero --dir "$d/work"
  rc=$?
  [ "$rc" -eq 42 ] || { fail "ordinary OpenCode exit expected 42, got $rc"; return 1; }
  ! contains "$d/err" 'LIMIT FIRED' || { fail "ordinary failure mislabeled as a limit"; return 1; }
}

test_lane_and_lean_routing() {
  local d="$TEST_ROOT/routing" rc
  mkdir -p "$d/work"
  FAKE_ROUTE_FILE="$d/route" FAKE_CHECK_LANE_FILE="$d/check" FAKE_ARGS_FILE="$d/args" FAKE_ENV_FILE="$d/env" \
    invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --role review --variant high
  rc=$?
  [ "$rc" -eq 0 ] || { fail "routed lean run expected 0, got $rc"; return 1; }
  contains "$d/route" '--role review' || { fail "role was not routed"; return 1; }
  contains "$d/check" 'check-lane fake-lane read-only' || { fail "lane admission was not checked"; return 1; }
  contains "$d/args" 'fake/model#high' || { fail "lane selector was not forwarded"; return 1; }
  contains "$d/args" '--standalone' || { fail "variant was not forwarded"; return 1; }
  contains "$d/env" 'config=' && ! contains "$d/env" "config=$TEST_HOME/.config" || { fail "config not isolated"; return 1; }
  contains "$d/env" "cwd=$(cd "$d/work" && pwd -P)" || { fail "run cwd not selected"; return 1; }
  contains "$d/env" 'project_config=1' || { fail "project config was not disabled"; return 1; }
  contains "$d/env" 'auth=present' || { fail "isolated auth copy missing"; return 1; }
}

test_roster_variant() {
  local d="$TEST_ROOT/effort" rc
  mkdir -p "$d/work"
  FAKE_ARGS_FILE="$d/args" FAKE_EFFORT_QUERY_FILE="$d/query" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work" --lane fake-lane --effort-role hard-reasoning
  rc=$?
  [ "$rc" -eq 0 ] && contains "$d/args" 'fake/model#max' && contains "$d/args" '--standalone' || { fail "roster max variant not passed, rc $rc"; return 1; }
  contains "$d/query" 'hard-reasoning' && contains "$d/err" 'effort max: roster' || { fail "effort role/source lost"; return 1; }
  FAKE_EFFORT_DEFAULT=provider-default FAKE_ARGS_FILE="$d/provider-args" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work"
  [ "$?" -eq 0 ] && contains "$d/provider-args" 'fake/model' && ! contains "$d/provider-args" 'fake/model#' || { fail "model without variants received unsupported flag"; return 1; }
  contains "$d/err" 'effort provider-default' || { fail "provider default exception silent"; return 1; }
  FAKE_EFFORT_MISSING=1 FAKE_ARGS_FILE="$d/missing-args" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work"
  [ "$?" -eq 2 ] && [ ! -e "$d/missing-args" ] || { fail "missing effort launched OpenCode"; return 1; }
}

test_write_cleanliness_gate() {
  local d="$TEST_ROOT/write-gate" rc
  mkdir -p "$d/dirty" "$d/clean"
  git -C "$d/dirty" init -q
  printf 'dirty\n' >"$d/dirty/untracked"
  FAKE_ARGS_FILE="$d/dirty-args" invoke "$d/dirty-out" "$d/dirty-err" --prompt quick --dir "$d/dirty" --write
  rc=$?
  [ "$rc" -eq 2 ] || { fail "dirty write tree expected 2, got $rc"; return 1; }
  [ ! -e "$d/dirty-args" ] || { fail "OpenCode ran despite dirty-tree gate"; return 1; }

  git -C "$d/clean" init -q
  FAKE_ARGS_FILE="$d/clean-args" invoke "$d/clean-out" "$d/clean-err" --prompt quick --dir "$d/clean" --write
  rc=$?
  [ "$rc" -eq 0 ] || { fail "clean write tree expected 0, got $rc"; return 1; }
  contains "$d/clean-args" 'build' || { fail "write mode lost build agent"; return 1; }
  contains "$d/clean-args" '--auto' || { fail "write mode lost --auto"; return 1; }
}

test_help_and_usage_contract() {
  local d="$TEST_ROOT/help" rc
  mkdir -p "$d"
  /bin/bash "$WRAPPER" -h >"$d/out" 2>"$d/err"
  rc=$?
  [ "$rc" -eq 0 ] || { fail "help expected 0, got $rc"; return 1; }
  contains "$d/out" '--idle-timeout S' || { fail "idle option missing from help"; return 1; }
  contains "$d/out" '125 idle kill' || { fail "idle exit missing from help"; return 1; }
  invoke "$d/usage-out" "$d/usage-err" --timeout
  rc=$?
  [ "$rc" -eq 2 ] || { fail "missing option value expected usage 2, got $rc"; return 1; }
}


# BR-OPENCODE-WORKER-001. This drives the emitted profile through an independent
# ordered-rule test double; native v2 runtime acceptance requires a permitted server.
test_v2_boundary() {
  local d="$TEST_ROOT/v2-boundary" action rc
  mkdir -p "$d/work"
  git -C "$d/work" init -q
  for action in edit shell external_directory subagent opencode_session_move; do
    FAKE_PROBE_ACTION="$action" FAKE_PROBE_MARKER="$d/work/$action" invoke "$d/out" "$d/err" --prompt policy-probe --dir "$d/work"
    rc=$?
    [ "$rc" -eq 0 ] && [ "$(cat "$d/out")" = deny ] && [ ! -e "$d/work/$action" ] || { fail "read-only permitted $action"; return 1; }
  done
  FAKE_PROBE_ACTION=edit FAKE_PROBE_MARKER="$d/work/control" invoke "$d/out" "$d/err" --prompt policy-probe --dir "$d/work" --write
  [ "$?" -eq 0 ] && [ "$(cat "$d/work/control")" = 'operation permitted' ] || { fail "write control did not observe a permitted edit"; return 1; }
  rm -f "$d/work/control"
  for action in external_directory opencode_session_move opencode.worktree_create; do
    FAKE_PROBE_ACTION="$action" FAKE_PROBE_MARKER="$d/external" invoke "$d/out" "$d/err" --prompt policy-probe --dir "$d/work" --write
    [ "$?" -eq 0 ] && [ ! -e "$d/external" ] && [ "$(cat "$d/out")" = deny ] || { fail "build permitted $action"; return 1; }
  done
  FAKE_PROBE_ACTION=read FAKE_PROBE_MARKER="$d/read" invoke "$d/out" "$d/err" --prompt policy-probe --dir "$d/work" --direct
  [ "$?" -eq 0 ] && [ ! -e "$d/read" ] && [ "$(cat "$d/out")" = deny ] || { fail "direct permitted a tool"; return 1; }
  FAKE_PROBE_ACTION=edit FAKE_PROBE_MARKER="$d/shared" invoke "$d/out" "$d/err" --prompt policy-probe --dir "$d/work" --context shared
  [ "$?" -eq 0 ] && [ ! -e "$d/shared" ] && [ "$(cat "$d/out")" = deny ] || { fail "shared profile weakened plan boundary"; return 1; }
}

test_v2_account_snapshot() {
  local d="$TEST_ROOT/account" rc
  mkdir -p "$d/work" "$d/data/opencode"
  sqlite3 "$d/data/opencode/opencode.db" 'CREATE TABLE witness(value); INSERT INTO witness VALUES (1);'
  XDG_DATA_HOME="$d/data" FAKE_ENV_FILE="$d/env" invoke "$d/out" "$d/err" --prompt quick --dir "$d/work"
  rc=$?
  [ "$rc" -eq 0 ] && contains "$d/env" 'db=present' && contains "$d/env" 'auth=missing' || { fail "database-only account sign-in not snapshotted, rc=$rc"; return 1; }
  [ "$(sqlite3 "$d/data/opencode/opencode.db" 'SELECT value FROM witness;')" = 1 ] || { fail "source database was changed"; return 1; }
}

test_v2_events() {
  local d="$TEST_ROOT/v2-events" rc prompt
  mkdir -p "$d/work"
  V2_FIXTURE="$SCRIPT_DIR/tests/fixtures/opencode-v2-run-ok-events.jsonl" invoke "$d/out" "$d/err" --prompt real-v2 --dir "$d/work"
  [ "$?" -eq 0 ] && [ "$(cat "$d/out")" = OK ] || { fail "real v2 success fixture rejected"; return 1; }
  for prompt in auth-error auth-error-nonzero; do
    invoke "$d/out" "$d/err" --prompt "$prompt" --dir "$d/work" --last "$d/last"
    rc=$?
    [ "$rc" -eq 5 ] && [ ! -s "$d/out" ] && [ ! -e "$d/last" ] && contains "$d/err" 'opencode auth login opencode --method device' || { fail "auth error rc=$rc leaked output or lost sign-in guidance"; return 1; }
  done
  invoke "$d/out" "$d/err" --prompt multi-text --dir "$d/work"
  [ "$?" -eq 0 ] && [ "$(cat "$d/out")" = 'final answer' ] || { fail "final text chunks not joined or stale text returned"; return 1; }
  invoke "$d/out" "$d/err" --prompt final-empty --dir "$d/work"
  [ "$?" -eq 7 ] && [ ! -s "$d/out" ] || { fail "earlier step text published for empty final step"; return 1; }
}

selected() {
  local wanted="$1" item
  [ "$SELECT_ALL" -eq 1 ] && return 0
  for item in "${SELECTED_TESTS[@]}"; do [ "$item" = "$wanted" ] && return 0; done
  return 1
}

run_test() {
  local name="$1" function_name="$2"
  selected "$name" || return 0
  RUN_COUNT=$((RUN_COUNT + 1))
  DETAIL=""; LAST_STDERR=""
  if "$function_name"; then
    PASS_COUNT=$((PASS_COUNT + 1))
    printf 'PASS %s\n' "$name"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf 'FAIL %s: %s\n' "$name" "${DETAIL:-test returned non-zero}"
    # The wrapper's own words are the diagnosis; a bare exit code is not.
    [ -z "${LAST_STDERR:-}" ] || [ ! -s "$LAST_STDERR" ] || tail -n 3 "$LAST_STDERR" | sed 's/^/    stderr: /'
  fi
}

if [ "$#" -eq 0 ]; then SELECT_ALL=1; SELECTED_TESTS=(); else SELECT_ALL=0; SELECTED_TESTS=("$@"); fi

run_test output_liveness test_output_liveness
run_test cpu_liveness test_cpu_liveness
run_test descendant_cpu_liveness test_descendant_cpu_liveness
run_test group_completion test_group_completion
run_test idle_kill test_idle_kill
run_test no_silent_wall_cap test_no_silent_wall_cap
run_test roster_wall_is_loud test_roster_wall_is_loud
run_test explicit_wall_kill test_explicit_wall_kill
run_test sigkill_escalation test_sigkill_escalation
run_test process_group_cleanup test_process_group_cleanup
run_test outer_cap_warning test_outer_cap_warning
run_test lease_padding test_lease_padding
run_test fast_child_pgid_miss test_fast_child_pgid_miss
run_test exit_3_modality test_exit_3_modality
run_test exit_4_lease_contention test_exit_4_lease_contention
run_test exit_4_invalid_json test_exit_4_invalid_json
run_test exit_5_session_error test_exit_5_session_error
run_test exit_6_no_terminal test_exit_6_no_terminal
run_test exit_7_empty_output test_exit_7_empty_output
run_test exit_8_telemetry test_exit_8_telemetry
run_test nonzero_passthrough test_nonzero_passthrough
run_test lane_and_lean_routing test_lane_and_lean_routing
run_test roster_variant test_roster_variant
run_test write_cleanliness_gate test_write_cleanliness_gate
run_test help_and_usage_contract test_help_and_usage_contract
run_test v2_events test_v2_events
run_test v2_boundary test_v2_boundary
run_test v2_account_snapshot test_v2_account_snapshot

if [ "$RUN_COUNT" -eq 0 ]; then printf 'FAIL no matching tests selected\n'; exit 2; fi
printf 'RESULT: %s passed, %s failed, %s total\n' "$PASS_COUNT" "$FAIL_COUNT" "$RUN_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
