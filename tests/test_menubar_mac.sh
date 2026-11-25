#!/usr/bin/env bash
# The macOS menu bar app: Swift unit tests (glance schema 1 decoding, unread pools, missing spend, the
# presenter), then the real build script, the ad-hoc signature, and the app's own selftest, which builds
# the menu from a fixture without creating a status item or showing anything. macOS only.
set -uo pipefail
if [ "$(uname -s)" != Darwin ]; then echo "RESULT: skipped, the Swift menu bar app is macOS only"; exit 0; fi
if ! command -v swift >/dev/null 2>&1; then echo "RESULT: skipped, no Swift toolchain"; exit 0; fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/crossfeed-mac-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
export CLANG_MODULE_CACHE_PATH="$WORK/cache/clang"
fail() { echo "FAIL: $*" >&2; exit 1; }

(cd "$ROOT/menubar/mac" && swift test --scratch-path "$WORK/test-swiftpm" --cache-path "$WORK/cache/swiftpm" --disable-sandbox) \
  || fail "swift test"

bash "$ROOT/menubar/build.sh" "$WORK/out" >"$WORK/build.log" 2>&1 || { tail -20 "$WORK/build.log"; fail "build.sh"; }
APP="$WORK/out/Crossfeed.app"
/usr/bin/codesign --verify --strict "$APP" || fail "codesign --verify --strict"
details="$(/usr/bin/codesign -dv "$APP" 2>&1)"
case "$details" in *Signature=adhoc*) ;; *) fail "the app is not signed ad hoc" ;; esac
[ -f "$APP/Contents/Resources/Fonts/Commissioner-Medium.ttf" ] || fail "fonts missing from the bundle"
[ -f "$APP/Contents/Resources/THIRD_PARTY_NOTICES.md" ] || fail "CodexBar's MIT notice missing from the bundle"

for fixture in glance-today.json glance-today-priced.json; do
  out="$("$APP/Contents/MacOS/Crossfeed" -selftest -fixture "$ROOT/menubar/mac/Fixtures/$fixture" 2>/dev/null)" \
    || fail "selftest on $fixture: $out"
  case "$out" in "selftest ok:"*) ;; *) fail "selftest on $fixture: $out" ;; esac
done
# The app refuses to start without an absolute fleetctl path, as the LaunchAgent always gives one.
if "$APP/Contents/MacOS/Crossfeed" >/dev/null 2>&1; then fail "started without -fleetctl"; fi
echo "RESULT: swift tests, build, ad-hoc signature, selftest on two fixtures: ok"
