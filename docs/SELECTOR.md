# Model selector

`fleetctl.py dispatch` chooses a funding pool, wrapper, model and thinking level, then runs a worker. `select` returns the choice for inspection. Both compare local quality evidence with quota pressure, latency and, among comparable measured choices, incremental cash. Existing `route` calls keep their current behavior.

```bash
python3 scripts/market-refresh.py
python3 scripts/fleetctl.py select --role implementation --stakes normal --json
python3 scripts/fleetctl.py select --role review --exclude-lineage openai --json
python3 scripts/fleetctl.py dispatch --role review --exclude-lineage openai \
  --prompt "Review the change and report concrete defects." --dir "$PWD" --mode read-only
python3 scripts/fleetctl.py dispatch --role implementation --prompt-file task.txt \
  --dir "$PWD" --mode write --last result.txt
```

Market refresh writes `evidence/levels.json` under `FLEET_STATE_DIR`. Benchmark payloads, measured task costs and selection receipts stay in local state. With an optional Artificial Analysis key, the refresh reads the [V2 Free language-model feed](https://artificialanalysis.ai/data-api/docs). It uses a complete paginated snapshot or marks that source unavailable. Metrics omitted by the free tier stay unknown; provider mean performance is kept separate from the older median telemetry.

## Benchmark evidence and model cards

Open `fleetctl.py console` and choose **Benchmarks** beside Providers. Each model and thinking level shows task-family estimates, outside metrics and their freshness-adjusted weights, local checks with sample counts and dates, source prices, quota pressure, and a short evidence verdict. Both themes use the console's existing styling. Reload the page to read current local state; it makes no provider calls. The tab and JSON export read the same `evidence/levels.json` the selector consumes, plus the current overlay and quota snapshot. Cards describe evidence, not admission: dispatch still applies switches, access rules and caps.

Import checked grid results into the live ledger, then rebuild evidence from the cached market catalog without network access:

```sh
python3 scripts/fleetctl.py import-bench /private/campaign/results.jsonl --measured-on 2026-10-03
python3 scripts/fleetctl.py model-cards > /private/model-cards.json
```

`--measured-on` supplies the measurement date for older rows without `started_at`; it is marked as operator-supplied provenance. New rows use their recorded timestamp. Supply final result files, not progress logs or intermediate attempts. Multiple result paths are accepted. Imports retain source paths locally, reject conflicting copies, and count repeated imports once. An existing matching run ID with a different ledger payload is rejected for review rather than risking double counting.

`import-bench` accepts boolean check verdicts after worker exit 0, including failed task checks. Other worker exits are excluded, except wall-clock exit 124: the main campaign's `grid/main-2026-10-03/PREREG.md`, amendment at 00:45 CEST, explicitly says “a timeout is a fail, as it would be in real use.” Exit 124 always counts as a failure. Exits 4 (empty output or lease failure), 5 (prelaunch refusal or session failure), 8 (receipt failure), 125 (idle kill), 127 (missing command), unknown nonzero exits, missing exit codes and execution/check errors never supply quality or telemetry. They are recorded once in the separate local `bench-excluded.jsonl`, with their exit, reason, measurement date and source path. Quota batches containing excluded cells cannot establish measured fit cost. The import receipt reports newly imported and excluded counts separately.

To correct a previous import, retract its original source paths, then re-import with the corrected importer:

```sh
python3 scripts/fleetctl.py import-bench --retract-source /private/campaign/results.jsonl --measured-on 2026-10-03
python3 scripts/fleetctl.py import-bench /private/campaign/results.jsonl --measured-on 2026-10-03
python3 scripts/fleetctl.py model-cards > /private/model-cards.json
```

Retraction removes only benchmark records whose stored absolute source path matches an argument, from both ledgers, and rebuilds evidence from the remaining records. It preserves unrelated ledger lines byte for byte. Use the original imported paths, even if the source files have since disappeared; a copied file's new path does not identify the original import. Repeating retraction or import is harmless. Retraction uses source provenance alone; `--measured-on` is validated when supplied but does not narrow which dates are retracted.

Only the frozen fit split enters the ledger. Held-out payloads and excluded tasks never train quality, latency or token estimates. Mixed fit/held-out quota batches cannot establish fit-only measured cost. The older `bench/to_evidence.py --split fit --levels-in ... --levels-out ...` interface still builds isolated replay evidence; it does not install live evidence. `import-bench` persists observations in `runs.jsonl`, so later market refreshes retain them. Every checked trial adds one pass or failure to the Beta update; outside priors keep their source weights and small pseudo-sample strength. No other thinking level borrows its results.

`model-cards` prints compact `crossfeed-model-cards/v1` JSON to stdout, including quality and uncertainty by family, ordered family strengths, outside metrics and weights, local counts and provenance, prices, latency and quota costs by source. Strength ordering compares `mean - sd` within a row; these heuristic family estimates are not calibrated probabilities or proof that one model beats another. Unmeasured families are listed in `unmeasured_families`; zero-trial families are omitted from `own`. Missing data stays unknown and estimates carry their basis. The authenticated console also serves the same export at `/model-cards.json`.

**Keep exports private.** Artificial Analysis scores are fetched at runtime into host-local state; its raw numbers must never be committed to a public repository. Tests use invented scores. Other leaderboards used for human roster decisions do not silently become selector inputs.

## Expected cost and hard-role floors

Compare eligible model, effort and funding routes by **price + (1 − chance of success) × stakes multiplier × typical job price**. Until checked outcomes exist, the grade is a proxy, not a calibrated pass rate. Additional effort wins when reducing expected failures is worth the extra price. Set `--rating-stakes routine|important|critical` for 1×, 5× or 20× typical price. Hard reasoning, review, debug and implementation default to important; other roles default to routine. `--value-mode absolute-best` aliases critical unless `--rating-stakes` is explicit. The separate `--stakes` flag still selects evidence and admission rules. `policy.selector.entitled_only: true` limits ranking to included and verified-free routes.

Typical price is the median available prior first-attempt dispatch price. With no history, use the median positive eligible price. Missing prices don't count as zero: priced routes rank first within their policy tier. If none is priced, use the available grade proxy and don't explore.

`policy.selector.quality_floors_by_role` sets the minimum exact-effort hard grade and family for a role. Unknown or below-floor options are excluded even at low stakes. The role's family cannot be overridden. If nothing clears the floor, selection refuses, except for the Opus preference.

With `policy.selector.prefer_opus_hard_reasoning`, exclude Sonnet from hard reasoning and rank eligible Opus 5.5 efforts first. If no admitted Opus effort clears the floor, record a floor exception for the chosen Opus effort. At 90% actual use of an applicable Claude weekly window, a full five-hour window or other unavailability, prefer eligible Astra, then other eligible Codex routes. The legacy `--stakes irreversible` exception to Opus-first still applies; `--rating-stakes critical` does not bypass the preference. Switches, lead reserve, quota circuits and data policy still apply.

## Running a worker

`dispatch` requires `--role`, an existing `--dir`, and exactly one of `--prompt TEXT` or `--prompt-file FILE`. Optional flags are `--stakes low|normal|high|irreversible`, `--family`, `--exclude-lineage`, `--mode read-only|write`, `--last FILE`, `--dry-run` and `--no-refresh`. Stakes default to normal; mode follows the selector's role policy unless you supply it. Use `--exclude-lineage` for an independent review or council seat.

Dispatch uses the chosen option's `command_argv`, fills its `--prompt <task>` slot, and preserves its `FLEET_SELECTION_FILE` assignment. Prompt files are read once and passed as literal text because AGY accepts only `--prompt`. Arguments go directly to the process launcher, so shell punctuation in the task remains text. The wrapper receives `--dir` and `--last`; OpenRouter rejects `--dir`, so its process uses that working directory without the flag. OpenRouter remains a worker with no filesystem tools.

Dispatch returns the successful worker's stdout unchanged after the attempt completes. Each attempt's stdout and stderr stay in local diagnostics until its outcome is known, so failed partial answers and stack traces never reach the caller's chat. Dispatch prints one stderr line per attempt naming the selected pool, model and level, the selection receipt, the dispatch receipt, the model receipt and the diagnostics file. The selected name is a routing choice: use the model receipt's provider identity to report what ran. An unconfirmed identity stays unconfirmed.

Each attempt gets a fresh directory under `FLEET_STATE_DIR/dispatches/` and a fresh `CROSSFEED_DISPATCH_ID`. Its `dispatch.json` contains paths, selection metadata, exit status and launch evidence, without the task text. Its `answer.txt.crossfeed.json` is the wrapper's model receipt. Diagnostics can contain worker output or task text; treat them as private local run logs. `--last` copies only a successful attempt's fresh answer and model receipt to your requested path. Its parent directory must exist. If all attempts fail, older caller files remain; the dispatch exit code identifies that failure.

Dispatch tries the chosen option first, including an exploratory choice outside the ranked top three. A bounded paid choice gets one attempt, including a prelaunch refusal; dispatch never retries into or out of a bounded attempt. Otherwise a nonzero exit tries the next distinct unbounded option among the remaining ranked top three, even after launch or receipt publication. The explicit legacy Gemini route retains this prior behavior. Explicit cancellation exits 129, 130 and 143, and a free-route billing incident (OpenRouter exit 8), stop dispatch. Each retry names the failure and replacement; exhausting the choices returns the last failure code. A failed writer may already have changed files; the replacement receives the same task in the resulting workspace.

`--dry-run` prints JSON with the resolved argument list, working directory and planned receipt paths. It creates selection receipts but starts no wrapper or quota refresh process. Its stdout includes the literal task in the argument list; it writes no dispatch receipt or run log. Use it to inspect the call before sending it.

Use `--allow FILE_OR_LIST` with either command to restrict selection to measured benchmark options or council seats, for example `--allow 'codex:gpt-6.1-sol:high,claude:claude-sonnet-5-5:*'`. Files accept one `pool:model_key:level` entry per line or comma-separated entries; blank lines are ignored. Model keys may contain colons, for example `chatgpt-work:chatgpt:latest-high:high`; the first field is the pool and the last is the level. Only the level accepts `*`, meaning any admitted level for that pool and model. Other options are rejected with `not in allow list` before scoring, exploration and fallback. Existing admission gates still apply. An empty list or a list with no eligible options produces an error. Every selection receipt records the normalized entries in `allow` (`null` when unrestricted), so changing the file cannot change the recorded restriction.

## Quality evidence

### One model across sources

`model_key` remains the route's key for switches, effort controls, allow lists and wrapper arguments. `model_identity` is the shared evidence key. Routes naming the same exact key already share evidence. To join a differently named route, its model card must point to a canonical card with dated evidence:

```json
{
  "model_cards": {
    "exact-model-v2": {"status": "current"},
    "partner/exact-model-v2": {
      "status": "current",
      "identity": {
        "model_key": "exact-model-v2",
        "evidence": "Provider catalog or native receipt identifying this exact release",
        "verified_on": "2026-10-13"
      }
    }
  }
}
```

The target must exist and have no further identity mapping. Missing evidence or dates, alias chains, ambiguous selectors, differing or multiple `served_snapshot` declarations, dynamic routes and unconfirmed relays stay separate. Provider prefixes, version suffixes and card display names are never stripped to join evidence. Existing `aliases` and `run_as` remain CLI/receipt compatibility names, not proof for joining quality records. Configure distinct keys for distinct snapshots and dynamic routes; reusing one key asserts they are the same release. This assertion needs provider evidence before admitting a new source. A dated mapping is an operator assertion, not a cryptographic proof of model weights or serving quantisation.

Quality and measured task tokens join by model identity and exact level. Duplicate records linked by run or attempt ID count once. Recorded per-run identities are retained. Without a recorded identity, an alias joins only for a run dated on or after its mapping's `verified_on`; older or undated records stay under their original route key. Per-run dynamic or relay-unconfirmed metadata also prevents joining, even if today's lane is fixed. Qualified Auto selectors and lanes marked `dynamic_model: true` remain separate. A backend name resolved to a registered card, effort key or lane key, through an exact lane selector or a recorded resolved identity, trains that model instead of the selected one. An unmatched reported name falls back to the selected identity as selection evidence, with `model_identity_unconfirmed` and the raw name in `provider_reported_models`. This does not assert that an unknown dated snapshot is the selected release. Register different releases with distinct keys before assigning native-backend evidence to them; no release suffix is stripped. Auto/service routers and relays with unconfirmed provider identity retain their own evidence. A harness's tools and permissions still constrain which jobs its route can run; a shared score does not prove equal end-to-end agent quality.

Quota windows, measured `own_cost.<pool>`, concurrency, paid opt-in, caps and source switches keep their existing scopes. A canonical global off also applies to proven aliases, including conversion of older global settings into per-source switches. Turning off one source leaves another admitted source available. `source_telemetry.<pool>/<harness>` stores source latency; benchmark imports without harness metadata use a pool key. Selection prefers source latency over pooled estimates, including pooled stakes-specific estimates. Its `metric` identifies time to first answer (`ttfa_s`) or whole-task completion (`completion_time_s`); these measures are not interchangeable. Ledger series are kept separately, and benchmark imports use completion time and a harness key when declared. A lane, or a direct model card, can declare `price_1m: {"input": ..., "output": ...}` in USD per million tokens for that source; `in/out` remains accepted. This overrides the shared API-equivalent estimate for that option only. Missing or invalid prices stay unknown. Declared API-equivalent source cost breaks exact score ties in the ranking (`top3`). Within the measured band the cash policy can narrow the draw before pace and variety. Irreversible selection keeps ignoring price. Scoring otherwise retains its existing quality, quota-pressure and latency trade-offs.

Identity mappings admit no new routes. Paid MiMo still requires an exact allow entry naming `openrouter-mimo` and its existing roles, in addition to the cash envelope and automatic grant below. A Go alias cannot supply that permission or bypass a cap. New sources still need their own access verification, effort declaration and capabilities.

Rebuild derived `evidence/levels.json` through the existing market refresh after reviewing mappings. The append-only `runs.jsonl` and raw catalog are preserved. Legacy caches use an exact canonical row when present. Otherwise differing aliases choose the newest valid `read_on` timestamp, then the lexicographically smallest route key and JSON payload for stable ties. The chosen row retains its origin and gains `alias_cache_conflict`; rebuild it from raw records when available. Freshness is a cache-selection rule, not evidence of higher quality. No cached posteriors or trial counts are added because observations may overlap. Only aliases dated on or after the mapping, or carrying a recorded identity, join; older or undated unmapped caches retain their route key. Conflicting canonical rows remain unavailable. Fit-only benchmark imports passed `--overlay /path/to/access-overlay.json` first respect registered keys and exact selectors, then apply historical provider-prefix and Gemini effort-suffix normalisation to unmatched grid names before resolving the dated identity. Version suffixes remain intact. Omitting the overlay preserves historical normalisation. Held-out results remain excluded.

Artificial Analysis rows map to the most recent `release_date` for each model and level. Dated slugs such as `-0424-high` and `-20260813` are eligible only when `model_cards.<key>.served_snapshot` or the model's lane declares that snapshot. The declaration can be a date token or a snapshot slug. Raw variants remain available for audit. Conflicting rows with the same newest date remain ambiguous.

Each family has one anchor metric declared by `policy.evidence.sources`. The defaults are:

| Family | Anchor |
| --- | --- |
| coding-agent, repo-qa, review | `terminalbench_v4_0` |
| reasoning, research | `artificial_analysis_intelligence_index` |
| extraction | `ifbench` |

The anchor becomes a percentile among current admitted roster cells with that metric: average rank divided by population size, with ties sharing a rank. Catalog-only, retired and unretained older models are excluded from that population. Fractions and percentages of the same metric normalize identically.

A missing anchor can be imputed, meaning estimated from another metric measured at that exact model and level. An ordinary least-squares fit predicts the anchor percentile from a co-observed metric across at least three admitted cells. It chooses the predictor with the most paired observations, then the smallest residual error. The residual standard deviation has a floor of 0.1 and is added in quadrature to the cell's uncertainty. The row and family are flagged `imputed`; `q.<family>.imputation` records the predictor, sample count, slope, intercept and residual. Without a usable fit, the family stays unknown. No level borrows another level's quality.

External evidence supplies a weak heuristic prior. Mechanically verified local passes and failures update a Beta posterior, a distribution for a success rate. A successful wrapper exit alone is not a passing task. Percentile ranks are assumptions about relative quality, not calibrated task pass probabilities.

LMArena is an optional secondary source for the visual/webdev family only. Configure a JSON or parquet export through `policy.evidence.lmarena.path` or `.url`, or `LMARENA_LEADERBOARD`. Rows must name an exact model and level. Parquet needs an installed pandas parquet engine; unavailable sources are skipped.

## Scoring and admission

Admission checks model switches, retirement, pool-specific older-model retention reasons, refused roles, task mode, input modality, capacity and the CRITICAL quota guard. The Go pool retains its CRITICAL role allowlist. AGY requires write mode because its wrapper has no proven read-only boundary.

### Operator role refusals

Executable role exclusions live in `effort.<model_key>.refuse_roles`, a map from role to refusal reason. Written operator instructions are not parsed by the selector. A `by_role` effort default sets a preferred level; it does not restrict enumeration to that level. The optional `scored_levels_by_role` map restricts automatic enumeration to its declared roles and supported levels (for example `{"implementation":["high"],"hard-reasoning":["high","xhigh"]}`) without changing score weights or explicit effort resolution. Unlisted roles get no scored option. `select --role review` defaults to normal stakes. Use `--stakes high` when the task needs the high-stakes quality weighting; a stakes change cannot replace a hard exclusion.

The optional [Haiku policy fragment](../examples/haiku-judgment-policy.json) encodes exclusions for the existing review and judgment roles. Merge only each model's `refuse_roles` entries into that model's existing effort block in your overlay, preserving all other fields and refusal entries. Do not replace the whole overlay or whole effort block with this fragment. Apply entries only to model keys already configured on your machine; it admits no new model. If you add another Haiku route key, copy the refusals to its effort entry too. The file is inert until the operator applies it, and a skill install does not modify the external overlay.

These refusals apply before selector scoring, exploration, allow-list and target filtering, and fallback receipt creation. Explicit selector levels grant no override. Direct wrapper calls have their own effort interface and are outside this selector policy guarantee. Choosing the next eligible model still uses the configured stakes and quota evidence; excluding Haiku does not pin Sonnet or Opus.

Pace prices use the most restrictive applicable window, based on the larger of actual and projected usage. `target` defaults to 90%; `lambda0` scales pressure above it. Missing projections use `lambda_unknown` (half of `lambda0` by default) without discounting actual pressure. Model-only windows apply to models they name. Admission checks still apply to every candidate. Unknown quality receives an uncertainty discount, and an equal-mean measured cell dominates an unknown cell. `--stakes irreversible` refuses unknown evidence and disables exploration.

Evidence can supply `q_by_stakes.<stakes>.<family>` instead of the default family estimate. Keep fit and held-out results separate. Latency is shown when available but is not part of expected cost.

## Task cost

Measured subscription consumption takes precedence. Evidence rows store `own_cost.<pool>.percent_per_task` and optional `requests_per_task` medians per model and level. A ledger task can provide `pool` and `own_cost: {percent_per_task, requests_per_task}`; linked identity, usage and proof records count once. Estimated selector costs never become measured observations.

When a USD allowance matches the binding window, token medians and API-equivalent input/output prices estimate task percent as `100 * task_usd / allowance_usd`. Configure the allowance in `quota_pools.<pool>.plan.allowance`, including amount, currency and window. An explicitly different window is never used.

Set `quota_pools.<pool>.plan.limit` to `"none-known"` for a pool with no known quota limit: its quota price and penalty are zero, usage remains unknown, quota snapshots and refresh sources are ignored, and brief/console show "no known limit"; explicit off switches and provider quota errors still block dispatch. The cost basis is `no_known_limit`. ChatGPT dollar cost is unknown; other pools retain any known API-equivalent price. Fresh-chat guards and elapsed time still constrain chat workers.

Without that allowance, Go uses `quota_pools.opencode-go.request_estimates.per_5h_week_month.<model>`, an array of request counts for the 5-hour, weekly and monthly limits. A level-keyed object of those arrays is also accepted. Task percent is `100 / binding_request_count`, multiplied by measured requests per task, or 1 if unmeasured.

Other options use API-equivalent task USD divided by the median USD across the pool's admitted options, multiplied by `quota_pools.<pool>.nominal_percent_per_task` (default 0.5). This is flagged `cost_relative_proxy`. Missing prices leave `cost.percent` and expected cost unknown. The grade/pressure order for unpriced fallbacks uses the nominal percent and flags `cost_unknown_nominal`; it is not a dollar ranking. Cost uncertainty alone never rejects an option. A measured Pi coding relay can retain its unpriced fallback under projected native Codex exhaustion, but is not the cheapest offer.

To compare efforts on the same task, `effort_estimate` holds input constant (4,000 tokens) and varies output. It uses exact-level [Artificial Analysis model-page](https://artificialanalysis.ai/models) Intelligence Index output ratios when available; the host can supply them under `policy.evidence.effort_output_tokens.<canonical model>` as `source`, `read_on`, `output_tokens_per_task` (map of level to count), and `snapshot` when the served release is pinned. High is anchored to 1,500 output tokens, or the available rows are anchored to the fallback ladder if High is absent. The built-in Opus/Sonnet comparison uses Sonnet High as its common reference. Otherwise, nonzero local token medians with prompts within 2× of the reference can calibrate the estimate. Raw per-effort receipt medians and cash inputs are unchanged.

The estimated fallback output ladder is `none .35, minimal .45, low .60, medium .80, high 1, thinking 1.15, xhigh 1.50, max 2.30, ultra 3.50`. It is a conservative planning prior (not measured): [OpenAI](https://platform.openai.com/docs/guides/reasoning), [Google](https://ai.google.dev/gemini-api/docs/thinking) and [Anthropic](https://platform.claude.com/docs/en/build-with-claude/effort) describe increasing reasoning work, but do not publish universal token ratios. This assumes a modest increase through High and wider jumps at top settings. Missing cells scale the nearest available anchor by this ladder; inversions in the resulting output estimates receive a 5% minimum step without changing the underlying observations. Latency uses exact-level readings first, or the same ladder applied to the closest timed tier (120 seconds at High if no timing is known); its observed values need not increase.

API-equivalent planning cost uses a common model reference tariff (the maximum published input and output rates across its levels, unless the route supplies a tariff). When no positive output tariff is known, effort still has relative compute units using a 5:1 output-to-input weight, not a fabricated dollar price. These effort units scale typical Go request costs and unpriced nominal quota penalties. Directly observed quota debit takes precedence. No-known-limit routes keep zero quota penalty. Claude API weights are not verified Claude Max quota weights. No paid tariff, cash estimate or envelope changes. An unlabelled local reasoning outcome updates general fit, not an externally anchored hard-reasoning grade.

## Absolute subscription capacity

Set `quota_pools.<pool>.value_capacity` to `{"weekly_usd_equivalent": 46, "source": "..."}` to account for pool size in expected cost. Use a common, sourced weekly service equivalent (e.g. monthly list price × 12 / 52 as a labelled proxy when no absolute quota is published). This is neither a fee paid per task, a dollar balance, nor a count of guaranteed jobs. It is a separate capacity signal, not an alias for another pool's gauge.

Quota opportunity cost = estimated share × window fraction × reference² / sourced weekly capacity × (1 + quota pressure + observed pace deficit + scarcity). The reference is a $200/month subscription converted to weekly equivalent ($46.15); scarcity uses the remaining sourced capacity. Inverting the capacity factor makes the same quota share cost more on a smaller pool. A relative debit with no window is a weekly planning proxy. Short and scoped windows still constrain admission and pressure. The typical price uses prior first attempts recorded under this price basis; until available, it uses the median positive eligible price. Shadow estimates use a different price basis and do not train that median. Metered-only lanes use their incremental cash estimate, not a market quote. These are not bills, and request-count and API-weighted debit proxies may not be comparable.

A declared lane may use the existing `spend_level: "low"` dial for last-resort automatic routing. It is eligible only in its declared `implementation` or `hard-reasoning` roles with a configured quality floor, only with no current pool lease, and only after every normal subscription candidate fails an admission or quality check. It never appears in a healthy shortlist, including in exploration and ordinary dispatch retries. The nightly cheap-role promoter does not admit a low lane into simple roles. This does not change the rest of the lane's pool or its request weight.

## Cash funding and owner envelopes

An overlay pool (or lane for a more specific declaration) uses `funding: included | verified-free | metered | unknown`. An API key is authentication, not funding: OpenCode Go can be included; catalog discovery proves neither subscription coverage nor free execution. For the existing subscription wrappers, the legacy included classifications assume overage is off. Do not declare included or verified-free without checking billing. A newly configured pool with unknown funding is ineligible. `cost.estimated_usd` uses legacy price fields for quota comparisons. The normalized market-price estimate, `cash.api_equivalent_usd`, is display only; it is not a route-specific cash quote or a bill.

The brief, usage, choice, top three and receipts show cash separately from quota pressure. Cash fields distinguish the expected increment, the worst-case amount, per-call/daily caps, cash settled today, unresolved holds (including older days) and available headroom after both the daily and pilot caps. A missing price, measurement or ledger is shown as unknown, not as a zero cost or open wallet. The bounded tariff carries its source, quote date, age and expiry. A selection or dry run quotes only; the ledger changes at actual send. The model and dispatch receipt carry the reservation ID and reported charge when available.

**Default: no envelope and no new paid selection.** Neither quota exhaustion, `forced`, a named target nor `--allow` enables the bounded transport. Owners can provision a bounded prompt-only route after checking its provider billing and data terms. The canonical `~/.config/orchestrator/access-overlay.json` must be a regular owner-only file; the paid transport will not run from a caller-selected overlay. The example's metered routes are inert templates with no envelope, tariff or approved prompt. An envelope is `cash_accounts.<account-id>`, with owner-set positive USD `per_call_usd`, `daily_usd`, `pilot_usd` (in that order of size), an offset-aware `expires_at` timestamp, optional `timezone` (IANA zone, default UTC) and optional `automatic: true`. Omission or false limits it to an explicit direct call. Multiple routes on the same billing account must use one account ID; don't mint separate IDs for one provider balance. Plan-level `daily_usd_cap` is not a cash envelope.

For the first release, use `paid_route.profile: "catalog-sse-v1"` on an admitted `openrouter` wrapper lane, with `endpoint`, exact upstream `provider`, exact `model` (equal to the lane selector), funding `account` and permitted `roles`. Set the lane's `auth.key_ref` to an owner-controlled 1Password reference. An endpoint must enforce the same catalog, provider price/identity bounds, streamed usage and maximum-output contract; OpenAI-shaped JSON alone is insufficient. `tariff` declares USD per million tokens as `input`/`output`, `context_tokens`, `max_output_tokens`, `max_input_bytes`, a human-readable `source` and offset-aware `quoted_at`/`valid_until`. Unknown fees, changed live prices beyond the quote or unsupported billing tiers refuse. The request sends one bounded text message, with no filesystem tools, attachments, redirects or provider fallback.

Paid calls support public class only. Select/dispatch use `--data-class public`; a direct call uses `FLEET_DATA_CLASS=public`. Unknown defaults to private. `paid_route.approved_prompt_sha256` lists the operator's SHA-256 digests of approved exact UTF-8 messages, including any final newline. A label alone grants no disclosure. A paid command does not prepend the worker identity instruction to those bytes. For selector eligibility add `paid_route.quality_floors.<family>` (positive measured quality floor at the exact level); unknown or imputed quality does not qualify. MiMo still requires `--paid-mimo` directly and an explicit `--allow` with the selector. Other compatible manual routes use `--paid --role ROLE`.

Only the operator should provision or reconcile `~/.local/state/orchestrator/cash-budget.json` (regular file accessible only to its filesystem owner). Both this path and the approval overlay resolve the home from the OS account database by UID, not worker `HOME`, XDG or `--state-dir`. Its schema is `{"schema":"crossfeed-cash-budget/v1","accounts":{"<account-id>":{"calls":{}}}}` for a genuinely new, unused billing account. Never recreate an absent ledger or replace an established account with an empty one to clear a hold. Before re-enrolling an older MiMo account, reconcile any prior `openrouter-mimo-budget.json` records and provider charges; there is no automatic migration or reset. A missing, corrupt or uninitialized ledger refuses. The transport reserves against the full bounded context and maximum output just before POST. It settles once with valid final reported cost on the settlement day in the envelope's timezone, independent of worker `TZ`. Do not change an active account's timezone without reconciling its current accounting day. Ambiguity keeps the hold across midnight; an overrun freezes the funding account for operator reconciliation. This bounds Crossfeed's authorization on one host, not outside use or an independent provider invoice; use a dedicated provider-side stop as a backstop.

The pre-existing Gemini OpenCode/Pi text tool loop in `gemini-metered` stays explicit-only: a named lane (or an explicit `--allow` when the other legacy selection gates admit it), never automatic. Its owner-approved $1/day cap is **estimate-only** and outside the hard ledger. An in-flight call can overshoot; its original data policy still applies. This is not an admission exception for new metered tool loops.

Free OpenRouter checks the live catalog and pins `https://openrouter.ai/api/v1` with no redirects or environment proxy override. A reported charge stops dispatch (exit 8) and freezes that selector in the OS account's `~/.local/state/orchestrator/free-billing-incidents/`. Only the operator should remove the identified record after reconciling the charge and verifying the route. The freeze limits repeat charges; catalog pricing is not a provider-enforced guarantee against the first charge.

## Checked exploration

Each exact model, effort and subscription starts with four Beta pseudo-observations centred on a calibrated prior. The brief's 80–85% planning range gives an 82.5% prior for a median-grade option on an ordinary task of any work kind. The existing replay's quarter-strength benchmark log odds and easy/ordinary/hard offsets adjust that anchor; hard-reasoning defaults to hard, and `--difficulty` can override the task label. This is not a measured acceptance rate. An ungraded effort may borrow its weakest admitted sibling grade halfway toward neutral, never above that sibling. It remains unknown for admission and evidence purposes. An independent `fleetctl.py accept RUN_ID --verdict pass|fail --check "what was checked"` updates the route once per task. Process exit codes are not verdicts.

Thompson sampling draws one success probability per safe option and chooses the lowest sampled expected cost. A different pick must be within 25% of the best mean expected cost and pass existing gates. Paid, Kimi, low-dial, quota-constrained, explicit-target and legacy irreversible choices are not explored. A shared launch-time cap limits non-greedy picks to 15% of dispatch starts by default; override with `policy.selector.exploration_cap` (0 disables draws). No additional work runs to learn. `--no-explore` uses the mean and `--explore-seed` reproduces a draw on the same state.

Selections show the mean top three and sampled pick; dispatch logs the draw and actual route in `rating-trials.jsonl`. A single draw does not measure selection probability. A pick outside the top three gets its own receipt. Directly running a printed command is not a booked fleet dispatch. Retain its `FLEET_SELECTION_FILE` assignment; a receipt that disagrees with the actual model, lane, effort, role or family is ignored with a `selection_error`.

Synthetic regression tests establish these mechanics. A held-out comparison against fixed policies is still needed to establish whether the selector improves real task outcomes.

Creative roles use distinct evidence: `design`, `visual` and `frontend-visual` map to `visual`; `writing` maps to `writing`. An unmeasured writing cell remains unknown. The AGY admission template ranks Argon first in manual `route` lists (for writing an owner decision of 1 November 2026, "Gemini Argon is the best writer"; for design a provisional preference); `select` and `dispatch` retain evidence scoring and all hard gates. See [the AGY admission reference](../skill/references/lane-antigravity.md) for current selectors and dated evidence limits.
