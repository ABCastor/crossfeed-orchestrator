Load before using Copilot as one small read-only observer.

# Copilot observer

The default Student configuration is Auto-only. Other accounts can admit specific named read-only workers under a single shared subscription pool. Configure its credit allowance from your own plan; inline completion and CLI credits are separate entitlements. Do not treat Auto's selected backend as a named-model entitlement.

Use the official copilot CLI and its intended login, not OpenCode's named Copilot provider catalogue. The wrapper refuses writes and models that are not active and verified on the configured account, denies shell and file-write tools, disables built-in MCP/remote export/custom instructions, and applies its credit cap. It removes token environment overrides to use the configured supported login. No token values belong in output.

## Configure a company login

On the machine signed in to that account, first check company policy, the permitted credit budget and the official CLI's model picker. Test each exact ID on a harmless read-only prompt with an approved credit limit. Listing alone is not proof of access. Keep the provider receipt and distinguish a requested model from a provider-confirmed identity. Do not use another person's account or copy their credentials.

Prepare a **separate file** from your local roster; never redirect output over the input:

```sh
python3 scripts/configure-copilot-models.py --roster "$ACCESS_OVERLAY" \
  --pool github-copilot-company --verified-model MODEL_A --verified-model MODEL_B \
  --max-credits APPROVED_INTEGER --funding included > proposed-roster.json
ACCESS_OVERLAY="$PWD/proposed-roster.json" bash scripts/roster.sh validate
ACCESS_OVERLAY="$PWD/proposed-roster.json" python3 scripts/fleetctl.py select --role review \
  --allow 'github-copilot-company:github-copilot-company-MODEL_A:*,github-copilot-company:github-copilot-company-MODEL_B:*' --no-refresh --json
```

Declare `included` only after verifying the applicable usage will not be metered. If you declare `metered`, the helper records the models but leaves them as candidates: there is no bounded Copilot transport. For exact selection replace the allow list with `github-copilot-company:github-copilot-company-MODEL_A:*`. Review the proposal and install it through the normal local roster workflow. Named models and Auto are billed to the same pool; the cap is per invocation, not a subscription balance. No company balance or quality advantage is inferred. Named lanes start with review only, text input, read-only tools and provider-chosen effort. Keep one login per roster and reverify after switching accounts. The helper does not change the provider login or live configuration.

The CLI can still discover inherited skills. Put the exact question, hard constraints and success criterion directly in the brief; disabling custom instructions does not prove a lean empty context. It may read permitted repo files but its tool policy is not an OS sandbox. No secrets in the working folder.

Auto can change model each run. Read the receipt's confirmed identity; if the backend is unconfirmed, report selected Auto with that uncertainty. Never manufacture model identity or claim proven independent lineage from an unresolved route.

Exit 4 invalid JSON and 5 missing/failing result can occur after launch; 5 also gates an exhausted/off pool before launch. Exit 6 means empty answer. Watchdog kills are 124/125, not quota evidence. Use one narrow objection, verify its claims, and inspect partial evidence rather than retrying raw status blindly.
