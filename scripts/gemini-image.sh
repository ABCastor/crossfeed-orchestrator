#!/usr/bin/env bash
# gemini-image.sh — budget-capped Gemini image generation and EDITING.
#
# The second image route, beside Codex CLI's built-in image_gen. Both exist because they
# are good at different things, and the difference is not quality:
#
#   codex exec ... (image_gen)   no API key, runs on the existing Codex auth, no ledger
#                                to keep. Best for a FRESH reference: "show me how a
#                                cartoon beaver's teeth are usually constructed".
#   gemini-image.sh              takes REFERENCE IMAGES as input and edits across turns,
#                                keeping the subject consistent. Best for "here is my
#                                drawing, change ONE thing about it" — which is the loop
#                                that actually moves a stuck mark forward, and the thing
#                                Codex cannot do because it starts from words every time.
#
# Same guard as gemini-media.sh, deliberately: a hard daily USD cap with a local ledger,
# reserved before the call and retained on an ambiguous failure, so an uncapped retry
# cannot happen. Image pricing is per-image rather than per-token, so the reservation is
# exact rather than estimated.
#
#   gemini-image.sh --prompt "..." --out ref.png [--ref mine.png ...] [--model ID] [--n 1]
#
# --ref may be repeated; with any --ref the call is an EDIT anchored on those images.
set -euo pipefail

DEFAULT_MODEL="gemini-3-pro-image"        # Nano Banana Pro: reference support + 4K + editing
CHEAP_MODEL="gemini-3.1-flash-image"      # a fifth of the price, for exploration
DEFAULT_CAP_USD="${GEMINI_IMAGE_CAP_USD:-1.00}"
KEY_FILE="${TRANSCRIBE_KEY_FILE:-$HOME/.config/transcribe/gemini_api_key}"
LEDGER="${GEMINI_IMAGE_LEDGER:-$HOME/.local/state/orchestrator/gemini-image-spend.json}"

err() { printf 'gemini-image: %s\n' "$*" >&2; }
usage() {
  cat >&2 <<'USAGE'
usage: gemini-image.sh --prompt <text> --out <file.png> [--ref <image> ...]
                       [--model gemini-3-pro-image|gemini-3.1-flash-image] [--cheap] [--n N]

  --ref   anchor generation on an existing image; repeatable. With one or more --ref this
          is an EDIT, which is the mode worth having: it keeps the subject consistent
          across turns, so "the same beaver but the teeth like this" stays the same beaver.
  --cheap use the flash image model (roughly a fifth of the cost) for exploration.
USAGE
  exit 2
}

PROMPT=""; OUT=""; MODEL="$DEFAULT_MODEL"; N=1; REFS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --prompt) PROMPT="${2:?}"; shift 2;;
    --prompt-file) PROMPT="$(cat "${2:?}")"; shift 2;;
    --out) OUT="${2:?}"; shift 2;;
    --ref) REFS+=("${2:?}"); shift 2;;
    --model) MODEL="${2:?}"; shift 2;;
    --cheap) MODEL="$CHEAP_MODEL"; shift;;
    --n) N="${2:?}"; shift 2;;
    -h|--help) usage;;
    *) err "unknown argument: $1"; usage;;
  esac
done
[ -n "$PROMPT" ] && [ -n "$OUT" ] || usage
# The Crossfeed console's switches reach this transport too: a model that is switched off,
# retired, or on a provider set to Off is refused (exit 5) before any key is read or call made.
# A model the roster does not list is not refused, and a gate that cannot run never blocks.
_gate_rc=0; _gate_msg="$("$(dirname "$0")/fleetctl.py" model-gate "$MODEL" --provider google --reason-only 2>&1 >/dev/null)" || _gate_rc=$?
if [ "$_gate_rc" = 5 ]; then
  err "${_gate_msg#fleetctl: }"
  exit 5
fi
[ -s "$KEY_FILE" ] || { err "no API key at $KEY_FILE"; exit 3; }
command -v python3 >/dev/null || { err "python3 not found"; exit 3; }

KEY="$(cat "$KEY_FILE")" MODEL="$MODEL" PROMPT="$PROMPT" OUT="$OUT" N="$N" \
LEDGER="$LEDGER" CAP="$DEFAULT_CAP_USD" python3 - ${REFS[@]+"${REFS[@]}"} <<'PY'
import base64, json, os, sys, time, urllib.request, urllib.error, pathlib, mimetypes

KEY, MODEL, PROMPT = os.environ['KEY'], os.environ['MODEL'], os.environ['PROMPT']
OUT, N = os.environ['OUT'], int(os.environ['N'])
LEDGER, CAP = pathlib.Path(os.environ['LEDGER']), float(os.environ['CAP'])
REFS = sys.argv[1:]

# per-image list price, August 2026. Reserved BEFORE the call and kept on an ambiguous
# failure: the daily cap is the real guard, and a retry that is not charged against it is
# how an uncapped path gets created by accident.
# Prices per ai.google.dev/gemini-api/docs/pricing, read 2026-08-26: flash-image is $0.067 at
# 1K and 2.5-flash-image is $0.039. Reserving less than the list price would make the daily
# cap looser than it claims to be (a $0.028 figure would under-reserve by ~2.4x).
PRICE = {'gemini-3-pro-image': 0.134, 'gemini-3.1-flash-image': 0.067,
         'gemini-2.5-flash-image': 0.039}.get(MODEL, 0.134)


def fail(msg, code=1):
    print(f'gemini-image: {msg}', file=sys.stderr); sys.exit(code)


def ledger_load():
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {'entries': []}


def spend_today(d):
    today = time.strftime('%Y-%m-%d')
    return sum(e['usd'] for e in d.get('entries', []) if e.get('day') == today)


LEDGER.parent.mkdir(parents=True, exist_ok=True)
led = ledger_load()
want = PRICE * N
if spend_today(led) + want > CAP:
    fail(f'daily cap ${CAP:.2f} would be exceeded: ${spend_today(led):.3f} spent, '
         f'this call is ${want:.3f}. Raise GEMINI_IMAGE_CAP_USD deliberately or wait.', 4)
led.setdefault('entries', []).append(
    {'day': time.strftime('%Y-%m-%d'), 'usd': want, 'model': MODEL, 'n': N,
     'out': OUT, 'refs': len(REFS), 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
LEDGER.write_text(json.dumps(led, indent=1))

parts = [{'text': PROMPT}]
for r in REFS:
    p = pathlib.Path(r)
    if not p.is_file():
        fail(f'reference not found: {r}', 3)
    mt = mimetypes.guess_type(str(p))[0] or 'image/png'
    parts.append({'inline_data': {'mime_type': mt,
                                  'data': base64.b64encode(p.read_bytes()).decode()}})

body = json.dumps({'contents': [{'parts': parts}],
                   'generationConfig': {'responseModalities': ['IMAGE']}}).encode()
url = (f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent'
       f'?key={KEY}')
req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
except urllib.error.HTTPError as e:
    fail(f'HTTP {e.code}: {e.read()[:300].decode(errors="replace")}', 5)

written = []
for cand in data.get('candidates', []):
    for part in cand.get('content', {}).get('parts', []):
        blob = part.get('inlineData') or part.get('inline_data')
        if not blob:
            continue
        path = OUT if not written else OUT.replace('.png', f'-{len(written)+1}.png')
        pathlib.Path(path).write_bytes(base64.b64decode(blob['data']))
        written.append(path)

if not written:
    fail('the response carried no image; the reservation stands rather than being refunded, '
         'because an ambiguous failure that refunds itself is an uncapped retry', 6)
print(json.dumps({'written': written, 'model': MODEL, 'refs': len(REFS),
                  'usd_reserved': round(want, 3),
                  'usd_today': round(spend_today(ledger_load()), 3), 'cap': CAP}, indent=1))
PY
