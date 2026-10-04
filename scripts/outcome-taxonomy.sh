#!/usr/bin/env bash
# Shared wrapper-consumer outcome taxonomy.
#
# fanout.sh, swarm.sh, and afk-run.sh source this file. Native wrapper exit codes
# are producer-scoped: 3, 4, and 5 do not have fleet-wide meanings. Consumers
# must map (producer, native code) here before branching.
#
# Normalized task outcomes:
#   SUCCEEDED                 usable deliverable, clean wrapper completion
#   SUCCEEDED_WITH_WARNING    usable deliverable, non-clean ancillary outcome
#   FAILED                    no usable completion; reason is mandatory
#   KILLED_WALL_CLOCK         native exit 124
#   KILLED_IDLE_WATCHDOG      native exit 125
#   SKIPPED                   consumer suppressed dispatch; reason is mandatory
#
# Only WRAPPER_OUTCOME_SUPPRESS_POOL=1 may suppress later work. It is set for an
# unambiguous producer-specific quota contract, never for matched stderr text.
# Quota-looking text is retained only as WRAPPER_OUTCOME_SECONDARY diagnostic data.
#
# Consumer boundary exits used by swarm when it invokes fanout:
#   0 complete; 1 incomplete work; 2 usage; 4 campaign refused, nothing dispatched;
#   5 downstream control failure; 124 wall-clock kill; 125 idle-watchdog kill;
#   other downstream codes are preserved as-is.
# 1 vs 4 is load-bearing: 1 means tasks RAN and some did not finish, so read summary.tsv;
# 4 means no task ever started, so there is no summary.tsv and no partial answers to salvage.

outcome_has_deliverable() {
  [ -f "$1" ] && LC_ALL=C grep -q '[^[:space:]]' "$1" 2>/dev/null
}

classify_wrapper_outcome() {
  local producer="$1" rc="$2" deliverable="$3" stderr_file="$4"
  WRAPPER_OUTCOME_CLASS="FAILED"
  WRAPPER_OUTCOME_REASON="${producer}-native-cli-failure"
  WRAPPER_OUTCOME_SECONDARY=""
  WRAPPER_OUTCOME_SUPPRESS_POOL=0

  # The exit-code guard is load-bearing. This text is metadata only and is never
  # consulted when deciding whether another task may run.
  if [ "$rc" -ne 0 ] && [ -f "$stderr_file" ] && grep -Eiq '429|quota|RESOURCE_EXHAUSTED|usage limit' "$stderr_file"; then
    WRAPPER_OUTCOME_SECONDARY="quota-text-observed"
  fi

  case "$rc" in
    0)
      if outcome_has_deliverable "$deliverable"; then
        WRAPPER_OUTCOME_CLASS="SUCCEEDED"
        WRAPPER_OUTCOME_REASON="completed"
      else
        WRAPPER_OUTCOME_REASON="missing-or-blank-deliverable"
      fi
      return
      ;;
    124)
      WRAPPER_OUTCOME_CLASS="KILLED_WALL_CLOCK"
      WRAPPER_OUTCOME_REASON="wall-clock-timeout"
      return
      ;;
    125)
      WRAPPER_OUTCOME_CLASS="KILLED_IDLE_WATCHDOG"
      WRAPPER_OUTCOME_REASON="idle-watchdog-timeout"
      return
      ;;
  esac

  case "$producer:$rc" in
    *:2)
      WRAPPER_OUTCOME_REASON="wrapper-usage-error"
      ;;
    *:127)
      WRAPPER_OUTCOME_REASON="wrapper-binary-missing"
      ;;
    agy:3)
      WRAPPER_OUTCOME_REASON="quota-exhausted"
      WRAPPER_OUTCOME_SUPPRESS_POOL=1
      ;;
    agy:4|claude:4)
      WRAPPER_OUTCOME_REASON="empty-output"
      ;;
    agy:5)
      WRAPPER_OUTCOME_REASON="lease-refused-or-no-eligible-lane"
      ;;
    copilot:4)
      WRAPPER_OUTCOME_REASON="invalid-json-event-stream"
      ;;
    copilot:5)
      WRAPPER_OUTCOME_REASON="missing-or-failing-result-event"
      ;;
    copilot:6)
      WRAPPER_OUTCOME_REASON="empty-final-output"
      ;;
    opencode:3)
      WRAPPER_OUTCOME_REASON="modality-not-admitted"
      ;;
    pi:3)
      WRAPPER_OUTCOME_REASON="lane-or-modality-not-admitted"
      ;;
    pi:4)
      WRAPPER_OUTCOME_REASON="lease-refused"
      ;;
    pi:5)
      WRAPPER_OUTCOME_REASON="provider-or-terms-error"
      ;;
    pi:6)
      WRAPPER_OUTCOME_REASON="no-terminal-event"
      ;;
    pi:7)
      WRAPPER_OUTCOME_REASON="empty-final-output"
      ;;
    opencode:4)
      # Exit 4 is historically overloaded inside this wrapper. It can be a quota
      # refusal, lane-capacity exhaustion, or invalid events, so it cannot open a
      # pool circuit safely. Stderr can refine the surfaced reason, never suppression.
      if [ -f "$stderr_file" ] && grep -qi 'invalid JSON event stream' "$stderr_file"; then
        WRAPPER_OUTCOME_REASON="invalid-json-event-stream"
      elif [ -f "$stderr_file" ] && grep -Eqi 'still at capacity|every eligible lane.*busy' "$stderr_file"; then
        WRAPPER_OUTCOME_REASON="lane-capacity-wait-exhausted"
      else
        WRAPPER_OUTCOME_REASON="lease-or-event-failure"
      fi
      ;;
    opencode:5)
      WRAPPER_OUTCOME_REASON="session-error-event"
      ;;
    opencode:6)
      WRAPPER_OUTCOME_REASON="no-successful-terminal-step"
      ;;
    opencode:7)
      WRAPPER_OUTCOME_REASON="empty-final-output"
      ;;
    opencode:8)
      if outcome_has_deliverable "$deliverable"; then
        WRAPPER_OUTCOME_CLASS="SUCCEEDED_WITH_WARNING"
        WRAPPER_OUTCOME_REASON="telemetry-unaccounted"
      else
        WRAPPER_OUTCOME_REASON="telemetry-unaccounted-without-deliverable"
      fi
      ;;
    *)
      if [ -n "$WRAPPER_OUTCOME_SECONDARY" ]; then
        WRAPPER_OUTCOME_SECONDARY="native-exit-$rc;$WRAPPER_OUTCOME_SECONDARY"
      else
        WRAPPER_OUTCOME_SECONDARY="native-exit-$rc"
      fi
      ;;
  esac
}

classify_consumer_outcome() {
  local producer="$1" rc="$2"
  CONSUMER_OUTCOME_CLASS="FAILED"
  CONSUMER_OUTCOME_REASON="${producer}-exit-${rc}"
  # PRESERVE the native code by default and remap only the deliberate collisions handled below.
  # Collapsing every unlisted code to 1 destroyed information callers branch on: a fanout interrupted
  # with 130 reached the caller as a generic failure, which is the same "make it quieter" regression
  # this taxonomy exists to prevent.
  CONSUMER_BOUNDARY_RC="$rc"

  case "$producer:$rc" in
    fanout:0)
      CONSUMER_OUTCOME_CLASS="SUCCEEDED"
      CONSUMER_OUTCOME_REASON="campaign-complete"
      CONSUMER_BOUNDARY_RC=0
      ;;
    fanout:1)
      CONSUMER_OUTCOME_REASON="campaign-incomplete"
      CONSUMER_BOUNDARY_RC=1
      ;;
    fanout:2)
      # Usage only. Preflight refusals used to land here in the doc and on 1 in the code; they now
      # have their own code below, so this string no longer over-claims.
      CONSUMER_OUTCOME_REASON="fanout-usage-error"
      CONSUMER_BOUNDARY_RC=2
      ;;
    fanout:4)
      CONSUMER_OUTCOME_REASON="fanout-campaign-refused-nothing-dispatched"
      CONSUMER_BOUNDARY_RC=4
      ;;
    fanout:3)
      CONSUMER_OUTCOME_REASON="fanout-write-claim-conflict"
      CONSUMER_BOUNDARY_RC=5
      ;;
    fanout:124)
      CONSUMER_OUTCOME_CLASS="KILLED_WALL_CLOCK"
      CONSUMER_OUTCOME_REASON="fanout-wall-clock-timeout"
      CONSUMER_BOUNDARY_RC=124
      ;;
    fanout:125)
      CONSUMER_OUTCOME_CLASS="KILLED_IDLE_WATCHDOG"
      CONSUMER_OUTCOME_REASON="fanout-idle-watchdog-timeout"
      CONSUMER_BOUNDARY_RC=125
      ;;
  esac
}
