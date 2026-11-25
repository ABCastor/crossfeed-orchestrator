#!/usr/bin/env bash
# Build an uninstalled Crossfeed.app on macOS or a Crossfeed executable on Linux.
set -euo pipefail
if [[ $# != 1 || $1 != /* ]]; then
  echo 'Usage: bash menubar/build.sh /absolute/output-directory' >&2; exit 2
fi
platform=$(uname -s)
case "$platform" in
  Darwin) target="$1/Crossfeed.app/Contents/MacOS/Crossfeed" ;;
  Linux) target="$1/Crossfeed" ;;
  *) echo 'Crossfeed requires macOS or Linux.' >&2; exit 2 ;;
esac
cd -- "$(dirname -- "$0")"
mkdir -p -- "$(dirname -- "$target")"
GOTOOLCHAIN=local go build -trimpath -mod=readonly -o "$target" .
if [[ $platform == Darwin ]]; then
  cp Info.plist "$1/Crossfeed.app/Contents/Info.plist"
  /usr/bin/codesign --force --sign - "$1/Crossfeed.app"
fi
printf '%s\n' "$target"
