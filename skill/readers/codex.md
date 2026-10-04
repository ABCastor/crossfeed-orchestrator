Load once per session when Codex is the lead harness.

# Codex reader

Never self-dispatch codex-agent.sh as an external worker or count OpenAI as this lead's independent reviewer/council seat. Pass `--exclude-lineage openai` for independent seats. Native Codex subagents can split work but remain same-vendor evidence, even with a fresh context or different model.

Use the harness's tracked execution sessions for external wrappers. If exec_command yields a session ID, retain it and poll with write_stdin until a terminal status; if an orchestration exec tool yields a cell ID, use that tool's wait contract. Keep the wrapper foreground in the tracked call. Never detach or add an outer timeout. Read current output/status, not merely a task-completion notification, before accepting a worker claim.

Use available native agent tools only when delegation is authorized by the task/instructions. Give each one a complete cold brief, named context home, exact ownership and proof command; tell builders they share the codebase and must preserve others' edits. Spawn parallel builds only on disjoint units, use a fresh-context reviewer, then run the actual proving command. Native agents inherit harness boundaries; sending work to another agent never bypasses a refusal.

Managed workspace-write protects .git and can block process inspection or loopback/thread-dependent tests. The lead must distinguish focused sandbox checks from full verification outside it. Never claim an environment-limited suite passed. If the lead also cannot write git, return an uncommitted diff and exact remaining acceptance step rather than claiming integration.

Before rebuilding a server used by a live MCP connection, disable that tool connection for the affected worker root using the available engine/harness mechanism; stale live tools can execute old code. Re-enable only after verifying the replacement. Claude-only hooks do not run here: explicitly carry headless confinement, model-switch and watchdog checks when needed.
