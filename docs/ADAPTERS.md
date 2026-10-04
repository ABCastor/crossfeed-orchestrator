# Adapter contract

An adapter runs a model through a command-line tool, an Agent Client Protocol (ACP) server, or an HTTP endpoint. It gives the caller one final response on stdout, diagnostics on stderr, and separate files for events and model identity.

This document defines the frozen interface for new adapters. Existing `*-agent.sh` wrappers retain their own flags and exit mappings; inspect their `--help` before calling them. There is no generic `adapter` executable or manifest loader yet. Pi implements the run flags below as `scripts/pi-agent.sh run`; [effort configuration](EFFORT.md) describes the roster's existing policy fields.

## Run interface

```text
adapter run --lane ID --prompt-file F --dir D \
  --effort {low|medium|high|xhigh|max} --mode {ro|rw} \
  --idle S --wall S --events F --last F
```

`ID` selects an admitted lane in the access overlay. `F` and `D` are caller-supplied paths. Time limits are seconds; zero disables that limit. A lane must declare its supported modalities and execution modes before launch. `ro` means the adapter applies the harness's native read-only permissions or tool restrictions; `rw` allows the declared write mode. State the restriction's limits, especially when a tool filter does not provide filesystem isolation.

On success, stdout and `--last` contain only the final response. A JSON-schema run preserves the worker's original JSON, including its top-level shape. Progress, tool output, warnings and the human `Crossfeed model receipt:` line go to stderr or the events file. Refusals and failed runs publish no successful final response. Help and dry-run output are outside the run-result contract.

| Exit | Meaning |
| --- | --- |
| 0 | Successful terminal event and nonempty final response |
| 2 | Invalid usage |
| 3 | Lane, mode or modality rejected |
| 4 | Quota lease refused or unavailable |
| 5 | Provider or authentication-policy error |
| 6 | No terminal event |
| 7 | Empty final response |
| 124 | Wall-clock limit reached |
| 125 | Idle limit reached |
| 127 | Missing dependency |

Map native exit statuses into this table explicitly. A native exit zero proves only that the process exited: require a successful terminal event where the transport supplies one, then check the final response. Existing wrappers also use exit 8 for receipt persistence failure or invalid schema JSON. Inspect diagnostics and the sidecar: invalid JSON can have a recorded failure receipt; a persistence failure means the run is unaccounted.

## Lane manifest

Declare these fields for each adapter lane. This is a contract checklist for implementers, not a new executable JSON schema. The current access overlay uses `lane_id`, `harness`, `selector`, `model_key`, `quota_pool` and companion policy sections; extend those deliberately when adding a manifest loader.

| Field | What to declare |
| --- | --- |
| `id`, `kind` | Unique lane ID; `cli`, `acp` or `http` |
| Transport | Command and argument template for CLI/ACP, or base URL for HTTP; dependency and minimum CLI version |
| `auth` | Official login, API-key reference or none; terms class `first-party`, `named-partner`, `api-only`, or explicitly recorded `owner-accepted` relay risk |
| `models` | Native and canonical IDs, declared aliases, context size, input and output modalities |
| `effort` | Shape, flag/parameter/config option, supported values and canonical-to-native map |
| `modes` | Read-only and write modes, their native controls and enforcement limits |
| `output` | `text`, `json`, `ndjson` or `acp`; paths to final text, terminal status, native model and usage |
| `exit_codes` | Native status mapping into the frozen table |
| `quota` | Pool ID, units, windows, metering source, peak multiplier and spend limits |
| `liveness` | Progress signals, tick interval and idle default |
| `capabilities` | MCP, skills, subagents, session resume and JSON schema support, each stated explicitly |

Keep credentials outside the manifest. A 1Password reference uses the vault ID, for example `op://<vault-id>/<item>/credential`. Read key values through protected files, stdin or the process environment; never put a value in diagnostics, argument logs, prompts, receipts or examples.

## Effort mapping

The caller supplies `low`, `medium`, `high`, `xhigh` or `max`. Every lane declares what that means in its native transport. Reject an unsupported mapping before spending quota; if a lane deliberately maps a higher level to a native ceiling, report that ceiling to the caller. Keep canonical `effort` for selection-receipt validation and record a separate `native_effort` when the values differ.

| Shape | Native control | Required mapping |
| --- | --- | --- |
| `enum` | Discrete value such as `reasoning.effort` or Pi `--thinking` | One declared value for each admitted canonical level |
| `budget` | Token count such as `thinking_budget` | Explicit integer budget per canonical level, with native bounds checked |
| `toggle` | Thinking on/off | Explicit boolean per canonical level; do not imply several distinct native levels |
| `none` | No exposed control | State that effort is service-chosen; do not claim enforcement |

Pi maps `low`, `medium`, `high` and `xhigh` directly; canonical `max` maps to native `xhigh` with a diagnostic. Its receipt keeps `effort: max` and `native_effort: xhigh`. Native Pi overrides `off` and `minimal` are outside the canonical enum. In ACP, map effort to the agent's advertised option ID through `session/set_config_option`.

## Lease, liveness and cancellation

Acquire the quota lease before the provider starts. Hold it until the entire child process group has stopped and usage has been recorded. Release it on completion, errors and cancellation; never release while a descendant can still consume quota. Refresh a finite lease during long runs. Apply the pool's declared windows, daily spend cap and any peak multiplier when metering consumption.

Launch the worker in its own process group and track that group's identity. Declare the adapter's progress signals: native events, changing output, aggregate CPU progress from the worker and its descendants, or a combination. A wrapper heartbeat keeps supervision alive but does not reset the idle timer by itself. The legacy CLI wrappers also watch aggregate CPU progress. Pi currently resets idle on growing event/stdout/stderr output; quiet CPU work can still reach its idle limit. Choose a suitable idle interval or disable it with `--idle 0` for that workload.

On a wall timeout, idle timeout or external cancellation, cancel the native session if available. ACP sends `session/cancel` first. Send TERM to the whole process group, wait the configured grace period, then send KILL to every surviving member. Keep supervising after the leader exits if descendants remain. Preserve partial events and the failed-run receipt, and report the original timeout or cancellation outcome.

## Events, receipts and usage

The events file carries native events. Pi redacts the selected credential and adds `crossfeed.liveness` ticks. The shared helper writes a prompt-free `crossfeed-model-run/v1` record to the run ledger and, when `--last` is supplied, to `<last>.crossfeed.json`. The record includes requested and selected identities, the native selector, `actual_model`, `identity_source`, run and lane IDs, quota pool, effort, timestamps and return code. Once receipt initialization succeeds, a failed run gets a fresh sidecar even when an older result file remains on disk. A preflight refusal starts no run and leaves any previous sidecar untouched.

Populate `actual_model` only from native provider metadata. Answer text such as "I am model X" proves nothing. Where native identity is unavailable, leave it null and report the selection as unconfirmed. The helper compares declared aliases and provider prefixes while preserving real model versions. A static selection that resolves to a different model prints `WARNING: MODEL IDENTITY DRIFT` on stderr. Auto and service routers intentionally resolve a native model and are exempt from this mismatch check. Doctor checks recent static identity drift with the same helper.

Record native token counts and cost when supplied, with their source. Missing usage stays unknown; it is never invented as zero. Pi reads assistant `message_end.message` and `agent_end.messages`, counts each assistant message once across both forms, and records `tokens` plus `cost.estimated_usd` from native `usage.cost.total`. The quota controller uses the recorded pool and cost for metered daily caps. Native estimates are not authoritative billing.

## Authentication gate

Authentication permission belongs in executable checks before launch. An API key and a subscription login can have different tool permissions even for the same model. Admit first-party subscription authentication only through the official client; admit a named partner only when its plan explicitly lists that client. Unclear permission means refusal until the lane is reviewed.

Crossfeed Chat supplies the `chatgpt-chat` harness through a separate `subscription-relay` route with terms class `owner-accepted`: a dated owner quote accepts account risk, without claiming vendor permission. Its adapter requires that record and a worker-health preflight, sends strict text-only requests with `tool_choice: none`, and permits only read-only mode. The server's `/v1/models` saved labels, model rows and numeric levels from 0 to 4 determine `chatgpt:<label>` lanes and selectors. Actual provider identity and usage stay unknown. See [ChatGPT setup](CHATGPT.md).

Pi refuses Anthropic subscription OAuth, OpenAI Codex ChatGPT sign-in and Gemini CLI OAuth provider routes with exit 5. Its `google` API-key route is separate from Gemini CLI OAuth. Z.ai, MiniMax and OpenCode Go lanes require their allowed plan/API keys. A secret key's presence does not prove its authentication class. State the class in the lane and enforce the route before invoking Pi. [Pi's provider documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md) describes native setup; verify the provider's current terms before admitting a new route.

## Add a harness

1. Declare the lane's transport, model identities, authentication class, modalities, effort map and quota pool. Verify current provider terms and minimum CLI versions from primary documentation.
2. Implement prompt-file reading, working directory, native permissions, effort mapping and supervised process-group launch. Parse final text, terminal status, native identity and usage from explicit event fields.
3. Connect the shared receipt helper's `begin`, `prompt` and `finish` stages. Keep the final response separate from stderr diagnostics, event artifacts and the receipt sidecar.
4. Test with a fake binary or an intercepted HTTP transport. Cover exact stdout, unchanged schema JSON, native identity and aliases, missing terminal events, empty output, usage aggregation, authentication refusal, lease rejection, progress, idle/wall limits, cancellation and surviving descendants.
5. Run `bash tests/run-all.sh`, then deliberately break and restore two guards to prove their tests detect failure. Add the lane only after those checks pass. A synthetic transport test proves the adapter contract; a separately authorised live run proves provider availability.
