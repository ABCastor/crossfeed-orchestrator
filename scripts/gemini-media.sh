#!/usr/bin/env bash
# gemini-media.sh — budget-capped native Gemini audio/video transport.
#
# The local ledger is intentionally conservative. It reserves the complete
# model output allowance before generateContent and retains that reservation on
# an ambiguous failure, rather than risking an uncapped retry.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -euo pipefail

DEFAULT_MODEL="gemini-flash-lite-latest"   # alias: auto-tracks Google's newest flash-lite (policy: always track newest)
DEFAULT_TIMEOUT=600
DEFAULT_CAP_USD="1.00"
KEY_FILE="${TRANSCRIBE_KEY_FILE:-$HOME/.config/transcribe/gemini_api_key}"
LEDGER="${GEMINI_MEDIA_LEDGER:-$HOME/.local/state/orchestrator/gemini-media-spend.json}"

err() { printf 'gemini-media: %s\n' "$*" >&2; }

usage() {
  cat >&2 <<'USAGE'
usage: gemini-media.sh --file <path> --modality audio|video --prompt <text> [--model <id>] [--timeout <seconds>]

Only gemini-flash-lite-latest is admitted. An arbitrary model would make the
daily cost cap unknowable, so this transport refuses it rather than creating
an uncapped paid path. The alias tracks Google's newest flash-lite, so the
per-token price can move under you: the daily cap is the real guard, not the
price assumed when this was written.
USAGE
}

FILE=""
MODALITY=""
PROMPT=""
MODEL="$DEFAULT_MODEL"
TIMEOUT="$DEFAULT_TIMEOUT"

while [ $# -gt 0 ]; do
  case "$1" in
    --file)
      [ $# -ge 2 ] || { err "--file needs a path"; exit 2; }
      FILE="$2"; shift 2 ;;
    --modality)
      [ $# -ge 2 ] || { err "--modality needs audio or video"; exit 2; }
      MODALITY="$2"; shift 2 ;;
    --prompt)
      [ $# -ge 2 ] || { err "--prompt needs text"; exit 2; }
      PROMPT="$2"; shift 2 ;;
    --model)
      [ $# -ge 2 ] || { err "--model needs an id"; exit 2; }
      MODEL="$2"; shift 2 ;;
    --timeout)
      [ $# -ge 2 ] || { err "--timeout needs seconds"; exit 2; }
      TIMEOUT="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      err "unknown argument: $1"; usage; exit 2 ;;
  esac
done

[ -n "$FILE" ] || { err "--file is required"; usage; exit 2; }
[ -f "$FILE" ] || { err "file not found: $FILE"; exit 2; }
[ -n "$MODALITY" ] || { err "--modality is required"; usage; exit 2; }
[ -n "$PROMPT" ] || { err "--prompt is required"; usage; exit 2; }
case "$MODALITY" in audio|video) ;; *) err "--modality must be audio or video"; exit 2 ;; esac
case "$TIMEOUT" in ''|*[!0-9]*|0) err "--timeout must be a positive integer"; exit 2 ;; esac
[ "$MODEL" = "$DEFAULT_MODEL" ] || {
  err "refusing unpriced model '$MODEL': only $DEFAULT_MODEL is budget-admitted"
  exit 2
}
# The Crossfeed console's switches reach this transport too: a model that is switched off,
# retired, or on a provider set to Off is refused (exit 5) before any key is read or call made.
# A model the roster does not list is not refused, and a gate that cannot run never blocks.
_gate_rc=0; _gate_msg="$("$(dirname "$0")/fleetctl.py" model-gate "$MODEL" --provider google --reason-only 2>&1 >/dev/null)" || _gate_rc=$?
if [ "$_gate_rc" = 5 ]; then
  err "${_gate_msg#fleetctl: }"
  exit 5
fi

# This is deliberately identical to skills/transcribe's key resolution.
[ -r "$KEY_FILE" ] || { err "API key not found at $KEY_FILE"; exit 3; }
KEY="$(cat "$KEY_FILE")"
[ -n "$KEY" ] || { err "API key at $KEY_FILE is empty"; exit 3; }
command -v python3 >/dev/null || { err "python3 not found"; exit 3; }

CAP_USD="${GEMINI_MEDIA_DAILY_CAP_USD:-$DEFAULT_CAP_USD}"
GEMINI_MEDIA_KEY="$KEY" GEMINI_MEDIA_CAP_USD="$CAP_USD" GEMINI_MEDIA_LEDGER="$LEDGER" \
  python3 - "$FILE" "$MODALITY" "$PROMPT" "$MODEL" "$TIMEOUT" <<'PY'
import datetime as dt
import fcntl
import json
import mimetypes
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


media_path, modality, prompt, model, timeout_s = sys.argv[1:6]
timeout_s = int(timeout_s)
key = os.environ["GEMINI_MEDIA_KEY"]
ledger_path = Path(os.environ["GEMINI_MEDIA_LEDGER"]).expanduser()

try:
    cap_usd = float(os.environ["GEMINI_MEDIA_CAP_USD"])
except ValueError as exc:
    raise SystemExit(f"gemini-media: invalid GEMINI_MEDIA_DAILY_CAP_USD: {exc}")
if cap_usd <= 0:
    raise SystemExit("gemini-media: GEMINI_MEDIA_DAILY_CAP_USD must be positive")

# Google public paid-tier list prices as of 2026-07-19.  The model override is
# intentionally rejected by the shell layer, so this table cannot silently be
# used to underprice a different model.  Count all audio request input at the
# higher audio rate, including the small text prompt, to stay conservative.
INPUT_USD_PER_M = {"audio": 0.50, "video": 0.25}
OUTPUT_USD_PER_M = 1.50
MODEL_OUTPUT_TOKEN_LIMIT = 65_536
GENERATION_MAX_OUTPUT_TOKENS = 8_192
API = "https://generativelanguage.googleapis.com/v1beta"
UPLOAD_API = "https://generativelanguage.googleapis.com/upload/v1beta"
deadline = time.monotonic() + timeout_s


def fail(message: str) -> None:
    raise RuntimeError(message)


def remaining() -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        fail(f"timed out after {timeout_s}s")
    return max(1.0, value)


def request_json(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=remaining()) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        fail(f"API error HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"network request failed: {exc}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        fail("API returned non-JSON data")


def best_effort_delete(name: str | None) -> None:
    if not name:
        return
    try:
        request = urllib.request.Request(
            f"{API}/{name}", headers={"x-goog-api-key": key}, method="DELETE"
        )
        with urllib.request.urlopen(request, timeout=min(60, remaining())):
            pass
    except Exception:
        pass


def today() -> str:
    return dt.datetime.now().astimezone().date().isoformat()


def load_ledger() -> dict:
    if not ledger_path.exists():
        return {"schema": "gemini-media-spend/v1", "days": {}}
    try:
        value = json.loads(ledger_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail(f"ledger is invalid: {ledger_path}")
    if value.get("schema") != "gemini-media-spend/v1" or not isinstance(value.get("days"), dict):
        fail(f"ledger is invalid: {ledger_path}")
    return value


def write_ledger(value: dict) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{ledger_path.name}.", dir=ledger_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, ledger_path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def locked_update(mutator):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            value = load_ledger()
            result = mutator(value)
            write_ledger(value)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def daily_spend(entries: list[dict]) -> float:
    # A pending or failed call retains its full reservation.  A lost response
    # must not turn into a free retry that defeats the cap.
    total = 0.0
    for entry in entries:
        if not isinstance(entry, dict):
            fail(f"ledger is invalid: {ledger_path}")
        if entry.get("status") == "completed":
            total += float(entry.get("actual_usd", 0.0))
        else:
            total += float(entry.get("reserved_usd", 0.0))
    return total


def assert_headroom() -> None:
    def check(value):
        spent = daily_spend(value["days"].get(today(), []))
        if spent >= cap_usd:
            fail(
                f"daily spend cap ${cap_usd:.2f} reached "
                f"(ledger has ${spent:.6f} since local midnight); refusing before upload"
            )
    locked_update(check)


def reserve(upper_bound_usd: float, input_tokens: int) -> str:
    if upper_bound_usd > cap_usd:
        fail(
            f"one request needs a conservative ${upper_bound_usd:.6f} reservation, "
            f"above the ${cap_usd:.2f} daily cap; refusing before generateContent"
        )
    entry_id = str(uuid.uuid4())

    def update(value):
        entries = value["days"].setdefault(today(), [])
        spent = daily_spend(entries)
        if spent + upper_bound_usd > cap_usd + 1e-12:
            fail(
                f"daily spend cap ${cap_usd:.2f} would be exceeded "
                f"(${spent:.6f} recorded + ${upper_bound_usd:.6f} reserved); "
                "refusing before generateContent"
            )
        entries.append(
            {
                "id": entry_id,
                "status": "reserved",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "model": model,
                "modality": modality,
                "input_tokens": input_tokens,
                "reserved_usd": upper_bound_usd,
            }
        )
    locked_update(update)
    return entry_id


def complete(entry_id: str, actual_usd: float, usage: dict) -> None:
    def update(value):
        entries = value["days"].get(today(), [])
        for entry in entries:
            if entry.get("id") == entry_id:
                entry["status"] = "completed"
                entry["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
                entry["actual_usd"] = actual_usd
                entry["usage"] = usage
                return
        fail("reserved ledger entry disappeared; refusing to print an unaccounted response")
    locked_update(update)


def upload_file(mime_type: str) -> tuple[str, str]:
    size = os.path.getsize(media_path)
    start = urllib.request.Request(
        f"{UPLOAD_API}/files",
        data=json.dumps({"file": {"display_name": os.path.basename(media_path)}}).encode(),
        headers={
            "x-goog-api-key": key,
            "Content-Type": "application/json",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(start, timeout=remaining()) as response:
            upload_url = response.headers.get("X-Goog-Upload-URL")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        fail(f"Files API upload start failed, HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        fail(f"Files API upload start failed: {exc}")
    if not upload_url:
        fail("Files API did not return an upload URL")
    with open(media_path, "rb") as handle:
        data = handle.read()
    finalize = urllib.request.Request(
        upload_url,
        data=data,
        headers={
            "Content-Length": str(size),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        },
        method="POST",
    )
    info = request_json(finalize)
    file_info = info.get("file", {})
    name = file_info.get("name")
    uri = file_info.get("uri")
    state = file_info.get("state")
    if not name or not uri:
        fail("Files API upload returned no file name or URI")
    while state != "ACTIVE":
        if state == "FAILED":
            fail("Files API processing failed")
        time.sleep(min(2, remaining()))
        file_info = request_json(
            urllib.request.Request(f"{API}/{name}", headers={"x-goog-api-key": key})
        )
        state = file_info.get("state")
    return name, uri


def token_count(media: dict) -> int:
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}, media]}]}
    result = request_json(
        urllib.request.Request(
            f"{API}/models/{model}:countTokens",
            data=json.dumps(body).encode(),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            method="POST",
        )
    )
    count = result.get("totalTokens")
    if not isinstance(count, int) or count <= 0:
        fail("countTokens returned no positive totalTokens; refusing an unpriced generation")
    return count


def answer(media: dict) -> dict:
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}, media]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": GENERATION_MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    return request_json(
        urllib.request.Request(
            f"{API}/models/{model}:generateContent",
            data=json.dumps(body).encode(),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            method="POST",
        )
    )


mime_type, _ = mimetypes.guess_type(media_path)
if not mime_type or not mime_type.startswith(modality + "/"):
    raise SystemExit(
        f"gemini-media: cannot confirm a {modality} MIME type for {media_path}; "
        "use a standard audio/video filename extension"
    )

uploaded_name = None
reservation_id = None
try:
    assert_headroom()
    uploaded_name, uri = upload_file(mime_type)
    media_part = {"file_data": {"mime_type": mime_type, "file_uri": uri}}
    input_tokens = token_count(media_part)
    reservation = (
        input_tokens * INPUT_USD_PER_M[modality] / 1_000_000
        + MODEL_OUTPUT_TOKEN_LIMIT * OUTPUT_USD_PER_M / 1_000_000
    )
    reservation_id = reserve(reservation, input_tokens)
    response = answer(media_part)
    usage = response.get("usageMetadata")
    if not isinstance(usage, dict):
        fail("generateContent returned no usageMetadata; full reservation remains charged")
    prompt_tokens = usage.get("promptTokenCount")
    candidate_tokens = usage.get("candidatesTokenCount", 0)
    thought_tokens = usage.get("thoughtsTokenCount", 0)
    if not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        fail("generateContent returned invalid prompt token usage; full reservation remains charged")
    if not all(isinstance(value, int) and value >= 0 for value in (candidate_tokens, thought_tokens)):
        fail("generateContent returned invalid output token usage; full reservation remains charged")
    actual = (
        prompt_tokens * INPUT_USD_PER_M[modality] / 1_000_000
        + (candidate_tokens + thought_tokens) * OUTPUT_USD_PER_M / 1_000_000
    )
    if actual > reservation + 1e-12:
        fail("actual usage exceeded the conservative reservation; full reservation remains charged")
    complete(
        reservation_id,
        actual,
        {
            "prompt_tokens": prompt_tokens,
            "candidate_tokens": candidate_tokens,
            "thought_tokens": thought_tokens,
        },
    )
    try:
        parts = response["candidates"][0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
    except (KeyError, IndexError, TypeError):
        fail("unexpected generateContent response; spend was recorded")
    if not text:
        fail("generateContent returned no text; spend was recorded")
    print(text)
except Exception as exc:
    print(f"gemini-media: {exc}", file=sys.stderr)
    raise SystemExit(5)
finally:
    best_effort_delete(uploaded_name)
PY

}
main "$@"; exit $?
