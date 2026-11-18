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

## Running a worker

`dispatch` requires `--role`, an existing `--dir`, and exactly one of `--prompt TEXT` or `--prompt-file FILE`. Optional flags are `--stakes low|normal|high|irreversible`, `--family`, `--exclude-lineage`, `--mode read-only|write`, `--last FILE`, `--dry-run` and `--no-refresh`. Stakes default to normal; mode follows the selector's role policy unless you supply it. Use `--exclude-lineage` for an independent review or council seat.

Dispatch uses the chosen option's `command_argv`, fills its `--prompt <task>` slot, and preserves its `FLEET_SELECTION_FILE` assignment. Prompt files are read once and passed as literal text because AGY accepts only `--prompt`. Arguments go directly to the process launcher, so shell punctuation in the task remains text. The wrapper receives `--dir` and `--last`; OpenRouter rejects `--dir`, so its process uses that working directory without the flag. OpenRouter remains a worker with no filesystem tools.

Dispatch returns the successful worker's stdout unchanged after the attempt completes. Each attempt's stdout and stderr stay in local diagnostics until its outcome is known, so failed partial answers and stack traces never reach the caller's chat. Dispatch prints one stderr line per attempt naming the selected pool, model and level, the selection receipt, the dispatch receipt, the model receipt and the diagnostics file. The selected name is a routing choice: use the model receipt's provider identity to report what ran. An unconfirmed identity stays unconfirmed.

Each attempt gets a fresh directory under `FLEET_STATE_DIR/dispatches/` and a fresh `CROSSFEED_DISPATCH_ID`. Its `dispatch.json` contains paths, selection metadata, exit status and launch evidence, without the task text. Its `answer.txt.crossfeed.json` is the wrapper's model receipt. Diagnostics can contain worker output or task text; treat them as private local run logs. `--last` copies only a successful attempt's fresh answer and model receipt to your requested path. Its parent directory must exist. If all attempts fail, older caller files remain; the dispatch exit code identifies that failure.

Dispatch tries the chosen option first, including an exploratory choice outside the ranked top three. A metered choice gets one attempt, including a prelaunch refusal; dispatch never retries into or out of a metered attempt. For a nonmetered choice, a nonzero exit tries the next distinct nonmetered option among the remaining ranked top three, even after launch or receipt publication. Explicit cancellation exits 129, 130 and 143 stop dispatch. Each retry names the failure and replacement; exhausting the choices returns the last failure code. A failed writer may already have changed files; the replacement receives the same task in the resulting workspace.

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

Executable role exclusions live in `effort.<model_key>.refuse_roles`, a map from role to refusal reason. Written operator instructions are not parsed by the selector. A `by_role` effort default sets a preferred level; it does not restrict enumeration to that level. `select --role review` defaults to normal stakes. Use `--stakes high` when the task needs the high-stakes quality weighting; a stakes change cannot replace a hard exclusion.

The optional [Haiku policy fragment](../examples/haiku-judgment-policy.json) encodes exclusions for the existing review and judgment roles. Merge only each model's `refuse_roles` entries into that model's existing effort block in your overlay, preserving all other fields and refusal entries. Do not replace the whole overlay or whole effort block with this fragment. Apply entries only to model keys already configured on your machine; it admits no new model. If you add another Haiku route key, copy the refusals to its effort entry too. The file is inert until the operator applies it, and a skill install does not modify the external overlay.

These refusals apply before selector scoring, exploration, allow-list and target filtering, and fallback receipt creation. Explicit selector levels grant no override. Direct wrapper calls have their own effort interface and are outside this selector policy guarantee. Choosing the next eligible model still uses the configured stakes and quota evidence; excluding Haiku does not pin Sonnet or Opus.

```text
quota_penalty = pool_lambda * task_pool_percent
quota_penalty = min(quota_penalty, high_cost_cap)           at high stakes
score = stakes_weight * quality - quota_penalty - mu * latency_seconds
pressure = max(actual_used, projected_usage)
pool_lambda = 0                                           when pressure <= target
pool_lambda = lambda0 * (pressure - target) / (100 - target) otherwise
pool_lambda = max(lambda_unknown, actual_usage_price)     when projection is unavailable
```

Default stakes weights are low 1, normal 2 and high 40. For high stakes, `high_cost_cap` bounds the quota penalty at one score unit by default. Quota cost alone can therefore trade at most 2.5 percentage points of estimated quality; latency still matters. Hard admission gates apply before scoring. Unknown quality receives an uncertainty discount. For exploitation, an unknown cell with the same mean as a measured cell is dominated by it. The binding quota window has the strongest price after considering actual and projected usage. An unknown projection uses `lambda_unknown`, which defaults to half of `lambda0`; actual usage still sets a price floor, and the option is flagged `quota_projection_unknown`. A window at 90% actual usage remains critical even if its forecast predicts unused allowance. Model-only windows apply to the models they name.

Evidence may supply `q_by_stakes.<stakes>.<family>` with the same quality fields as `q.<family>`. Selection uses that stratum when present, otherwise the family estimate. This lets separately validated hard-task outcomes inform high stakes without treating easy-task success as hard-task evidence. The evidence producer must keep fit and held-out results separate.

`mu` defaults to 0.001 per second, halves for high stakes and becomes zero for irreversible work. The default latency evidence uses measured `ttfa_s` or `time_to_first_answer_s`, then the publisher's first-answer measurement, then a flagged 120-second placeholder. An optional `latency_by_stakes.<stakes>` overrides it with a measured completion-time median for that stratum; the evidence producer must name its basis.

`irreversible` selects the highest admitted known quality estimate, ignoring price and latency. It uses its own quality stratum when present, then the high-stakes stratum, then the family estimate. All-unknown quality causes refusal.

## Task cost

Measured subscription consumption takes precedence. Evidence rows store `own_cost.<pool>.percent_per_task` and optional `requests_per_task` medians per model and level. A ledger task can provide `pool` and `own_cost: {percent_per_task, requests_per_task}`; linked identity, usage and proof records count once. Estimated selector costs never become measured observations.

When a USD allowance matches the binding window, token medians and API-equivalent input/output prices estimate task percent as `100 * task_usd / allowance_usd`. Configure the allowance in `quota_pools.<pool>.plan.allowance`, including amount, currency and window. An explicitly different window is never used.

Set `quota_pools.<pool>.plan.limit` to `"none-known"` for a pool with no known quota limit: its quota price and penalty are zero, usage remains unknown, quota snapshots and refresh sources are ignored, and brief/console show "no known limit"; explicit off switches and provider quota errors still block dispatch. The cost basis is `no_known_limit`. ChatGPT dollar cost is unknown; other pools retain any known API-equivalent price. Fresh-chat guards and elapsed time still constrain chat workers.

Without that allowance, Go uses `quota_pools.opencode-go.request_estimates.per_5h_week_month.<model>`, an array of request counts for the 5-hour, weekly and monthly limits. A level-keyed object of those arrays is also accepted. Task percent is `100 / binding_request_count`, multiplied by measured requests per task, or 1 if unmeasured.

Other options use API-equivalent task USD divided by the median USD across the pool's admitted options, multiplied by `quota_pools.<pool>.nominal_percent_per_task` (default 0.5). This is flagged `cost_relative_proxy`. Missing prices leave `cost.percent` unknown; scoring uses the nominal percent and flags `cost_unknown_nominal`. Cost uncertainty alone never rejects an option. Each result names its cost basis and the percent used for the penalty.

## Cash funding and owner envelopes

An overlay pool (or lane for a more specific declaration) uses `funding: included | verified-free | metered | unknown`. An API key is authentication, not funding: OpenCode Go can be included; catalog discovery proves neither subscription coverage nor free execution. For the existing subscription wrappers, the legacy included classifications assume overage is off. Do not declare included or verified-free without checking billing. Unknown is not zero. `cost.estimated_usd` remains an API-equivalent task estimate for quota comparisons, not an amount paid.

The brief, usage, choice, top three and receipts show cash separately from quota pressure. Cash fields distinguish the expected increment, the worst-case amount, per-call/daily caps, cash settled today, unresolved holds (including older days) and available headroom after both the daily and pilot caps. A missing price, measurement or ledger is shown as unknown, not as a zero cost or open wallet. The bounded tariff carries its source, quote date, age and expiry. A selection or dry run quotes only; the ledger changes at actual send. The model and dispatch receipt carry the reservation ID and reported charge when available.

**Default: no envelope and no automatic paid selection.** Neither quota exhaustion, `forced`, a named target nor `--allow` permits spending. Owners can provision a bounded prompt-only route after checking its provider billing and data terms. The canonical `~/.config/orchestrator/access-overlay.json` must be a regular owner-only file; the paid transport will not run from a caller-selected overlay. The example's metered routes are inert templates with no envelope, tariff or approved prompt. An envelope is `cash_accounts.<account-id>`, with owner-set positive USD `per_call_usd`, `daily_usd`, `pilot_usd` (in that order of size), an offset-aware `expires_at` timestamp and optional `automatic: true`. Omission or false limits it to an explicit direct call. Multiple routes on the same billing account must use one account ID; don't mint separate IDs for one provider balance. Plan-level `daily_usd_cap` is not a cash envelope.

For the first release, use `paid_route.profile: "catalog-sse-v1"` on an admitted `openrouter` wrapper lane, with `endpoint`, exact upstream `provider`, exact `model` (equal to the lane selector), funding `account` and permitted `roles`. Set the lane's `auth.key_ref` to an owner-controlled 1Password reference. An endpoint must enforce the same catalog, provider price/identity bounds, streamed usage and maximum-output contract; OpenAI-shaped JSON alone is insufficient. `tariff` declares USD per million tokens as `input`/`output`, `context_tokens`, `max_output_tokens`, `max_input_bytes`, a human-readable `source` and offset-aware `quoted_at`/`valid_until`. Unknown fees, changed live prices beyond the quote or unsupported billing tiers refuse. The request sends one bounded text message, with no filesystem tools, attachments, redirects or provider fallback.

Paid calls support public class only. Select/dispatch use `--data-class public`; a direct call uses `FLEET_DATA_CLASS=public`. Unknown defaults to private. `paid_route.approved_prompt_sha256` lists the owner's SHA-256 digests of approved exact UTF-8 messages, including any final newline. A label alone grants no disclosure. A paid command does not prepend the worker identity instruction to those bytes. For selector eligibility add `paid_route.quality_floors.<family>` (positive measured quality floor at the exact level); unknown or imputed quality does not qualify. MiMo still requires `--paid-mimo` directly and an explicit `--allow` with the selector. Other compatible manual routes use `--paid --role ROLE`.

Only the owner should provision or reconcile `~/.local/state/orchestrator/cash-budget.json` (regular owner-only file, default home independent of `--state-dir` and XDG). Its schema is `{"schema":"crossfeed-cash-budget/v1","accounts":{"<account-id>":{"calls":{}}}}` for a genuinely new, unused billing account. Never recreate an absent ledger or replace an established account with an empty one to clear a hold. Before re-enrolling an older MiMo account, reconcile any prior `openrouter-mimo-budget.json` records and provider charges; there is no automatic migration or reset. A missing, corrupt or uninitialized ledger refuses. The transport reserves against the full bounded context and maximum output just before POST. It settles once with valid final reported cost on the local settlement day. Ambiguity keeps the hold across midnight; an overrun freezes the funding account for owner reconciliation. This bounds Crossfeed's authorization on one host, not outside use or an independent provider invoice; use a dedicated provider-side stop as a backstop.

The Gemini Pi/OpenCode tool loops and other unbounded paid agents are not metered selector candidates. Legacy explicit Gemini text's daily cap is **estimate-only**; an in-flight tool run can overshoot and must not be used as an automatic fallback.

## Pace and variety

Plain scoring breaks near ties by fixed keys, so it would send every equal-quality task to the same lane. After scoring, the selector instead draws among the measured options within a quality band of the option scoring picked (`policy.selector.variety.quality_band`, default low 0.05, normal 0.03, high 0.01, irreversible 0). A candidate must be within the band on estimated quality and on score (band times the stakes weight), so quota price and latency still count. One option per lane (pool and model), its best-scored level. Every hard gate (switches, refusals, allow lists, effort evidence, data policy, caps, lead pressure) runs before scoring, so the band can only choose among options already admitted. An explicit target and irreversible stakes are never varied. If a metered option is in the measured quality and score band, usable expiring included quota wins first; otherwise keep only those peers with the lowest marginal cash (use the ceiling when a task estimate is unknown). Run the existing pace and variety draw among remaining peers. Pure nonmetered routing keeps its prior behavior. The top three retain their raw score order; `cash_preference` explains a change of choice.

Each candidate's preference is `pace_weight * pace - recent_use - score_behind`:

- `pace` is the pool's pace in allowance fractions. Per applicable window it is elapsed minus used (the glance reading), or, when the oracle projects the window, elapsed fraction times the share its projection leaves unused. If any window is more than 5 points in deficit, the worst deficit is the pool's pace; otherwise the largest surplus is. Unknown, stale and no-known-limit pools are 0.
- `recent_use` counts the lane's runs, halving every `recency_half_life_s` (default 3600). It reads the tail of `runs.jsonl` and launched dispatch receipts, so a parallel burst sees its own earlier picks; one dispatch counts once.
- `score_behind` runs from 0 for scoring's pick to 1 at the band edge.

The draw is a softmax over preference with `temperature` (default 0.5); `pace_weight` defaults to 2. A surplus pool about to reset therefore wins most draws over an equal-quality pool in deficit, and repeated equal-quality dispatches rotate. `selection_probability` is the mixture probability of the draw and exploration; the result's `variety` block lists every candidate with its pace, recent use and probability. Set `policy.selector.variety` to `false` to restore the fixed tie-break. These defaults are pending the owner's decision (BR-SELECT-002).

## Exploration and replay

At low stakes, `policy.selector.explore` defaults to 0.1. That fraction of choices samples uniformly from admitted nonmetered options with unknown quality. Paid options are never explored. Other stakes never explore unknown options; they exploit through the variety draw above.

Every choice and immutable receipt records `selection_probability`, the unconditional probability under the mixture policy. With N unknown options and exploration rate e, an unknown option receives e/N, plus (1-e) times its variety probability if it is the exploitation pick. A measured candidate receives (1-e) times its variety probability. With exploration disabled and a single candidate, the chosen option receives 1. This probability survives into wrapper and AFK ledger metadata for later replay.

The JSON result includes the chosen option, the top three options ranked by exploitation score, all pool prices and exact wrapper commands. An exploratory choice can be outside that top three and has its own receipt. Replace the command's `<task>` placeholder and retain its `FLEET_SELECTION_FILE` assignment. The wrapper records the effort that actually ran. A receipt that disagrees with the actual model, lane, effort, role or family is omitted with a `selection_error`. Each alternative command has its own receipt; its probability describes the selector policy, so a manually chosen alternative is not a randomized policy observation.

`policy.selector` also accepts `allowed_pools_by_role`, `stakes_weights`, `high_cost_cap`, `target`, `lambda0`, `families_by_role`, `mode_by_role` and `uncertainty_weight`. Source overrides must supply their assumption text and exactly one anchor per anchored family. See the [example overlay](../examples/access-overlay.example.json).

Synthetic regression tests establish these mechanics. A held-out comparison against fixed policies is still needed to establish whether the selector improves real task outcomes.

Creative roles use distinct evidence: `design`, `visual` and `frontend-visual` map to `visual`; `writing` maps to `writing`. An unmeasured writing cell remains unknown. The AGY admission template ranks Argon first in manual `route` lists (for writing an owner decision of 1 November 2026, "Gemini Argon is the best writer"; for design a provisional preference); `select` and `dispatch` retain evidence scoring and all hard gates. See [the AGY admission reference](../skill/references/lane-antigravity.md) for current selectors and dated evidence limits.
