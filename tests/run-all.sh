#!/usr/bin/env bash
# run-all.sh — the ONE command that proves this skill's dispatch machinery.
#
# WHY THIS EXISTS. The checks were already complete; nothing pointed at them. SKILL.md named the
# two STATIC scanners and stopped there, so the BEHAVIOURAL suites - the only ones that prove a
# watchdog actually FIRES rather than merely being written - were discoverable only by listing
# tests/. That gap once led to a report that the wrappers "have never been watched actually killing
# a stuck job". That was flatly false: claude, agy, copilot, codex and opencode each have
# idle-kill, wall-kill, SIGKILL-escalation and process-group-cleanup cases. A suite nobody is told
# to run is indistinguishable from a suite that does not exist, and the failure mode is not a
# silent regression - it is a confident wrong report.
#
# DISCOVERED, never hardcoded. A fixed list silently drops the next suite somebody adds, which is
# the same blind spot check-dispatch-invariants.sh exists to prevent one layer down.
#
# Usage:  tests/run-all.sh
# Exit:   0 = everything green. 1 = at least one suite failed.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SELF="$HERE/$(basename "$0")"

# Static first: they are seconds, not minutes, and a wrapper that lost its watchdog should fail
# the cheap check before anyone waits four minutes for the expensive one to say the same thing.
SUITES=( "$ROOT/scripts/check-dispatch-invariants.sh" "$ROOT/scripts/sabotage-check.sh" )
for f in "$HERE"/*.sh; do
  [ -f "$f" ] || continue
  [ "$f" = "$SELF" ] || SUITES+=( "$f" )
done
for f in "$HERE"/*.py; do
  [ -f "$f" ] || continue
  [ "$(basename "$f")" = "__init__.py" ] && continue
  SUITES+=( "$f" )
done
# The console page's script is tested with node (no browser); skipped where node is not installed.
if command -v node >/dev/null 2>&1; then
  for f in "$HERE"/*.cjs; do
    [ -f "$f" ] || continue
    SUITES+=( "$f" )
  done
fi

LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/orchestrator-run-all.XXXXXX")"
trap 'rm -rf "$LOGDIR"' EXIT

failed=0
results=()
started_all="$(date +%s)"

for suite in "${SUITES[@]}"; do
  name="$(basename "$suite")"
  printf '== %s\n' "$name"
  log="$LOGDIR/$name.log"
  started="$(date +%s)"
  case "$suite" in
    # As a module from the repo root, so `from tests.x import y` resolves and a test class defined
    # after a file's `unittest.main()` guard is still collected (run as a script it is skipped).
    *.py) (cd "$ROOT" && python3 -m unittest "tests.$(basename "$suite" .py)") >"$log" 2>&1 ;;
    *.cjs) node --test "$suite" >"$log" 2>&1 ;;
    *)    bash "$suite" >"$log" 2>&1 ;;
  esac
  rc=$?
  elapsed=$(( $(date +%s) - started ))
  # The verdict line each suite already prints. Echoing it here means this runner never has to
  # invent a summary of someone else's result, which is how a runner starts lying.
  verdict="$(grep -E 'ALL DISPATCH INVARIANTS HOLD|DISPATCH INVARIANT\(S\) VIOLATED|EVERY INVARIANT PROVEN ABLE TO FAIL|sabotage results:|^RESULT|^OK$|^FAILED|passed, .* failed|PASS$|^ℹ pass [0-9]+' "$log" | tail -1)"
  if [ "$rc" -eq 0 ]; then
    printf '   PASS  %-28s %3ds  %s\n' "$name" "$elapsed" "${verdict:-(no verdict line)}"
    results+=( "PASS  $name" )
  else
    printf '   FAIL  %-28s %3ds  exit %s\n' "$name" "$elapsed" "$rc"
    printf '   ---- failed suite output ----\n'
    sed 's/^/   /' "$log"
    results+=( "FAIL  $name (exit $rc)" )
    failed=$((failed + 1))
  fi
done

printf '\n== summary (%ss total)\n' "$(( $(date +%s) - started_all ))"
printf '   %s\n' "${results[@]}"
echo
if [ "$failed" -eq 0 ]; then
  printf 'ALL %s SUITES GREEN\n' "${#SUITES[@]}"
  exit 0
fi
printf '%s OF %s SUITES FAILED\n' "$failed" "${#SUITES[@]}"
exit 1
