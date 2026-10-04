Load only when overriding the thinking level returned by select or debugging a level refusal.

# Thinking-level overrides

The overlay's `effort` block is authoritative for supported controls, per-role levels, evidence dates and recheck triggers. Selector evidence is per model and level; do not replace it with a fixed model ranking. `select --json` shows choice/top-three reasoning and receipt paths. `--family` selects a task/evidence family, not provider or lineage exclusion.

| Wrapper | Control |
|---|---|
| codex-agent.sh | `--reasoning <level>`, `--role <role>` |
| claude-agent.sh | `--effort <level>`, `--role <role>` |
| agy-agent.sh | `--effort <level>`, with lane/role selection |
| opencode-agent.sh | `--variant <level>`, `--effort-role <role>` |

Resolve with `fleetctl.py effort <model> <role> --harness <harness> --explain`. A supported explicit level wins. An unsupported explicit variant refuses; a console stand-in resolves its own controls and may clamp an override with announced provenance. Missing required controls refuse; unavailable controls are a logged exception, not a silent assumption. Wrappers announce provenance and pin flags rather than relying on app defaults.

Recheck evidence after a family, harness or index change, or its recorded age trigger. Doctor/verbose brief name stale evidence, unsupported levels and knee drift. Model receipts describe resolved effort as well as identity. Never edit an app-owned config to enforce a dispatch override. Claude native Agent cannot pin effort per spawn: use the wrapper when enforcing a level matters.
