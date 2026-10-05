#!/usr/bin/env bash
# check-dispatch-invariants.sh — mechanized scanner for the dispatch-reliability bug CLASS.
#
# Why a scanner and not a doc:
#   A silent wall-clock cap (codex-agent.sh once bound TIMEOUT="600") killed long jobs while
#   looking like an ordinary failure. The operating doc ALREADY told callers to pass --timeout;
#   395 of 400 real dispatches did not. A rule written down and violated anyway is
#   the condition under which this system mechanises instead of re-wording. Production
#   silent-failure research is blunt: a point fix without a mechanized scanner recurs within days,
#   and defending the MECHANISM immunizes a class where defending a location immunizes one file.
#
# This file is checked by sabotage: sabotage-check.sh reintroduces each antipattern and asserts the
# matching invariant goes RED. A guard nobody has watched fail is only a claim.
#
# Usage:  check-dispatch-invariants.sh [SCRIPTS_DIR] [SKILL_MD]
# Exit:   0 = all hold. 1 = at least one violated. 2 = usage/setup error.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -uo pipefail

# Script-relative defaults: this file scans the directory it ships in. The operating doc sits
# beside scripts/ in an installed skill and under skill/ in a source checkout; take whichever exists.
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="${1:-$HERE}"
if [ -n "${2:-}" ]; then SKILL_MD="$2"
elif [ -f "$HERE/../SKILL.md" ]; then SKILL_MD="$HERE/../SKILL.md"
else SKILL_MD="$HERE/../skill/SKILL.md"; fi
[ -d "$DIR" ] || { echo "check-dispatch-invariants: no such dir: $DIR" >&2; exit 2; }

# Discovered, never hardcoded: a hardcoded list silently exempts the next wrapper somebody adds,
# which is the same class of blind spot this scanner exists to prevent.
WRAPPERS="$(cd "$DIR" && ls *-agent.sh 2>/dev/null | tr '\n' ' ')"
CONSUMERS="fanout.sh swarm.sh afk-run.sh"
[ -n "$WRAPPERS" ] || { echo "check-dispatch-invariants: no *-agent.sh found in $DIR" >&2; exit 2; }

fails=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails+1)); }
have() { [ -f "$DIR/$1" ]; }
# strip comment lines so a comment can never satisfy or trip an invariant
code() { sed -e 's/[[:space:]]*#.*$//' "$DIR/$1" 2>/dev/null; }

# cgrep — grep the comment-stripped source as a FILE, never through a pipe.
# `code "$w" | grep -q PATTERN` is a landmine under `set -o pipefail`: grep -q exits at the first
# match, sed then dies of SIGPIPE, and the PIPELINE reports 141 — which every `if !` in this file
# read as "pattern absent". It only bites once the source is bigger than the pipe buffer (~16KB on
# macOS), so it silently exempted the LARGEST wrappers and left the small ones looking fine.
# Observed once: opencode-agent.sh (31KB) was reported "no self-supervision" by I2b and was
# therefore not checked for TERM/KILL escalation at all, while it in fact escalates correctly.
# Same blind-spot class as the one I2b itself was written to close, one layer down.
CODE_TMP="$(mktemp "${TMPDIR:-/tmp}/dispatch-invariants.code.XXXXXX")"
CODE_TMP2="$(mktemp "${TMPDIR:-/tmp}/dispatch-invariants.code2.XXXXXX")"
trap 'rm -f "$CODE_TMP" "$CODE_TMP2"' EXIT
cgrep() { local f="$1"; shift; code "$f" >"$CODE_TMP"; grep "$@" "$CODE_TMP"; }

echo "== dispatch invariants: $DIR"

# I1 — no silent wall-clock default.
# Binding a LITERAL number is the defect. Binding from an argument (TIMEOUT="$2") is correct and
# must not trip. Deliberately unanchored: the original bug sat mid-line in a multi-assignment.
echo "I1  no silent wall-clock default"
for w in $WRAPPERS; do
  have "$w" || continue
  # BSD grep -E does not honour \b, so anchor explicitly on a non-word char or line start.
  # This is what stops IDLE_TIMEOUT="600" (correct, required) reading as TIMEOUT="600" (the bug).
  # Also catch the COMPUTED and DEFAULTED forms, which an adversarial review flagged as the most
  # likely accidental reintroduction: someone writing a "cleaner" default.
  N='(TIMEOUT|WALL|WALL_TIMEOUT|DEFAULT_TIMEOUT)'
  hit="$(code "$w" | grep -nE "(^|[^A-Za-z0-9_])$N=[\"']?[0-9]+" | head -1)"
  [ -z "$hit" ] && hit="$(code "$w" | grep -nE "(^|[^A-Za-z0-9_])$N=[\"']?\\\$\(\(" | head -1)"          # TIMEOUT=$((10*60))
  # ${TIMEOUT:-600} is a WALL-CLOCK default only when it is assigned to a wall-clock variable, or fed
  # straight into a timeout invocation. Reading TIMEOUT into an unrelated variable (a quota lease TTL,
  # say) is a different thing, and flagging that made the scanner cry wolf on a correct fix. A guard
  # that cries wolf stops being obeyed, so the precision is worth the extra line.
  [ -z "$hit" ] && hit="$(code "$w" | grep -nE "(^|[^A-Za-z0-9_])$N=[\"']?\\\$\{$N:-[0-9]+\}" | head -1)"
  [ -z "$hit" ] && hit="$(code "$w" | grep -nE "(gtimeout|timeout)[^|]*\\\$\{$N:-[0-9]+\}" | head -1)"
  [ -z "$hit" ] && hit="$(code "$w" | grep -nE "readonly[[:space:]]+$N=[\"']?[0-9]" | head -1)"          # readonly TIMEOUT=600
  if [ -n "$hit" ]; then fail "$w binds a wall-clock default -> $hit"; else pass "$w"; fi
done
for c in $CONSUMERS; do
  have "$c" || continue
  hit="$(code "$c" | grep -nE '(^|[^A-Za-z0-9_])DEFAULT_TIMEOUT=["'"'"']?[0-9]+' | head -1)"
  if [ -n "$hit" ]; then fail "$c binds a literal default timeout -> $hit"; else pass "$c"; fi
done

# I2 — any timeout invocation must escalate to SIGKILL.
# `gtimeout S cmd` sends SIGTERM only; a child that traps or ignores it makes the timeout itself
# hang forever. The timeout must have a timeout.
echo "I2  SIGTERM escalates to SIGKILL"
for f in $WRAPPERS $CONSUMERS; do
  have "$f" || continue
  # Only an INVOCATION counts. Exclude the flag form (--timeout), the assignment form
  # (TIMEOUT_BIN="gtimeout"), and the detection form (command -v gtimeout). Matching those made
  # every script that merely ACCEPTS a timeout flag look guilty, which is how a check goes blind.
  # Checked PER INVOCATION, not per file: one escalating call must not vouch for a second bare one.
  # An INVOCATION passes a duration next: `gtimeout 600 cmd`, `gtimeout --kill-after N "$T" cmd`,
  # `"$TIMEOUT_BIN" "$TIMEOUT" cmd`. Requiring a duration-shaped token immediately after excludes
  # prose inside an echo ("no timeout/gtimeout on PATH") and non-shell lines in embedded heredocs
  # (`timeout = task.get(...)`), both of which produced false alarms on the real scripts.
  invs="$(code "$f" | grep -n -v 'command -v' | grep -vE '^[0-9]*:?[[:space:]]*(echo|printf)' \
        | grep -E '(^|[^-A-Za-z0-9_])(gtimeout|timeout)[[:space:]]+(-|[0-9]|"?\$)|(\$TIMEOUT_BIN|\$timeout_bin)[[:space:]]+("?\$|[0-9])' || true)"
  if [ -z "$invs" ]; then
    pass "$f (no timeout invocation)"
  else
    bare="$(printf '%s\n' "$invs" | grep -vE '(--kill-after|[[:space:]]-k[[:space:]])' | head -1 || true)"
    if [ -z "$bare" ]; then pass "$f escalates (all $(printf '%s\n' "$invs" | grep -c . ) invocation(s))"
    else fail "$f has a timeout invocation with no -k/--kill-after -> $bare"; fi
  fi
done

# I2b — the escalation that the LIVE wrappers actually use.
# I2 above only understands `gtimeout --kill-after`. The wrappers supervise their own child instead,
# so they never invoke a timeout binary and I2 waved every one of them through as "no timeout
# invocation" while their real escalation went unchecked. Proven blind: deleting EVERY `kill -KILL`
# from a live wrapper still returned ALL DISPATCH INVARIANTS HOLD. A guard that does not look at the
# mechanism in use is decorative.
echo "I2b self-supervised wrappers escalate TERM to KILL on the GROUP"
for w in $WRAPPERS; do
  have "$w" || continue
  if [ "$w" = chatgpt-agent.sh ]; then
    if ! cgrep "$w" -q 'exec python3.*chatgpt_runner.py' || ! have chatgpt_runner.py \
        || ! cgrep chatgpt_runner.py -q 'os.killpg(child.pid, signal.SIGTERM)' \
        || ! cgrep chatgpt_runner.py -q 'os.killpg(child.pid, signal.SIGKILL)'; then
      fail "$w lost its Python TERM/KILL process-group supervisor"
    else
      pass "$w (HTTP child TERM then KILL, group-targeted)"
    fi
    continue
  fi
  if [ "$w" = pi-agent.sh ]; then
    if ! cgrep "$w" -q 'exec python3.*pi_runner.py' || ! have pi_runner.py; then
      fail "$w lost its Python process-group supervisor"
    elif ! cgrep pi_runner.py -q 'os.killpg(child.pid, sig)' \
        || ! cgrep pi_runner.py -q 'signal_group(child, signal.SIGTERM)' \
        || ! cgrep pi_runner.py -q 'signal_group(child, signal.SIGKILL)'; then
      fail "$w Python supervisor must send TERM then KILL to the process group"
    else
      pass "$w (Python TERM then KILL, group-targeted)"
    fi
    continue
  fi
  if ! cgrep "$w" -qE 'IDLE_TIMEOUT'; then pass "$w (no self-supervision)"; continue; fi
  if ! cgrep "$w" -qE 'kill[[:space:]]+-TERM'; then
    fail "$w supervises its own child but never sends SIGTERM"
  elif ! cgrep "$w" -qE 'kill[[:space:]]+-KILL'; then
    fail "$w sends SIGTERM but never escalates to SIGKILL (a child ignoring TERM never dies)"
  elif ! cgrep "$w" -qE 'kill[[:space:]]+-KILL[^\n]*--?[[:space:]]*"?-\$'; then
    fail "$w escalates to SIGKILL but not against the process GROUP (descendants survive)"
  else
    pass "$w (TERM then KILL, group-targeted)"
  fi
done

# I3 — every wrapper needs a liveness watchdog, because I1 removed the wall clock.
# Removing the cap without a liveness check trades a silent kill for a silent hang.
echo "I3  idle/liveness watchdog present"
for w in $WRAPPERS; do
  have "$w" || continue
  if [ "$w" = chatgpt-agent.sh ]; then
    if ! have chatgpt_runner.py || ! cgrep chatgpt_runner.py -q 'os.setsid()' \
        || ! cgrep chatgpt_runner.py -q 'args.idle and now - progress >= args.idle' \
        || ! cgrep chatgpt_runner.py -q 'args.wall and now - started >= args.wall' \
        || ! cgrep chatgpt_runner.py -q '= supervise(' \
        || ! cgrep chatgpt_runner.py -q '"--wall", type=seconds, default=0'; then
      fail "$w lost its running HTTP idle/wall watchdog or added a wall default"
    else
      pass "$w (Python HTTP watchdog; buffered idle explicitly disabled by default)"
    fi
    continue
  fi
  if [ "$w" = pi-agent.sh ]; then
    if ! have pi_runner.py \
        || ! cgrep pi_runner.py -q 'start_new_session=True' \
        || ! cgrep pi_runner.py -q 'args.idle and now - activity >= args.idle' \
        || ! cgrep pi_runner.py -q 'terminate_group(child, args.kill_after)' \
        || ! cgrep pi_runner.py -q 'while poller.get_map()' \
        || ! cgrep pi_runner.py -q '"--idle", "--idle-timeout", type=seconds, default=0' \
        || ! cgrep pi_runner.py -q '"--wall", "--timeout", type=seconds, default=0'; then
      fail "$w lost its running Python idle watchdog or added a wall-clock default"
    else
      pass "$w (Python process-group idle watchdog running, wall uncapped)"
    fi
    continue
  fi
  # Three levels of not-good-enough, each caught separately, because an adversarial review walked
  # straight through the first two: a file can MENTION the variable, or DEFINE a watchdog function,
  # and still never start it. Only a backgrounded launch means supervision actually happens.
  n="$(code "$w" | grep -cE 'IDLE_TIMEOUT|idle-timeout|idle_timeout' || true)"
  launched="$(code "$w" | grep -cE '[A-Za-z_]*watchdog[A-Za-z_]*[[:space:]]*&([^&]|$)' || true)"
  if [ "${n:-0}" -eq 0 ]; then
    fail "$w has no idle watchdog (no wall clock AND no liveness check = silent hang)"
  elif [ "${n:-0}" -eq 1 ]; then
    fail "$w declares an idle watchdog but never reads it (dead variable)"
  elif [ "${launched:-0}" -eq 0 ]; then
    fail "$w defines an idle watchdog but never LAUNCHES it (no backgrounded watchdog call)"
  else
    pass "$w (watchdog defined, read in $n places, and launched)"
  fi
done

# I4 — work must never be suppressed on matched TEXT alone.
# fanout.sh decided quota exhaustion by grepping stderr: a timeout whose stderr merely contained
# the word "quota" silently skipped every remaining task on that pool and still exited 0.
# Only flag a MATCH CONSTRUCT (grep/case/=~) over those words, never an incidental path or key.
echo "I4  suppression decided by exit code, not matched text"
for c in $CONSUMERS; do
  have "$c" || continue
  # Include the glob-compare form `[[ $err == *quota* ]]`, which an adversarial review found
  # invisible to a grep/case/=~ pattern.
  MATCHFORM='(grep|=~|case|==[[:space:]]*\*)'
  hit="$(cgrep "$c" -nE "$MATCHFORM[^|]*(429|RESOURCE_EXHAUSTED|usage limit|quota)" | head -1)"
  if [ -z "$hit" ]; then
    pass "$c (no text-match decision)"
  elif cgrep "$c" -E "$MATCHFORM[^|]*(429|RESOURCE_EXHAUSTED|usage limit|quota)" >"$CODE_TMP2" \
       && grep -qE '(rc|exit_code|\$\?|_status)' "$CODE_TMP2"; then
    pass "$c (text-match gated on an exit code)"
  else
    fail "$c may suppress work on matched text alone -> $hit"
  fi
done

# I5 — an incomplete campaign must not exit 0.
# Skipped or failed tasks that still return 0 are a false success to every caller and to every
# downstream automation.
echo "I5  incomplete campaign exits non-zero"
if have fanout.sh; then
  # A bare `exit 1` somewhere in the file proves nothing: the original bug had usage-error exits
  # and still returned 0 for an incomplete campaign. Require a failure-COUNTER whose name says so,
  # AND a non-zero exit. Sabotage S5 (renaming the counter away) must turn this red.
  # The counter and the exit must be LINKED on one line. Checked separately, a counter that is
  # never incremented plus an unrelated `exit 1` in some error branch passes while the campaign
  # still returns 0 for a partial run.
  if cgrep fanout.sh -qE '(failed_count|failures|incomplete|any_fail|FAILED_TASKS|not_ok_count)[^;]*exit[[:space:]]+[1-9]|exit[[:space:]]+[1-9][^;]*(failed_count|failures|incomplete|any_fail)'; then
    pass "fanout.sh links its failure counter to a non-zero exit"
  else
    fail "fanout.sh appears to exit 0 regardless of per-task outcomes"
  fi
fi

# I7 — a REFUSED campaign must exit non-zero, and must not exit 1 either.
# Why I5 was not enough, precisely: I5 asks "does SOME line link a failure counter to a non-zero
# exit?", and one line at the very BOTTOM of fanout.sh satisfies it forever. It therefore says
# nothing about the ~40 exit points ABOVE that line, every one of which can end the campaign
# having dispatched nothing. Worse, most refusals live inside the embedded python preflight as
# `raise SystemExit(...)`, which carries no `exit N` token at all: NO grep over the shell source
# can ever see their status. A textual proxy is structurally blind here, so this invariant RUNS
# the dispatcher on inputs it must refuse and reads the real exit code.
# 1 is banned alongside 0 on purpose. outcome-taxonomy.sh maps fanout:1 to "campaign-incomplete",
# which tells the caller some tasks ran and did not finish — so go read summary.tsv. A refusal
# that dispatched nothing wrote no summary.tsv, and that mismatch is the whole defect.
echo "I7  a refused campaign exits non-zero and never 1"
# The probe runs the real dispatcher, which reads an overlay before it reaches its refusal paths.
# Use the caller's or the operator's; on a checkout with neither, the example shipped beside the
# engine, because this probe is about how fanout refuses, not about anyone's fleet.
if [ -z "${ACCESS_OVERLAY:-}" ] \
   && [ ! -f "${XDG_CONFIG_HOME:-$HOME/.config}/orchestrator/access-overlay.json" ] \
   && [ -f "$DIR/../examples/access-overlay.example.json" ]; then
  export ACCESS_OVERLAY="$DIR/../examples/access-overlay.example.json"
fi
if ! have fanout.sh; then
  pass "fanout.sh (absent, skipped)"
elif [ ! -x "$DIR/fanout.sh" ]; then
  pass "fanout.sh (not executable, behavioural probe skipped)"
else
  T7="$(mktemp -d 2>/dev/null)"
  if [ -z "$T7" ] || [ ! -d "$T7" ]; then
    fail "I7 could not create a temp dir; the behavioural probe did not run"
  # Probe deliberately NOT written as a pipeline: this file runs under `pipefail`, which would hand
  # back fanout's own non-zero status instead of grep's and misread the real dispatcher as a stub.
  elif "$DIR/fanout.sh" "$T7/no-such-tasks.jsonl" --dry-run >"$T7/probe" 2>&1;
       ! grep -q 'tasks file not found' "$T7/probe"; then
    # A stub or a different script sits at this path (the sabotage harness builds one). Do not
    # invent a verdict about something that is not the dispatcher.
    pass "fanout.sh (not the real dispatcher in $DIR, behavioural probe skipped)"
  else
    verdict() {  # verdict LABEL RC STDERR_FILE EXPECTED_MESSAGE
      if ! grep -q "$4" "$3" 2>/dev/null; then
        fail "fanout.sh did not refuse $1 with the expected message ($4); got: $(head -1 "$3")"
      elif [ "$2" -eq 0 ]; then
        fail "fanout.sh returns 0 for $1 — nothing was dispatched and the caller reads SUCCESS"
      elif [ "$2" -eq 1 ]; then
        fail "fanout.sh returns 1 for $1 — 1 means campaign-incomplete, sending the caller to a summary.tsv that was never written"
      else
        pass "$1 refused with exit $2"
      fi
    }
    mkdir -p "$T7/out-nonempty"
    : >"$T7/out-nonempty/leftover"
    printf '{"id":"i7ok","prompt":"x","agent":"codex","mode":"read-only","dir":"%s"}\n' "$T7" >"$T7/ok.jsonl"
    "$DIR/fanout.sh" "$T7/ok.jsonl" --out "$T7/out-nonempty" --dry-run >/dev/null 2>"$T7/e1"
    verdict "a non-empty --out directory" "$?" "$T7/e1" 'refusing non-empty output directory'

    printf '{"id":"i7bad","prompt":"x","agent":"no-such-wrapper","mode":"read-only","dir":"%s"}\n' "$T7" >"$T7/bad.jsonl"
    "$DIR/fanout.sh" "$T7/bad.jsonl" --out "$T7/out-preflight" --dry-run >/dev/null 2>"$T7/e2"
    verdict "an inadmissible task" "$?" "$T7/e2" 'fanout preflight'
  fi
  [ -n "${T7:-}" ] && [ -d "${T7:-}" ] && /bin/rm -rf "$T7" 2>/dev/null
fi

# I6 — declared state must match runtime state.
# The longest-latency silent failures live in the seam between what the doc declares and what the
# code does. The doc must name the actual contract, not the retired one.
echo "I6  docs match behaviour"
if [ -f "$SKILL_MD" ]; then
  # A NEGATIVE mention ("we removed the idle watchdog") must not satisfy a docs check.
  # AND the doc must carry the SAME NUMBER the code actually uses. Prose agreement is not agreement:
  # the wrappers moved to 2400 while the doc still said 1200, the scanner went green, and that
  # declared-vs-runtime gap is precisely the seam this invariant exists to close.
  doc_ok=1
  grep -iE 'idle watchdog' "$SKILL_MD" | grep -qivE '(remov|delet|drop|no longer|retired|without)' || doc_ok=0
  if [ "$doc_ok" -eq 0 ]; then
    fail "SKILL.md does not document the idle watchdog; docs still describe the retired contract"
  else
    # Collect the default from EVERY wrapper, not just one. Sampling a single file let a drifted
    # sibling hide behind a compliant first-in-list, and wrappers silently disagreeing with each
    # other is itself the defect: there is supposed to be ONE contract.
    defaults=""
    for w in $WRAPPERS; do
      d="$(code "$w" | grep -oE 'IDLE_TIMEOUT="[0-9]+"' | head -1 | grep -oE '[0-9]+')"
      [ -n "$d" ] && defaults="$defaults $d"
    done
    uniq_defaults="$(printf '%s\n' $defaults | sort -u | tr '\n' ' ' | sed 's/ *$//')"
    n_uniq="$(printf '%s\n' $defaults | sort -u | grep -c .)"
    if [ "${n_uniq:-0}" -gt 1 ]; then
      fail "wrappers disagree on the idle default ($uniq_defaults) - there is supposed to be one contract"
    elif [ -n "$uniq_defaults" ] && ! grep -q "$uniq_defaults" "$SKILL_MD"; then
      fail "SKILL.md never mentions the idle default the code actually uses ($uniq_defaults) - declared state has drifted from runtime state"
    elif [ -n "$uniq_defaults" ] && [ "$uniq_defaults" != 0 ]; then
      fail "idle killing must be opt-in (default 0); declared state has drifted from the owner contract"
    else
      pass "SKILL.md documents the idle watchdog and its real default (${uniq_defaults:-n/a})"
    fi
  fi
else
  pass "SKILL.md (absent, skipped)"
fi

# The agent-facing table and its full-text escape hatch must remain discoverable.
# Tiny sabotage fixtures have no fleetctl; their wrapper-only doc stays sufficient.
if have fleetctl.py && [ -f "$SKILL_MD" ]; then
  if grep -Fq 'pool | level | binding window | used | resets | price | models on' "$SKILL_MD" \
     && grep -Fq 'off:' "$SKILL_MD" \
     && grep -Fq 'pick: fleetctl.py select --role R [--stakes S]' "$SKILL_MD" \
     && grep -Fq 'brief --verbose' "$SKILL_MD" \
     && grep -Fq 'lambda' "$SKILL_MD"; then
    pass "SKILL.md documents the compact brief and verbose details"
  else
    fail "SKILL.md compact brief contract drifted (table, off, pick, lambda or verbose missing)"
  fi
fi

# I8 is backed by fake-CLI argv tests: this scanner protects the resolution and
# mandatory flag sites, while the suites prove the flags actually reach the CLI.
echo "I8  reasoning wrappers resolve and announce effort"
for w in codex-agent.sh claude-agent.sh opencode-agent.sh agy-agent.sh; do
  have "$w" || continue
  if ! cgrep "$w" -q 'effort_args=( effort ' || ! cgrep "$w" -q 'fleetctl.py.*effort_args' || ! cgrep "$w" -q 'effort .*_effort_why.*>&2'; then
    fail "$w lost roster effort resolution or its stderr provenance"
  elif [ "$w" = codex-agent.sh ] && ! cgrep "$w" -q '^args+=( -c "model_reasoning_effort='; then
    fail "$w no longer passes model_reasoning_effort unconditionally"
  else
    pass "$w resolves effort with provenance"
  fi
done

echo
if [ "$fails" -eq 0 ]; then echo "ALL DISPATCH INVARIANTS HOLD"; exit 0; fi
echo "$fails DISPATCH INVARIANT(S) VIOLATED"
exit 1

}
main "$@"; exit $?
