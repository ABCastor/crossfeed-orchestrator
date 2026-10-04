Load when installing on a new machine, declaring quota sources, or admitting a new lane/vendor.

# Setup and lane admission

Install only transports the owner can use. `python3 scripts/fleetctl.py doctor` reports binaries, adapters and missing prerequisites. Engine requirements include Python 3.9+, Bash and jq. Do not silently substitute an SDK, proxy or paid API key for a missing CLI.

| Transport | Intended authentication |
|---|---|
| Claude Code | `claude auth login` |
| Codex | `codex login`, ChatGPT mode |
| Antigravity | Run agy and sign in to its Google product |
| OpenCode Go | `opencode auth login`, choose Go subscription |
| Copilot | Official `copilot login`/existing supported Keychain login |
| OpenRouter | Documented API key file, free-only wrapper |
| Native Gemini media/image | Explicitly authorized metered key transport |

Keep credentials outside worker directories and source repos, in the owner's secret store; provision via stdin/protected file, never print values. OpenCode Go's key is subscription-scoped, not extracted OAuth from another vendor. API-key presence alone says nothing about billing. Recheck current product terms before sustained automation. No proxy, raw consumer-token endpoint, borrowed login or API fallback.

The local overlay owns lanes, role rankings, switches, pool boundaries and evidence. Respect its configured location (ACCESS_OVERLAY or the engine's config default). Do not depend on another person's roster. Catalogue visibility is not an entitlement. `provisional` quality evidence is different from operational admission: access, tool mode, isolation, capacity and budget must be verified independently.

Before admitting a route require: a verified entitlement, documented/product-backed headless transport, a useful distinct role, capability and mode evidence, an explicit budget/stop rule, confinement appropriate to the task and a real-call sentinel. Record exactly what was tested; a saturated fixture ranks nothing and an advertised modality is not delivered transport evidence.

Before recording that a model cannot do something, run the vendor CLI directly without the wrapper; if the bare CLI can, the limit is ours.

An adapter must accept bounded prompts/working roots and supported controls, print final text on stdout and diagnostics on stderr, preserve wrapper-specific status, provide liveness supervision and model receipts, enforce switches/quota/caps, and never log prompt/response/secret payload in the run ledger. Add allowlisted fanout support explicitly; a new `*-agent.sh` alone does not establish safe task admission. Run fake-CLI behavior tests plus a bounded real sentinel on the authorized account.

`doctor` currently checks adapter drift, not skill source drift. Installation and the required future source comparison are specified in engine `docs/SKILL-INSTALL.md`; do not claim that skill drift check already exists.
