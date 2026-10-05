#!/usr/bin/env bash
# sabotage-check.sh — prove check-dispatch-invariants.sh can actually FAIL.
#
# A guard nobody has watched fail is only a claim. This builds a synthetic script set that
# satisfies every invariant (expect GREEN), then reintroduces each antipattern one at a time and
# asserts the MATCHING invariant goes RED. If a sabotage does not turn the scanner red, that
# invariant is decorative and this script says so.
#
# S1-S6 are the original defect classes. S7-S10 were added after an independent adversarial review
# found bypasses that the first version of the scanner waved through.
# Parse the complete body before starting, so an in-flight edit cannot change this run.
main() {
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCANNER="$HERE/check-dispatch-invariants.sh"
[ -x "$SCANNER" ] || { echo "sabotage: scanner not executable at $SCANNER" >&2; exit 2; }

WORK="$HERE/.sabotage-work"
pass=0; fail=0

# In-place edit that behaves the same with BSD sed (macOS) and GNU sed (Linux): both accept a
# suffix glued to -i, and neither accepts the other's bare form (`-i ''` vs `-i`).
sedi() {
  local expr="$1" f; shift
  for f in "$@"; do sed -i.sabotage-orig "$expr" "$f" && /bin/rm -f "$f.sabotage-orig"; done
}

build_clean() {
  local d="$WORK"
  /bin/rm -rf "$d" 2>/dev/null
  mkdir -p "$d/scripts"
  for w in codex claude agy copilot opencode; do
    cat > "$d/scripts/$w-agent.sh" <<'EOF'
#!/usr/bin/env bash
TIMEOUT=""; IDLE_TIMEOUT="0"; KILL_AFTER="30"
_watchdog() { local q="$IDLE_TIMEOUT"; while :; do sleep 5; [ "$q" -le 0 ] && break; done; }
start_supervision() { _watchdog & WATCHDOG_PID=$!; }
run() { start_supervision; gtimeout --kill-after "$KILL_AFTER" "$TIMEOUT" real-binary "$@"; }
_terminate_group() { kill -TERM -- "-$CHILD_PGID"; sleep 1; kill -KILL -- "-$CHILD_PGID"; }
EOF
  done
  for w in codex claude agy opencode; do
    cat >> "$d/scripts/$w-agent.sh" <<'EOF'
effort_args=( effort "$MODEL" "$ROLE" --explain )
effort_row="$("$HERE/fleetctl.py" "${effort_args[@]}")" || exit 2
echo "effort $EFFORT: $_effort_why" >&2
args+=( -c "model_reasoning_effort=\"$REASONING\"" )
EOF
  done
  cat > "$d/scripts/fanout.sh" <<'EOF'
#!/usr/bin/env bash
DEFAULT_TIMEOUT=""; failed_count=0
run() { gtimeout --kill-after 30 "$t" ./worker.sh; rc=$?
  if [ "$rc" = "3" ] && grep -Eiq '429|quota' "$err"; then note_quota; fi
  [ "$rc" != 0 ] && failed_count=$((failed_count+1)); }
finish() { [ "$failed_count" -gt 0 ] && exit 1; exit 0; }
EOF
  printf '#!/usr/bin/env bash\necho "this profile dispatches nothing directly"\n' > "$d/scripts/swarm.sh"
  printf '#!/usr/bin/env bash\nrun() { gtimeout --kill-after 30 "$t" ./worker.sh; }\n' > "$d/scripts/afk-run.sh"
  printf '# Skill\n\nDispatch uses an idle watchdog rather than a wall clock (default 0).\n' > "$d/SKILL.md"
}

check() {
  local label="$1" expect="$2" needle="${3:-}" out rc
  out="$("$SCANNER" "$WORK/scripts" "$WORK/SKILL.md" 2>&1)"; rc=$?
  if [ "$expect" = "green" ]; then
    if [ "$rc" -eq 0 ]; then printf 'PASS  %s (green as expected)\n' "$label"; pass=$((pass+1))
    else printf 'FAIL  %s expected GREEN, got RED:\n%s\n' "$label" "$out"; fail=$((fail+1)); fi
  else
    if [ "$rc" -ne 0 ] && echo "$out" | grep -qi "$needle"; then
      printf 'PASS  %s (caught: %s)\n' "$label" "$needle"; pass=$((pass+1))
    else
      printf 'FAIL  %s NOT caught (rc=%s, wanted a FAIL mentioning "%s")\n%s\n' "$label" "$rc" "$needle" "$out"; fail=$((fail+1))
    fi
  fi
}

echo "== baseline"
build_clean; check "clean set" green

echo "== S1 literal wall-clock default (the original bug)"
build_clean; sedi 's/^TIMEOUT=""/TIMEOUT="600"/' "$WORK/scripts/codex-agent.sh"
check "S1 literal default" red "binds a wall-clock default"

echo "== S2 drop SIGKILL escalation"
build_clean; sedi 's/ --kill-after "\$KILL_AFTER"//' "$WORK/scripts/codex-agent.sh"
check "S2 no kill-after" red "no -k/--kill-after"

echo "== S3 remove the idle watchdog entirely"
build_clean; sedi 's/IDLE_TIMEOUT="0"; //; s/^_watchdog.*$//; s/^start_supervision.*$//' "$WORK/scripts/codex-agent.sh"
check "S3 no watchdog" red "no idle watchdog"

echo "== S4 suppress work on matched text alone"
build_clean; sedi 's/if \[ "\$rc" = "3" \] && grep/if grep/' "$WORK/scripts/fanout.sh"
check "S4 text-only suppression" red "matched text alone"

echo "== S5 incomplete campaign exits 0"
build_clean; sedi 's/\[ "\$failed_count" -gt 0 \] && exit 1; //' "$WORK/scripts/fanout.sh"
check "S5 campaign always exits 0" red "exit 0 regardless"

echo "== S6 docs describe the retired contract"
build_clean; printf '# Skill\n\nSize --timeout to the reasoning tier. 0.\n' > "$WORK/SKILL.md"
check "S6 docs drift" red "does not document the idle watchdog"

echo "== S7 COMPUTED default (the 'cleaner' rewrite an engineer actually writes)"
build_clean; sedi 's/^TIMEOUT=""/TIMEOUT=$((10*60))/' "$WORK/scripts/codex-agent.sh"
check "S7 computed default" red "binds a wall-clock default"

echo "== S7b PARAMETER-DEFAULT form"
build_clean; sedi 's/gtimeout --kill-after "\$KILL_AFTER" "\$TIMEOUT"/gtimeout --kill-after "$KILL_AFTER" "${TIMEOUT:-600}"/' "$WORK/scripts/codex-agent.sh"
check "S7b \${TIMEOUT:-600}" red "binds a wall-clock default"

echo "== S8 DEAD watchdog variable (declared, never read)"
build_clean; sedi 's/^_watchdog() .*$//' "$WORK/scripts/codex-agent.sh"
check "S8 dead watchdog var" red "never reads it"

echo "== S9 a SECOND, bare timeout invocation alongside a compliant one"
build_clean; printf 'retry() { gtimeout "$TIMEOUT" real-binary --again; }\n' >> "$WORK/scripts/codex-agent.sh"
check "S9 second bare invocation" red "no -k/--kill-after"

echo "== S10 docs mention the watchdog only to say it was removed"
build_clean; printf '# Skill\n\nWe removed the idle watchdog in favour of a fixed cap. 0.\n' > "$WORK/SKILL.md"
check "S10 negative docs mention" red "does not document the idle watchdog"

echo "== S12 watchdog DEFINED but never launched (review walked through the old check)"
build_clean; sedi 's/^start_supervision.*$//' "$WORK/scripts/codex-agent.sh"
check "S12 watchdog never launched" red "never LAUNCHES it"

echo "== S13 one wrapper drifts away from its siblings"
build_clean; sedi 's/IDLE_TIMEOUT="0"/IDLE_TIMEOUT="1800"/' "$WORK/scripts/codex-agent.sh"
check "S13 wrappers disagree" red "disagree on the idle default"

echo "== S13b ALL wrappers move but the doc is left behind (true declared-vs-runtime drift)"
build_clean; sedi 's/IDLE_TIMEOUT="0"/IDLE_TIMEOUT="1800"/' "$WORK"/scripts/*-agent.sh
check "S13b doc left behind" red "declared state has drifted"

echo "== S14 the escalation the LIVE wrappers actually use is removed (review 3 proved I2 blind here)"
build_clean; sedi 's/ kill -KILL -- "-\$CHILD_PGID";//' "$WORK/scripts/codex-agent.sh"
check "S14 no group SIGKILL" red "never escalates to SIGKILL"

echo "== S14b escalation exists but targets the PID, not the process group"
build_clean; sedi 's/kill -KILL -- "-\$CHILD_PGID"/kill -KILL "$CHILD_PID"/' "$WORK/scripts/codex-agent.sh"
check "S14b KILL not group-targeted" red "not against the process GROUP"

echo "== S14c a wrapper too big for the pipe buffer must not be silently exempted"
# The scanner used to run `code "$w" | grep -q PATTERN`. grep -q exits at the first match, sed then
# dies of SIGPIPE, and under `set -o pipefail` the PIPELINE reported 141 - which `if !` read as
# "pattern absent", so I2b waved the file through as "no self-supervision" and never checked its
# escalation at all. It only bites once the source passes the pipe buffer (~16KB on macOS), which
# is exactly why the small synthetic wrappers above never caught it while the real 31KB
# opencode-agent.sh sat unchecked. Pad past the buffer, remove the group SIGKILL, demand RED.
build_clean
python3 -c 'print("".join("padding_%05d=\"%s\"\n" % (i, "x"*60) for i in range(800)), end="")' >> "$WORK/scripts/codex-agent.sh"
sedi 's/ kill -KILL -- "-\$CHILD_PGID";//' "$WORK/scripts/codex-agent.sh"
check "S14c oversized wrapper still scanned" red "never escalates to SIGKILL"

echo "== S11 a NEW wrapper is added and must not be exempt"
build_clean; sed 's/TIMEOUT=""/TIMEOUT="900"/' "$WORK/scripts/codex-agent.sh" > "$WORK/scripts/gemini-agent.sh"
check "S11 new wrapper scanned" red "gemini-agent.sh"

# ---------------------------------------------------------------------------
# I7 is BEHAVIOURAL: it runs the dispatcher on inputs it must refuse. The synthetic fanout.sh above
# is a five-line stub, and I7 correctly declines to judge it, so I7's sabotage cannot use $WORK.
# It uses a MIRROR instead: a temp dir that symlinks every real script and substitutes one
# sabotaged COPY of fanout.sh. Nothing under the real scripts dir is modified.
#
# The mirror also re-runs I1-I6 against the real wrappers, which may be red for unrelated reasons,
# so these cases assert on an I7-SPECIFIC needle rather than on the overall exit code — and M0
# first proves that needle is absent when nothing is sabotaged.
REAL_DIR="${REAL_SCRIPTS_DIR:-$HERE}"
MIRROR="$HERE/.sabotage-mirror"
# The overlay the I7 probe routes against: the caller's, else the operator's configured one, else
# the example that ships with the engine, so a clean checkout can still run the probe.
MIRROR_OVERLAY="${ACCESS_OVERLAY:-${XDG_CONFIG_HOME:-$HOME/.config}/orchestrator/access-overlay.json}"
[ -f "$MIRROR_OVERLAY" ] || MIRROR_OVERLAY="$REAL_DIR/../examples/access-overlay.example.json"
if [ -f "$REAL_DIR/../SKILL.md" ]; then MIRROR_SKILL_MD="$REAL_DIR/../SKILL.md"
else MIRROR_SKILL_MD="$REAL_DIR/../skill/SKILL.md"; fi

build_mirror() {  # build_mirror [sed-expression applied to fanout.sh]
  /bin/rm -rf "$MIRROR" 2>/dev/null
  mkdir -p "$MIRROR"
  for f in "$REAL_DIR"/*.sh "$REAL_DIR"/*.py; do
    [ -e "$f" ] || continue
    ln -s "$f" "$MIRROR/$(basename "$f")"
  done
  /bin/rm -f "$MIRROR/fanout.sh"
  if [ -n "${1:-}" ]; then sed "$1" "$REAL_DIR/fanout.sh" >"$MIRROR/fanout.sh"
  else cp "$REAL_DIR/fanout.sh" "$MIRROR/fanout.sh"; fi
  chmod +x "$MIRROR/fanout.sh"
}

mirror_scan() {
  ACCESS_OVERLAY="$MIRROR_OVERLAY" "$SCANNER" "$MIRROR" "$MIRROR_SKILL_MD" 2>&1
}

check_mirror() {  # check_mirror LABEL present|absent NEEDLE
  local label="$1" expect="$2" needle="$3" out
  out="$(mirror_scan)"
  if [ "$expect" = "absent" ]; then
    if echo "$out" | grep -q "$needle"; then
      printf 'FAIL  %s expected NO "%s", but the scanner emitted it:\n%s\n' "$label" "$needle" "$out"; fail=$((fail+1))
    else printf 'PASS  %s (no "%s", as expected)\n' "$label" "$needle"; pass=$((pass+1)); fi
  else
    if echo "$out" | grep -q "$needle"; then
      printf 'PASS  %s (caught: %s)\n' "$label" "$needle"; pass=$((pass+1))
    else
      printf 'FAIL  %s NOT caught (wanted a FAIL mentioning "%s")\n%s\n' "$label" "$needle" "$out"; fail=$((fail+1))
    fi
  fi
}

if [ -f "$REAL_DIR/fanout.sh" ] && [ -f "$MIRROR_OVERLAY" ]; then
  echo "== M0 baseline: the REAL dispatcher refuses correctly, so I7 must not fire"
  build_mirror; check_mirror "M0 real dispatcher clean" absent "returns 0 for"
  build_mirror; check_mirror "M0 real dispatcher clean (no 1-collision)" absent "returns 1 for"
  build_mirror; check_mirror "M0 I7 actually ran (did not skip the probe)" present "refused with exit"

  echo "== S15 a refusal exits 0 (the reported defect: nothing dispatched, caller reads success)"
  build_mirror 's/^EXIT_REFUSED=4$/EXIT_REFUSED=0/'
  check_mirror "S15 refusal exits 0" present "returns 0 for"

  echo "== S16 a refusal exits 1, colliding with campaign-incomplete"
  build_mirror 's/^EXIT_REFUSED=4$/EXIT_REFUSED=1/'
  check_mirror "S16 refusal collides with 1" present "returns 1 for"

  echo "== S17 the preflight refusal is not converted at all (the literal pre-fix code path)"
  build_mirror 's/^if \[ "\$preflight_rc" -ne 0 \]; then$/if false; then/'
  check_mirror "S17 preflight status leaks" present "for an inadmissible task"

  /bin/rm -rf "$MIRROR" 2>/dev/null
else
  printf 'FAIL  I7 sabotage could not run: need %s and %s\n' "$REAL_DIR/fanout.sh" "$MIRROR_OVERLAY"
  fail=$((fail+1))
fi

echo "== S18 each reasoning wrapper loses effort resolution"
for w in codex claude agy opencode; do
  build_clean
  sed '/effort_args=( effort /d' "$WORK/scripts/$w-agent.sh" > "$WORK/edited"
  cp "$WORK/edited" "$WORK/scripts/$w-agent.sh"
  check "S18 $w effort missing" red "$w-agent.sh lost roster effort resolution"
done
echo "== S19 Codex falls back to a saved effort"
build_clean
sed '/^args+=( -c "model_reasoning_effort=/d' "$WORK/scripts/codex-agent.sh" > "$WORK/edited"
cp "$WORK/edited" "$WORK/scripts/codex-agent.sh"
check "S19 Codex effort flag missing" red 'no longer passes model_reasoning_effort unconditionally'

echo "== S20 the compact brief doc loses its verbose escape hatch"
build_clean
# A wrapper-only fixture has no fleetctl. Add it to exercise the brief contract.
touch "$WORK/scripts/fleetctl.py"
cat >> "$WORK/SKILL.md" <<'EOF'
pool | level | binding window | used | resets | price | models on
off: none
pick: fleetctl.py select --role R [--stakes S]
price is selector lambda
brief --verbose
EOF
check "S20 compact brief docs baseline" green
sed '/brief --verbose/d' "$WORK/SKILL.md" > "$WORK/edited"
cp "$WORK/edited" "$WORK/SKILL.md"
check "S20 compact brief docs drift" red 'compact brief contract drifted'

/bin/rm -rf "$WORK" 2>/dev/null
echo
echo "sabotage results: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
echo "EVERY INVARIANT PROVEN ABLE TO FAIL"

}
main "$@"; exit $?
