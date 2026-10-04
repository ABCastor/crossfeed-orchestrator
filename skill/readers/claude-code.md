Load once per session when Claude Code is the lead harness.

# Claude Code reader

Use another vendor for independent review/council seats, passing `--exclude-lineage anthropic` to select/dispatch. Never run claude-agent.sh as this session's independent reviewer or external copy of itself. Native Claude agents share lineage. Implementation-pool preferences belong in applicable live policy; do not assume the selector encodes them. Keep bulk implementation external when that policy calls for it, and explain work that genuinely needs Claude's own tools.

Track external wrapper calls with Bash run_in_background where available, leaving the wrapper foreground inside that tracked call. Native Agent calls are tracked by the harness. Never nohup, detach with ampersand/disown/setsid, or apply an outer timeout. Name jobs by actual selected lane/model and plain task; report confirmed receipt identity after completion.

Plain background subagents fit independent reads where only a digest matters. Teams fit related disjoint units needing the harness's tools and actual peer debate/steerability. Teams need `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`; check it is set before planning one. One team per session; cost scales with full teammate contexts and teammates do not survive resume. The Agent tool cannot pin thinking effort per spawn; use a wrapper for an enforced level.

Teams use named Agent teammates, SendMessage, TaskCreate/List/Get/Update and ListAgents where available. The teammate name is its address; messages resume completed teammates where supported. Teammates address the lead as main. Assign scope, files, report path and proof command in the cold-agent brief itself. Before relying on shared-board claiming, have a teammate call TaskList: that tool did not reach all teammates in the source's tested harness. Fall back to brief assignment if absent. Use TaskUpdate addBlockedBy for dependencies when available; otherwise serialize dependent launches yourself. Plain output is not peer messaging.

Permission boundaries are per-session: never hand a teammate work your session was denied or hook-blocked. Same-file work has one writer; partition disjoint files or serialize. Lead verifies every diff and acceptance command. If a native agent stops with an orphan runner, message it to terminate or use TaskStop and inspect cleanup.

Claude Code runs `scripts/hooks/model-switch-guard.py` (refuses a switched-off model) plus any live detach and headless-confinement guards the install adds. Other harnesses do not inherit those guards; run the actual checks. Hook presence is not proof a specific invocation was checked.
