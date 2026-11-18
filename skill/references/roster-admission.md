Load when installing on a new machine, declaring quota sources, or admitting a new lane/vendor.

# Setup and lane admission

Install only transports the operator can use. `python3 scripts/fleetctl.py doctor` reports binaries, adapters and missing prerequisites. Engine requirements include Python 3.9+, Bash and jq. Do not silently substitute an SDK, proxy or paid API key for a missing CLI.

| Transport | Intended authentication |
|---|---|
| Claude Code | `claude auth login` |
| Codex | `codex login`, ChatGPT mode |
| Antigravity | Run agy and sign in to its Google product |
| OpenCode Go | `opencode auth login`, choose Go subscription |
| Copilot | Official `copilot login`/existing supported Keychain login |
| OpenRouter | Documented API key file, free-only wrapper |
| Native Gemini media/image | Explicitly authorized metered key transport |

Keep credentials outside worker directories and source repos, in the operator's secret store; provision via stdin/protected file, never print values. OpenCode Go's key is subscription-scoped, not extracted OAuth from another vendor. API-key presence alone says nothing about billing. Recheck current product terms before sustained automation. No proxy, raw consumer-token endpoint, borrowed login or API fallback.

The local overlay owns lanes, role rankings, switches, pool boundaries and evidence. Respect its configured location (ACCESS_OVERLAY or the engine's config default). Do not depend on another person's roster. Catalogue visibility is not an entitlement. `provisional` quality evidence is different from operational admission: access, tool mode, isolation, capacity and budget must be verified independently.

Before admitting a route require: a verified entitlement, documented/product-backed headless transport, a useful distinct role, capability and mode evidence, an explicit budget/stop rule, confinement appropriate to the task and a real-call sentinel. Record exactly what was tested; a saturated fixture ranks nothing and an advertised modality is not delivered transport evidence.

Before recording that a model cannot do something, run the vendor CLI directly without the wrapper; if the bare CLI can, the limit is ours.

An adapter must accept bounded prompts/working roots and supported controls, print final text on stdout and diagnostics on stderr, preserve wrapper-specific status, provide liveness supervision and model receipts, enforce switches/quota/caps, and never log prompt/response/secret payload in the run ledger. Add allowlisted fanout support explicitly; a new `*-agent.sh` alone does not establish safe task admission. Run fake-CLI behavior tests plus a bounded real sentinel on the authorized account.

`doctor` currently checks adapter drift, not skill source drift. Installation and the required future source comparison are specified in engine `docs/SKILL-INSTALL.md`; do not claim that skill drift check already exists.

## Model release checklist

A release is complete when the new model's exact route is proven, its evidence is recorded, and the old route behaves as intended. Apply this checklist when the operator asks to add, replace, or archive a model. Do not use a picker listing as proof that a worker ran.

1. **Inventory before changing anything.** Record the exact provider IDs, account and funding pool, supported effort levels, modalities, and current defaults. Inspect live cards, lanes, aliases, role rankings, switches, managed pins, and any separate harness helper shortlist or fallback. Distinguish a manual favourite from an automatic helper route.
2. **Collect outside evidence.** Use the benchmark source guide and check Artificial Analysis by exact model and effort, then Vals or another independent source. Check task-specific benchmarks for every proposed role. Record the benchmark version, harness, effort, date, uncertainty, latency and price where supplied; mark missing rows and failed feeds. API prices are not subscription costs. Store a dated dossier and keep restricted figures host-local. Never lend a predecessor's score or one preset's score to another effort.
3. **Run the small admission check (BR-BENCH-001).** Use a handful of synthetic tasks with objective checks at the intended effort on the exact transport the helper will use. Save the command, exit, observed model identity, timings and receipts. For a tool/read/write/media claim, exercise that capability and confinement too. A text smoke through one harness does not admit the same model through another. Keep unverified routes candidate-only and keep role rankings unchanged unless the evidence supports a change. In a direct pool, adding a runnable model card can make it selectable even when the separate catalog lane is still a candidate. Check the real selector before declaring either state.
4. **Roll out and archive deliberately.** Keep unrelated defaults and spend levels unchanged. For an operator-approved archive, persist the `owner` off control, archive the dossier without discarding its evidence, update stale policy references, sync managed pins and remove it from applicable helper shortlists/fallbacks. Do not use a vendor-withdrawal date for an operator archive. Check that refresh and a recovery restore cannot silently bring the old route back.
5. **Verify both directions.** Validate the live and recovery rosters, prove a named replacement resolves to itself, and prove a named request for each archived ID cannot start that ID. Check an explicitly constrained selector/dispatch and the managed pins and applicable harness helper registry; use dry-run or an isolated fixture for the negative case. Keep catalog visibility, successful live calls, automatic routing, and picker favourites separate in the hand-back. Report any incomplete leg and keep it open in the project home.

## Daily catalog refresh and free promotions

The existing `scripts/market-refresh.py` daily job also reconciles installed provider catalogs. New static routes enter as unverified candidates with no roles, pending the small admission check required by BR-BENCH-001. Discovery does not change switches, spend profiles, effort controls or entitlement. AGY models with unknown funding go into the candidate-only `antigravity-catalog` pool. Gateway saved labels keep their existing live expansion path; underlying models remain unconfirmed. Native Copilot remains Auto; its OpenCode catalog is visibility evidence only.

Daily automatic ordering covers only `default`, `repo-map` and `research-scout` OpenCode Go and free-promotion lanes that are already active and verified. A standard helper call is 8,000 input plus 2,000 output tokens. Qualified zero-cost models lead; priced models follow by AA intelligence per helper-call dollar. Default bounds are AA intelligence at least 40, helper cost at most $0.01 and at most two changed role names per local day, including repeated runs. `policy.model_refresh` can tighten `aa_floor`, `helper_usd_cap` and `max_roles_per_day`. Missing scores or route prices do not qualify. Outside scores remain host-local priors. Vals is recorded when supplied in the joined evaluations; no additional paid source is queried.

A failed catalog preserves its routes. Retirement removes routing/council references; if that would empty a council band, validation refuses the whole transaction and the market report records the refusal. Every changed overlay receives a backup and validation before and after atomic replacement. The daily counter is part of that same write.

Free promotions default to `data_policy: public-only`. Tasks default to private. There are three material classes, least to most sensitive: `public`, `code` (private project source code and its docs, with no notes, memory, personal or client data, or secrets) and `private` (everything else). A lane's `data_policy` says how far up it may go: `public-only` takes public only, `code-ok` takes public and code, `private-ok` takes all three. `fleetctl.py route`, `select` and `dispatch` accept `--data-class public` only when all material is public, and `--data-class code` only when all material is project code with nothing from notes, memory, personal or client data, or credentials. Direct wrapper callers use `FLEET_DATA_CLASS=public` or `FLEET_DATA_CLASS=code`. A public-only OpenCode lane requires `--direct`, which disables tools, project configuration, external skills, attachments and injected context. A code-ok lane does not need `--direct`: it may work in the project workspace, so point it at code only. Repo-map tasks requiring repository access therefore use a code-ok or private-ok lane. The operator may approve `data_policy: code-ok` or `private-ok` per model in the overlay; refresh preserves that value. Approving code-ok for a free model accepts the risk that the code lands in the provider's training data.
