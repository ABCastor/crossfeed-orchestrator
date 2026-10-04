#!/usr/bin/env bash
# Prepare the human-carried GPT Pro review bundle.
# a named, dated bundle with a PROMPT.md plus only the evidence the reviewer needs.
set -euo pipefail

usage() {
  printf '%s\n' "usage: gpt-pro-bundle.sh --prompt-spec <file-or-text> [--context <file-or-text>] [--include <file-or-dir> ...] [--readme <file-or-text>] [--bundle-name <topic-YYYY-MM-DD>] --out <dir>"
  printf '%s\n' "       gpt-pro-bundle.sh --interactive [--bundle-name <topic-YYYY-MM-DD>] --out <dir>"
}

CONTEXT=""
PROMPT_SPEC=""
README=""
BUNDLE_NAME=""
OUT=""
INTERACTIVE=0
INCLUDES=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --context) CONTEXT="${2:-}"; shift 2 ;;
    --prompt-spec) PROMPT_SPEC="${2:-}"; shift 2 ;;
    --include) INCLUDES+=("${2:-}"); shift 2 ;;
    --readme) README="${2:-}"; shift 2 ;;
    --bundle-name) BUNDLE_NAME="${2:-}"; shift 2 ;;
    --out) OUT="${2:-}"; shift 2 ;;
    --interactive) INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'gpt-pro-bundle: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$OUT" ] || { usage >&2; exit 2; }
command -v zip >/dev/null || { printf '%s\n' 'gpt-pro-bundle: zip is required' >&2; exit 3; }

write_source() {
  local source="$1" destination="$2"
  if [ -f "$source" ]; then
    cp "$source" "$destination"
  else
    printf '%s\n' "$source" >"$destination"
  fi
}

write_interactive_prompt() {
  local destination="$1" header value
  : >"$destination"
  for header in Role Goal Given Constraints "Success criteria" Verification Output "Stop rule"; do
    printf '%s: ' "$header" >&2
    IFS= read -r value
    printf '## %s\n%s\n\n' "$header" "$value" >>"$destination"
  done
}

write_readme() {
  local destination="$1"
  if [ -n "$README" ]; then
    write_source "$README" "$destination"
    return
  fi

  # the README is the upload map, not more review material.
  printf '%s\n\n' '# GPT Pro consult bundle' >"$destination"
  printf '%s\n' 'Read in this order:' >>"$destination"
  printf '%s\n' '1. `PROMPT.md` — the task, boundaries, verification, and stop rule.' >>"$destination"
  if [ -n "$CONTEXT" ]; then
    printf '%s\n' '2. `CONTEXT.md` — the self-contained background snapshot.' >>"$destination"
  fi
  if [ "${#INCLUDES[@]}" -gt 0 ]; then
    printf '%s\n' '3. `docs/` — the selected primary evidence, in the order named by the prompt.' >>"$destination"
  fi
  printf '%s\n' >>"$destination"
  printf '%s\n' 'Upload this bundle in ChatGPT Pro. Save the returned answer beside this bundle as `RESPONSE.md`; it is not part of the upload.' >>"$destination"
}

section_count() {
  local header="$1" pattern
  case "$header" in
    Given) pattern='Given([[:space:]]+\(.*\))?' ;;
    *) pattern="$header" ;;
  esac
  grep -Ec "^(#{1,2}[[:space:]]+)?${pattern}[[:space:]]*$" "$PROMPT_FILE" || true
}

if [ "$INTERACTIVE" -eq 1 ]; then
  [ -z "$CONTEXT" ] && [ -z "$PROMPT_SPEC" ] && [ "${#INCLUDES[@]}" -eq 0 ] || {
    printf '%s\n' 'gpt-pro-bundle: --interactive cannot be combined with --context, --prompt-spec, or --include' >&2
    exit 2
  }
  [ -t 0 ] || { printf '%s\n' 'gpt-pro-bundle: --interactive needs a terminal' >&2; exit 2; }
  printf 'Context: ' >&2
  IFS= read -r CONTEXT
else
  [ -n "$PROMPT_SPEC" ] || { usage >&2; exit 2; }
fi

if [ -n "$BUNDLE_NAME" ]; then
  [[ "$BUNDLE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    printf '%s\n' 'gpt-pro-bundle: --bundle-name may contain only letters, numbers, dot, underscore, and hyphen' >&2
    exit 2
  }
  BUNDLE_DIR="$OUT/$BUNDLE_NAME"
  ARCHIVE="$OUT/$BUNDLE_NAME.zip"
  [ ! -e "$BUNDLE_DIR" ] && [ ! -e "$ARCHIVE" ] || {
    printf '%s\n' 'gpt-pro-bundle: refusing to overwrite an existing bundle or archive' >&2
    exit 2
  }
  mkdir -p "$OUT" "$BUNDLE_DIR"
else
  BUNDLE_DIR="$OUT"
  ARCHIVE="$OUT/gpt-pro-bundle.zip"
  [ ! -e "$BUNDLE_DIR" ] || { printf 'gpt-pro-bundle: refusing to overwrite %s\n' "$BUNDLE_DIR" >&2; exit 2; }
  mkdir -p "$BUNDLE_DIR"
fi

CONTEXT_FILE="$BUNDLE_DIR/CONTEXT.md"
PROMPT_FILE="$BUNDLE_DIR/PROMPT.md"
README_FILE="$BUNDLE_DIR/00-README.md"

if [ -n "$CONTEXT" ]; then
  write_source "$CONTEXT" "$CONTEXT_FILE"
  [ -s "$CONTEXT_FILE" ] || { printf '%s\n' 'gpt-pro-bundle: context must not be empty' >&2; exit 2; }
fi
if [ "$INTERACTIVE" -eq 1 ]; then
  write_interactive_prompt "$PROMPT_FILE"
else
  write_source "$PROMPT_SPEC" "$PROMPT_FILE"
fi

[ -s "$PROMPT_FILE" ] || { printf '%s\n' 'gpt-pro-bundle: prompt must not be empty' >&2; exit 2; }
if [ -z "$CONTEXT" ] && [ "${#INCLUDES[@]}" -eq 0 ]; then
  printf '%s\n' 'gpt-pro-bundle: supply --context or at least one --include evidence file' >&2
  exit 2
fi
for header in Role Goal Given Constraints "Success criteria" Verification Output "Stop rule"; do
  count="$(section_count "$header")"
  [ "$count" -eq 1 ] || {
    printf 'gpt-pro-bundle: PROMPT.md needs exactly one "%s" section (plain or ## heading)\n' "$header" >&2
    exit 2
  }
done

if [ "${#INCLUDES[@]}" -gt 0 ]; then
  mkdir "$BUNDLE_DIR/docs"
  for source in "${INCLUDES[@]}"; do
    [ -n "$source" ] && [ -e "$source" ] || {
      printf 'gpt-pro-bundle: include does not exist: %s\n' "$source" >&2
      exit 2
    }
    cp -R "$source" "$BUNDLE_DIR/docs/"
  done
fi

write_readme "$README_FILE"
ARCHIVE_DIR="$(cd "$(dirname "$ARCHIVE")" && pwd)"
ARCHIVE_ABS="$ARCHIVE_DIR/$(basename "$ARCHIVE")"
if [ -n "$BUNDLE_NAME" ]; then
  (cd "$OUT" && zip -qr "$ARCHIVE_ABS" "$(basename "$BUNDLE_DIR")")
else
  (cd "$BUNDLE_DIR" && zip -qr "$ARCHIVE_ABS" . -x "$(basename "$ARCHIVE")")
fi
printf '%s\n' "$ARCHIVE"
