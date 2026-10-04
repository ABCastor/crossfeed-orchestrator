Load after a killed or hung run, or before editing a wrapper or dispatch supervisor.

# Liveness and recovery

The idle watchdog defaults to 2400 seconds. CLI wrappers watch output growth or process-group CPU; streaming HTTP watches arriving bytes, including keepalives and reasoning deltas. The default has margin over 134 historical Codex runs' longest observed gap of 1074 seconds; that sample does not establish every lane's event cadence. Recheck against real transport events when behavior changes.

An idle kill on a lane with thin gap history (agy, Copilot, OpenCode) is a reason to re-measure that lane, not proof the job hung.

No outer `timeout` or `gtimeout`. Most agent wrappers have no default wall clock; OpenCode deliberately retains lane `timeout_s` because its lease is not renewed atomically. Use explicit wrapper `--timeout` only when a hard deadline is intended. Native Gemini media has its own documented budget and transport contract, not the agent-wrapper contract.

Supervision sends TERM, then KILL after `--kill-after` (default 30 seconds), to the whole process group. A bare gtimeout can hang on a child that ignores TERM. Exit 124 means wall budget, 125 idle watchdog. Both may leave partial edits. Inspect `git status`, incremental reports and event logs before relaunching.

Never detach a dispatch with nohup, trailing ampersand, disown or setsid. Keep the wrapper foreground inside the harness's tracked call; see your reader file. A tool completion only proves the process it tracked ended. Check no unwanted descendants or orphan workers remain before releasing ownership. For a stopped native agent with a surviving runner, message it to terminate or use the harness stop tool and inspect the process state.

For a manual probe outside restrictive sandboxes, inspect elapsed/CPU time for the actual worker process group and recent writes in its working directory. Growing CPU or delivered output is evidence of liveness; low local CPU alone is not evidence a server-side reasoning run is stuck. Use unique `--events`/`--last` paths where supported. Never print secrets from logs.

Recover by explicit session ID when available. `codex exec resume --last` is best-effort and can identify the wrong concurrent session; it is not a recovery plan. Persist reports inside `--dir`, create them first and append results as they arrive. Preserve failure evidence before retrying, without moving or deleting unnamed files.

## Maintainers

After any `scripts/` change, run `bash tests/run-all.sh` and read each suite's verdict. `check-dispatch-invariants.sh` checks no literal/computed/defaulted wall cap, timeout escalation, real watchdog launch sites, exit-code-based suppression, incomplete/refused campaign statuses, documented idle default/compact brief and effort resolution. `sabotage-check.sh` reintroduces defects to prove the scanner can fail. Text checks are not proof a watchdog fires: fake-CLI/local-server suites exercise 124/125, TERM-to-KILL and process-group cleanup. Report sandbox failures by exact test, never as passing coverage.
