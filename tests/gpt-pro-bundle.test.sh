#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/gpt-pro-bundle.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/gpt-pro-bundle-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

printf '%s\n' 'Options, constraints, and what remains undecided.' >"$TMP/context.md"
PROMPT_SPEC=$'## Role\nIndependent adversarial reviewer.\n\n## Goal\nChallenge the leading decision.\n\n## Given\nThe supplied context.\n\n## Constraints\nDo not edit anything.\n\n## Success criteria\nOne evidence-backed challenge.\n\n## Verification\nLabel uncertain claims.\n\n## Output\nA tight critic digest.\n\n## Stop rule\nStop after the digest.'

"$SCRIPT" --context "$TMP/context.md" --prompt-spec "$PROMPT_SPEC" --out "$TMP/out" >/dev/null

[ -s "$TMP/out/CONTEXT.md" ]
[ -s "$TMP/out/PROMPT.md" ]
[ -s "$TMP/out/gpt-pro-bundle.zip" ]
zip -T "$TMP/out/gpt-pro-bundle.zip" >/dev/null
grep -qx '## Stop rule' "$TMP/out/PROMPT.md"
printf '%s\n' 'gpt-pro-bundle smoke test: PASS'
