Load before writing tasks.jsonl, using a swarm profile, or wiring orchestration into a project.

# Fan-out

Run from the engine root, not a copied script directory:

```bash
scripts/fanout.sh tasks.jsonl --parallel 3 --out /project/run-unique --dry-run
scripts/fanout.sh tasks.jsonl --parallel 3 --out /project/run-unique
```

Use a fresh output path for each real run. A nonempty `--out` refuses the campaign. Without `--out`, runtime artifacts live below `FLEET_STATE_DIR` (default `~/.local/state/orchestrator`). Dry-run validates/resolves but does not start models; it can still create campaign artifacts, so choose a separate output path.

One JSON object per line; explicit mode makes scope reviewable:

```json
{"id":"map","agent":"opencode","dir":"/project","mode":"read-only","role":"repo-map","prompt":"Map retries, cite files, return a digest."}
{"id":"build","agent":"codex","dir":"/worktrees/build","mode":"write","prompt":"Implement SPEC.md. Run the project test command. Write report.md incrementally."}
```

| Field | Meaning |
|---|---|
| id | Unique safe identifier, 1-64 letters/digits/dot/underscore/hyphen, starts alphanumeric |
| prompt, dir | Complete bounded brief and existing working directory |
| agent | Approved wrapper: claude, codex, agy, opencode, copilot, openrouter or pi |
| mode | read-only default, or write when admitted |
| role | Task role for automatic routing/level resolution |
| lane_id, model_key, model | Explicit lane/model selectors supported by the chosen wrapper; do not combine conflicting selectors |
| modality, file | Declared input modality and attachment path |
| context | OpenCode lean default or shared when justified |
| effort, variant | Supported thinking-level override |
| timeout | Intentional per-task wall budget, never an outer timeout |

Preflight checks IDs, wrapper allowlists, role/modality/attachment admission, switches/circuits, lane caps and disjoint write roots. An inadmissible row rejects the whole campaign before launch. Maximum campaign size is 32, `--parallel` is 1-16, and admitted lane caps can be lower. Fix the reported rejection and validate again; do not silently drop a lens.

Fanout claims write paths across sessions/campaigns, exits 3 on conflict and releases on exit. Direct/manual writers must claim their scopes too; see [isolation](isolation.md). Different artifact paths and ignored databases are part of the partition.

Read exit code before outputs: 0 complete; 1 ran incomplete, inspect `summary.tsv`; 2 bad call; 3 write claim held; 4 refused, nothing dispatched and no summary. Outputs include resolved `manifest.jsonl`, task stdout/error/event files and summary. Read digests and inspect actual edits; a 0 alone does not verify correctness.

`swarm.sh explore|review|media-review` builds curated read-only profiles from the live overlay. Image media-review uses admitted OpenCode transport; audio/video routes to the separately capped native Gemini transport. `swarm.sh research` reserves `chatgpt:latest-pro` in every quota band when the example research profile is present in the overlay. It is text-only, has no known limit, and can take minutes. Crossfeed Chat tasks use `agent: "chatgpt-chat"` and one explicit lane/model selector; no default wall or idle timeout is imposed. Use `--exclude-lineage <lead vendor>` for independent councils. Profile selection can omit unavailable seats while retaining a successful exit: read stderr for `PANEL INCOMPLETE`, count actual lenses and replace missing ones. A swarm is not automatically independent of its lead: exclude the lead lineage by designing an explicit task list when the profile cannot enforce that condition. Copilot is one observer, never swarm capacity.

Project integration: put conventions and proof commands in `AGENTS.md`, continuity in the named context home, and dispatch from the canonical engine. Do not copy wrappers into projects. If tooling must be pinned, use a verified link or versioned package.

For authorized away-window proof loops, `afk-run.sh` is sequential, not a scheduler: objective, admitted lanes, fresh proof command, time/attempt budgets. A worker saying done with a failing proof is not accepted. `rank-routes` reports verified correctness, cost and latency evidence; it does not promote routes automatically. Private away-window policy stays live-only.
