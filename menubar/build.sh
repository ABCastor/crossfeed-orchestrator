#!/usr/bin/env bash
# Build Crossfeed: an ad-hoc signed Crossfeed.app (Swift, SwiftPM, no Xcode project) on macOS, or the
# Go AppIndicator tray (a Crossfeed executable) on Linux. Everything it writes goes under the one
# absolute output directory, including SwiftPM's scratch and cache folders.
set -euo pipefail
if [[ $# != 1 || $1 != /* ]]; then
  echo 'Usage: bash menubar/build.sh /absolute/output-directory' >&2; exit 2
fi
out=$1
platform=$(uname -s)
here=$(cd -- "$(dirname -- "$0")" && pwd)
mkdir -p -- "$out"
case "$platform" in
  Linux)
    cd -- "$here"
    GOTOOLCHAIN=local go build -trimpath -mod=readonly -o "$out/Crossfeed" .
    printf '%s\n' "$out/Crossfeed"
    ;;
  Darwin)
    app="$out/Crossfeed.app"
    export CLANG_MODULE_CACHE_PATH="$out/cache/clang"
    scratch=(--scratch-path "$out/swiftpm" --cache-path "$out/cache/swiftpm" --disable-sandbox)
    cd -- "$here/mac"
    swift build -c release --product Crossfeed "${scratch[@]}" >&2
    bin=$(swift build -c release --product Crossfeed "${scratch[@]}" --show-bin-path)/Crossfeed
    [[ -x $bin ]] || { echo "SwiftPM did not produce $bin" >&2; exit 1; }
    staged="$out/Crossfeed.staging.app"
    rm -rf -- "$staged"
    mkdir -p -- "$staged/Contents/MacOS" "$staged/Contents/Resources/Fonts"
    cp -- "$bin" "$staged/Contents/MacOS/Crossfeed"
    cp -- "$here/Info.plist" "$staged/Contents/Info.plist"
    cp -- Resources/Fonts/*.ttf Resources/Fonts/OFL-*.txt "$staged/Contents/Resources/Fonts/"
    cp -- THIRD_PARTY_NOTICES.md "$staged/Contents/Resources/"
    /usr/bin/plutil -lint "$staged/Contents/Info.plist" >&2
    /usr/bin/codesign --force --sign - --identifier org.crossfeed.tray "$staged" >&2
    /usr/bin/codesign --verify --strict "$staged" >&2
    rm -rf -- "$app"
    mv -- "$staged" "$app"
    /usr/bin/codesign --verify --strict "$app" >&2
    printf '%s\n' "$app/Contents/MacOS/Crossfeed"
    ;;
  *) echo 'Crossfeed requires macOS or Linux.' >&2; exit 2 ;;
esac
