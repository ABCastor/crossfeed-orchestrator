#!/usr/bin/env bash
# swarm.sh: launch a curated, read-only multi-model profile from the local access overlay.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FLEET_ROOT="$(cd "$HERE/.." && pwd)"
ROSTER="${ACCESS_OVERLAY:-${XDG_CONFIG_HOME:-$HOME/.config}/orchestrator/access-overlay.json}"
# Shared normalized outcomes and consumer-boundary exit mappings live here.
. "$HERE/outcome-taxonomy.sh"

PROFILE="${1:-}"; shift || true
PROMPT=""; DIR="$PWD"; OUT=""; FILE=""; MODALITY="text"; DRY_RUN=0; EXCLUDE_LINEAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prompt) PROMPT="$2"; shift 2;;
    --dir) DIR="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --file) FILE="$2"; shift 2;;
    --modality) MODALITY="$2"; shift 2;;
    --exclude-lineage) EXCLUDE_LINEAGE="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    *) echo "swarm: unknown arg: $1" >&2; exit 2;;
  esac
done

[ -n "$PROFILE" ] || { echo "usage: swarm.sh <explore|review|research|media-review> --prompt TASK [--dir DIR] [--file PATH --modality image|audio|video] [--out DIR] [--exclude-lineage VENDOR] [--dry-run]" >&2; exit 2; }
[ -n "$PROMPT" ] || { echo "swarm: --prompt is required" >&2; exit 2; }
[ -d "$DIR" ] || { echo "swarm: directory not found: $DIR" >&2; exit 2; }
[ -f "$ROSTER" ] || { echo "swarm: access overlay not found: $ROSTER" >&2; exit 2; }
case "$MODALITY" in text|image|audio|video) ;; *) echo "swarm: invalid modality $MODALITY" >&2; exit 2;; esac
[ -z "$FILE" ] || [ -f "$FILE" ] || { echo "swarm: attachment not found: $FILE" >&2; exit 2; }
[ "$PROFILE" != "media-review" ] || [ -n "$FILE" ] || { echo "swarm: media-review requires --file" >&2; exit 2; }
[ "$PROFILE" != "media-review" ] || [ "$MODALITY" != "text" ] || { echo "swarm: media-review requires --modality image|audio|video" >&2; exit 2; }

# OpenCode's attachment transport is proven only for images. Audio and video
# bypass the overlay entirely and use the native Gemini Files API transport.
if [ "$PROFILE" = "media-review" ] && { [ "$MODALITY" = "audio" ] || [ "$MODALITY" = "video" ]; }; then
  media_args=( --file "$FILE" --modality "$MODALITY" --prompt "$PROMPT" )
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'swarm: dry-run'
    printf ' %q' "$HERE/gemini-media.sh" "${media_args[@]}"
    printf '\n'
    exit 0
  fi
  if [ -n "$OUT" ]; then
    if [ -e "$OUT" ] && [ -n "$(find "$OUT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
      echo "swarm: refusing non-empty output directory: $OUT" >&2
      exit 2
    fi
    mkdir -p "$OUT"
    "$HERE/gemini-media.sh" "${media_args[@]}" | tee "$OUT/gemini-media.out"
  else
    "$HERE/gemini-media.sh" "${media_args[@]}"
  fi
  exit
fi

tasks="$(mktemp)"
trap 'rm -f "$tasks"' EXIT
state="$("$HERE/fleetctl.py" usage --json | jq -er '.quota_state')" || {
  echo "swarm: could not determine quota state" >&2; exit 3;
}
case "$state" in
  ABUNDANT|HEALTHY) band="quality_first";;
  UNKNOWN) band="unknown";;
  CONSERVE) band="conserve";;
  CRITICAL) band="critical";;
  EXHAUSTED) if [ "$PROFILE" = "research" ]; then band="critical"; else echo "swarm: OpenCode Go quota pool is exhausted; no council launched" >&2; exit 4; fi;;
  *) echo "swarm: unsupported quota state $state" >&2; exit 3;;
esac
parallel="$(jq -er --arg profile "$PROFILE" --arg band "$band" '.swarm_profiles[$profile].bands[$band].parallel' "$ROSTER")" || {
  echo "swarm: unknown profile $PROFILE" >&2; exit 3;
}

python3 - "$ROSTER" "$PROFILE" "$band" "$PROMPT" "$DIR" "$FILE" "$MODALITY" "$EXCLUDE_LINEAGE" "$HERE" >"$tasks" <<'PY'
import json
import os
from pathlib import Path
import sys

roster_path, profile_name, band_name, prompt, directory, attachment, modality, exclude_lineage, scripts_dir = sys.argv[1:]
with open(roster_path, encoding="utf-8") as f:
    roster = json.load(f)
if profile_name == "research":
    sys.path.insert(0, scripts_dir)
    import fleetctl
    roster = fleetctl.read_overlay(Path(roster_path), Path(os.environ.get("FLEET_STATE_DIR", str(fleetctl.DEFAULT_STATE_DIR))))
profile = roster.get("swarm_profiles", {}).get(profile_name)
if profile is None:
    raise SystemExit(f"unknown profile: {profile_name}")
band = profile.get("bands", {}).get(band_name)
if band is None:
    raise SystemExit(f"profile {profile_name} has no {band_name} band")
lanes = {lane["lane_id"]: lane for lane in roster.get("lanes", [])}
# A DROPPED worker is a missing lens, and a missing lens is invisible in the output: that is exactly
# how a degraded panel gets read as a full one. Record why each one was dropped and say so loudly;
# never let a reduced panel present itself as the profile that was requested.
eligible = []
omitted = []
for worker in band["workers"]:
    lane_id = worker.get("lane_id") or worker.get("selector")
    lane = lanes.get(lane_id) if worker.get("lane_id") else next(
        (lane for lane in lanes.values() if lane.get("selector") == worker.get("selector")), None)
    worker = {**worker, "lane_id": lane["lane_id"] if lane else lane_id}
    if not lane:
        omitted.append((worker["lane_id"], "lane not in roster")); continue
    if exclude_lineage:
        sys.path.insert(0, scripts_dir)
        import selector
        lineage = selector._lineage(roster, {"model_key": lane["model_key"], "lane_id": lane["lane_id"], "provider": lane.get("provider")})
        if lineage == exclude_lineage.lower():
            omitted.append((worker["lane_id"], "excluded lead lineage")); continue
    if lane.get("access_status") != "verified" or lane.get("admission_status") != "active":
        omitted.append((worker["lane_id"], f"access={lane.get('access_status')} admission={lane.get('admission_status')}")); continue
    if "read-only" not in lane.get("allowed_modes", []):
        omitted.append((worker["lane_id"], "lane does not allow read-only")); continue
    if modality not in lane.get("capabilities", {}).get("input", ["text"]):
        omitted.append((worker["lane_id"], f"lane does not accept {modality}")); continue
    eligible.append(worker)
for lane_id, why in omitted:
    print(f"swarm: OMITTED WORKER {lane_id}: {why}", file=sys.stderr)
if omitted:
    print(f"swarm: PANEL INCOMPLETE - {len(eligible)} of {len(band['workers'])} workers dispatched; "
          f"{len(omitted)} lens(es) missing from this profile", file=sys.stderr)
    # Deliberately NOT converted into a non-zero exit here: existing callers treat swarm's exit
    # code as fanout's, and changing that contract untested during this pass would trade one silent
    # failure for another. Visibility is the half that matters and is fixed; the exit-code half is
    # still open.
if not eligible:
    raise SystemExit(
        f"profile {profile_name}/{band_name} has no active lane for modality {modality}"
    )
for worker in eligible:
    full_prompt = (
        prompt.rstrip()
        + "\n\nYour assigned angle: " + worker["angle"]
        + "\nReturn: claims, exact evidence, uncertainty, and the one next check that would falsify your conclusion. Do not edit files."
    )
    # The agent MUST come from the lane's harness, not a constant. Hardcoding "opencode" was correct
    # only while every profile worker happened to be an OpenCode lane; once a council spans pools (so
    # it can still convene when one is exhausted) that constant would dispatch a Gemini or Claude lane
    # through the OpenCode wrapper.
    lane_harness = lanes[worker["lane_id"]].get("harness")
    if not lane_harness:
        raise SystemExit(f"profile {profile_name}: lane {worker['lane_id']} has no harness")
    task = {
        "id": worker["id"],
        "agent": lane_harness,
        "lane_id": worker["lane_id"],
        "mode": "read-only",
        "dir": directory,
        "prompt": full_prompt,
        "modality": modality,
    }
    task["role"] = worker.get("role", "default")
    if "timeout" in worker:
        task["timeout"] = worker["timeout"]
    if attachment:
        task["file"] = attachment
    print(json.dumps(task))
PY

task_count="$(wc -l < "$tasks" | tr -d ' ')"
[ "$parallel" -le "$task_count" ] || parallel="$task_count"
args=( "$tasks" --parallel "$parallel" )
[ -n "$OUT" ] && args+=( --out "$OUT" )
[ "$DRY_RUN" -eq 0 ] || args+=( --dry-run )
echo "swarm: profile=$PROFILE quota_state=$state band=$band workers=$task_count parallel=$parallel" >&2
set +e
"$HERE/fanout.sh" "${args[@]}"
fanout_rc=$?
set -e
classify_consumer_outcome fanout "$fanout_rc"
if [ "$CONSUMER_OUTCOME_CLASS" != "SUCCEEDED" ]; then
  echo "swarm: downstream outcome=$CONSUMER_OUTCOME_CLASS reason=$CONSUMER_OUTCOME_REASON native_fanout_exit=$fanout_rc mapped_exit=$CONSUMER_BOUNDARY_RC" >&2
fi
exit "$CONSUMER_BOUNDARY_RC"

}
main "$@"; exit $?
