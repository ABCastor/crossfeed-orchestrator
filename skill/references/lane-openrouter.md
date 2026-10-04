Load before an OpenRouter free completion or when debugging free-only pricing, streaming or identity.

# OpenRouter free lane

This transport is a single toolless raw-HTTP completion: no repo, files, attachments or web search. Put the complete public/synthetic material in the prompt. It can serve text-only reasoning/long-context roles, not jobs requiring implementation, file inspection, review or web discovery tools. Fanout deliberately does not pass dir to imply nonexistent filesystem reach.

Public material only. Never send private notes, memory, personal/client data, credentials or unreleased project facts. Unknown retention/operator identity is not a privacy guarantee, especially for a large long-context paste. Credentials stay in the protected configured key file, provisioned through the owner's secret store, never read aloud or copied into the prompt.

Before every call the wrapper checks the live models price list and refuses unless every pricing field is zero and output is pure text. Roster labels are not sufficient: a temporary preview can start charging. Do not add a paid route as an automatic fallback. The owner may separately maintain an account hard cap as a backstop, which is not a reason to skip the free-only gate.

Requests stream. The idle watchdog watches SSE bytes, including keepalives/reasoning, with the same 2400-second default; a nonstreaming wait would turn an idle check into a hidden wall clock. Bounded transient 429/5xx retries are transport retries under the caller's deadline, not permission for duplicate task calls. Shared upstream capacity can fail while the account is idle. Keep declared concurrency, generally one.

Router or anonymous model labels do not establish underlying identity or lineage. Report the receipt's confirmed actual model, or selected identity as selected/unconfirmed. Never invent an identity or count unresolved lineage as independent confirmation.

Exits: 3 rejected lane/missing key; 4 lease failure; 5 live free-only/control refusal; 6 API/network failure; 7 empty output; 124 explicit wall budget; 125 idle kill. A blank completion is not success. Never add an outer timeout or detach; preserve incremental report material in the lead's authorized root.
