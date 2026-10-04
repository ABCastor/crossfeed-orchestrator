#!/usr/bin/env bash
# afk-run.sh — bounded sequential AFK controller.  Proof exit status, never a
# model's self-report, is the only success condition.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OBJECTIVE=""; PROOF=""; TIME_BUDGET=""; MAX_ATTEMPTS=""; ROUTES=""; DIR="$PWD"; RUNNER=""
ROLE=""; FAMILY=""
# Shared normalized outcomes and producer-specific exit mappings live here.
. "$HERE/outcome-taxonomy.sh"

usage() {
  cat <<'EOF'
usage: afk-run.sh --objective TEXT --proof-command COMMAND --time-budget-s SECONDS \
  --max-attempts N --routes lane-a,lane-b [--dir WORKTREE] [--runner COMMAND]

Routes are tried in listed order. The default runner is the admitted OpenCode Go
worker in write mode. --runner is a test seam: it receives --route, --objective,
and --dir. Every real attempt is appended to fleetctl's shared runs.jsonl ledger.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --objective) OBJECTIVE="$2"; shift 2;;
    --proof-command) PROOF="$2"; shift 2;;
    --time-budget-s) TIME_BUDGET="$2"; shift 2;;
    --max-attempts|--max-turns) MAX_ATTEMPTS="$2"; shift 2;;
    --routes) ROUTES="$2"; shift 2;;
    --dir) DIR="$2"; shift 2;;
    --runner) RUNNER="$2"; shift 2;;
    --role) ROLE="$2"; shift 2;;
    --family) FAMILY="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "afk-run: unknown argument: $1" >&2; exit 2;;
  esac
done

for value in OBJECTIVE PROOF TIME_BUDGET MAX_ATTEMPTS ROUTES; do
  [ -n "${!value}" ] || { echo "afk-run: a required argument is missing" >&2; exit 2; }
done
case "$TIME_BUDGET" in *[!0-9]*|'') echo "afk-run: time budget must be a non-negative integer" >&2; exit 2;; esac
case "$MAX_ATTEMPTS" in *[!0-9]*|'') echo "afk-run: attempt budget must be a non-negative integer" >&2; exit 2;; esac
[ "$TIME_BUDGET" -gt 0 ] && [ "$MAX_ATTEMPTS" -gt 0 ] || { echo "afk-run: budgets must be positive" >&2; exit 2; }
[ -d "$DIR" ] || { echo "afk-run: work directory not found: $DIR" >&2; exit 2; }

IFS=',' read -r -a ROUTE_LIST <<< "$ROUTES"
[ "${#ROUTE_LIST[@]}" -gt 0 ] || { echo "afk-run: at least one route is required" >&2; exit 2; }
for route in "${ROUTE_LIST[@]}"; do
  [ -n "$route" ] || { echo "afk-run: empty route in --routes" >&2; exit 2; }
done

start_epoch="$(date +%s)"; deadline=$((start_epoch + TIME_BUDGET))
attempt=0; route_index=0; unchanged=0; previous_hash=""
run_id="afk-$(date +%Y%m%d-%H%M%S)-$$"
artifact_root="${AFK_ARTIFACT_DIR:-${FLEET_STATE_DIR:-$HOME/.local/state/orchestrator}/afk-runs}/$run_id"
mkdir -p "$artifact_root"

# macOS does not ship GNU timeout. Start a new process group so a deadline kills
# the wrapper and any child agent it launched, rather than leaving an AFK worker
# behind after the controller has stopped.
run_bounded() {
  local seconds="$1" output="$2"
  shift 2
  python3 - "$seconds" "$output" "$@" <<'PY'
import os, signal, subprocess, sys
seconds, output, command = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
if seconds <= 0:
    open(output, "w", encoding="utf-8").write("AFK deadline reached before command start\n")
    raise SystemExit(124)
with open(output, "w", encoding="utf-8") as handle:
    process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        raise SystemExit(process.wait(timeout=seconds))
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        handle.write("AFK deadline exceeded; worker process group terminated\n")
        raise SystemExit(124)
PY
}

while [ "$attempt" -lt "$MAX_ATTEMPTS" ] && [ "$route_index" -lt "${#ROUTE_LIST[@]}" ]; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "AFK STOP: time budget exhausted before attempt $((attempt + 1))" >&2
    exit 3
  fi
  route="${ROUTE_LIST[$route_index]}"
  attempt=$((attempt + 1))
  attempt_id="$run_id-$attempt"
  attempt_prompt="$(printf 'AFK attempt %s on route %s.\n\n%s' "$attempt" "$route" "$OBJECTIVE")"
  started_at="$(python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).isoformat().replace("+00:00", "Z"))')"
  worker_output="$artifact_root/attempt-$attempt.worker.log"
  proof_output="$artifact_root/attempt-$attempt.proof.log"
  deliverable="$artifact_root/attempt-$attempt.last"
  worker_started="$(date +%s)"
  remaining=$((deadline - worker_started))
  worker_args=()
  if [ -n "$RUNNER" ]; then
    worker_args=("$RUNNER" --route "$route" --objective "$OBJECTIVE" --dir "$DIR")
  else
    worker_args=("$HERE/opencode-agent.sh" --lane "$route" --write --dir "$DIR" --prompt "$attempt_prompt" --last "$deliverable")
    [ -z "$ROLE" ] || worker_args+=(--effort-role "$ROLE")
  fi
  set +e
  AFK_ATTEMPT_ID="$attempt_id" run_bounded "$remaining" "$worker_output" "${worker_args[@]}"
  worker_rc=$?
  if ! outcome_has_deliverable "$deliverable" && outcome_has_deliverable "$worker_output"; then
    cp "$worker_output" "$deliverable"
  fi
  proof_remaining=$((deadline - $(date +%s)))
  run_bounded "$proof_remaining" "$proof_output" bash -c "$PROOF"
  proof_rc=$?
  set -e
  ended_epoch="$(date +%s)"; duration_ms=$(((ended_epoch - worker_started) * 1000))
  # macOS ships `shasum` but not `sha256sum`; most Linux images ship the reverse.
  # python3 is already a hard dependency of this script (see run_bounded), so hashing
  # through it is one code path everywhere instead of a two-branch fallback.
  proof_hash="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$proof_output")"
  classify_wrapper_outcome opencode "$worker_rc" "$deliverable" "$worker_output"
  attempt_outcome="$WRAPPER_OUTCOME_CLASS"
  attempt_reason="$WRAPPER_OUTCOME_REASON"
  case "$WRAPPER_OUTCOME_CLASS" in
    SUCCEEDED|SUCCEEDED_WITH_WARNING)
      if [ "$proof_rc" -eq 0 ]; then
        result="verified"; failure_class=""
      elif [ "$proof_rc" -eq 124 ]; then
        result="failed"; failure_class="proof-wall-clock-timeout"
        [ "$WRAPPER_OUTCOME_CLASS" = "SUCCEEDED_WITH_WARNING" ] || attempt_outcome="KILLED_WALL_CLOCK"
        attempt_reason="$attempt_reason;proof-wall-clock-timeout"
      else
        result="failed"; failure_class="proof-failed"
        [ "$WRAPPER_OUTCOME_CLASS" = "SUCCEEDED_WITH_WARNING" ] || attempt_outcome="FAILED"
        attempt_reason="$attempt_reason;proof-exit-$proof_rc"
      fi
      ;;
    KILLED_WALL_CLOCK)
      result="failed"; failure_class="worker-wall-clock-timeout"
      ;;
    KILLED_IDLE_WATCHDOG)
      result="failed"; failure_class="worker-idle-watchdog-timeout"
      ;;
    FAILED)
      result="failed"; failure_class="worker-$WRAPPER_OUTCOME_REASON"
      ;;
  esac
  ledger_args=(afk-record --lane "$route" --attempt-id "$attempt_id" --started-at "$started_at"
    --duration-ms "$duration_ms" --result "$result" --proof-output-hash "$proof_hash"
    --proof-returncode "$proof_rc" --worker-returncode "$worker_rc")
  [ -z "$failure_class" ] || ledger_args+=(--failure-class "$failure_class")
  [ -z "$ROLE" ] || ledger_args+=(--role "$ROLE")
  [ -z "$FAMILY" ] || ledger_args+=(--family "$FAMILY")
  "$HERE/fleetctl.py" "${ledger_args[@]}" >/dev/null
  echo "AFK attempt $attempt route=$route outcome=$attempt_outcome reason=$attempt_reason result=$result failure=${failure_class:-none} proof_sha256=$proof_hash deliverable=${deliverable:-none}"
  if [ "$WRAPPER_OUTCOME_CLASS" = "SUCCEEDED_WITH_WARNING" ]; then
    skipped_routes=$((${#ROUTE_LIST[@]} - route_index - 1))
    echo "AFK WARNING: worker completed but $WRAPPER_OUTCOME_REASON; deliverable preserved at $deliverable; automatic retry refused" >&2
    echo "AFK summary: succeeded_with_warning=1 failed_proof=$([ "$proof_rc" -eq 0 ] && echo 0 || echo 1) killed_wall_clock=0 killed_idle_watchdog=0 skipped=$skipped_routes skip_reason=deliverable-preserved-no-retry"
    if [ "$proof_rc" -eq 0 ]; then exit 0; else exit 3; fi
  fi
  if [ "$result" = "verified" ]; then
    skipped_routes=$((${#ROUTE_LIST[@]} - route_index - 1))
    echo "AFK summary: succeeded=1 failed=0 killed_wall_clock=0 killed_idle_watchdog=0 skipped=$skipped_routes skip_reason=proof-passed"
    echo "AFK STOP: proof passed after $attempt attempt(s)"
    exit 0
  fi
  if [ -z "$previous_hash" ]; then unchanged=1
  elif [ "$proof_hash" = "$previous_hash" ]; then unchanged=$((unchanged + 1))
  else unchanged=0; fi
  previous_hash="$proof_hash"
  if [ "$unchanged" -ge 2 ]; then
    echo "AFK STOP: no measurable progress across 2 consecutive attempts" >&2
    exit 3
  fi
  route_index=$((route_index + 1))
done

if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then echo "AFK STOP: attempt budget exhausted" >&2
else echo "AFK STOP: allowed routes exhausted" >&2; fi
exit 3
