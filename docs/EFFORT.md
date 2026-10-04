# Thinking levels

Crossfeed resolves a thinking level from `effort.<model_key>` in your access overlay. For models with exposed controls, wrappers pass the level to the CLI on every dispatch, so a saved app setting cannot silently choose it. `route --json` includes `effort` and `effort_reason`; `roster.sh effort <model> <role> --harness <harness> --explain` prints the level, model key and reason as tab-separated fields.

## Configure a model

The shipped effort blocks are examples. They carry no deployment evidence or benchmark history. Verify which controls your CLI exposes, choose your policy, and replace `evidence.source` and `evidence.read_on` with your own dated observation. A missing date produces a doctor warning.

```json
{
  "levels": {"codex": ["low", "medium", "high", "xhigh"]},
  "default": "medium",
  "knee": "high",
  "by_role": {"builder": "high", "implementation": "xhigh"},
  "by_band": {"conserve": {"builder": "medium"}},
  "stand_in_ceiling": "xhigh",
  "refuse_roles": {"critical/builder": "Parallel builders paused at CRITICAL quota."},
  "evidence": {
    "status": "unmeasured",
    "source": "Your CLI control check; quality curve not measured",
    "read_on": "YYYY-MM-DD"
  },
  "recheck": "New model or CLI controls, index change, or 30 days after read_on"
}
```

`knee` names the level whose quality and cost tradeoff you monitor. A measured entry also needs `evidence.index_version` and may carry `evidence.curve.<level>.intelligence_index`. Supported controls and measured quality are separate claims: use `unmeasured` when you have only verified the controls.

Resolution uses a supported explicit override first, then `by_band.<band>.<role>`, `by_role.<role>`, and `default`. A role or `<band>/<role>` in `refuse_roles` refuses an implicit dispatch; an explicit supported override bypasses that effort policy. Console model and provider switches still apply.

## Wrapper controls

| Wrapper | Role flag | Caller override | CLI control |
|---|---|---|---|
| Codex | `--role` | `--reasoning`, or `-c model_reasoning_effort=...` | `-c model_reasoning_effort=...` |
| Claude | `--role` | `--effort` | `--effort` |
| Antigravity | `--role` | `--effort`, or a named Gemini selector suffix | selector suffix and `--effort` |
| OpenCode | `--role`, or `--effort-role` for a named lane | `--variant` | `--variant` |
| Copilot, OpenRouter | service chooses | none | one stderr notice |

Codex resolves `-c model=...` before a console stand-in. `--model` wins over that config, and the last config model wins over earlier ones. `--reasoning` wins over config effort. The resolved effort flag is appended last. If no model is named, Crossfeed reads the configured model, then `policy.effort_defaults.codex` as a fallback. Stand-ins resolve their own role policy and clamp caller levels to their supported ceiling.

Fanout forwards `role` and `effort` to Claude, Codex and Antigravity, and `role` and `variant` to OpenCode. A Codex write task without a role uses `builder`; read-only tasks use `default`. Automatically routed Gemini selectors are remapped to the table's level before admission and capacity checks. An explicitly named model or lane retains its suffix unless `--effort` overrides it, and leases stay on that model's pool.

Missing effort data refuses a wrapper launch. Unsupported explicit levels refuse, including for models without controls. An entry with an empty supported-level list must record `default` and `knee` as `provider-default` or `service-chosen`. OpenCode also logs a provider-default exception when an implicit table level is unsupported; doctor flags that invalid policy. An explicit supported level can run an unlisted direct Codex or Claude model, with caller provenance.

## Recheck evidence

```bash
python3 scripts/fleetctl.py effort-check
python3 scripts/fleetctl.py doctor
python3 scripts/fleetctl.py brief --verbose --no-refresh
scripts/roster.sh doctor
```

Checks cover active routed lanes, explicit-only lanes and current direct models. Evidence warns from day 23, is stale after day 30, and warns on invalid controls, a changed index version, an ambiguous serving snapshot, or a knee score that moved by more than two points. `effort-check` and doctor return nonzero for effort issues; `roster.sh doctor` still checks the provider pickers before reporting an effort failure. Warnings prompt re-evaluation; they do not silently rewrite your policy.

Market refresh retains all effort variants and repeated snapshots in `variants`, provides a per-level lookup in `levels`, and keeps the strongest headline score for existing catalog consumers. Fresh catalog comparisons use data fetched within 30 days.

## Protect a live tool from its builder

A Codex worker inherits its configured MCP tools. Set `policy.codex_mcp_denials` to map server names to tool-source directories, for example `{"browser_tool": ["~/src/browser-tool"]}`. A worker whose directory is that root or a descendant receives `-c mcp_servers.browser_tool.enabled=false` after all caller config, with a stderr notice. The sample map is empty. Add each separately located worktree root to the map too. `fleetctl.py codex-mcp-denials <directory>` previews the matched server names.
