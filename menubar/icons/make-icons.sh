#!/usr/bin/env bash
# Rasterise the provider logos the web panel draws (scripts/console-assets/panel/*.svg) into the 44 px PNGs
# the Linux tray embeds. Run it after a logo changes; the PNGs are committed so a build needs no browser.
# One-colour logos are drawn in a mid grey that reads on both light and dark panels.
set -euo pipefail
here=$(cd -- "$(dirname -- "$0")" && pwd)
src=$here/../../scripts/console-assets/panel
chrome=${CHROME:-$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)}
[[ -n $chrome ]] || { echo 'make-icons.sh needs Chrome or Chromium (set CHROME)' >&2; exit 2; }
work=$(mktemp -d); trap 'rm -rf -- "$work"' EXIT
for pair in claude:claude-color openai:openai githubcopilot:githubcopilot antigravity:antigravity-color gemini:gemini-color opencode:opencode openrouter:openrouter; do
  name=${pair%%:*}; file=${pair##*:}
  svg=$(sed -e 's/height="1em"/height="40"/; s/width="1em"/width="40"/' "$src/$file.svg")
  printf '<!doctype html><meta charset="utf-8"><body style="margin:0;background:transparent;color:#8a8a8a"><div style="width:44px;height:44px;display:flex;align-items:center;justify-content:center">%s</div>' "$svg" > "$work/$name.html"
  "$chrome" --headless=new --no-sandbox --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
    --default-background-color=00000000 --window-size=44,44 --screenshot="$here/$name.png" "file://$work/$name.html" >/dev/null 2>&1
done
ls -l "$here"/*.png
