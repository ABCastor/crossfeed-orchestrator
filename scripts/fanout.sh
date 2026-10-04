#!/usr/bin/env bash
# fanout.sh: preflight and run independent subscription-backed CLI workers in fixed batches.
#
# Usage:
#   fanout.sh TASKS.jsonl [--agent NAME] [--parallel N] [--timeout S] [--out DIR] [--dry-run]
#
# Task fields: id, prompt, dir, agent, mode, role, modality, file, context,
# lane_id/model_key/model, variant, effort, timeout. Mode defaults to read-only.
#
# EXIT CODES (mirrored in outcome-taxonomy.sh and SKILL.md; check-dispatch-invariants.sh I7 runs
# this script against inputs it must refuse and asserts the contract behaviourally):
#   0   campaign complete, every task produced a deliverable
#   1   the campaign RAN and is INCOMPLETE (failed / killed / skipped tasks; read summary.tsv)
#   2   usage error: the invocation itself is wrong (bad flag, missing tasks file, bad --parallel)
#   3   write-claim conflict: another campaign holds a claimed path
#   4   campaign REFUSED: admissible-campaign check said no, so NOTHING was dispatched
#   124/125 are wrapper-level kills and never originate here.
# 4 exists because 1 was being used for both "some tasks failed" and "no task ever started", and
# those send the caller to opposite places: 1 means read summary.tsv, 4 means there is no
# summary.tsv to read. That collision is what made a refusal look like a half-finished run.
set -euo pipefail
EXIT_REFUSED=4

HERE="$(cd "$(dirname "$0")" && pwd)"
FLEET_ROOT="$(cd "$HERE/.." && pwd)"
ROSTER="${ACCESS_OVERLAY:-${XDG_CONFIG_HOME:-$HOME/.config}/orchestrator/access-overlay.json}"
# Shared normalized outcomes and producer-specific exit mappings live here.
. "$HERE/outcome-taxonomy.sh"

TASKS="${1:-}"; shift || true
DEFAULT_AGENT="codex"; PARALLEL=4; DEFAULT_TIMEOUT=""; OUT=""; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --agent)    DEFAULT_AGENT="$2"; shift 2;;
    --parallel) PARALLEL="$2"; shift 2;;
    --timeout)  DEFAULT_TIMEOUT="$2"; shift 2;;
    --out)      OUT="$2"; shift 2;;
    --dry-run)  DRY_RUN=1; shift;;
    *) echo "fanout: unknown arg: $1" >&2; exit 2;;
  esac
done

[ -n "$TASKS" ] && [ -f "$TASKS" ] || { echo "fanout: tasks file not found: $TASKS" >&2; exit 2; }
[ -f "$ROSTER" ] || { echo "fanout: access overlay not found: $ROSTER" >&2; exit 2; }
case "$PARALLEL" in *[!0-9]*|'') echo "fanout: --parallel must be 1-16" >&2; exit 2;; esac
# Empty is the DEFAULT and means "no wall clock": each wrapper's idle watchdog bounds the run.
case "$DEFAULT_TIMEOUT" in '') : ;; *[!0-9]*) echo "fanout: --timeout must be a positive integer" >&2; exit 2;; esac
[ "$PARALLEL" -ge 1 ] && [ "$PARALLEL" -le 16 ] || { echo "fanout: --parallel must be 1-16" >&2; exit 2; }
[ -z "$DEFAULT_TIMEOUT" ] || [ "$DEFAULT_TIMEOUT" -ge 1 ] || { echo "fanout: --timeout must be positive" >&2; exit 2; }

# Run artifacts are runtime state, not source. Defaulting them into the source tree meant a
# clone accumulated manifests and prompts inside itself; they belong beside the rest of the
# fleet's state. An explicit --out is unaffected.
[ -n "$OUT" ] || OUT="${FLEET_STATE_DIR:-$HOME/.local/state/orchestrator}/runs/run-$(date +%Y%m%d-%H%M%S)-$$"
if [ -e "$OUT" ] && [ -n "$(find "$OUT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
  echo "fanout: refusing non-empty output directory: $OUT" >&2
  # Not a usage error (the flags were fine) — a refused campaign that dispatched nothing.
  exit "$EXIT_REFUSED"
fi
mkdir -p "$OUT/tasks" "$OUT/quota-open"

# ABORT, NOT SKIP, and that is a decision rather than an accident. One inadmissible row kills the
# whole campaign instead of being dropped with a warning, because (a) several preflight rules are
# CROSS-task — duplicate ids, overlapping write roots, per-lane and per-batch caps — so "skip the
# bad one" is not even well defined for them; (b) a fanout task list is a designed partition, and
# quietly running 2 of 3 lanes hands back a council with a missing lens, which is the exact failure
# `swarm.sh` still has (it reports a missing lens on stderr but keeps its exit code);
# and (c) nearly every preflight rejection is a cheap authoring mistake in the task file, so a
# partial run spends scarce shared quota on a campaign the caller is about to rewrite anyway.
# Fail closed, name the reason, let the caller fix the row. The cost that remains is that only the
# FIRST offending row is reported, so a task file with several mistakes still costs several
# round-trips; batching all preflight errors into one report is the right follow-up and is a
# larger change to the block below than this fix is allowed to make.
set +e
python3 - "$TASKS" "$OUT" "$DEFAULT_AGENT" "$DEFAULT_TIMEOUT" "$PARALLEL" "$ROSTER" "$HERE" <<'PY'
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

src, out, default_agent, default_timeout, parallel, roster_path, scripts_dir = sys.argv[1:]
# Empty means NO wall clock: the wrapper's idle watchdog bounds the run instead.
default_timeout = int(default_timeout) if default_timeout else 0
parallel = int(parallel)
allowed_agents = {"claude", "codex", "agy", "opencode", "copilot", "openrouter", "pi", "chatgpt-chat"}
safe_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

with open(roster_path, encoding="utf-8") as f:
    roster = json.load(f)
lanes = roster["lanes"]
by_lane = {lane["lane_id"]: lane for lane in lanes}

raw = []
with open(src, encoding="utf-8") as f:
    for number, line in enumerate(f, 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"fanout preflight: line {number}: invalid JSON: {exc}")

if not raw:
    raise SystemExit("fanout preflight: no tasks")
if len(raw) > 32:
    raise SystemExit("fanout preflight: maximum 32 tasks per campaign")

# Saved ChatGPT workers are catalog lanes, not static overlay entries.
if any((task.get("agent") or default_agent) == "chatgpt-chat" for task in raw if isinstance(task, dict)):
    sys.path.insert(0, scripts_dir)
    import fleetctl
    roster = fleetctl.read_overlay(Path(roster_path), Path(os.environ.get("FLEET_STATE_DIR", str(fleetctl.DEFAULT_STATE_DIR))))
    lanes = roster["lanes"]
    by_lane = {lane["lane_id"]: lane for lane in lanes}

seen_ids = set()
write_roots = {}
normalized = []
lane_totals = Counter()

def choose_lane(task, mode, modality):
    lane_id = task.get("lane_id", "")
    model_key = task.get("model_key", "")
    selector = task.get("model", "")
    chosen = []
    if lane_id:
        lane = by_lane.get(lane_id)
        if lane is None:
            raise SystemExit(f"fanout preflight: unknown lane_id {lane_id}")
        chosen.append(lane)
    if model_key:
        chosen.extend(l for l in lanes if l["harness"] == "opencode" and l["model_key"] == model_key)
    if selector:
        chosen.extend(l for l in lanes if l["harness"] == "opencode" and l["selector"] == selector)
    if not chosen:
        role = task.get("role", "default")
        command = [
            str(Path(scripts_dir) / "fleetctl.py"),
            "--overlay", roster_path,
            "route", "--role", role, "--mode", mode, "--modality", modality, "--harness", "opencode", "--json",
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise SystemExit(f"fanout preflight: automatic route failed: {result.stderr.strip()}")
        chosen = [json.loads(result.stdout)]
    unique = {l["lane_id"]: l for l in chosen}
    if len(unique) != 1:
        raise SystemExit("fanout preflight: choose only one of lane_id, model_key, or model")
    return next(iter(unique.values()))

def writable_scope(directory):
    """Return the owning git worktree, or the real directory outside git."""
    result = subprocess.run(
        ["git", "-C", directory, "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return os.path.realpath(result.stdout.strip())
    return directory

for index, task in enumerate(raw):
    if not isinstance(task, dict):
        raise SystemExit(f"fanout preflight: task {index + 1} is not an object")
    task_id = task.get("id")
    if not isinstance(task_id, str) or not safe_id.fullmatch(task_id):
        raise SystemExit(f"fanout preflight: unsafe task id {task_id!r}")
    if task_id in seen_ids:
        raise SystemExit(f"fanout preflight: duplicate task id {task_id}")
    seen_ids.add(task_id)

    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SystemExit(f"fanout preflight: {task_id}: prompt is required")
    agent = task.get("agent") or default_agent
    if agent not in allowed_agents:
        raise SystemExit(f"fanout preflight: {task_id}: unknown agent {agent}")
    mode = task.get("mode", "read-only")
    if mode not in {"read-only", "write"}:
        raise SystemExit(f"fanout preflight: {task_id}: mode must be read-only or write")
    directory = os.path.realpath(os.path.expanduser(task.get("dir") or os.getcwd()))
    if not os.path.isdir(directory):
        raise SystemExit(f"fanout preflight: {task_id}: directory not found: {directory}")
    if mode == "write":
        scope = writable_scope(directory)
        for prior_scope, prior_id in write_roots.items():
            common = os.path.commonpath([scope, prior_scope])
            if common in {scope, prior_scope}:
                raise SystemExit(
                    f"fanout preflight: writable worktree overlaps for {prior_id} and {task_id}: "
                    f"{prior_scope} <> {scope}"
                )
        write_roots[scope] = task_id

    timeout = task.get("timeout", default_timeout)
    # 0 (or absent) = uncapped, bounded by the wrapper's idle watchdog. A per-task explicit value is
    # still allowed, and the old 7200 ceiling is kept for anyone who sets one deliberately.
    if not isinstance(timeout, int) or timeout < 0 or timeout > 7200:
        raise SystemExit(f"fanout preflight: {task_id}: timeout must be 0 (uncapped) or 1-7200 seconds")

    model = task.get("model", "")
    lane_id = ""
    quota_pool = ""
    role = task.get("role", "builder" if agent == "codex" and mode == "write" else "default")
    modality = task.get("modality", "text")
    context_profile = task.get("context", "lean")
    if not isinstance(role, str) or not role:
        raise SystemExit(f"fanout preflight: {task_id}: role must be a non-empty string")
    if modality not in {"text", "image", "audio", "video"}:
        raise SystemExit(f"fanout preflight: {task_id}: invalid modality {modality}")
    if agent == "opencode":
        if context_profile not in {"lean", "shared"}:
            raise SystemExit(f"fanout preflight: {task_id}: context must be lean or shared")
        if role not in roster.get("routing", {}).get("roles", {}):
            raise SystemExit(f"fanout preflight: {task_id}: unknown OpenCode role {role}")
    elif "context" in task:
        raise SystemExit(f"fanout preflight: {task_id}: context is only valid for OpenCode tasks")
    attachment = task.get("file", "")
    if attachment:
        attachment = os.path.realpath(os.path.expanduser(attachment))
        if not os.path.isfile(attachment):
            raise SystemExit(f"fanout preflight: {task_id}: attachment not found: {attachment}")
    if agent == "opencode" and role == "research-scout":
        if mode != "read-only" or context_profile != "lean" or modality != "text" or attachment:
            raise SystemExit(
                f"fanout preflight: {task_id}: research-scout must be read-only, lean, text-only, and attachment-free"
            )
    if agent == "opencode":
        lane = choose_lane(task, mode, modality)
        lane_id = lane["lane_id"]
        model = lane["selector"]
        quota_pool = lane["quota_pool"]
        if lane["access_status"] != "verified":
            raise SystemExit(f"fanout preflight: {task_id}: lane access is {lane['access_status']}")
        admission = lane["admission_status"]
        if admission != "active":
            raise SystemExit(f"fanout preflight: {task_id}: lane admission is {admission}")
        if mode not in lane["allowed_modes"]:
            raise SystemExit(f"fanout preflight: {task_id}: {mode} is not allowed for {lane_id}")
        if modality not in lane.get("capabilities", {}).get("input", ["text"]):
            raise SystemExit(f"fanout preflight: {task_id}: {modality} input is not allowed for {lane_id}")
        timeout = task.get("timeout", lane["timeout_s"])
        lane_totals[lane_id] += 1
        if lane_totals[lane_id] > lane["max_tasks_per_run"]:
            raise SystemExit(f"fanout preflight: {lane_id} exceeds max_tasks_per_run={lane['max_tasks_per_run']}")
    elif agent in {"pi", "chatgpt-chat"}:
        transport = "Crossfeed Chat" if agent == "chatgpt-chat" else "Pi"
        if agent == "chatgpt-chat" and mode != "read-only":
            raise SystemExit(f"fanout preflight: {task_id}: Crossfeed Chat is read-only")
        if modality != "text" or attachment:
            raise SystemExit(f"fanout preflight: {task_id}: {transport} transport is text-only and attachment-free")
        matches = [l for l in lanes if l.get("harness") == agent
                   and (l.get("lane_id") == task.get("lane_id") if task.get("lane_id")
                        else l.get("model_key") == task.get("model_key") if task.get("model_key")
                        else l.get("selector") == model if model else False)]
        if len(matches) != 1:
            raise SystemExit(f"fanout preflight: {task_id}: select one explicit {transport} lane via lane_id, model_key, or model")
        lane = matches[0]
        if ((task.get("model_key") and task["model_key"] != lane["model_key"])
                or (model and model != lane["selector"])):
            raise SystemExit(f"fanout preflight: {task_id}: conflicting {transport} lane selectors")
        lane_id, model, quota_pool = lane["lane_id"], lane["selector"], lane["quota_pool"]
        if lane["access_status"] != "verified" or lane["admission_status"] != "active":
            raise SystemExit(f"fanout preflight: {task_id}: {transport} lane {lane_id} is not active and verified")
        if mode not in lane["allowed_modes"] or modality not in lane.get("capabilities", {}).get("input", ["text"]):
            raise SystemExit(f"fanout preflight: {task_id}: mode or modality is not allowed for {lane_id}")
        if agent == "chatgpt-chat":
            if not lane.get("worker_label") or role not in lane.get("roles", []):
                raise SystemExit(f"fanout preflight: {task_id}: saved worker or role is unavailable for {lane_id}")
            if task.get("variant") or task.get("effort") not in {None, "", "service-chosen"}:
                raise SystemExit(f"fanout preflight: {task_id}: Crossfeed Chat uses its saved numeric level")
        # A lane's old wall budget must not impose a hidden default on a live worker.
        timeout = task.get("timeout", default_timeout)
        lane_totals[lane_id] += 1
        if lane_totals[lane_id] > lane["max_tasks_per_run"]:
            raise SystemExit(f"fanout preflight: {lane_id} exceeds max_tasks_per_run={lane['max_tasks_per_run']}")
    elif agent == "agy" and task.get("lane_id"):
        if task.get("model_key") or model:
            raise SystemExit(f"fanout preflight: {task_id}: AGY lane_id cannot combine with model_key or model")
        lane = by_lane.get(task["lane_id"])
        if lane is None or lane.get("harness") != "agy":
            raise SystemExit(f"fanout preflight: {task_id}: unknown AGY lane_id {task['lane_id']}")
        lane_id = lane["lane_id"]
        model = lane["selector"]
        quota_pool = lane["quota_pool"]
        if lane["access_status"] != "verified" or lane["admission_status"] != "active":
            raise SystemExit(f"fanout preflight: {task_id}: AGY lane {lane_id} is not active and verified")
        if mode not in lane["allowed_modes"]:
            raise SystemExit(f"fanout preflight: {task_id}: {mode} is not allowed for {lane_id}")
        if modality not in lane.get("capabilities", {}).get("input", ["text"]):
            raise SystemExit(f"fanout preflight: {task_id}: {modality} input is not allowed for {lane_id}")
        # AGY has no wall clock of its own any more; its lane budget must not be smuggled back in
        # as a hidden default. OpenCode keeps its lane value deliberately (lease has no renewal).
        timeout = task.get("timeout", default_timeout)
        lane_totals[lane_id] += 1
        if lane_totals[lane_id] > lane["max_tasks_per_run"]:
            raise SystemExit(f"fanout preflight: {lane_id} exceeds max_tasks_per_run={lane['max_tasks_per_run']}")
    elif agent == "copilot":
        if mode != "read-only":
            raise SystemExit(f"fanout preflight: {task_id}: Copilot Student is read-only")
        if model not in {"", "auto"}:
            raise SystemExit(f"fanout preflight: {task_id}: Copilot Student is Auto-only")
        lane = by_lane["github-copilot-student-auto"]
        lane_id = lane["lane_id"]
        model = "auto"
        quota_pool = lane["quota_pool"]
        lane_totals[lane_id] += 1
        if lane_totals[lane_id] > lane["max_tasks_per_run"]:
            raise SystemExit("fanout preflight: Copilot Student allows one task per campaign")
    elif agent == "openrouter":
        # TOOLLESS LANE. openrouter-agent.sh is a single chat completion over raw HTTP: no repo,
        # no file reads, no attachments, no web search. It is therefore only admissible for work
        # whose whole material fits in the prompt (hard-reasoning, long-context). The dispatch arm
        # deliberately does NOT pass --dir, so a task cannot quietly imply filesystem access the
        # model does not have.
        if mode != "read-only":
            raise SystemExit(f"fanout preflight: {task_id}: OpenRouter lanes are read-only (no filesystem access at all)")
        if modality != "text":
            raise SystemExit(f"fanout preflight: {task_id}: OpenRouter transport is text-only, got {modality}")
        if attachment:
            raise SystemExit(f"fanout preflight: {task_id}: OpenRouter lanes cannot take attachments")
        if task.get("model"):
            raise SystemExit(f"fanout preflight: {task_id}: select an OpenRouter lane with lane_id or model_key, not model")
        if task.get("lane_id"):
            lane = by_lane.get(task["lane_id"])
            if lane is None or lane.get("harness") != "openrouter":
                raise SystemExit(f"fanout preflight: {task_id}: unknown OpenRouter lane_id {task['lane_id']}")
        elif task.get("model_key"):
            matches = [l for l in lanes if l["harness"] == "openrouter" and l["model_key"] == task["model_key"]]
            if len(matches) != 1:
                raise SystemExit(
                    f"fanout preflight: {task_id}: expected one OpenRouter lane for model_key "
                    f"{task['model_key']}, found {len(matches)}"
                )
            lane = matches[0]
        else:
            command = [
                str(Path(scripts_dir) / "fleetctl.py"), "--overlay", roster_path,
                "route", "--role", role, "--mode", mode, "--modality", modality,
                "--harness", "openrouter", "--json",
            ]
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            if result.returncode != 0:
                raise SystemExit(f"fanout preflight: {task_id}: no OpenRouter lane for role {role}: {result.stderr.strip()}")
            lane = json.loads(result.stdout)
        lane_id = lane["lane_id"]
        model = lane["selector"]
        quota_pool = lane["quota_pool"]
        if lane["access_status"] != "verified" or lane["admission_status"] != "active":
            raise SystemExit(f"fanout preflight: {task_id}: OpenRouter lane {lane_id} is not active and verified")
        if mode not in lane["allowed_modes"]:
            raise SystemExit(f"fanout preflight: {task_id}: {mode} is not allowed for {lane_id}")
        timeout = task.get("timeout", lane["timeout_s"])
        lane_totals[lane_id] += 1
        if lane_totals[lane_id] > lane["max_tasks_per_run"]:
            raise SystemExit(f"fanout preflight: {lane_id} exceeds max_tasks_per_run={lane['max_tasks_per_run']}")
    elif agent == "agy" and task.get("model_key"):
        raise SystemExit(f"fanout preflight: {task_id}: AGY model_key routing is unsupported; use lane_id")
    elif agent == "agy" and mode == "read-only":
        raise SystemExit(f"fanout preflight: {task_id}: Antigravity wrapper has no proven read-only boundary")

    normalized.append({
        "id": task_id,
        "prompt": prompt,
        "dir": directory,
        "agent": agent,
        "mode": mode,
        "lane_id": lane_id,
        "model": model,
        "role": role,
        "modality": modality,
        "context": context_profile if agent == "opencode" else "",
        "file": attachment,
        "variant": task.get("variant", ""),
        "effort": task.get("effort", ""),
        "timeout": timeout,
        "quota_pool": quota_pool,
    })

for start in range(0, len(normalized), parallel):
    batch = normalized[start:start + parallel]
    counts = Counter(t["lane_id"] for t in batch if t["lane_id"])
    for lane_id, count in counts.items():
        cap = by_lane[lane_id]["max_parallel"]
        if count > cap:
            raise SystemExit(
                f"fanout preflight: batch would run {count} x {lane_id}, "
                f"max_tasks_per_run={cap}"
            )

out_path = Path(out)
tasks_path = out_path / "tasks"
with open(out_path / "manifest.jsonl", "w", encoding="utf-8") as manifest:
    for task in normalized:
        manifest.write(json.dumps(task, sort_keys=True) + "\n")
        task_id = task["id"]
        for field, value in task.items():
            if field == "id":
                continue
            text = "1" if value is True else "0" if value is False else str(value)
            (tasks_path / f"{task_id}.{field}").write_text(text, encoding="utf-8")
(tasks_path / "_ids").write_text("\n".join(t["id"] for t in normalized) + "\n", encoding="utf-8")
(tasks_path / "_write_scopes").write_text(
    "\n".join(sorted(write_roots)) + ("\n" if write_roots else ""), encoding="utf-8"
)
PY
preflight_rc=$?
set -e
if [ "$preflight_rc" -ne 0 ]; then
  # Python's SystemExit leaks 1, and 1 is already the code for "the campaign ran and is
  # incomplete" — outcome-taxonomy.sh maps fanout:1 to campaign-incomplete. So every preflight
  # refusal reached callers as a half-finished run and sent them to a summary.tsv that was never
  # written. Convert it here, loudly, to the refusal code.
  echo "fanout: campaign REFUSED before dispatch — NO agent ran, no summary.tsv, no answers in $OUT" >&2
  exit "$EXIT_REFUSED"
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "fanout: preflight PASS -> $OUT/manifest.jsonl" >&2
  cat "$OUT/manifest.jsonl"
  exit 0
fi

# Cross-campaign write claim: preflight only catches overlap WITHIN this run;
# the fleet-level claim rejects a second concurrent writer from any other
# campaign or session (conflict, not clobber). Released on exit; TTL is the
# dead-process safety net.
CLAIM_TOKEN=""
release_claim() {
  if [ -n "$CLAIM_TOKEN" ]; then
    "$HERE/fleetctl.py" release-paths --token "$CLAIM_TOKEN" >/dev/null 2>&1 || true
    CLAIM_TOKEN=""
  fi
}
trap release_claim EXIT
if [ -s "$OUT/tasks/_write_scopes" ]; then
  SCOPES=()
  while IFS= read -r scope; do
    [ -n "$scope" ] && SCOPES+=("$scope")
  done <"$OUT/tasks/_write_scopes"
  if ! CLAIM_TOKEN="$("$HERE/fleetctl.py" claim-paths --owner "fanout:$(basename "$OUT")" --ttl 14400 "${SCOPES[@]}")"; then
    echo "fanout: write-claim CONFLICT — another campaign or session holds a claimed path (fleetctl.py claims); refusing to clobber" >&2
    exit 3
  fi
fi

: >"$OUT/summary.tsv"
IDS=()
while IFS= read -r id; do
  [ -n "$id" ] && IDS+=("$id")
done <"$OUT/tasks/_ids"
echo "fanout: ${#IDS[@]} task(s), fixed batches up to $PARALLEL -> $OUT" >&2

pids=()
running=0
for id in "${IDS[@]}"; do
  (
    dir="$(cat "$OUT/tasks/$id.dir")"
    agent="$(cat "$OUT/tasks/$id.agent")"
    mode="$(cat "$OUT/tasks/$id.mode")"
    lane_id="$(cat "$OUT/tasks/$id.lane_id")"
    model="$(cat "$OUT/tasks/$id.model")"
    modality="$(cat "$OUT/tasks/$id.modality")"
    role="$(cat "$OUT/tasks/$id.role")"
    context_profile="$(cat "$OUT/tasks/$id.context")"
    attachment="$(cat "$OUT/tasks/$id.file")"
    variant="$(cat "$OUT/tasks/$id.variant")"
    effort="$(cat "$OUT/tasks/$id.effort")"
    task_timeout="$(cat "$OUT/tasks/$id.timeout")"
    quota_pool="$(cat "$OUT/tasks/$id.quota_pool")"
    script="$HERE/${agent}-agent.sh"

    if [ -n "$quota_pool" ] && [ -f "$OUT/quota-open/$quota_pool" ]; then
      quota_trigger="$(cat "$OUT/quota-open/$quota_pool")"
      printf 'SKIPPED\t%s\t%s\t%s\tquota-exhausted\tpool=%s;trigger=%s\n' \
        "$id" "$agent" "${lane_id:-${model:-default}}" "$quota_pool" "$quota_trigger" >>"$OUT/summary.tsv"
      echo "fanout: SKIPPED $id because quota pool $quota_pool was exhausted by $quota_trigger" >&2
      exit 0
    fi

    args=( --prompt-file "$OUT/tasks/$id.prompt" --dir "$dir" --last "$OUT/$id.out" )
    # Only pass a wall clock when one was deliberately requested; 0 means leave it uncapped.
    [ -n "$task_timeout" ] && [ "$task_timeout" != "0" ] && args+=( --timeout "$task_timeout" )
    case "$agent" in
      claude)
        args+=( --role "$role" )
        [ "$mode" = "read-only" ] && args+=( --read-only )
        [ -n "$model" ] && args+=( --model "$model" )
        [ -n "$effort" ] && args+=( --effort "$effort" )
        ;;
      codex)
        args+=( --role "$role" )
        if [ "$mode" = "read-only" ]; then args+=( --sandbox read-only ); else args+=( --sandbox workspace-write ); fi
        [ -n "$model" ] && args+=( --model "$model" )
        [ -n "$effort" ] && args+=( --reasoning "$effort" )
        ;;
      opencode)
        args+=( --effort-role "$role" )
        args+=( --lane "$lane_id" )
        if [ "$mode" = "read-only" ]; then args+=( --read-only ); else args+=( --write ); fi
        args+=( --modality "$modality" )
        args+=( --context "$context_profile" )
        [ "$role" = "research-scout" ] && args+=( --web-search )
        [ -n "$attachment" ] && args+=( --file "$attachment" )
        [ -n "$variant" ] && args+=( --variant "$variant" )
        args+=( --events "$OUT/$id.events.jsonl" )
        ;;
      chatgpt-chat)
        script="$HERE/chatgpt-agent.sh"
        args=( --prompt-file "$OUT/tasks/$id.prompt" --dir "$dir" --last "$OUT/$id.out"
               --lane "$lane_id" --effort-role "$role" --mode ro --modality text --idle 0
               --events "$OUT/$id.events.jsonl" )
        [ -n "$task_timeout" ] && [ "$task_timeout" != "0" ] && args+=( --wall "$task_timeout" )
        [ -n "$effort" ] && args+=( --effort "$effort" )
        ;;
      pi)
        args+=( --lane "$lane_id" --effort-role "$role" --events "$OUT/$id.events.jsonl" )
        if [ "$mode" = "read-only" ]; then args+=( --read-only ); else args+=( --write ); fi
        [ -n "$effort" ] && args+=( --effort "$effort" )
        ;;
      copilot)
        args+=( --read-only --model auto --events "$OUT/$id.events.jsonl" )
        ;;
      agy)
        prompt="$(cat "$OUT/tasks/$id.prompt")"
        args=( --prompt "$prompt" --dir "$dir" --last "$OUT/$id.out" --sandbox )
        args+=( --role "$role" )
        [ -n "$effort" ] && args+=( --effort "$effort" )
        [ -n "$task_timeout" ] && [ "$task_timeout" != "0" ] && args+=( --timeout "$task_timeout" )
        [ -n "$lane_id" ] && args+=( --lane "$lane_id" )
        [ -n "$model" ] && args+=( --model "$model" )
        ;;
      openrouter)
        # Rebuilt from scratch WITHOUT --dir: this lane has no filesystem access, and the wrapper
        # rejects --dir on purpose so the absence is a hard error rather than a silent assumption.
        args=( --prompt-file "$OUT/tasks/$id.prompt" --last "$OUT/$id.out" --lane "$lane_id" )
        [ -n "$task_timeout" ] && [ "$task_timeout" != "0" ] && args+=( --timeout "$task_timeout" )
        ;;
    esac

    set +e
    dispatch_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
    CROSSFEED_DISPATCH_ID="$dispatch_id" "$script" "${args[@]}" >/dev/null 2>"$OUT/$id.err"
    rc=$?
    set -e
    classify_wrapper_outcome "$agent" "$rc" "$OUT/$id.out" "$OUT/$id.err"
    reason="$WRAPPER_OUTCOME_REASON"
    [ -z "$WRAPPER_OUTCOME_SECONDARY" ] || reason="$reason;$WRAPPER_OUTCOME_SECONDARY"
    target="${lane_id:-${model:-default}}"
    # A direct wrapper (codex, claude) that ran a stand-in for a model switched off in the console
    # says so as its last stderr line. The summary then names the model that RAN, and says which one
    # the task asked for, so a report built from it never names a model that did not run.
    stand_in="$(grep -E '^Crossfeed: this run used [^ ]+, not ' "$OUT/$id.err" 2>/dev/null | tail -1 || true)"
    if [ -n "$stand_in" ]; then
      ran="${stand_in#Crossfeed: this run used }"; ran="${ran%%,*}"
      reason="$reason;stand-in-for=${model:-default}"
      target="$ran"
      echo "fanout: $id: $stand_in" >&2
    fi

    # The common receipt is authoritative for all wrappers, including Auto and provider aliases.
    # Correlate it to this dispatch so a refusal can never reuse a previous run's sidecar.
    receipt_model="$(jq -r --arg dispatch "$dispatch_id" 'select(.schema == "crossfeed-model-run/v1" and .dispatch_id == $dispatch) | .actual_model // .selected_model // empty' "$OUT/$id.out.crossfeed.json" 2>/dev/null || true)"
    [ -z "$receipt_model" ] || target="$receipt_model"

    if [ "$WRAPPER_OUTCOME_SUPPRESS_POOL" -eq 1 ] && [ -n "$quota_pool" ]; then
      printf '%s\n' "$id" >"$OUT/quota-open/$quota_pool"
      echo "fanout: quota circuit OPEN for pool=$quota_pool after $id ($agent native exit $rc); later same-pool tasks will be skipped" >&2
    fi

    case "$WRAPPER_OUTCOME_CLASS" in
      SUCCEEDED|SUCCEEDED_WITH_WARNING)
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$WRAPPER_OUTCOME_CLASS" "$id" "$agent" "$target" "$reason" "$dir" >>"$OUT/summary.tsv"
        ;;
      FAILED|KILLED_WALL_CLOCK|KILLED_IDLE_WATCHDOG)
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$WRAPPER_OUTCOME_CLASS" "$id" "$agent" "$target" "$reason" "$OUT/$id.err" >>"$OUT/summary.tsv"
        ;;
    esac
  ) &
  pids+=("$!")
  running=$((running + 1))
  if [ "$running" -ge "$PARALLEL" ]; then
    for pid in "${pids[@]}"; do wait "$pid"; done
    pids=()
    running=0
  fi
done
if [ "$running" -gt 0 ]; then
  for pid in "${pids[@]}"; do wait "$pid"; done
fi

sort -o "$OUT/summary.tsv" "$OUT/summary.tsv"
cat "$OUT/summary.tsv"
succeeded_count="$(awk -F '\t' '$1 == "SUCCEEDED" || $1 == "SUCCEEDED_WITH_WARNING" {n++} END {print n+0}' "$OUT/summary.tsv")"
warning_count="$(awk -F '\t' '$1 == "SUCCEEDED_WITH_WARNING" {n++} END {print n+0}' "$OUT/summary.tsv")"
failed_count="$(awk -F '\t' '$1 == "FAILED" {n++} END {print n+0}' "$OUT/summary.tsv")"
wall_count="$(awk -F '\t' '$1 == "KILLED_WALL_CLOCK" {n++} END {print n+0}' "$OUT/summary.tsv")"
idle_count="$(awk -F '\t' '$1 == "KILLED_IDLE_WATCHDOG" {n++} END {print n+0}' "$OUT/summary.tsv")"
skipped_count="$(awk -F '\t' '$1 == "SKIPPED" {n++} END {print n+0}' "$OUT/summary.tsv")"
echo "fanout: summary succeeded=$succeeded_count warnings=$warning_count failed=$failed_count killed_wall_clock=$wall_count killed_idle_watchdog=$idle_count skipped=$skipped_count -> $OUT" >&2
incomplete_count=$((failed_count + wall_count + idle_count + skipped_count))
[ "$incomplete_count" -eq 0 ] || exit 1
