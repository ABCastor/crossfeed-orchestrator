#!/usr/bin/env bash
# opencode-agent.sh: run one roster-admitted OpenCode Go worker and return its final text.
#
# Defaults to the read-only plan agent. --write enables build+auto and is accepted only
# in a clean git tree. OpenCode has no kernel sandbox: use a disposable worktree.
#
# Usage:
#   opencode-agent.sh --prompt "<task>" [--dir <workdir>]
#                     [--role default|implementation|review|debug|repo-map|long-context|frontend-visual|audio-video|research-scout]
#                     [--lane ID | --model-key KEY | --model SELECTOR]
#                     [--modality text|image|audio|video] [--file PATH ...]
#                     [--context lean|shared | --shared-context]
#                     [--inject <file> ...]   # prepend a specific skill-slice / fact by path
#                     [--web-search]
#                     [--direct]               # one toolless model call; text-only, no repo/project harness
#                     [--variant high|max] [--read-only|--write]
#                     [--timeout S] [--idle-timeout S] [--kill-after S]
#                     [--events <file.jsonl>] [--last <file>]
#
# Without --timeout, the selected lane's roster timeout remains the deliberate hard
# budget. The quota lease is padded through the complete TERM-to-KILL window so it
# cannot expire while its child group is still alive. Idle timeout defaults to 2400
# seconds and kill-after defaults to 30 seconds.
# Exit codes: 0 success; 2 usage; 3 modality rejection; 4 lease/event JSON failure;
# 5 session error; 6 no successful terminal step; 7 empty output; 8 telemetry failure;
# 124 wall-clock kill; 125 idle kill; 127 missing dependency.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$PWD"; PROMPT=""; PROMPT_FILE=""; MODEL=""; MODEL_KEY=""; LANE=""
ROLE="default"; ROLE_SET=0; MODALITY="text"; VARIANT=""; MODE="read-only"; CONTEXT="lean"
EFFORT_ROLE=""
TIMEOUT=""; IDLE_TIMEOUT="2400"; KILL_AFTER="30"; ROSTER_TIMEOUT=""
EVENTS=""; LAST=""; FILES=(); FILE_COUNT=0; WEB_SEARCH=0; DIRECT=0; INJECT_FILES=()

_usage_error() {
  echo "opencode-agent: $1" >&2
  exit 2
}

_require_value() {
  [ "$#" -ge 2 ] || _usage_error "$1 requires a value"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prompt)      _require_value "$@"; PROMPT="$2"; shift 2;;
    --prompt-file) _require_value "$@"; PROMPT_FILE="$2"; shift 2;;
    --dir)         _require_value "$@"; DIR="$2"; shift 2;;
    --lane)        _require_value "$@"; LANE="$2"; shift 2;;
    --model-key)   _require_value "$@"; MODEL_KEY="$2"; shift 2;;
    --model)       _require_value "$@"; MODEL="$2"; shift 2;;
    --role)        _require_value "$@"; ROLE="$2"; ROLE_SET=1; shift 2;;
    --effort-role) _require_value "$@"; EFFORT_ROLE="$2"; shift 2;;
    --modality)    _require_value "$@"; MODALITY="$2"; shift 2;;
    --file)        _require_value "$@"; FILES+=("$2"); FILE_COUNT=$((FILE_COUNT + 1)); shift 2;;
    --context)     _require_value "$@"; CONTEXT="$2"; shift 2;;
    --shared-context) CONTEXT="shared"; shift;;
    --inject)      _require_value "$@"; INJECT_FILES+=("$2"); shift 2;;
    --web-search)  WEB_SEARCH=1; shift;;
    --direct)      DIRECT=1; shift;;
    --variant)     _require_value "$@"; VARIANT="$2"; shift 2;;
    --read-only)   MODE="read-only"; shift;;
    --write)       MODE="write"; shift;;
    --timeout)     _require_value "$@"; TIMEOUT="$2"; shift 2;;
    --idle-timeout) _require_value "$@"; IDLE_TIMEOUT="$2"; shift 2;;
    --kill-after)  _require_value "$@"; KILL_AFTER="$2"; shift 2;;
    --events)      _require_value "$@"; EVENTS="$2"; shift 2;;
    --last)        _require_value "$@"; LAST="$2"; shift 2;;
    -h|--help)     sed -n '2,27p' "$0"; exit 0;;
    *) _usage_error "unknown arg: $1";;
  esac
done

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

if [ "$ROLE" = "research-scout" ]; then
  WEB_SEARCH=1
elif [ "$WEB_SEARCH" = "1" ] && [ "$ROLE_SET" = "1" ]; then
  echo "opencode-agent: --web-search cannot be combined with a non-research role" >&2
  exit 2
fi
[ "$WEB_SEARCH" = "1" ] && ROLE="research-scout"

if [ -n "$PROMPT_FILE" ] && [ -z "$PROMPT" ]; then
  [ -f "$PROMPT_FILE" ] || { echo "opencode-agent: prompt-file not found: $PROMPT_FILE" >&2; exit 2; }
  PROMPT="$(<"$PROMPT_FILE")"
fi

[ -n "$PROMPT" ] || { echo "opencode-agent: --prompt or --prompt-file is required" >&2; exit 2; }
[ -d "$DIR" ] || { echo "opencode-agent: work directory not found: $DIR" >&2; exit 2; }

# --inject: prepend one or more specific context slices (a skill-method file, one reference,
# or a fact line) so a lean worker gets exactly what its task needs without the full skill
# surface. Injected content is prompt text; the caller is responsible for it carrying no
# secrets, and it is refused for a public-web scout (which must receive no local context).
if [ "${#INJECT_FILES[@]}" -gt 0 ]; then
  [ "$WEB_SEARCH" = "1" ] && { echo "opencode-agent: --inject cannot combine with --web-search (a public-web scout must not receive local context)" >&2; exit 2; }
  [ "$DIRECT" = "0" ] || { echo "opencode-agent: --direct rejects --inject; pass the complete model-only prompt explicitly" >&2; exit 2; }
  injected=""
  for f in "${INJECT_FILES[@]}"; do
    [ -f "$f" ] || { echo "opencode-agent: --inject file not found: $f" >&2; exit 2; }
    injected+="=== INJECTED CONTEXT: $f ==="$'\n'"$(<"$f")"$'\n\n'
  done
  PROMPT="${injected}=== TASK ==="$'\n'"$PROMPT"
fi

selectors=0
[ -n "$LANE" ] && selectors=$((selectors + 1))
[ -n "$MODEL_KEY" ] && selectors=$((selectors + 1))
[ -n "$MODEL" ] && selectors=$((selectors + 1))
[ "$selectors" -le 1 ] || { echo "opencode-agent: choose only one of --lane, --model-key, or --model" >&2; exit 2; }
[ "$selectors" -eq 0 ] || [ "$ROLE_SET" -eq 0 ] || {
  echo "opencode-agent: --role is automatic routing; do not combine it with an explicit lane/model" >&2; exit 2;
}
case "$MODALITY" in text|image|audio|video) ;; *) echo "opencode-agent: invalid modality $MODALITY" >&2; exit 2;; esac
case "$CONTEXT" in lean|shared) ;; *) echo "opencode-agent: invalid context profile $CONTEXT" >&2; exit 2;; esac
if [ "$WEB_SEARCH" = "1" ]; then
  [ "$MODE" = "read-only" ] || { echo "opencode-agent: --web-search is read-only" >&2; exit 2; }
  [ "$CONTEXT" = "lean" ] || { echo "opencode-agent: --web-search requires --context lean" >&2; exit 2; }
  [ "$MODALITY" = "text" ] || { echo "opencode-agent: --web-search accepts text only" >&2; exit 2; }
  [ "$FILE_COUNT" -eq 0 ] || { echo "opencode-agent: --web-search rejects attachments and local evidence" >&2; exit 2; }
fi
if [ "$DIRECT" = "1" ]; then
  [ "$MODE" = "read-only" ] || { echo "opencode-agent: --direct is read-only" >&2; exit 2; }
  [ "$WEB_SEARCH" = "0" ] || { echo "opencode-agent: --direct cannot combine with --web-search" >&2; exit 2; }
  [ "$CONTEXT" = "lean" ] || { echo "opencode-agent: --direct does not load shared context" >&2; exit 2; }
  [ "$MODALITY" = "text" ] || { echo "opencode-agent: --direct accepts text only" >&2; exit 2; }
  [ "$FILE_COUNT" -eq 0 ] || { echo "opencode-agent: --direct rejects attachments and local evidence" >&2; exit 2; }
fi
# Environment probes come after argument validation: a malformed invocation should report
# what is wrong with the invocation, not which binary is missing. Nothing above this line
# shells out, so the checks are still ahead of every external call.
command -v opencode >/dev/null || { echo "opencode-agent: opencode binary not on PATH" >&2; exit 127; }
command -v jq >/dev/null || { echo "opencode-agent: jq is required" >&2; exit 127; }

# A static scanner cannot see `timeout ./opencode-agent.sh`, so detect the exact
# layered-cap failure at runtime and make the outer wall clock explicit.
_parent_comm="$(ps -o comm= -p "$PPID" 2>/dev/null | tr -d '[:space:]' || true)"
case "${_parent_comm##*/}" in
  timeout|gtimeout)
    echo "opencode-agent: WARNING - launched under an outer '${_parent_comm##*/}' wall-clock cap." >&2
    echo "opencode-agent: that outer cap will kill this run regardless of progress, and the idle watchdog cannot prevent it." >&2
    echo "opencode-agent: remove the outer wrapper; pass --timeout to this script if you genuinely need a different hard budget." >&2
    ;;
esac

WORKER_CONFIG_DIR="$HOME/.config/opencode/fleet-worker"
if [ "$CONTEXT" = "lean" ] && [ "$DIRECT" = "0" ]; then
  [ -f "$WORKER_CONFIG_DIR/opencode.jsonc" ] && [ -f "$WORKER_CONFIG_DIR/AGENTS.md" ] || {
    echo "opencode-agent: lean profile is incomplete: $WORKER_CONFIG_DIR" >&2
    exit 2
  }
fi
if [ "$FILE_COUNT" -gt 0 ]; then
  for file in "${FILES[@]}"; do
    [ -f "$file" ] || { echo "opencode-agent: attachment not found: $file" >&2; exit 2; }
  done
fi

# LANE_EXPLICIT distinguishes "the operator named this model" from "the router picked it".
# An explicitly named lane is never silently substituted; a routed lane may be.
#
# Every selector lookup below is caught explicitly. A bare x="$(cmd)" under `set -e`
# aborts the script carrying the CALLEE's exit code, and roster.sh answers 3 for
# anything it cannot resolve. 3 is also this wrapper's own code for "lane does not
# admit this modality" (lane_setup below), so a typo arrived at the caller wearing the
# code for a rejected request, with no line naming this wrapper. The ambiguity outlives
# the run: fanout.sh writes the raw code into summary.tsv as FAIL(3) and afk-run.sh
# persists it in the ledger as worker-returncode. Catching each failure keeps the
# callee's own message on stderr and answers 2, this wrapper's bad-usage code.
LANE_EXPLICIT=0
if [ -n "$MODEL_KEY" ]; then
  LANE="$("$HERE/roster.sh" resolve-lane "$MODEL_KEY" opencode)" || {
    echo "opencode-agent: no OpenCode lane for --model-key $MODEL_KEY (see roster message above; try 'roster.sh list opencode')" >&2
    exit 2; }
  LANE_EXPLICIT=1
elif [ -n "$MODEL" ]; then
  LANE="$("$HERE/roster.sh" lookup opencode "$MODEL")" || {
    echo "opencode-agent: no OpenCode lane for --model $MODEL (see roster message above; try 'roster.sh list opencode')" >&2
    exit 2; }
  LANE_EXPLICIT=1
elif [ -n "$LANE" ]; then
  LANE_EXPLICIT=1
else
  LANE="$("$HERE/fleetctl.py" route --role "$ROLE" --mode "$MODE" --modality "$MODALITY" --harness opencode)" || {
    echo "opencode-agent: no eligible OpenCode lane for role=$ROLE mode=$MODE modality=$MODALITY (see fleetctl message above)" >&2
    exit 2; }
fi

# Re-runnable: a routed lane can change when the first choice is busy, and every
# lane-derived value has to be re-derived with it.
TIMEOUT_EXPLICIT=0; [ -n "$TIMEOUT" ] && TIMEOUT_EXPLICIT=1
CALLER_VARIANT="$VARIANT"
lane_setup() {
  # Same catch as the selector block above, and this is where it was measured: with a
  # bare assignment, `--lane <typo>` exited 3 with only "roster: unknown lane <typo>" on
  # stderr, indistinguishable from the exit 3 four lines down that means the lane cannot
  # take this modality. lane_setup also re-runs after a step-down inside the acquire
  # loop, so aborting here on an unresolvable lane is the right answer in both callers.
  lane_json="$("$HERE/roster.sh" lane-json "$LANE")" || {
    echo "opencode-agent: unknown lane: $LANE (see roster message above; try 'roster.sh list opencode')" >&2
    exit 2; }
  [ "$(jq -r '.harness' <<<"$lane_json")" = "opencode" ] || { echo "opencode-agent: lane $LANE is not an OpenCode lane" >&2; exit 2; }
  MODEL="$(jq -r '.selector' <<<"$lane_json")"
  effort_args=( effort "$MODEL" "${EFFORT_ROLE:-$ROLE}" --harness opencode --explain )
  [ -z "$CALLER_VARIANT" ] || effort_args+=( --level "$CALLER_VARIANT" )
  effort_row="$("$HERE/fleetctl.py" "${effort_args[@]}")" || { echo "opencode-agent: cannot resolve variant" >&2; exit 2; }
  IFS=$'\t' read -r VARIANT _effort_model _effort_why <<<"$effort_row"
  echo "opencode-agent: effort $VARIANT: $_effort_why" >&2
  [ "$VARIANT" != provider-default ] || VARIANT=""
  ROSTER_TIMEOUT="$(jq -r '.timeout_s' <<<"$lane_json")"
  _is_seconds "$ROSTER_TIMEOUT" || { echo "opencode-agent: lane $LANE has an invalid timeout_s" >&2; exit 4; }
  [ "$TIMEOUT_EXPLICIT" = "1" ] || TIMEOUT="$ROSTER_TIMEOUT"
  "$HERE/roster.sh" check-lane "$LANE" "$MODE"
  jq -e --arg modality "$MODALITY" '.capabilities.input | index($modality) != null' <<<"$lane_json" >/dev/null || {
    echo "opencode-agent: lane $LANE does not admit $MODALITY input" >&2
    exit 3
  }
}
lane_setup

if [ "$MODE" = "write" ]; then
  git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "opencode-agent: --write requires a git worktree" >&2; exit 2;
  }
  [ -z "$(git -C "$DIR" status --porcelain)" ] || {
    echo "opencode-agent: --write refuses a dirty tree; create a dedicated clean worktree" >&2; exit 2;
  }
fi

tmp_events=""
if [ -z "$EVENTS" ]; then tmp_events="$(mktemp)"; EVENTS="$tmp_events"; fi
stderr_file="$(mktemp)"
answer_file="$(mktemp)"
KILL_STATE="$(mktemp)"
record_error=""
search_dir=""
oc_data_home=""
direct_config_dir=""
RUN_DIR="$DIR"
lease_token=""
CHILD_PID=""
CHILD_PGID=""
WATCHDOG_PID=""
cleanup() {
  status=$?
  # Only the wrapper's own shell cleans up. With bash 5 a background subshell (the watchdog,
  # a job just forked) can run this inherited EXIT trap and delete the run's files early.
  [ "${BASHPID:-$$}" = "$$" ] || return "$status"
  crossfeed_finish "$status" || status=8
  set +e
  [ -n "$WATCHDOG_PID" ] && kill -s KILL "$WATCHDOG_PID" 2>/dev/null || true
  if [ -n "$lease_token" ]; then "$HERE/fleetctl.py" release --token "$lease_token" >/dev/null 2>&1 || true; fi
  rm -f "$tmp_events" "$stderr_file" "$answer_file" "$KILL_STATE" "$record_error"
  if [ -n "$direct_config_dir" ] && [ -d "$direct_config_dir" ]; then rm -rf "$direct_config_dir"; fi
  if [ -n "$search_dir" ] && [ -d "$search_dir" ]; then rm -rf "$search_dir"; fi
  if [ -n "$oc_data_home" ] && [ -d "$oc_data_home" ]; then
    if [ "$status" -eq 0 ]; then
      rm -rf "$oc_data_home"
    else
      echo "opencode-agent: failed run kept isolated OpenCode data for debugging: $oc_data_home" >&2
    fi
  fi
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT
if [ "$WEB_SEARCH" = "1" ]; then
  command -v git >/dev/null || { echo "opencode-agent: git is required for isolated web search" >&2; exit 127; }
  system_temp_root=""
  if [ -x /usr/bin/getconf ]; then
    system_temp_root="$(/usr/bin/getconf DARWIN_USER_TEMP_DIR 2>/dev/null || true)"
  fi
  if [ -z "$system_temp_root" ] || [ ! -d "$system_temp_root" ]; then system_temp_root="/private/tmp"; fi
  search_root="${system_temp_root%/}/opencode-fleet-public-search"
  mkdir -p "$search_root"
  chmod 700 "$search_root"
  search_dir="$(mktemp -d "$search_root/run.XXXXXX")"
  if ! git -C "$search_dir" init -q; then
    rm -rf "$search_dir"
    echo "opencode-agent: could not initialize isolated public-search root" >&2
    exit 2
  fi
  RUN_DIR="$search_dir"
fi
auth_source="$HOME/.local/share/opencode/auth.json"
[ -f "$auth_source" ] || { echo "opencode-agent: OpenCode auth not found: $auth_source" >&2; exit 2; }
oc_homes_root="${FLEET_STATE_DIR:-$HOME/.local/state/orchestrator}/oc-homes"
mkdir -p -m 700 "$oc_homes_root"
oc_data_home="$(mktemp -d "$oc_homes_root/run.XXXXXXXX")"
mkdir -m 700 "$oc_data_home/opencode"
cp "$auth_source" "$oc_data_home/opencode/auth.json"
chmod 600 "$oc_data_home/opencode/auth.json"
if [ "$DIRECT" = "1" ]; then
  direct_config_dir="$(mktemp -d)"
  cat >"$direct_config_dir/opencode.json" <<'JSON'
{
  "agent": {
    "direct": {
      "tools": {"*": false},
      "permission": {"*": "deny"}
    }
  }
}
JSON
fi
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# A busy lane must never silently kill the task.
#   routed lane   -> step down to the next ranked candidate, announced on stderr
#   explicit lane -> wait for that lane's own slot, never substituted, because
#                    swapping a model the operator named would make the reply a lie
# Waiting is bounded; on expiry the error names why and what to do.
LANE_WAIT_S="${OPENCODE_LANE_WAIT_S:-900}"
acquire_err="$(mktemp)"
acquire_deadline=$(( $(date +%s) + LANE_WAIT_S ))
lease_token=""
while : ; do
  # The roster value sizes the reservation even when the caller supplies a
  # different wall budget. Keep the lease beyond whichever deadline is later,
  # the full escalation grace, and a setup/recording margin. Therefore a new
  # dispatch cannot take this slot while the supervised group is still alive.
  lease_budget="$ROSTER_TIMEOUT"
  if [ -n "$TIMEOUT" ] && [ "$TIMEOUT" -gt "$lease_budget" ]; then lease_budget="$TIMEOUT"; fi
  lease_ttl=$((lease_budget + KILL_AFTER + 60))
  if lease_token="$("$HERE/fleetctl.py" acquire --lane "$LANE" --ttl "$lease_ttl" 2>"$acquire_err")"; then
    break
  fi
  # Only lane contention is recoverable here; quota exhaustion is not.
  if ! grep -q "active lease(s), cap is" "$acquire_err"; then
    cat "$acquire_err" >&2; rm -f "$acquire_err"; exit 4
  fi
  if [ "$LANE_EXPLICIT" != "1" ]; then
    next_lane="$("$HERE/fleetctl.py" route --role "$ROLE" --mode "$MODE" --modality "$MODALITY" --harness opencode 2>/dev/null || true)"
    if [ -n "$next_lane" ] && [ "$next_lane" != "$LANE" ]; then
      echo "opencode-agent: lane $LANE is at capacity; stepping down to $next_lane" >&2
      LANE="$next_lane"; lane_setup; continue
    fi
  fi
  if [ "$(date +%s)" -ge "$acquire_deadline" ]; then
    echo "opencode-agent: lane $LANE still at capacity after ${LANE_WAIT_S}s." >&2
    if [ "$LANE_EXPLICIT" = "1" ]; then
      echo "opencode-agent: it was named explicitly, so it was never substituted. Drop --lane/--model-key to let the router pick the next-best free lane, or raise OPENCODE_LANE_WAIT_S." >&2
    else
      echo "opencode-agent: every eligible lane for role=$ROLE is busy. Retry later or reduce parallelism." >&2
    fi
    rm -f "$acquire_err"; exit 4
  fi
  sleep 10
done
rm -f "$acquire_err"

args=( run --pure --model "$MODEL" --format json --dir "$RUN_DIR" )
if [ "$DIRECT" = "1" ]; then
  AGENT_PROFILE="direct"
  args+=( --agent direct )
elif [ "$MODE" = "write" ]; then
  AGENT_PROFILE="build"
  args+=( --agent build --auto )
elif [ "$WEB_SEARCH" = "1" ]; then
  AGENT_PROFILE="fleet-research"
  args+=( --agent fleet-research )
  PROMPT="Public-web source scouting only. Never put local, private, or credential data into a search query. $PROMPT"
else
  AGENT_PROFILE="plan"
  args+=( --agent plan )
fi
[ -n "$VARIANT" ] && args+=( --variant "$VARIANT" )
if [ "$FILE_COUNT" -gt 0 ]; then
  for file in "${FILES[@]}"; do args+=( --file "$file" ); done
fi

crossfeed_prepare opencode

run_opencode() {
  if [ "$DIRECT" = "1" ]; then
    exec env -u COPILOT_GITHUB_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
      -u XDG_DATA_HOME \
      -u OPENCODE_CONFIG -u OPENCODE_CONFIG_CONTENT -u OPENCODE_CONFIG_DIR -u OPENCODE_ENABLE_EXA \
      -u OPENCODE_WEBSEARCH_PROVIDER -u PARALLEL_API_KEY -u EXA_API_KEY \
      -u OPENCODE_ENABLE_PARALLEL -u OPENCODE_EXPERIMENTAL_PARALLEL -u OPENCODE_EXPERIMENTAL_EXA \
      -u OPENCODE_EXPERIMENTAL -u OPENCODE_AUTO_SHARE \
      XDG_DATA_HOME="$oc_data_home" \
      OPENCODE_AUTO_SHARE=false \
      OPENCODE_CONFIG_DIR="$direct_config_dir" OPENCODE_DISABLE_PROJECT_CONFIG=1 \
      OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1 \
      opencode "${args[@]}" -- "$PROMPT" </dev/null >"$EVENTS" 2>"$stderr_file"
  elif [ "$CONTEXT" = "lean" ]; then
    if [ "$WEB_SEARCH" = "1" ]; then
      exec env -u COPILOT_GITHUB_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
        -u XDG_DATA_HOME \
        -u OPENCODE_CONFIG -u OPENCODE_CONFIG_CONTENT -u OPENCODE_CONFIG_DIR -u OPENCODE_ENABLE_EXA \
        -u OPENCODE_WEBSEARCH_PROVIDER -u PARALLEL_API_KEY -u EXA_API_KEY \
        -u OPENCODE_ENABLE_PARALLEL -u OPENCODE_EXPERIMENTAL_PARALLEL -u OPENCODE_EXPERIMENTAL_EXA \
        -u OPENCODE_EXPERIMENTAL -u OPENCODE_AUTO_SHARE -u TMPDIR \
        XDG_DATA_HOME="$oc_data_home" \
        OPENCODE_ENABLE_EXA=1 OPENCODE_WEBSEARCH_PROVIDER=exa OPENCODE_AUTO_SHARE=false \
        OPENCODE_CONFIG_DIR="$WORKER_CONFIG_DIR" OPENCODE_DISABLE_PROJECT_CONFIG=1 \
        OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1 \
        opencode "${args[@]}" -- "$PROMPT" </dev/null >"$EVENTS" 2>"$stderr_file"
    else
      exec env -u COPILOT_GITHUB_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
        -u XDG_DATA_HOME \
        -u OPENCODE_CONFIG -u OPENCODE_CONFIG_CONTENT -u OPENCODE_CONFIG_DIR -u OPENCODE_ENABLE_EXA \
        -u OPENCODE_WEBSEARCH_PROVIDER -u PARALLEL_API_KEY -u EXA_API_KEY \
        -u OPENCODE_ENABLE_PARALLEL -u OPENCODE_EXPERIMENTAL_PARALLEL -u OPENCODE_EXPERIMENTAL_EXA \
        -u OPENCODE_EXPERIMENTAL -u OPENCODE_AUTO_SHARE \
        XDG_DATA_HOME="$oc_data_home" \
        OPENCODE_AUTO_SHARE=false \
        OPENCODE_CONFIG_DIR="$WORKER_CONFIG_DIR" OPENCODE_DISABLE_PROJECT_CONFIG=1 \
        OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1 \
        opencode "${args[@]}" -- "$PROMPT" </dev/null >"$EVENTS" 2>"$stderr_file"
    fi
  else
    exec env -u COPILOT_GITHUB_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
      -u XDG_DATA_HOME \
      -u OPENCODE_CONFIG -u OPENCODE_CONFIG_CONTENT -u OPENCODE_CONFIG_DIR \
      -u OPENCODE_DISABLE_PROJECT_CONFIG -u OPENCODE_ENABLE_EXA \
      -u OPENCODE_WEBSEARCH_PROVIDER -u PARALLEL_API_KEY -u EXA_API_KEY \
      -u OPENCODE_ENABLE_PARALLEL -u OPENCODE_EXPERIMENTAL_PARALLEL -u OPENCODE_EXPERIMENTAL_EXA \
      -u OPENCODE_EXPERIMENTAL -u OPENCODE_AUTO_SHARE \
      XDG_DATA_HOME="$oc_data_home" \
      OPENCODE_AUTO_SHARE=false \
      OPENCODE_DISABLE_EXTERNAL_SKILLS=1 OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1 \
      opencode "${args[@]}" -- "$PROMPT" </dev/null >"$EVENTS" 2>"$stderr_file"
  fi
}

if stat -f '%z:%m' "$0" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
elif stat -c '%s:%Y' "$0" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
else
  echo "opencode-agent: neither BSD nor GNU stat interface is available" >&2
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
  _file_signature "$EVENTS"
  printf '|'
  _file_signature "$stderr_file"
}

# Keep the historical function name, but sample the whole OpenCode process
# group. Tool descendants are real work and must count even when the leader waits.
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
  kill -0 -- "-$CHILD_PGID" 2>/dev/null
}

_wait_for_group_completion() {
  local announced=0
  while _group_alive; do
    if [ "$announced" -eq 0 ]; then
      echo "opencode-agent: OpenCode leader exited $rc, but process group $CHILD_PGID still has live descendants; keeping supervision active until the group exits." >&2
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
  echo "opencode-agent: ${label} LIMIT FIRED after ${elapsed}s (${detail})." >&2
  echo "opencode-agent: working directory: $DIR" >&2
  echo "opencode-agent: killing OpenCode process group $CHILD_PGID; partial state and edits may be on disk." >&2
  echo "opencode-agent: recovery: inspect the working directory and git status before deciding whether to rerun." >&2
}

_terminate_group() {
  local term_started now since_term
  kill -TERM -- "-$CHILD_PGID" 2>/dev/null || true
  term_started="$(_now)"

  while _group_alive; do
    now="$(_now)"
    since_term=$((now - term_started))
    if [ "$since_term" -ge "$KILL_AFTER" ]; then
      echo "opencode-agent: KILL-AFTER LIMIT FIRED after ${since_term}s; sending SIGKILL to process group $CHILD_PGID." >&2
      kill -KILL -- "-$CHILD_PGID" 2>/dev/null || true
      return
    fi
    sleep 1
  done
}

# The watchdog is stopped with SIGKILL, never TERM: a TERM that lands while bash is still forking
# the watchdog makes bash 5 run the wrapper's EXIT trap in it, deleting this run's files.
_watchdog() {
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
      if [ "$TIMEOUT_EXPLICIT" = "1" ]; then
        _print_kill_message "WALL-CLOCK" "$elapsed" "configured --timeout ${TIMEOUT}s"
      else
        _print_kill_message "WALL-CLOCK" "$elapsed" "roster lane $LANE wall budget ${TIMEOUT}s"
      fi
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
    # --idle-timeout 0 DISABLES idle supervision. Without this, 0 meant "kill the instant nothing
    # has changed", so anyone trying to turn supervision off got instant kills instead. Keeping the
    # off-switch honest matters: it is the one lever for anyone who would rather risk an unbounded
    # hang than any false kill, and a reviewer argued exactly that position.
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
  trap - HUP INT TERM
  [ -n "$WATCHDOG_PID" ] && kill -s KILL "$WATCHDOG_PID" 2>/dev/null || true
  if [ -n "$CHILD_PGID" ] && _group_alive; then
    now="$(_now)"
    elapsed=$((now - RUN_STARTED))
    echo "opencode-agent: EXTERNAL ${signal_name} RECEIVED after ${elapsed}s; no configured limit fired." >&2
    echo "opencode-agent: working directory: $DIR" >&2
    echo "opencode-agent: killing OpenCode process group $CHILD_PGID; partial state and edits may be on disk." >&2
    _terminate_group
  fi
  exit "$signal_code"
}

# Job control makes the exec'd OpenCode process a process-group leader. All
# supervision signals target that group, so tool descendants cannot be orphaned.
CROSSFEED_STARTED=1
set -m
run_opencode &
CHILD_PID=$!
if ! CHILD_PGID="$(_read_child_pgid "$CHILD_PID")"; then
  # set -m made the child its own group leader at launch (PID = PGID), even when it finished
  # before ps could see it, so that identity still reaches any descendants it left behind.
  # A child that already exited is not a failure: `wait` below still returns its status.
  CHILD_PGID="$CHILD_PID"
  if kill -0 "$CHILD_PID" 2>/dev/null; then
    echo "opencode-agent: PROCESS-GROUP DISCOVERY WARNING: ps did not return a PGID; supervising live child group $CHILD_PGID via the set -m PID=PGID invariant." >&2
  fi
fi
set +m

RUN_STARTED="$(_now)"
trap '_handle_signal HUP 129' HUP
trap '_handle_signal INT 130' INT
trap '_handle_signal TERM 143' TERM

_watchdog &
WATCHDOG_PID=$!

set +e
wait "$CHILD_PID"
rc=$?
set -e

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
WATCHDOG_PID=""
CHILD_PID=""

case "$(sed -n '1p' "$KILL_STATE")" in
  wall) rc=124;;
  idle) rc=125;;
esac

record_error="$(mktemp)"
telemetry_failed=0
if ! "$HERE/fleetctl.py" record --lane "$LANE" --events "$EVENTS" --stderr "$stderr_file" \
  --identity "$CROSSFEED_IDENTITY" --returncode "$rc" --started-at "$started_at" --context-profile "$CONTEXT" \
  --effort "$CROSSFEED_RESOLVED_EFFORT" --role "${EFFORT_ROLE:-$ROLE}" \
  --agent-profile "$AGENT_PROFILE" --execution-mode "$([ "$DIRECT" = "1" ] && echo direct || echo harness)" >/dev/null 2>"$record_error"; then
  telemetry_failed=1
  echo "opencode-agent: TELEMETRY FAILURE, this run was not accounted and must not be retried automatically" >&2
  tail -8 "$record_error" >&2 || true
fi

if [ $rc -ne 0 ]; then
  echo "opencode-agent: opencode exited $rc (124=wall-clock kill, 125=idle kill). stderr tail:" >&2
  tail -8 "$stderr_file" >&2 || true
  exit $rc
fi

jq -e . "$EVENTS" >/dev/null 2>&1 || {
  echo "opencode-agent: invalid JSON event stream" >&2; exit 4;
}
# Collect every match before deciding: jq 1.6 (Debian 12, Ubuntu 22.04) sets -e from the LAST
# input only, so a session error followed by any other event used to read as a clean run.
if jq -e -n '[inputs | select(((.type // "") | test("error"; "i")) or (.error? != null))] | length > 0' "$EVENTS" >/dev/null; then
  echo "opencode-agent: session error event despite process exit 0" >&2
  jq -c 'select(((.type // "") | test("error"; "i")) or (.error? != null))' "$EVENTS" | tail -3 >&2
  exit 5
fi

reason="$(jq -r 'select(.type == "step_finish") | .part.reason // empty' "$EVENTS" | tail -1)"
[ "$reason" = "stop" ] || {
  echo "opencode-agent: no successful terminal step (last reason=${reason:-missing})" >&2; exit 6;
}
jq -rs -r '[.[] | select(.type == "text") | .part.text // empty] | last // empty' "$EVENTS" >"$answer_file"
[ -s "$answer_file" ] || { echo "opencode-agent: empty final model output" >&2; exit 7; }

cat "$answer_file"
[ -n "$LAST" ] && cp "$answer_file" "$LAST"

sync_verify="${AGENT_SYNC_VERIFY:-}"
if [ ! -x "$sync_verify" ]; then
  echo "opencode-agent: SYNC VERIFIER MISSING OR NOT EXECUTABLE: $sync_verify" >&2
elif ! "$sync_verify" --source opencode-wrapper --strict >/dev/null; then
  echo "opencode-agent: SYNC VERIFICATION FAILURE, inspect ~/.local/state/agent-sync/last-receipt.json" >&2
fi

# Fail loud when the run succeeded but telemetry did not record: the spend is
# unaccounted, so the caller must not treat this as a clean success. The deliverable
# was already written to --last above, so nothing is lost; exit 8 flags "ran but
# unaccounted — do not auto-retry".
if [ "$telemetry_failed" = "1" ]; then
  echo "opencode-agent: exiting 8 — telemetry was not recorded; spend is unaccounted (deliverable still written to --last)" >&2
  exit 8
fi
