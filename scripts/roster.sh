#!/usr/bin/env bash
# roster.sh: validate and query the local access overlay.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FLEET_ROOT="$(cd "$HERE/.." && pwd)"
ROSTER="${ACCESS_OVERLAY:-${XDG_CONFIG_HOME:-$HOME/.config}/orchestrator/access-overlay.json}"

command -v jq >/dev/null || { echo "roster: jq is required" >&2; exit 127; }
[ -f "$ROSTER" ] || { echo "roster: missing $ROSTER" >&2; exit 2; }

# Query the same live lanes as fleetctl; validation still checks the static file.
case "${1:-}" in
  list|candidates|lane-json|lookup|resolve-lane|check-lane|check)
    if jq -e 'has("chatgpt_gateway") or any(.provider_sources[]?; .kind == "crossfeed-chat") or any(.lanes[]?; .gateway_service != null)' "$ROSTER" >/dev/null; then
      EXPANDED_ROSTER="$(mktemp "${TMPDIR:-/tmp}/chatgpt-roster.XXXXXX")"
      # Sandbox cannot access the Bin; this is our own TMPDIR scratch file.
      trap 'rm -f "$EXPANDED_ROSTER"' EXIT
      python3 "$HERE/fleetctl.py" --overlay "$ROSTER" roster-json > "$EXPANDED_ROSTER"
      ROSTER="$EXPANDED_ROSTER"
      # roster-json already printed the gateway reason. An explicit catalog
      # request must not replace it with a second, misleading "unknown lane".
      if jq -e '.chatgpt_catalog.error != null' "$ROSTER" >/dev/null; then
        case "${1:-}/${2:-}/${3:-}" in
          lane-json/chatgpt:*/*|check-lane/chatgpt:*/*|resolve-lane/chatgpt:*/chatgpt-chat)
            exit 3;;
          lookup/chatgpt-chat/chatgpt:*|check/chatgpt-chat/chatgpt:*)
            exit 3;;
        esac
      fi
    fi
    ;;
esac

# NAMES every violated rule on stderr instead of failing mutely. The previous version was
# one `jq -e` boolean returning exit 1 with NO output, which is precisely how the duplicate-route
# collision below once sat broken for days: `validate` and `doctor` were both red and nothing
# said why, so nobody noticed. A guard that cannot say what it caught is a guard nobody acts on.
# Keep this loud.
validate() {
  # Saved-label selectors share the HTTP transport's contract, including Unicode and spaces.
  python3 - "$ROSTER" "$HERE" <<'PYSELECTOR'
import json, sys
sys.path.insert(0, sys.argv[2])
from chatgpt_transport import canonical_selector, Rejected
roster = json.load(open(sys.argv[1]))
for profile in roster.get("swarm_profiles", {}).values():
    for band in profile.get("bands", {}).values():
        for worker in band.get("workers", []):
            if "selector" not in worker:
                continue
            try:
                if canonical_selector(worker["selector"]) is None:
                    raise Rejected(3, "invalid selector")
            except Rejected:
                print("roster: INVALID swarm worker selector", file=sys.stderr)
                sys.exit(3)
PYSELECTOR
  violations="$(jq -r '
    [
      (if .schema_version != 3 then "schema_version must be 3 (found \(.schema_version | tostring))" else empty end),
      (if (.identity | type) != "string" then "identity must be a string" else empty end),
      (if (.model_evidence | type) != "object" then "model_evidence must be an object" else empty end),
      (if (.routing.roles | type) != "object" then "routing.roles must be an object" else empty end),
      (if (.lanes | type) != "array" then "lanes must be an array" else empty end)
    ]
    + (if (.lanes | type) == "array" then
      [
        ([.lanes[].lane_id] | group_by(.) | map(select(length > 1) | .[0])
          | if length > 0 then "duplicate lane_id: \(join(", "))" else empty end),

        # A lane is a ROUTE, and the roster README defines the route as the unit of operation:
        # model x harness x serving provider x tool policy. Two lanes may therefore share a
        # model_key and harness while differing in selector - that is how reasoning-effort
        # variants of one model are expressed (agy exposes gemini-3.7-flash at high and medium
        # as separate selectors). Uniqueness is enforced on the whole route so genuine
        # duplicates are still caught. resolve_lane below reports its own ambiguity when a
        # model_key maps to several routes on one harness, so nothing silently picks for you.
        ([.lanes[] | [.model_key, .harness, .selector]] | group_by(.) | map(select(length > 1) | .[0] | join("/"))
          | if length > 0 then "duplicate route (model_key/harness/selector): \(join(", "))" else empty end),

        (.lanes[] | select(
          (.lane_id | type != "string") or
          (.model_key | type != "string") or
          (.harness | type != "string") or
          (.provider | type != "string") or
          (.selector | type != "string") or
          (.access_status | type != "string") or
          (.admission_status as $s | ["active", "candidate", "rejected", "retired"] | index($s) | not) or
          (.quality_tier | type != "string") or
          (.capabilities.input | type != "array") or
          (.allowed_modes | type != "array") or
          (.max_parallel | type != "number") or
          (.max_tasks_per_run | type != "number") or
          (.retries != 0)
        ) | "lane \(.lane_id // "<missing lane_id>") has a missing or invalid required field"),

        (. as $root | .swarm_profiles[]?.bands[]?.workers[]?
          | .selector as $selector
          | select((has("lane_id") == has("selector")) or
              (has("selector") and ((.selector | type) != "string" or
                (($root.chatgpt_gateway | type) != "object" and
                 ([$root.provider_sources[]? | select(.kind == "crossfeed-chat")] | length) == 0 and
                 ([$root.lanes[]? | select(.harness == "chatgpt-chat") | .selector] | index($selector)) == null))))
          | "swarm worker must name one lane_id or a configured Crossfeed Chat selector"),

        (([.swarm_profiles[]?.bands[]?.workers[]?.lane_id | select(. != null)] - [.lanes[].lane_id])
          | if length > 0 then "swarm profile references unknown lane(s): \(join(", "))" else empty end),

        # A reference to a lane that EXISTS but is retired/rejected was invisible here, and that gap
        # cost real work twice: openrouter-ox-alpha sat FIRST in six routing bands after
        # its preview died upstream, and opencode-go-grok-4.5 held a seat in the review council after
        # opencode-go stopped serving it. validate stayed green through both, because it only ever
        # asked whether the lane_id resolves. Existence is not admission. Routing falls through a
        # non-active lane at runtime, so this is not a crash - it is a quality ranking and a council
        # roster that quietly describe a fleet you do not have, which is the declared-vs-runtime drift
        # this file exists to catch. Unknown and non-active are reported separately so the message
        # names which mistake was made.
        (. as $root
          | [$root.routing.roles[]?[]?[]?] | unique
          | map(select(. != "chatgpt" or ($root.chatgpt_gateway | type) != "object"))
          | map(select(. as $id | ($root.lanes | map(.lane_id) | index($id)) | not))
          | if length > 0 then "routing band references unknown lane(s): \(join(", "))" else empty end),

        (. as $root
          | [$root.routing.roles[]?[]?[]?] | unique
          | map(select(. as $id
                | ($root.lanes | map(.lane_id) | index($id))
                  and (($root.lanes | map(select(.admission_status == "active") | .lane_id) | index($id)) | not)))
          | if length > 0 then "routing band references non-active lane(s): \(join(", "))" else empty end),

        (. as $root
          | [$root.swarm_profiles[]?.bands[]?.workers[]?.lane_id] | unique
          | map(select(. as $id
                | ($root.lanes | map(.lane_id) | index($id))
                  and (($root.lanes | map(select(.admission_status == "active") | .lane_id) | index($id)) | not)))
          | if length > 0 then "swarm profile references non-active lane(s): \(join(", "))" else empty end),

        ((.swarm_profiles // {}) | to_entries[]
          | select(([.value.bands | keys[]] | sort) != (["conserve", "critical", "quality_first", "unknown"] | sort))
          | "swarm profile \(.key) must define exactly the bands conserve, critical, quality_first, unknown"),

        ((.swarm_profiles // {}) | to_entries[]
          | select([.value.bands[] | select((.parallel | type != "number") or (.parallel < 1) or (.workers | type != "array") or (.workers | length < 1))] | length > 0)
          | "swarm profile \(.key) has a band with a bad parallel count or an empty worker list")
      ]
    else [] end)
    | .[]
  ' "$ROSTER")"
  [ -z "$violations" ] || {
    printf 'roster: INVALID %s\n' "$ROSTER" >&2
    # One bullet PER violation. `printf '  - %s\n' "$violations"` bulleted only the first line of a
    # multi-line list and dumped the rest unprefixed, so a report of three faults read as one fault
    # plus stray text - and this function is built to name every rule it caught, not just the first.
    printf '%s\n' "$violations" | while IFS= read -r _v; do printf '  - %s\n' "$_v" >&2; done
    return 1
  }
}

lookup_lane() {
  harness="$1"; selector="$2"
  count="$(jq -r --arg harness "$harness" --arg selector "$selector" '[.lanes[] | select(.harness == $harness and .selector == $selector)] | length' "$ROSTER")"
  [ "$count" = "1" ] || { echo "roster: expected one lane for $harness/$selector, found $count" >&2; exit 3; }
  jq -r --arg harness "$harness" --arg selector "$selector" '.lanes[] | select(.harness == $harness and .selector == $selector) | .lane_id' "$ROSTER"
}

resolve_lane() {
  model_key="$1"; harness="$2"
  count="$(jq -r --arg key "$model_key" --arg harness "$harness" '[.lanes[] | select(.model_key == $key and .harness == $harness)] | length' "$ROSTER")"
  # Ambiguity is legal in the overlay (reasoning-effort variants share a model_key) but is never
  # resolved by guessing: name the candidates so the caller picks a lane explicitly.
  if [ "$count" != "1" ]; then
    echo "roster: expected one lane for $model_key in $harness, found $count" >&2
    if [ "$count" != "0" ]; then
      echo "roster: candidates (pass one with --lane):" >&2
      jq -r --arg key "$model_key" --arg harness "$harness" \
        '.lanes[] | select(.model_key == $key and .harness == $harness) | "  \(.lane_id)  selector=\(.selector)"' "$ROSTER" >&2
    fi
    exit 3
  fi
  jq -r --arg key "$model_key" --arg harness "$harness" '.lanes[] | select(.model_key == $key and .harness == $harness) | .lane_id' "$ROSTER"
}

check_lane() {
  lane_id="$1"; mode="$2"
  lane="$(jq -ce --arg lane "$lane_id" '.lanes[] | select(.lane_id == $lane)' "$ROSTER")" || {
    echo "roster: unknown lane $lane_id" >&2; exit 3;
  }
  access="$(jq -r '.access_status' <<<"$lane")"
  admission="$(jq -r '.admission_status' <<<"$lane")"
  if [ "$access" != "verified" ]; then
    echo "roster: REJECTED $lane_id, access is $access rather than verified" >&2
    exit 3
  fi
  [ "$admission" = "active" ] || {
    echo "roster: REJECTED $lane_id, operational admission is $admission" >&2
    exit 3
  }
  jq -e --arg mode "$mode" '.allowed_modes | index($mode) != null' <<<"$lane" >/dev/null || {
    echo "roster: REJECTED $lane_id, mode $mode is not allowed" >&2
    exit 3
  }
}

case "${1:-}" in
  validate)
    validate
    echo "roster: valid ($ROSTER)"
    ;;
  list)
    harness="${2:-}"
    jq -r --arg harness "$harness" '
      .lanes[]
      | select($harness == "" or .harness == $harness)
      | [.lane_id, .model_key, .harness, .selector, .access_status, .admission_status, (.roles | join("; "))]
      | @tsv
    ' "$ROSTER"
    ;;
  candidates)
    jq -r '
      . as $root
      | .lanes[]
      | select(.admission_status != "active" or $root.model_evidence[.model_key].status != "supported")
      | [.lane_id, .admission_status, ($root.model_evidence[.model_key].status // "missing"), ($root.model_evidence[.model_key].note // "")]
      | @tsv
    ' "$ROSTER"
    ;;
  lane-json)
    lane_id="${2:-}"; [ -n "$lane_id" ] || { echo "usage: roster.sh lane-json <lane-id>" >&2; exit 2; }
    jq -ce --arg lane "$lane_id" '.lanes[] | select(.lane_id == $lane)' "$ROSTER" || {
      echo "roster: unknown lane $lane_id" >&2; exit 3;
    }
    ;;
  lookup)
    [ $# -eq 3 ] || { echo "usage: roster.sh lookup <harness> <selector>" >&2; exit 2; }
    lookup_lane "$2" "$3"
    ;;
  resolve-lane)
    [ $# -eq 3 ] || { echo "usage: roster.sh resolve-lane <model-key> <harness>" >&2; exit 2; }
    resolve_lane "$2" "$3"
    ;;
  check-lane)
    [ $# -eq 3 ] || { echo "usage: roster.sh check-lane <lane-id> <read-only|write>" >&2; exit 2; }
    check_lane "$2" "$3"
    ;;
  check)
    [ $# -ge 3 ] && [ $# -le 4 ] || { echo "usage: roster.sh check <harness> <selector> [mode]" >&2; exit 2; }
    lane_id="$(lookup_lane "$2" "$3")"
    check_lane "$lane_id" "${4:-read-only}"
    ;;
  profile)
    name="${2:-}"; [ -n "$name" ] || { echo "usage: roster.sh profile <name>" >&2; exit 2; }
    jq -ce --arg name "$name" '.swarm_profiles[$name] // empty' "$ROSTER" || {
      echo "roster: unknown swarm profile $name" >&2; exit 3;
    }
    ;;
  doctor)
    validate
    effort_rc=0
    "$HERE/fleetctl.py" --overlay "$ROSTER" effort-check || effort_rc=$?
    # No shared-roster check: this side is self-contained. A peer's roster
    # is theirs to keep; nothing here reads it, so its absence is not a fault.
    command -v opencode >/dev/null || { echo "roster: opencode binary missing" >&2; exit 127; }
    live="$(mktemp)"; expected="$(mktemp)"
    trap 'rm -f "$live" "$expected"' EXIT
    # Each admitted opencode-harness lane must appear in ITS OWN provider's live picker,
    # not only opencode-go: a metered google lane lists under `opencode models google`.
    providers="$(jq -r '.lanes[] | select(.harness == "opencode" and .admission_status == "active") | .provider' "$ROSTER" | sort -u)"
    for provider in $providers; do
      env -u GITHUB_TOKEN -u GH_TOKEN -u OPENAI_API_KEY \
        OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1 \
        opencode models "$provider" >"$live"
      jq -r --arg p "$provider" '.lanes[] | select(.harness == "opencode" and .admission_status == "active" and .provider == $p) | .selector' "$ROSTER" | sort >"$expected"
      missing="$(comm -23 "$expected" <(sort "$live"))"
      [ -z "$missing" ] || { printf 'roster: picker/catalogue for %s missing admitted selectors:\n%s\n' "$provider" "$missing" >&2; exit 4; }
    done
    if [ "$effort_rc" -ne 0 ]; then
      echo "roster: doctor FAIL (effort check failed; picker checks completed)" >&2
      exit "$effort_rc"
    fi
    echo "roster: doctor PASS"
    ;;
  route)
    shift
    "$HERE/fleetctl.py" route "$@"
    ;;
  effort|effort-check)
    command_name="$1"; shift
    "$HERE/fleetctl.py" --overlay "$ROSTER" "$command_name" "$@"
    ;;
  usage)
    shift
    "$HERE/fleetctl.py" usage "$@"
    ;;
  dashboard)
    "$HERE/fleetctl.py" dashboard
    ;;
  market)
    python3 "$HERE/market-refresh.py"
    ;;
  *)
    echo "usage: roster.sh validate | list [harness] | candidates | lane-json <id> | lookup <harness> <selector> | resolve-lane <model-key> <harness> | check-lane <id> <mode> | check <harness> <selector> [mode] | profile <name> | route ... | usage [--json] | dashboard | doctor" >&2
    exit 2
    ;;
esac

}
main "$@"; exit $?
