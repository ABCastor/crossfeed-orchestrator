#!/usr/bin/env bash
# openrouter-agent.sh — run one free-tier OpenRouter chat completion and return its final text.
#
# FREE-ONLY BY DESIGN: before every call this fetches OpenRouter's live model catalog
# (GET /api/v1/models) and refuses to send the request unless every field in that model's
# `pricing` object is exactly "0" AND its output is pure text. This is the real safety rail,
# not the roster entry: a temporary free preview (e.g. a stealth model) that starts billing
# mid-week is refused live, not silently charged. Non-text output (audio/image generation) is
# excluded because some models price those outside the prompt/completion fields this checks.
#
# Usage:
#   openrouter-agent.sh --prompt "<task>" [--lane ID | --model-key KEY | --model SELECTOR]
#                       [--timeout S] [--idle-timeout S] [--kill-after S] [--last <file>]
#
#   --timeout has no default: absent means no wall-clock limit.
#   --idle-timeout defaults to 2400 seconds; --kill-after defaults to 30 seconds.
#
# Without a lane/model-key/model, --model-key is required (there is no auto-routing yet:
# no OpenRouter lane is in any routing.roles list, so fleetctl route --harness openrouter
# would have nothing to return).
#
# WHY THIS WRAPPER STREAMS. It used to bind TIMEOUT="120" — the exact silent wall-clock default
# that once killed long runs on codex-agent.sh, and it killed slow free-tier reasoning
# runs while reporting something that read like a network error. Removing the cap alone would
# have traded a silent kill for a silent hang, because a NON-streaming completion offers nothing
# to observe: between "request sent" and "response complete" there is no progress at all, so an
# "idle watchdog" over it would just be a wall clock wearing a costume. The request is therefore
# sent with `stream: true`, and the liveness signal is THE HTTP STREAM ITSELF — every SSE byte,
# including OpenRouter's `: OPENROUTER PROCESSING` keepalives and reasoning deltas, counts as the
# server still working. That makes the same contract the CLI wrappers hold (no wall clock, idle
# supervision, TERM then KILL on the process group) an honest one here rather than a relabelling.
# The idle default matches the other wrappers on purpose: one contract, one number, no second
# hidden bound. This transport is chattier than a CLI agent, so pass --idle-timeout for a tighter
# bound if you want one; pass --idle-timeout 0 to disable idle supervision entirely.
#
# Prints the model's final text to stdout. Diagnostics go to stderr.
# Exit codes: 0 success; 2 usage; 3 lane rejected by roster or key missing; 4 lease failure;
# 5 model failed the live free-only gate; 6 API/network error; 7 empty output;
# 124 wall-clock kill; 125 idle-watchdog kill; 127 missing dep.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROMPT=""; PROMPT_FILE=""; MODEL=""; MODEL_KEY=""; LANE=""; LAST=""
TIMEOUT=""; IDLE_TIMEOUT="2400"; KILL_AFTER="30"

_usage_error() {
  echo "openrouter-agent: $1" >&2
  exit 2
}

_require_value() {
  [ "$#" -ge 2 ] || _usage_error "$1 requires a value"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prompt)       _require_value "$@"; PROMPT="$2"; shift 2;;
    --prompt-file)  _require_value "$@"; PROMPT_FILE="$2"; shift 2;;
    --lane)         _require_value "$@"; LANE="$2"; shift 2;;
    --model-key)    _require_value "$@"; MODEL_KEY="$2"; shift 2;;
    --model)        _require_value "$@"; MODEL="$2"; shift 2;;
    --timeout)      _require_value "$@"; TIMEOUT="$2"; shift 2;;
    --idle-timeout) _require_value "$@"; IDLE_TIMEOUT="$2"; shift 2;;
    --kill-after)   _require_value "$@"; KILL_AFTER="$2"; shift 2;;
    --last)         _require_value "$@"; LAST="$2"; shift 2;;
    -h|--help)      sed -n '2,39p' "$0"; exit 0;;
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

if [ -n "$PROMPT_FILE" ] && [ -z "$PROMPT" ]; then
  [ -f "$PROMPT_FILE" ] || _usage_error "prompt-file not found: $PROMPT_FILE"
  PROMPT="$(<"$PROMPT_FILE")"
fi
[ -n "$PROMPT" ] || _usage_error "--prompt or --prompt-file is required"

selectors=0
[ -n "$LANE" ] && selectors=$((selectors + 1))
[ -n "$MODEL_KEY" ] && selectors=$((selectors + 1))
[ -n "$MODEL" ] && selectors=$((selectors + 1))
[ "$selectors" -le 1 ] || _usage_error "choose only one of --lane, --model-key, or --model"
[ "$selectors" -ge 1 ] || _usage_error "no auto-routing yet: pass --lane, --model-key, or --model explicitly"

command -v jq >/dev/null || { echo "openrouter-agent: jq is required" >&2; exit 127; }
command -v python3 >/dev/null || { echo "openrouter-agent: python3 is required" >&2; exit 127; }

# OUTER-CAP DETECTION. A file scanner cannot see call sites, so `timeout 900 ./openrouter-agent.sh`
# sits outside any static check, and that outer clock overrides the watchdog while knowing nothing
# about whether the stream is still delivering. Say so loudly rather than dying mysteriously.
_parent_comm="$(ps -o comm= -p "$PPID" 2>/dev/null | tr -d '[:space:]' || true)"
case "${_parent_comm##*/}" in
  timeout|gtimeout)
    echo "openrouter-agent: WARNING - launched under an outer '${_parent_comm##*/}' wall-clock cap." >&2
    echo "openrouter-agent: that outer cap will kill this run regardless of stream progress, and the idle watchdog cannot prevent it." >&2
    echo "openrouter-agent: remove the outer wrapper; pass --timeout to this script if you genuinely need a hard budget." >&2
    ;;
esac

if [ -n "$MODEL_KEY" ]; then
  LANE="$("$HERE/roster.sh" resolve-lane "$MODEL_KEY" openrouter)" || {
    echo "openrouter-agent: no OpenRouter lane for --model-key $MODEL_KEY (see roster message above; try 'roster.sh list openrouter')" >&2
    exit 2; }
elif [ -n "$MODEL" ]; then
  LANE="$("$HERE/roster.sh" lookup openrouter "$MODEL")" || {
    echo "openrouter-agent: no OpenRouter lane for --model $MODEL (see roster message above; try 'roster.sh list openrouter')" >&2
    exit 2; }
fi

lane_json="$("$HERE/roster.sh" lane-json "$LANE")" || {
  echo "openrouter-agent: unknown lane: $LANE (see roster message above; try 'roster.sh list openrouter')" >&2
  exit 2; }
[ "$(jq -r '.harness' <<<"$lane_json")" = "openrouter" ] || { echo "openrouter-agent: lane $LANE is not an OpenRouter lane" >&2; exit 2; }
MODEL="$(jq -r '.selector' <<<"$lane_json")"
echo "openrouter-agent: effort service-chosen: the service chooses the level" >&2
"$HERE/roster.sh" check-lane "$LANE" "read-only"

KEY_REF="$(jq -r '.auth.key_ref // empty' <<<"$lane_json")"
if [ -n "$KEY_REF" ]; then
  KEY="$(python3 - "$HERE" "$KEY_REF" <<'PYKEY'
import sys
sys.path.insert(0, sys.argv[1])
from providers import resolve_key, ProviderError
try:
    sys.stdout.write(resolve_key(sys.argv[2]))
except ProviderError:
    sys.exit(3)
PYKEY
  )" || { echo "openrouter-agent: key reference could not be read" >&2; exit 3; }
else
KEY_FILE="${OPENROUTER_KEY_FILE:-$HOME/.config/orchestrator/openrouter_api_key}"
[ -r "$KEY_FILE" ] || { echo "openrouter-agent: API key not found at $KEY_FILE" >&2; exit 3; }
KEY="$(cat "$KEY_FILE")"
[ -n "$KEY" ] || { echo "openrouter-agent: API key at $KEY_FILE is empty" >&2; exit 3; }
fi

# The deliverable ALWAYS lands in a run-private file first. Writing straight into the caller's
# --last means a run that dies mid-stream leaves the PREVIOUS run's answer in place and the
# wrapper publishes stale bytes as this run's result.
RUN_LAST="$(mktemp "${TMPDIR:-/tmp}/openrouter-agent.runlast.XXXXXX")"
# PROGRESS is the liveness file: the Python child rewrites it with the running SSE byte count as
# the stream arrives, so the watchdog is reading the HTTP transport, not CPU. A raw-HTTP call
# burns no CPU while it waits, so the codex-style CPU signal would false-kill every healthy run.
PROGRESS="$(mktemp "${TMPDIR:-/tmp}/openrouter-agent.progress.XXXXXX")"
RUNOUT="$(mktemp "${TMPDIR:-/tmp}/openrouter-agent.runout.XXXXXX")"
KILL_STATE="$(mktemp "${TMPDIR:-/tmp}/openrouter-agent.kill.XXXXXX")"

lease_token=""
CHILD_PID=""
CHILD_PGID=""
WATCHDOG_PID=""
PENDING_SIGNAL_NAME=""
PENDING_SIGNAL_CODE=""

cleanup() {
  status=$?
  # Only the wrapper's own shell cleans up. With bash 5 a background subshell (the watchdog,
  # a job just forked) can run this inherited EXIT trap and delete the run's files early.
  [ "${BASHPID:-$$}" = "$$" ] || return "$status"
  crossfeed_finish "$status" || status=8
  set +e
  [ -n "$WATCHDOG_PID" ] && kill -s KILL "$WATCHDOG_PID" 2>/dev/null
  [ -n "$lease_token" ] && "$HERE/fleetctl.py" release --token "$lease_token" >/dev/null 2>&1
  rm -f "${EVENTS:-}" "$RUN_LAST" "$PROGRESS" "$RUNOUT" "$KILL_STATE"
  trap - EXIT
  exit "$status"
}
trap cleanup EXIT

# The lease must outlive the call, and with no wall clock the only bound left is the idle
# watchdog, so that is what the TTL is sized against. Reading TIMEOUT into an unrelated
# lease variable is not a wall-clock default; it is the lease borrowing whichever bound applies.
lease_ttl=$(( ${TIMEOUT:-$IDLE_TIMEOUT} + 60 ))
acquire_err="$(mktemp)"
if ! lease_token="$("$HERE/fleetctl.py" acquire --lane "$LANE" --ttl "$lease_ttl" 2>"$acquire_err")"; then
  cat "$acquire_err" >&2; rm -f "$acquire_err"
  echo "openrouter-agent: lane $LANE is at capacity; retry shortly" >&2
  exit 4
fi
rm -f "$acquire_err"

if stat -f '%z:%m' "$0" >/dev/null 2>&1; then
  STAT_STYLE="bsd"
elif stat -c '%s:%Y' "$0" >/dev/null 2>&1; then
  STAT_STYLE="gnu"
else
  echo "openrouter-agent: neither BSD nor GNU stat interface is available" >&2
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
  _file_signature "$PROGRESS"
  printf '|'
  _file_signature "$RUN_LAST"
  printf '|'
  _file_signature "$RUNOUT"
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

_now() {
  date +%s
}

_print_kill_message() {
  local label="$1" elapsed="$2" detail="$3"
  echo "openrouter-agent: ${label} LIMIT FIRED after ${elapsed}s (${detail})." >&2
  echo "openrouter-agent: lane $LANE, model $MODEL." >&2
  echo "openrouter-agent: killing the request process group $CHILD_PGID; no partial answer is published." >&2
  echo "openrouter-agent: this lane is toolless and stateless, so there is nothing to resume - relaunch the task." >&2
}

_terminate_group() {
  local term_started now since_term
  kill -TERM -- "-$CHILD_PGID" 2>/dev/null || true
  term_started="$(_now)"

  while _group_alive "$CHILD_PGID"; do
    now="$(_now)"
    since_term=$((now - term_started))
    if [ "$since_term" -ge "$KILL_AFTER" ]; then
      echo "openrouter-agent: KILL-AFTER LIMIT FIRED after ${since_term}s; sending SIGKILL to process group $CHILD_PGID." >&2
      kill -KILL -- "-$CHILD_PGID" 2>/dev/null || true
      return
    fi
    sleep 1
  done
}

# The watchdog is stopped with SIGKILL, never TERM: a TERM that lands while bash is still forking
# the watchdog makes bash 5 run the wrapper's EXIT trap in it, deleting this run's files.
_watchdog() {
  local started last_activity now elapsed idle_for last_output current_output

  started="$(_now)"
  last_activity="$started"
  last_output="$(_output_signature)"

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
    if [ "$current_output" != "$last_output" ]; then
      last_activity="$now"
    fi
    last_output="$current_output"

    idle_for=$((now - last_activity))
    # --idle-timeout 0 DISABLES idle supervision, the one lever for anyone who would rather risk
    # an unbounded hang than any false kill. Without this branch, 0 would mean "kill the instant
    # nothing has changed", so the off-switch would produce instant kills instead.
    if [ "$IDLE_TIMEOUT" -gt 0 ] && [ "$idle_for" -ge "$IDLE_TIMEOUT" ]; then
      printf 'idle\n' >"$KILL_STATE"
      _print_kill_message "IDLE" "$elapsed" "no HTTP stream progress for ${idle_for}s; configured --idle-timeout ${IDLE_TIMEOUT}s"
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

  # set -m already guarantees PID=PGID between `command &` and PGID sampling. Use that only
  # while the child still exists; if it has exited, let the normal wait path keep its real status.
  if [ -z "$CHILD_PGID" ] && _child_alive; then
    CHILD_PGID="$CHILD_PID"
  fi
  if [ -n "$CHILD_PGID" ] && _group_alive "$CHILD_PGID"; then
    trap - HUP INT TERM
    now="$(_now)"
    elapsed=$((now - RUN_STARTED))
    echo "openrouter-agent: EXTERNAL ${signal_name} RECEIVED after ${elapsed}s; no configured limit fired." >&2
    _terminate_group
    exit "$signal_code"
  fi
  return 0
}

RUN_STARTED="$(_now)"
trap '_handle_signal HUP 129' HUP
trap '_handle_signal INT 130' INT
trap '_handle_signal TERM 143' TERM

if [ -n "$PENDING_SIGNAL_CODE" ]; then
  trap - HUP INT TERM
  exit "$PENDING_SIGNAL_CODE"
fi

# Job control gives the request its own process group, so the watchdog can signal the whole group
# and nothing the child spawns can outlive a killed run.
crossfeed_prepare openrouter
EVENTS="$(mktemp "${TMPDIR:-/tmp}/openrouter-model.XXXXXX")"
CROSSFEED_STARTED=1
set -m
# OPENROUTER_API_BASE exists so the watchdog can be PROVEN, not asserted: the tests point it
# at a local server that stalls, dribbles, or hangs, and assert the real exit codes. It is a
# test seam, not a routing knob - never point it at a third party, the API key travels with it.
OPENROUTER_MODEL="$MODEL" OPENROUTER_KEY="$KEY" OPENROUTER_PROMPT="$PROMPT" \
OPENROUTER_API_BASE="${OPENROUTER_API_BASE:-https://openrouter.ai/api/v1}" \
OPENROUTER_TIMEOUT="$TIMEOUT" OPENROUTER_IDLE="$IDLE_TIMEOUT" \
OPENROUTER_MODEL_EVENTS="$EVENTS" OPENROUTER_OUT="$RUN_LAST" OPENROUTER_PROGRESS="$PROGRESS" \
  python3 - >"$RUNOUT" <<'PY' &
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

model = os.environ["OPENROUTER_MODEL"]
key = os.environ["OPENROUTER_KEY"]
prompt = os.environ["OPENROUTER_PROMPT"]
out_path = os.environ["OPENROUTER_OUT"]
progress_path = os.environ["OPENROUTER_PROGRESS"]
wall_raw = os.environ.get("OPENROUTER_TIMEOUT", "").strip()
wall_s = int(wall_raw) if wall_raw else 0
idle_s = int(os.environ["OPENROUTER_IDLE"])

API = os.environ["OPENROUTER_API_BASE"]
deadline = (time.monotonic() + wall_s) if wall_s > 0 else None

stream_bytes = 0
last_beat = 0.0


def fail(code: int, message: str) -> None:
    print(f"openrouter-agent: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def remaining() -> float | None:
    """Seconds left on the caller's wall clock, or None when the run is uncapped."""
    if deadline is None:
        return None
    value = deadline - time.monotonic()
    if value <= 0:
        fail(124, f"wall-clock --timeout {wall_s}s expired")
    return max(1.0, value)


def sock_timeout() -> float | None:
    """Per-socket-operation bound, NOT a wall clock.

    The bash idle watchdog is the SOLE authority on idleness: it is the one that can kill the
    whole process group and the one that produces the documented exit 125. Setting this to the
    idle bound too would make both fire in the same second and let a coin toss decide whether a
    stalled stream is reported as an idle kill or as a network error - the "silent failure that
    reads like an ordinary failure" this whole invariant set exists to prevent. So it is
    deliberately slack (idle + 60): a backstop for a python left orphaned by a dead parent,
    never the thing that fires first.
    """
    left = remaining()
    idle = (idle_s + 60) if idle_s > 0 else None
    if left is None:
        return idle
    if idle is None:
        return left
    return min(left, idle)


def sock_timeout_note() -> str:
    value = sock_timeout()
    return "an unbounded read" if value is None else f"{value:.0f}s"


def beat(force: bool = False) -> None:
    """Publish stream liveness for the bash watchdog. Throttled; never fatal."""
    global last_beat
    now = time.monotonic()
    if not force and now - last_beat < 0.5:
        return
    last_beat = now
    try:
        with open(progress_path, "w") as handle:
            handle.write(f"{stream_bytes}\n")
    except OSError:
        pass


# Free models - and the stealth previews especially - sit on a SHARED upstream pool, so a 429
# means "someone else is using it right now", not "you are over your quota". Seen in practice:
# ox-alpha returned `limit_source: upstream_provider_shared_pool` within one second on a first
# call with the account completely idle. That is transient contention, and retrying a request
# that produced no tokens costs nothing and double-spends nothing, so it is safe in a way a
# task-level retry is not (lane `retries` stays 0 on purpose: re-running a COMPLETED task is
# what the roster forbids). Retries are bounded, honour the caller's deadline, and never apply
# to a 4xx that means the request itself is wrong.
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


def backoff_or_fail(attempt: int, backoff: float, last: str, code: int) -> float:
    wait = backoff
    left = remaining()
    if left is not None:
        wait = min(backoff, left - 1)
    if wait <= 0:
        fail(code, f"{last} (no time left to retry within --timeout {wall_s}s)")
    print(
        f"openrouter-agent: transient upstream error, retry {attempt}/{MAX_ATTEMPTS - 1} in {wait:.0f}s",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(wait)
    return backoff * 2


def request_json(request: urllib.request.Request, error_code: int) -> dict:
    backoff = 2.0
    last = ""
    body = b""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=sock_timeout()) as response:
                body = response.read()
            beat(force=True)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:600]
            last = f"API error HTTP {exc.code}: {detail}"
            if exc.code not in RETRY_STATUS or attempt == MAX_ATTEMPTS:
                fail(error_code, last)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"network request failed: {exc}"
            if attempt == MAX_ATTEMPTS:
                fail(error_code, last)
        backoff = backoff_or_fail(attempt, backoff, last, error_code)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        fail(error_code, "API returned non-JSON data")


def stream_completion(request: urllib.request.Request) -> tuple[str, dict | None]:
    """Read the SSE response, writing content deltas to disk as they arrive.

    Every received byte - keepalive comments and reasoning deltas included - is liveness, and
    only `delta.content` is the deliverable. A stream that has already emitted content is never
    retried: re-running it would duplicate tokens already on disk.
    """
    global stream_bytes
    backoff = 2.0
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        emitted = 0
        streaming = False
        parts: list[str] = []
        usage: dict | None = None
        try:
            with urllib.request.urlopen(request, timeout=sock_timeout()) as response:
                streaming = True
                with open(out_path, "w") as sink:
                    for raw in response:
                        stream_bytes += len(raw)
                        beat()
                        if deadline is not None and time.monotonic() > deadline:
                            fail(124, f"wall-clock --timeout {wall_s}s expired mid-stream")
                        line = raw.decode("utf-8", errors="replace").strip()
                        # An SSE comment (": OPENROUTER PROCESSING") is the server saying it is
                        # still working. It carries no content but it IS liveness.
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("model"):
                            with open(os.environ["OPENROUTER_MODEL_EVENTS"], "a") as identity:
                                identity.write(json.dumps({"type": "crossfeed.provider_model", "model": chunk["model"]}) + "\n")
                        if chunk.get("error"):
                            fail(6, f"stream carried an error: {json.dumps(chunk['error'])[:600]}")
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            piece = ((choice.get("delta") or {}).get("content")) or ""
                            if piece:
                                parts.append(piece)
                                emitted += len(piece)
                                sink.write(piece)
                                sink.flush()
            beat(force=True)
            return "".join(parts), usage
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:600]
            last = f"API error HTTP {exc.code}: {detail}"
            if exc.code not in RETRY_STATUS or attempt == MAX_ATTEMPTS:
                fail(6, last)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"network request failed: {exc}"
            # Retries exist for CONNECTION contention on a shared free pool. Once the response
            # body is open, a failure is not contention: it is this generation dying, and
            # re-running it would either duplicate tokens already on disk or silently relabel a
            # stalled stream as a transient blip. Report what actually happened instead.
            if streaming:
                if deadline is not None and time.monotonic() >= deadline:
                    fail(124, f"wall-clock --timeout {wall_s}s expired mid-stream")
                if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
                    fail(125, f"the HTTP stream stalled with no bytes for {sock_timeout_note()}; "
                              "the idle watchdog should normally have fired first")
                fail(6, f"{last} (stream died after {emitted} characters; a partial generation is never retried)")
            if attempt == MAX_ATTEMPTS:
                fail(6, last)
        backoff = backoff_or_fail(attempt, backoff, last, 6)
    fail(6, "exhausted stream attempts")


beat(force=True)

# FREE-ONLY GATE: live-checked on every call, never trusted from the roster entry alone.
catalog = request_json(
    urllib.request.Request(f"{API}/models", headers={"User-Agent": "crossfeed-orchestrator-openrouter-agent/1"}),
    6,
)
entry = next((m for m in catalog.get("data", []) if m.get("id") == model), None)
if entry is None:
    fail(5, f"model '{model}' not found in the live OpenRouter catalog; refusing an unpriced call")

output_modalities = (entry.get("architecture") or {}).get("output_modalities") or []
if output_modalities != ["text"]:
    fail(
        5,
        f"model '{model}' outputs {output_modalities}, not pure text; some non-text generation is "
        "priced outside prompt/completion, so this wrapper only admits text-out models",
    )

pricing = entry.get("pricing") or {}
non_zero = {k: v for k, v in pricing.items() if str(v) != "0"}
if non_zero:
    fail(5, f"model '{model}' is not free right now (non-zero pricing fields: {non_zero}); refusing")

body = {
    "model": model,
    "messages": [{"role": "user", "content": prompt}],
    "stream": True,
    "usage": {"include": True},
}
text, usage = stream_completion(
    urllib.request.Request(
        f"{API}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "crossfeed-orchestrator-openrouter-agent/1",
        },
        method="POST",
    )
)

if usage is None:
    # The pre-flight catalog gate already refused non-zero pricing, so this is not a billing
    # risk, but it does mean the post-hoc confirmation did not run. Say so rather than implying
    # a check that never happened.
    print(
        "openrouter-agent: NOTE - the stream carried no usage block, so the post-hoc zero-cost "
        "confirmation did not run; the pre-flight free-only gate did pass.",
        file=sys.stderr,
        flush=True,
    )
else:
    cost = usage.get("cost")
    if cost not in (None, 0, 0.0, "0"):
        fail(6, f"OpenRouter reported a non-zero cost ({cost!r}) for a call the free-gate approved; stop and investigate")

text = text.strip()
if not text:
    fail(7, "empty final model output")
with open(out_path, "w") as sink:
    sink.write(text + "\n")
PY
CHILD_PID=$!
if [ -n "$PENDING_SIGNAL_CODE" ]; then
  _handle_signal "$PENDING_SIGNAL_NAME" "$PENDING_SIGNAL_CODE"
fi

rc=0
child_completed=0
if ! CHILD_PGID="$(_read_child_pgid "$CHILD_PID")"; then
  # set -m established PID=PGID at launch even if the child exited before ps observed it.
  CHILD_PGID="$CHILD_PID"
  if ! _child_alive; then
    set +e
    wait "$CHILD_PID"
    rc=$?
    set -e
    child_completed=1
  fi
fi
set +m

if [ "$child_completed" -eq 0 ]; then
  _watchdog &
  WATCHDOG_PID=$!

  set +e
  wait "$CHILD_PID"
  rc=$?
  set -e

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
  [ -s "$RUNOUT" ] && cat "$RUNOUT" >&2
  exit "$rc"
fi

# Exit 0 is not proof of a deliverable: a stream that ends having produced only reasoning tokens
# leaves an empty file, and publishing that reads downstream as "the worker had nothing to
# report" rather than "the worker produced nothing".
if [ ! -s "$RUN_LAST" ] || ! grep -q '[^[:space:]]' "$RUN_LAST" 2>/dev/null; then
  echo "openrouter-agent: the call succeeded but produced NO text (blank deliverable). Not publishing it." >&2
  [ -s "$RUNOUT" ] && cat "$RUNOUT" >&2
  exit 7
fi

[ -s "$RUNOUT" ] && cat "$RUNOUT" >&2

# Publish atomically, so a reader never observes a half-written deliverable and a failed run
# never leaves the previous run's answer sitting in the caller's --last.
if [ -n "$LAST" ]; then
  cp "$RUN_LAST" "$LAST.part.$$" && mv -f "$LAST.part.$$" "$LAST"
fi

cat "$RUN_LAST"
exit 0
