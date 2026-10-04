#!/usr/bin/env bash
# Consumer regression tests. All wrapper/fleet executables are fakes in a temporary PATH.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# Works whether this file sits beside scripts/ (dev tree) or in tests/ (installed skill).
[ -d "$ROOT/scripts" ] || ROOT="$(cd "$ROOT/.." && pwd)"
SOURCE_SCRIPTS="${CONSUMER_SCRIPTS_DIR:-$ROOT/scripts}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/consumer-tests.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT

passes=0
failures=0

prepare_case() {
  local name="$1" dir="$TEST_ROOT/$name"
  mkdir -p "$dir"
  cp "$SOURCE_SCRIPTS/fanout.sh" "$SOURCE_SCRIPTS/swarm.sh" \
    "$SOURCE_SCRIPTS/afk-run.sh" "$SOURCE_SCRIPTS/outcome-taxonomy.sh" "$dir/"
  chmod +x "$dir/fanout.sh" "$dir/swarm.sh" "$dir/afk-run.sh"

  cat >"$dir/fleetctl.py" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  usage) printf '%s\n' '{"quota_state":"HEALTHY"}' ;;
  claim-paths|acquire) printf '%s\n' 'fake-token' ;;
esac
exit 0
EOF
  chmod +x "$dir/fleetctl.py"

  cat >"$dir/agy-agent.sh" <<'EOF'
#!/usr/bin/env bash
prompt=""; last=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt) prompt="$2"; shift 2 ;;
    --last) last="$2"; shift 2 ;;
    --dir|--timeout|--lane|--model) shift 2 ;;
    --sandbox) shift ;;
    *) shift ;;
  esac
done
[ -z "${FAKE_CALL_LOG:-}" ] || printf '%s\n' 'CALL' >>"$FAKE_CALL_LOG"
case "$prompt" in
  timeout-quota) echo 'quota was mentioned before this wall-clock timeout' >&2; exit 124 ;;
  quota) exit 3 ;;
  fail) echo 'lease refused' >&2; exit 5 ;;
  wall) echo 'wall-clock expired' >&2; exit 124 ;;
  idle) echo 'idle watchdog expired' >&2; exit 125 ;;
  *) printf 'deliverable for %s\n' "$prompt" >"$last"; exit 0 ;;
esac
EOF
  chmod +x "$dir/agy-agent.sh"

  cat >"$dir/opencode-agent.sh" <<'EOF'
#!/usr/bin/env bash
prompt=""; prompt_file=""; last=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt) prompt="$2"; shift 2 ;;
    --prompt-file) prompt_file="$2"; shift 2 ;;
    --last) last="$2"; shift 2 ;;
    --dir|--timeout|--lane|--modality|--context|--events|--variant) shift 2 ;;
    --read-only|--write|--web-search) shift ;;
    *) shift ;;
  esac
done
[ -n "$prompt" ] || prompt="$(cat "$prompt_file")"
[ -z "${FAKE_CALL_LOG:-}" ] || printf '%s\n' 'CALL' >>"$FAKE_CALL_LOG"
case "$prompt" in
  *telemetry-deliverable*)
    printf '%s\n' 'completed work despite telemetry failure' >"$last"
    printf '%s\n' 'completed work despite telemetry failure'
    exit 8
    ;;
  timeout-quota) echo 'quota appears in timeout stderr' >&2; exit 124 ;;
  *) printf 'deliverable for %s\n' "$prompt" >"$last"; exit 0 ;;
esac
EOF
  chmod +x "$dir/opencode-agent.sh"

  cat >"$dir/access-overlay.json" <<'EOF'
{
  "lanes": [
    {
      "lane_id": "fake-open", "harness": "opencode", "model_key": "fake-open",
      "selector": "fake/open", "quota_pool": "open-pool", "access_status": "verified",
      "admission_status": "active", "allowed_modes": ["read-only"],
      "capabilities": {"input": ["text"]}, "timeout_s": 60,
      "max_tasks_per_run": 32, "max_parallel": 1
    },
    {
      "lane_id": "fake-agy", "harness": "agy", "model_key": "fake-agy",
      "selector": "fake-agy-model", "quota_pool": "agy-pool", "access_status": "verified",
      "admission_status": "active", "allowed_modes": ["write"],
      "capabilities": {"input": ["text"]}, "timeout_s": 60,
      "max_tasks_per_run": 32, "max_parallel": 1
    }
  ],
  "routing": {"roles": {"default": {}}},
  "swarm_profiles": {
    "review": {
      "bands": {
        "quality_first": {
          "parallel": 1,
          "workers": [{"id": "reader", "lane_id": "fake-open", "angle": "read"}]
        }
      }
    }
  }
}
EOF
  printf '%s\n' "$dir"
}

append_task() {
  local file="$1" id="$2" agent="$3" lane="$4" mode="$5" dir="$6" prompt="$7"
  python3 - "$file" "$id" "$agent" "$lane" "$mode" "$dir" "$prompt" <<'PY'
import json, sys
path, task_id, agent, lane, mode, directory, prompt = sys.argv[1:]
with open(path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "id": task_id, "agent": agent, "lane_id": lane, "mode": mode,
        "dir": directory, "prompt": prompt,
    }) + "\n")
PY
}

run_fanout() {
  local case_dir="$1" tasks="$2" out="$3" stdout="$4" stderr="$5" calls="$6"
  ACCESS_OVERLAY="$case_dir/access-overlay.json" FAKE_CALL_LOG="$calls" \
    PATH="$case_dir:$PATH" bash "$case_dir/fanout.sh" "$tasks" --parallel 1 --out "$out" \
    >"$stdout" 2>"$stderr"
}

test_timeout_quota_text_does_not_suppress() {
  local case_dir tasks out stdout stderr calls rc
  case_dir="$(prepare_case timeout_text)"; tasks="$case_dir/tasks.jsonl"; out="$case_dir/out"
  stdout="$case_dir/stdout"; stderr="$case_dir/stderr"; calls="$case_dir/calls"
  mkdir -p "$case_dir/work-a" "$case_dir/work-b"
  append_task "$tasks" timeout opencode fake-open read-only "$case_dir/work-a" timeout-quota
  append_task "$tasks" after opencode fake-open read-only "$case_dir/work-b" success-after-timeout
  run_fanout "$case_dir" "$tasks" "$out" "$stdout" "$stderr" "$calls"; rc=$?
  [ "$rc" -eq 1 ] || return 1
  grep -q '^KILLED_WALL_CLOCK.*wall-clock-timeout;quota-text-observed' "$out/summary.tsv" || return 1
  grep -q '^SUCCEEDED[[:space:]].*after' "$out/summary.tsv" || return 1
  ! grep -q '^SKIPPED' "$out/summary.tsv" || return 1
  [ "$(wc -l <"$calls" | tr -d ' ')" -eq 2 ] || return 1
  grep -q 'skipped=0' "$stderr"
}

test_genuine_quota_exit_suppresses_visibly() {
  local case_dir tasks out stdout stderr calls rc
  case_dir="$(prepare_case quota_exit)"; tasks="$case_dir/tasks.jsonl"; out="$case_dir/out"
  stdout="$case_dir/stdout"; stderr="$case_dir/stderr"; calls="$case_dir/calls"
  mkdir -p "$case_dir/work-a" "$case_dir/work-b"
  append_task "$tasks" quota agy fake-agy write "$case_dir/work-a" quota
  append_task "$tasks" after agy fake-agy write "$case_dir/work-b" must-not-run
  run_fanout "$case_dir" "$tasks" "$out" "$stdout" "$stderr" "$calls"; rc=$?
  [ "$rc" -eq 1 ] || return 1
  grep -q $'^FAILED\tquota\t.*\tquota-exhausted\t' "$out/summary.tsv" || return 1
  grep -q $'^SKIPPED\tafter\t.*\tquota-exhausted\t.*trigger=quota' "$out/summary.tsv" || return 1
  [ "$(wc -l <"$calls" | tr -d ' ')" -eq 1 ] || return 1
  grep -q 'quota circuit OPEN' "$stderr" || return 1
  grep -q 'skipped=1' "$stderr"
}

test_summary_has_all_five_classes() {
  local case_dir tasks out stdout stderr calls rc id prompt
  case_dir="$(prepare_case five_classes)"; tasks="$case_dir/tasks.jsonl"; out="$case_dir/out"
  stdout="$case_dir/stdout"; stderr="$case_dir/stderr"; calls="$case_dir/calls"
  for id in success fail wall idle quota skipped; do mkdir -p "$case_dir/work-$id"; done
  append_task "$tasks" success agy fake-agy write "$case_dir/work-success" success
  append_task "$tasks" fail agy fake-agy write "$case_dir/work-fail" fail
  append_task "$tasks" wall agy fake-agy write "$case_dir/work-wall" wall
  append_task "$tasks" idle agy fake-agy write "$case_dir/work-idle" idle
  append_task "$tasks" quota agy fake-agy write "$case_dir/work-quota" quota
  append_task "$tasks" skipped agy fake-agy write "$case_dir/work-skipped" skipped
  run_fanout "$case_dir" "$tasks" "$out" "$stdout" "$stderr" "$calls"; rc=$?
  [ "$rc" -eq 1 ] || return 1
  grep -q '^SUCCEEDED[[:space:]]' "$out/summary.tsv" || return 1
  grep -q $'^FAILED\t.*\tlease-refused-or-no-eligible-lane\t' "$out/summary.tsv" || return 1
  grep -q '^KILLED_WALL_CLOCK[[:space:]]' "$out/summary.tsv" || return 1
  grep -q '^KILLED_IDLE_WATCHDOG[[:space:]]' "$out/summary.tsv" || return 1
  grep -q '^SKIPPED[[:space:]]' "$out/summary.tsv" || return 1
  grep -q 'succeeded=1 warnings=0 failed=2 killed_wall_clock=1 killed_idle_watchdog=1 skipped=1' "$stderr"
}

test_telemetry_deliverable_is_preserved_without_retry() {
  local case_dir stdout stderr calls rc deliverable
  case_dir="$(prepare_case telemetry)"; stdout="$case_dir/stdout"; stderr="$case_dir/stderr"; calls="$case_dir/calls"
  mkdir -p "$case_dir/work" "$case_dir/state"
  FLEET_STATE_DIR="$case_dir/state" FAKE_CALL_LOG="$calls" PATH="$case_dir:$PATH" \
    bash "$case_dir/afk-run.sh" --objective telemetry-deliverable --proof-command false \
    --time-budget-s 30 --max-attempts 2 --routes route-a,route-b --dir "$case_dir/work" \
    >"$stdout" 2>"$stderr"; rc=$?
  [ "$rc" -eq 3 ] || return 1
  [ "$(wc -l <"$calls" | tr -d ' ')" -eq 1 ] || return 1
  grep -q 'outcome=SUCCEEDED_WITH_WARNING reason=telemetry-unaccounted;proof-exit-1' "$stdout" || return 1
  grep -q 'skipped=1 skip_reason=deliverable-preserved-no-retry' "$stdout" || return 1
  deliverable="$(sed -n 's/.* deliverable=\([^[:space:]]*\).*/\1/p' "$stdout" | head -1)"
  [ -n "$deliverable" ] && grep -q 'completed work despite telemetry failure' "$deliverable" || return 1
  grep -q 'automatic retry refused' "$stderr"
}

test_swarm_maps_fanout_conflict_at_boundary() {
  local case_dir stdout stderr rc
  case_dir="$(prepare_case swarm_boundary)"; stdout="$case_dir/stdout"; stderr="$case_dir/stderr"
  cat >"$case_dir/fanout.sh" <<'EOF'
#!/usr/bin/env bash
exit 3
EOF
  chmod +x "$case_dir/fanout.sh"
  ACCESS_OVERLAY="$case_dir/access-overlay.json" PATH="$case_dir:$PATH" \
    bash "$case_dir/swarm.sh" review --prompt inspect --dir "$case_dir" \
    >"$stdout" 2>"$stderr"; rc=$?
  [ "$rc" -eq 5 ] || return 1
  grep -q 'reason=fanout-write-claim-conflict.*native_fanout_exit=3.*mapped_exit=5' "$stderr"
}

test_native_codes_are_producer_scoped() {
  local stderr_file="$TEST_ROOT/taxonomy.stderr" deliverable="$TEST_ROOT/taxonomy.last"
  : >"$stderr_file"
  : >"$deliverable"
  # shellcheck source=scripts/outcome-taxonomy.sh
  . "$SOURCE_SCRIPTS/outcome-taxonomy.sh"

  classify_wrapper_outcome agy 3 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON:$WRAPPER_OUTCOME_SUPPRESS_POOL" = "quota-exhausted:1" ] || return 1
  classify_wrapper_outcome opencode 3 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON:$WRAPPER_OUTCOME_SUPPRESS_POOL" = "modality-not-admitted:0" ] || return 1

  classify_wrapper_outcome claude 4 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON" = "empty-output" ] || return 1
  classify_wrapper_outcome copilot 4 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON" = "invalid-json-event-stream" ] || return 1
  classify_wrapper_outcome opencode 4 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON:$WRAPPER_OUTCOME_SUPPRESS_POOL" = "lease-or-event-failure:0" ] || return 1

  classify_wrapper_outcome agy 5 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON" = "lease-refused-or-no-eligible-lane" ] || return 1
  classify_wrapper_outcome copilot 5 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON" = "missing-or-failing-result-event" ] || return 1
  classify_wrapper_outcome opencode 5 "$deliverable" "$stderr_file"
  [ "$WRAPPER_OUTCOME_REASON" = "session-error-event" ]
}

test_all_consumers_point_to_shared_taxonomy() {
  grep -q 'outcome-taxonomy.sh' "$SOURCE_SCRIPTS/fanout.sh" || return 1
  grep -q 'outcome-taxonomy.sh' "$SOURCE_SCRIPTS/swarm.sh" || return 1
  grep -q 'outcome-taxonomy.sh' "$SOURCE_SCRIPTS/afk-run.sh"
}

run_test() {
  local name="$1" fn="$2"
  if "$fn"; then
    printf 'PASS %s\n' "$name"
    passes=$((passes + 1))
  else
    printf 'FAIL %s\n' "$name"
    failures=$((failures + 1))
  fi
}

run_test timeout-quota-text-does-not-suppress test_timeout_quota_text_does_not_suppress
run_test genuine-quota-exit-suppresses-visibly test_genuine_quota_exit_suppresses_visibly
run_test summary-distinguishes-five-outcomes test_summary_has_all_five_classes
run_test telemetry-deliverable-preserved-no-retry test_telemetry_deliverable_is_preserved_without_retry
run_test swarm-boundary-maps-conflict test_swarm_maps_fanout_conflict_at_boundary
run_test native-codes-are-producer-scoped test_native_codes_are_producer_scoped
run_test all-consumers-share-taxonomy test_all_consumers_point_to_shared_taxonomy

printf 'RESULT pass=%s fail=%s\n' "$passes" "$failures"
[ "$failures" -eq 0 ]
