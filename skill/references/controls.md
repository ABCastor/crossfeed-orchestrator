Load when the owner changes a spend level or model switch, a stand-in appears, or pins look wrong.

# Controls

Run `python3 scripts/fleetctl.py brief` before planning. `brief --verbose` gives limits, resets, plan details, pins and effort warnings; `brief --json` retains the complete data. The compact table's binding window is the window the selector prices, not necessarily the largest used percentage. Price is quota scarcity (lambda), not subscription cost; `-` means the renderer lacks selection context, and a marked fallback means a projection is unknown.

Spend levels and switches change only on the owner's word. Never switch a model on yourself. A shortage is not authorization to undo a limit.

## Owner spend profiles

On the owner's instruction, apply the matching preset with `fleetctl.py profile NAME --because "<owner's words>" --who "<agent identity>"`, then report the command's exact changes in one line. Each changed pool records who, when and the phrase in local runtime `profile_changes`. No-op profiles report `no level changes`.

| Owner phrase or intent | Profile | Levels |
|---|---|---|
| "Claude will orchestrate, use as little Claude as possible, external agents for everything" | save-claude | Claude low; Codex, Antigravity Gemini and ChatGPT Work high |
| "Save Codex, use the others" | save-codex | Codex low; Claude, Antigravity Gemini and ChatGPT Work high |
| "Use the normal balance" | balanced | Every declared pool normal |
| "Use maximum quality" | max-quality | Every subscription/free pool high; metered/paid pools unchanged |
| "Reset spend levels" | reset | Every declared pool normal |

Presets touch only declared pools. An off pool stays off unless `--because` explicitly names its pool identifier or an unambiguous provider name. "Use others" never lifts off. Use `antigravity-gemini` or "Antigravity Gemini" when Gemini has multiple funding pools; "ChatGPT" alone cannot identify Codex versus ChatGPT Work. Model switches are unchanged.

## Lead preservation

`brief`, `select` and `dispatch` accept `--lead POOL`; explicit selection wins. Detection uses a live `CODEX_THREAD_ID` first, then `CLAUDECODE`/`CLAUDE_CODE_*`, then Pi funding/provider metadata, then generic `CODEX_*`. This avoids treating inherited `CODEX_HOME` as stronger than Claude's active marker. Mixed inherited environments can still be ambiguous: pass `--lead` for the current lead. Pi supports `PI_QUOTA_POOL` or known subscription providers in `PI_PROVIDER` (`openai-codex`, `google-antigravity`); other `PI_*` identify the harness but not its funding, so pass `--lead`. Bare API vendors `anthropic`/`openai`/`google` never imply a subscription pool.

When the lead's observed quota is CONSERVE, CRITICAL or EXHAUSTED, or its spend level is low/off, obey the `lead:` directive and say so. Unknown quota alone is not pressure. `dispatch` carries the same lead exclusion as `select`; irreversible stakes bypass only this exclusion, never off, exhaustion, circuits, capacity or paid caps. Unknown readings are reported as unknown, including a lead on low/off with no gauge.

| Level | Meaning |
|---|---|
| off | Refuse that pool |
| low | Cheapest capable routes, one slot across the pool; one-shot quality is still one slot |
| normal | Ordinary quota-aware routing |
| high | No step-down at CONSERVE and no frontier clamp until CRITICAL; never above a lane's declared max_parallel |
| forced | Gauge override only, never an open quota-error circuit or a paid cap |

Claude, Codex and Copilot wrappers enforce off and low themselves: at low each run takes the pool's one slot (`pool-slot`), and a second run waits `FLEET_POOL_WAIT_S` (default 900 s) and then exits 5; for them high is advice only.

Inspect with `fleetctl.py level [pool]`; an authorized change uses `level <pool> <level>`. Older `switch <pool> off|on|auto` maps to off/forced/normal. Use `model-toggle <pool> <model>` to inspect a model; changes require the owner. `model-choice` changes a whole pool's model set, so do not use it as a local dispatch workaround.

The console wins over model names everywhere. `model-run <pool> [model]` resolves direct-wrapper stand-ins; `model-gate <model>` tests routed/media gates, with optional `--harness`, `--provider`, `--pool` and `--reason-only` filters/output. Codex and Claude can replace a switched-off requested model with an enabled stand-in; other transports may refuse instead. Report `actual_model` only when confirmed by the model receipt. If it is null or unconfirmed, report the selected model as selected, with that uncertainty. An Auto/free router's underlying identity must never be guessed. `--last` receives a `.crossfeed.json` sidecar; stderr carries receipt diagnostics, stdout carries final text. Codex schema mode preserves original JSON unchanged on stdout and in `--last`; the sidecar holds its receipt.

`pins status` inspects declared model pins, `pins sync` updates managed pins with backups, and `pins undo` restores them; each supports `--json`. App-owned files are observed, not rewritten behind the app.

## Older-model retention

An older model is eligible only when its card records a comparative advantage for a named job against a current model runnable on the same funding pool. It never stands in for a switched-off current model. Generic best_for text and "still served" are not retention evidence. Owner off switches remain off after a reason is added.

The model card's older_model_reasons object holds one record per funding pool:

```json
"older_model_reasons": {
  "<pool-id>": {
    "job": "<specific job>",
    "advantage": "faster",
    "compared_to": "<current model on this pool>",
    "reason": "<observed comparative advantage>",
    "evidence": "<path or URL of the named measurement>"
  }
}
```

All five fields must be nonempty. Advantage is cheaper, faster or better; compared_to must name a current model runnable on that same pool. These are owner-supplied records, not benchmarks automatically run by Crossfeed. Recheck when the current model changes: a comparison against a superseded model no longer qualifies. Existing better_than_successor_at records also qualify when capability, this/successor measurements and source are present, and that successor is current on the pool.

`older-models list` reads switches/reasons. `older-models apply --dry-run` previews and `older-models apply` persists off switches for unjustified older models; both support `--json`. Apply never enables a model or edits the roster. Adding a reason does not authorize an agent to turn a model on.

## Console

`fleetctl.py console` opens the signed-in local page. It binds loopback, uses a launch secret exchanged for an HttpOnly SameSite=Strict cookie, validates Host and form origin/token, and invalidates the link when stopped. Do not publish the sign-in URL or its secret.
