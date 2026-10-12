Load before any OpenRouter completion or when debugging pricing, spending caps, streaming or identity.

# OpenRouter lanes

This transport is a single toolless raw-HTTP completion: no repo, files, attachments or web search. Put the complete public/synthetic material in the prompt. It can serve text-only reasoning/long-context roles, not jobs requiring implementation, file inspection, review or web discovery tools. Fanout deliberately does not pass dir to imply nonexistent filesystem reach.

Public material only. Never send private notes, memory, personal/client data, credentials or unreleased project facts. Unknown retention/operator identity is not a privacy guarantee, especially for a large long-context paste. Credentials stay in the protected configured key file, provisioned through the owner's secret store, never read aloud or copied into the prompt.

By default, before every call the wrapper checks the live models price list and refuses unless every pricing field is zero and output is pure text. Roster labels are not sufficient: a temporary preview can start charging. Do not add a paid route as an automatic fallback. The owner may separately maintain an account hard cap as a backstop, which is not a reason to skip the free-only gate.

Requests stream. The idle watchdog watches SSE bytes, including keepalives/reasoning, with the same 2400-second default; a nonstreaming wait would turn an idle check into a hidden wall clock. Bounded transient 429/5xx retries are transport retries under the caller's deadline, not permission for duplicate task calls. Shared upstream capacity can fail while the account is idle. Keep declared concurrency, generally one.

Router or anonymous model labels do not establish underlying identity or lineage. Report the receipt's confirmed actual model, or selected identity as selected/unconfirmed. Never invent an identity or count unresolved lineage as independent confirmation.

Exits: 3 rejected lane/missing key; 4 lease failure; 5 live free-only/control refusal; 6 API/network failure; 7 empty output; 124 explicit wall budget; 125 idle kill. A blank completion is not success. Never add an outer timeout or detach; preserve incremental report material in the lead's authorized root.

## Owner-approved paid MiMo

The owner approved MiMo V2.6 Pro and Flash via OpenRouter in the supplied 13 Oct 2026 lane brief. The provisional shared cap is $5 per local calendar day; the owner may change `quota_pools.openrouter-mimo.daily_usd_cap`. The date above is the approval date supplied by the caller, not the runtime verification date. The two allowed selectors are `xiaomi/mimo-v2.6-pro` and `xiaomi/mimo-v2.6-flash`; variants, routers and other paid models are excluded. This remains a toolless, public/synthetic text lane.

The example overlay declares both routes and their service-chosen effort. Its `auth.key_ref` must refer to the owner's Automation vault by ID in 1Password. Provision the key there; never print it or put it in prompts. The live overlay and installed skill belong to the lead and are not changed by this branch.

Direct opt-in:

```bash
scripts/openrouter-agent.sh --lane openrouter-mimo-v2.6-flash --paid-mimo --prompt "Return PONG" --max-tokens 512
```

Selection and dispatch use the existing allow list to make the paid choice explicit:

```bash
python3 scripts/fleetctl.py select --role hard-reasoning --allow 'openrouter-mimo:mimo-v2.6-flash:*' --json
python3 scripts/fleetctl.py dispatch --role hard-reasoning --allow 'openrouter-mimo:mimo-v2.6-pro:*' --prompt-file /path/public-task.txt --dir /path/work --last /path/work/answer.txt
```

Roles are limited to `hard-reasoning` and `long-context`. `explicit_only` excludes the pool from automatic selection even when it appears in the role's allowed pools. The selected command passes `--paid-mimo`; a paid selector without that flag still faces the free gate. Console switches, capacity and other admission gates still apply.

Before sending, the wrapper reads live pricing and atomically reserves an upper cost bound in the shared state directory's `openrouter-mimo-budget.json`. It reserves the entire advertised context at the input price plus the provider's maximum completion at the output price, rather than estimating prompt tokens. `--max-tokens` can reduce the completion bound for a deliberately tiny task. The request also limits provider prices to the live catalog prices and disables provider fallback. Unknown/additional billing fields or unsupported price tiers refuse admission. Concurrent Pro and Flash calls share the same locked ledger across worktrees that use the same state directory. Do not give real calls separate state directories to evade the cap.

A completed call replaces its reservation with OpenRouter's reported cost and includes that cost in its receipt and run ledger. Paid requests are never automatically retried. An interruption, network failure or missing/invalid cost retains its reservation, including across midnight; the owner reconciles such entries against OpenRouter's activity before releasing them. An unexpected bill above the reserved amount blocks all further paid calls for owner investigation. Reported costs count on the local day the usage arrives; those costs reset at midnight. A previous-day call that settles today therefore still counts against today's cap. The provider's billing and price enforcement remain the external authority; account spending limits can provide a second backstop.

Catalog checked 12 Oct 2026: Pro $0.435 input / $0.87 output per million tokens, cached input $0.0036; Flash $0.14 / $0.28, cached input $0.0028. These are a dated observation, never the admission price. Source: [OpenRouter live model catalog](https://openrouter.ai/api/v1/models); provider bounds: [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection#max-price).

Business policy and executed tests are linked in [the scoped ledger](../../BUSINESS-RULES.md). Paid pricing/cap refusal exits 5; missing/invalid usage or transport failure exits 6, with the reservation retained.
