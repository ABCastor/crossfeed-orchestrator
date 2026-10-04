Load for an unsettled consequential judgment that needs adversarial review, or an explicit request to challenge it.

# Council

Use independent critics to stress-test a forming decision, research conclusion or review finding. The lead judges claims, verifies them against sources, and folds surviving dissent into one actionable answer. A settled decision is not reopened. Routine cheap decisions do not need a council.

Choose lenses by the failure modes, usually three to five:

| Lens | Question |
|---|---|
| Pre-mortem | If this fails, what caused it? |
| Red-team | Where does the leading option break on its own terms? |
| Steelman alternative | What is the best case for the rejected option? |
| Outside view | What do comparable cases establish? |
| Second-order | What follows after the first effect? |
| Cost of inaction | What does delaying or choosing caution cost? |

Balance directions before dispatch. Red-team plus steelman alternative can attack from the same side; assign a seat to defend the leading option and attack the alternative. On a reversible call, at most half the seats may lean cautious and the cost-of-inaction seat is mandatory. State the for/against split and the caution split when you surface the result. Confidence describes evidence strength; it never substitutes for an action. An unknown that matters should lead to a bounded test and review date. Irreversible action retains its owner's gates.

Each lens receives the neutral frame, options, criteria, leading choice, relevant evidence and confidence, plus its one declared lens. Label facts as verified, inferred or unknown. Do not pre-argue an answer, reveal another critic's response or load a private store wholesale. Require the strongest objection, evidence/source and what would change the recommendation, in a short digest.

Read the brief, then select/dispatch with `--exclude-lineage <lead vendor>`. `--family` is task/evidence family, not a lineage filter. Never use your own vendor as an independent reviewer or council seat. Give different seats different lineages; exclude the author of the artifact from independent review of it. Same-vendor subagents can supply distinct lenses only as an explicitly disclosed degraded review, never as independent confirmation. Copilot may supply one small Auto objection but never swarm or named-model capacity.

The reviewer identity must be grounded in receipts. A selected family or Auto router is not proof of the underlying actual model. Report unconfirmed identity honestly and do not count unconfirmed diversity as proven independence. If a curated swarm cannot meet the lineage contract, make an explicit fanout task list instead.

Replace failed lenses at once rather than dropping them. Prefer another eligible lineage; if none exists, rerun the lens on an already-used lineage rather than lose it, disclose the diversity trade, and never wait for a quota reset to complete a panel. Lens coverage outranks lineage purity; disclose any resulting independence gap. A blocked action is never reassigned to bypass permission. Read fanout status before artifacts: exit 4 dispatched nothing. Count actual digests and inspect incomplete-panel warnings even after exit 0.

Judge digests without weighting the vendor label. Mark each consequential objection survives or dies against evidence. Reopen citations and check the exact claim, denominator, scope and direction: a real source used incorrectly is still false evidence. Public scouts receive public questions only; private evidence stays on authorized transports. No worker self-report counts as verification.

Fold survivors into the recommendation: changed action, wider option, kill criterion, review date or justified choice reversal. Give the owner the recommendation and surviving dissent, including any coverage or independence gap, not a transcript of the panel.

For high-level research and deep reasoning, reserve a standing seat for `chatgpt:latest-pro` through Crossfeed Chat. It uses the owner's subscription, has **no known limit**, and accepts **text only**. Give it the neutral frame and source excerpts in the prompt. It does not browse, run tools, or accept attachments. Answers take minutes; use no default wall or idle timeout, wait for its digest, and inspect the receipt before naming the model behind the saved selector. No known limit is not a promise of unlimited capacity: the relay's health, lease, switches and admission gates still apply.

The `research` swarm profile in the example overlay includes this seat in every quota band, alongside outside-view and red-team critics. Select with `scripts/swarm.sh research --prompt "..." --dir /project --exclude-lineage <lead vendor> --out /project/run-unique`. Profiles belong to the owner's overlay; installing code does not install that profile into an existing overlay. If the profile is absent, use an explicit fanout task list. If Latest Pro is unavailable or excluded because the lead is OpenAI, report the missing seat, replace its lens, and disclose the independence gap when applicable. Do not claim a full panel from an incomplete result.

A text-only fanout seat is `{"id":"pro-reasoning","agent":"chatgpt-chat","model":"chatgpt:latest-pro","role":"hard-reasoning","mode":"read-only","timeout":0,"dir":"/project","prompt":"Neutral frame, evidence excerpts, and your assigned lens..."}`. Fanout resolves the saved worker from the live gateway catalog and applies the same relay gates as direct dispatch.

A human-carried GPT Pro seat remains a fallback for the hardest artifact-bound questions when Crossfeed Chat cannot carry the evidence. It is a separate UI transport, not a Codex/API entitlement; use the `consult-gpt-pro` skill if available. `scripts/gpt-pro-bundle.sh` validates the eight prompt sections and packages selected evidence for the human to upload. Do not send private bundles without authorization. Treat the returned answer as a critic digest, verify its claims and retain owner control of irreversible actions. If unavailable, report the gap rather than inventing an API fallback.
