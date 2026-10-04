#!/usr/bin/env bash
# Deterministic tests for scripts/openrouter-agent.sh. No call ever leaves this machine:
# OPENROUTER_API_BASE points at a local server that can stall, dribble keepalives, or lie about
# pricing on demand, so the idle watchdog and the free-only gate are PROVEN rather than asserted.
#
# The point of the stall tests: check-dispatch-invariants.sh can only prove the watchdog is
# WRITTEN. Only these prove it FIRES, and with which exit code.
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -d "$SCRIPT_DIR/scripts" ] || SCRIPT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_WRAPPER="${WRAPPER_UNDER_TEST:-$SCRIPT_DIR/scripts/openrouter-agent.sh}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/openrouter-agent-test.XXXXXX")"
FIXTURE_DIR="$TEST_ROOT/fixture/scripts"
mkdir -p "$FIXTURE_DIR"
cp "$SOURCE_WRAPPER" "$FIXTURE_DIR/openrouter-agent.sh"
cp "$SCRIPT_DIR/scripts/run-identity.sh" "$SCRIPT_DIR/scripts/run_identity.py" "$FIXTURE_DIR/"
export FLEET_STATE_DIR="$TEST_ROOT/model-state"
chmod +x "$FIXTURE_DIR/openrouter-agent.sh"
WRAPPER="$FIXTURE_DIR/openrouter-agent.sh"

SERVER_PID=""
cleanup() {
  # reap quietly: a green run that prints "Killed: 9" teaches people to skim the output
  [ -n "$SERVER_PID" ] && { kill -KILL "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; }
  case "$TEST_ROOT" in
    "${TMPDIR:-/tmp}"/openrouter-agent-test.*) rm -rf "$TEST_ROOT";;
  esac
}
trap cleanup EXIT

cat >"$FIXTURE_DIR/roster.sh" <<'FAKE_ROSTER'
#!/bin/bash
set -u
case "${1:-}" in
  resolve-lane|lookup) printf 'fake-lane\n';;
  lane-json) printf '{"harness":"openrouter","selector":"fake/free-model","timeout_s":180}\n';;
  check-lane) exit 0;;
  *) exit 2;;
esac
FAKE_ROSTER

cat >"$FIXTURE_DIR/fleetctl.py" <<'FAKE_FLEET'
#!/bin/bash
set -u
case "${1:-}" in
  acquire)
    shift
    ttl=""
    while [ "$#" -gt 0 ]; do
      case "$1" in --ttl) ttl="$2"; shift 2;; *) shift;; esac
    done
    [ -z "${FAKE_TTL_FILE:-}" ] || printf '%s\n' "$ttl" >"$FAKE_TTL_FILE"
    printf 'fake-token\n'
    ;;
  release) exit 0;;
  *) exit 2;;
esac
FAKE_FLEET
chmod +x "$FIXTURE_DIR/roster.sh" "$FIXTURE_DIR/fleetctl.py"

KEY_FILE="$TEST_ROOT/key"
printf 'sk-fake-test-key\n' >"$KEY_FILE"

# ---------------------------------------------------------------------------
# Fake OpenRouter. The mode is the first path segment, so one server serves all
# behaviours and each test just points OPENROUTER_API_BASE at a different mode.
# ---------------------------------------------------------------------------
cat >"$TEST_ROOT/fake-openrouter.py" <<'FAKE_SERVER'
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "fake/free-model"


def catalog(price):
    return {"data": [{
        "id": MODEL,
        "architecture": {"output_modalities": ["text"]},
        "pricing": {"prompt": price, "completion": price},
    }]}


def chunk(**delta):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}) + "\n\n"


def usage_chunk(cost):
    return "data: " + json.dumps({
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": cost},
    }) + "\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def mode(self):
        return self.path.strip("/").split("/")[0]

    def send_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.path.endswith("/models"):
            self.send_response(404); self.end_headers(); return
        self.send_json(catalog("0.000002" if self.mode() == "paid" else "0"))

    def open_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

    def emit(self, text):
        self.wfile.write(text.encode())
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if not self.path.endswith("/chat/completions"):
            self.send_response(404); self.end_headers(); return
        mode = self.mode()
        try:
            if mode == "stall":
                # Headers only, then silence. A connection that is open and delivering nothing
                # is the exact failure a wall clock used to hide behind.
                self.open_stream()
                time.sleep(600)
                return
            self.open_stream()
            if mode == "alive":
                # Eight seconds of keepalives before the first token: healthy slow reasoning.
                # A watchdog that ignored these would false-kill every thinking model.
                for _ in range(8):
                    self.emit(": OPENROUTER PROCESSING\n\n")
                    time.sleep(1)
                self.emit(chunk(content="ALIVE"))
                self.emit(usage_chunk(0))
            elif mode == "reasoning-only":
                self.emit(chunk(reasoning="thinking, but never answering"))
                self.emit(usage_chunk(0))
            elif mode == "costly":
                self.emit(chunk(content="expensive"))
                self.emit(usage_chunk(0.01))
            else:  # "ok"
                self.emit(chunk(content="  PONG  "))
                self.emit(usage_chunk(0))
            self.emit("data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
server.daemon_threads = True
server.serve_forever()
FAKE_SERVER

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
python3 "$TEST_ROOT/fake-openrouter.py" "$PORT" &
SERVER_PID=$!
for _ in $(seq 1 50); do
  python3 -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)" && break
  sleep 0.1
done

pass=0; fail=0
ok()   { printf 'PASS  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf 'FAIL  %s\n' "$1"; fail=$((fail+1)); }

run() {  # run MODE [extra wrapper args...]; sets RC, OUT, ERR
  local mode="$1"; shift
  OUT="$TEST_ROOT/out"; ERR="$TEST_ROOT/err"
  OPENROUTER_API_BASE="http://127.0.0.1:$PORT/$mode" \
  OPENROUTER_KEY_FILE="$KEY_FILE" \
  FAKE_TTL_FILE="$TEST_ROOT/ttl" \
    "$WRAPPER" --lane fake-lane --prompt "hello" "$@" >"$OUT" 2>"$ERR"
  RC=$?
}

expect_rc() {  # expect_rc LABEL WANT
  if [ "$RC" -eq "$2" ]; then ok "$1 (exit $RC)"
  else bad "$1: wanted exit $2, got $RC; stderr: $(head -2 "$ERR" | tr '\n' ' ')"; fi
}

echo "== happy path"
run ok
expect_rc "a free streaming completion succeeds" 0
if [ "$(cat "$OUT")" = "PONG" ] && grep -q '^Crossfeed model receipt:' "$ERR"; then ok "the deliverable is the stripped model text"
else bad "expected 'PONG' on stdout, got: $(cat "$OUT")"; fi
if [ "$(grep -c 'effort service-chosen: the service chooses the level' "$ERR")" -eq 1 ]; then
  ok "openrouter_service_chosen_effort"
else bad "openrouter_service_chosen_effort: service effort notice must appear exactly once"; fi

echo "== the idle watchdog actually fires"
start="$(date +%s)"
run stall --idle-timeout 3
elapsed=$(( $(date +%s) - start ))
expect_rc "a stream that opens and then delivers nothing is killed as IDLE" 125
if grep -q 'IDLE LIMIT FIRED' "$ERR"; then ok "the idle kill names itself on stderr"
else bad "the idle kill was silent; stderr: $(head -3 "$ERR" | tr '\n' ' ')"; fi
if [ "$elapsed" -lt 20 ]; then ok "the idle kill landed in ${elapsed}s, not at some wall clock"
else bad "the idle kill took ${elapsed}s - it is not the watchdog doing the killing"; fi

echo "== the wall clock still works when explicitly asked for"
run stall --timeout 3 --idle-timeout 0
expect_rc "an explicit --timeout kills a stalled stream as WALL-CLOCK" 124

echo "== no false kill on a slow but live stream"
# Eight seconds of keepalives under a 4s idle bound: this is the test that would go red if
# liveness were measured on CPU (a blocking HTTP read burns none) or on content only.
run alive --idle-timeout 4
expect_rc "keepalives and reasoning count as liveness" 0
if [ "$(cat "$OUT")" = "ALIVE" ] && grep -q '^Crossfeed model receipt:' "$ERR"; then ok "the slow stream's answer survives intact"
else bad "expected 'ALIVE', got: $(cat "$OUT")"; fi

echo "== the free-only gate is unchanged by streaming"
run paid
expect_rc "a model with non-zero pricing is refused before the request" 5
run costly
expect_rc "a stream reporting a non-zero cost is refused after the fact" 6

echo "== a stream with no content is not published as success"
run reasoning-only
expect_rc "reasoning tokens with no answer is a blank deliverable" 7

echo "== the lease is sized against the bound that actually applies"
run ok --idle-timeout 100
if [ "$(cat "$TEST_ROOT/ttl")" = "160" ]; then ok "uncapped run leases idle+60"
else bad "expected lease ttl 160, got $(cat "$TEST_ROOT/ttl")"; fi
run ok --timeout 50
if [ "$(cat "$TEST_ROOT/ttl")" = "110" ]; then ok "capped run leases timeout+60"
else bad "expected lease ttl 110, got $(cat "$TEST_ROOT/ttl")"; fi

echo
printf '%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
