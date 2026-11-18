Load before any OpenRouter completion or when debugging pricing, cash reservations, streaming or identity.

# OpenRouter lanes

One toolless text completion: no repository, files, attachments or web search. Use complete public/synthetic prompt-contained material, never private notes, memory, personal/client data, credentials or unreleased project facts. A router label is not proof of provider identity or retention.

The default free route checks the live catalog before sending: both input and output prices must be present, all fields must be zero and output must be text. Free transport is pinned to OpenRouter, refuses redirects and cannot send a configured metered lane. A reported charge is terminal (exit 8): the exact selector is frozen until the owner reconciles the incident. A zero catalog quote does not prevent a provider from reporting an unexpected charge. Keep credentials in the owner-approved protected source; never put them in a prompt or report. Transient free 429/5xx failures may retry at the transport. Paid requests never retry. The idle watchdog watches stream bytes; default 0 is diagnostics only. Never add an outer timeout or detach.

## Bounded paid text (disabled by default)

The current release grants no new spending. The former provisional MiMo $5/day default is not authorization. See [cash setup](../../docs/SELECTOR.md#cash-funding-and-owner-envelopes) before provisioning. The owner must name the billing account, set a per-call/daily/pilot envelope and expiry, approve the exact prompt hashes and initialize the canonical ledger. Compatible endpoints must enforce the same pricing and stream contract. A key alone proves nothing.

MiMo remains limited to `xiaomi/mimo-v2.6-pro` and `xiaomi/mimo-v2.6-flash` for `hard-reasoning` and `long-context`. The following forms show the flags after the owner approves and configures the route and prompt; they do not grant permission:

```bash
FLEET_DATA_CLASS=public scripts/openrouter-agent.sh --lane openrouter-mimo-v2.6-flash \
  --paid-mimo --prompt-file /path/approved-public-task.txt --max-tokens 512
python3 scripts/fleetctl.py select --data-class public --role hard-reasoning \
  --allow 'openrouter-mimo:mimo-v2.6-flash:*' --json
```

Selection needs the separate automatic grant, measured family fit and the exact MiMo allow entry. Before any request, the wrapper checks the exact public text against the owner digest and pins the endpoint, upstream provider and model. It rechecks the tariff, reserves the full bounded input context plus maximum output and sends one request without redirects or fallback.

Use the cash fields and reservation ID in the receipt. Ambiguous or interrupted calls retain their holds across midnight. A valid final cost settles once, on the settlement day in the envelope's timezone (default UTC). An overrun freezes the account. Do not clear a hold or recreate a missing ledger; reconcile against provider activity with the owner. No fallback into or out of a paid dispatch. See [business rules](../../BUSINESS-RULES.md).

Exits: 3 lane/credential rejection; 4 lease failure; 5 free gate, data, tariff or cap refusal; 6 transport or final-usage failure; 7 empty answer; 8 free-route billing incident (no retry); 124 wall timeout; 125 idle kill. Exit 0 is a completed response, not a task-quality check. Report exact confirmed identity or selected identity as unconfirmed.
