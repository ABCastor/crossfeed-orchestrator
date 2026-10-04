# Model selector

`fleetctl.py dispatch` chooses a subscription pool, wrapper, model and thinking level, then runs one worker. `select` returns the same choice for inspection. Both compare local quality evidence with quota pressure. Existing `route` calls keep their current behavior.

```bash
python3 scripts/market-refresh.py
python3 scripts/fleetctl.py select --role implementation --stakes normal --json
python3 scripts/fleetctl.py select --role review --exclude-lineage openai --json
python3 scripts/fleetctl.py dispatch --role review --exclude-lineage openai \
  --prompt "Review the change and report concrete defects." --dir "$PWD" --mode read-only
python3 scripts/fleetctl.py dispatch --role implementation --prompt-file task.txt \
  --dir "$PWD" --mode write --last result.txt
```

Market refresh writes `evidence/levels.json` under `FLEET_STATE_DIR`. Benchmark payloads, measured task costs and selection receipts stay in local state.

## Running a worker

`dispatch` requires `--role`, an existing `--dir`, and exactly one of `--prompt TEXT` or `--prompt-file FILE`. Optional flags are `--stakes low|normal|high|irreversible`, `--family`, `--exclude-lineage`, `--mode read-only|write`, `--last FILE`, `--dry-run` and `--no-refresh`. Stakes default to normal; mode follows the selector's role policy unless you supply it. Use `--exclude-lineage` for an independent review or council seat.

Dispatch uses the chosen option's `command_argv`, fills its `--prompt <task>` slot, and preserves its `FLEET_SELECTION_FILE` assignment. Prompt files are read once and passed as literal text because AGY accepts only `--prompt`. Arguments go directly to the process launcher, so shell punctuation in the task remains text. The wrapper receives `--dir` and `--last`; OpenRouter rejects `--dir`, so its process uses that working directory without the flag. OpenRouter remains a worker with no filesystem tools.

Dispatch returns the successful worker's stdout unchanged after the attempt completes. Each attempt's stdout and stderr stay in local diagnostics until its outcome is known, so failed partial answers and stack traces never reach the caller's chat. Dispatch prints one stderr line per attempt naming the selected pool, model and level, the selection receipt, the dispatch receipt, the model receipt and the diagnostics file. The selected name is a routing choice: use the model receipt's provider identity to report what ran. An unconfirmed identity stays unconfirmed.

Each attempt gets a fresh directory under `FLEET_STATE_DIR/dispatches/` and a fresh `CROSSFEED_DISPATCH_ID`. Its `dispatch.json` contains paths, selection metadata, exit status and launch evidence, without the task text. Its `answer.txt.crossfeed.json` is the wrapper's model receipt. Diagnostics can contain worker output or task text; treat them as private local run logs. `--last` copies only a successful attempt's fresh answer and model receipt to your requested path. Its parent directory must exist. If all attempts fail, older caller files remain; the dispatch exit code identifies that failure.

Dispatch tries the chosen option first, including an exploratory choice outside the ranked top three. Any nonzero exit tries the next distinct choice among the remaining ranked top three, even after launch or receipt publication. Each fallback prints one line naming the failed lane, its exit code and the replacement. This includes unavailable ChatGPT workers, wake failures, timeouts and missing wrappers. Explicit cancellation exits 129, 130 and 143 stop dispatch. Exhausting the choices returns the last failure code and a plain message. A failed write-capable worker may already have changed files; the replacement receives the same task in the resulting workspace.

`--dry-run` prints JSON with the resolved argument list, working directory and planned receipt paths. It creates selection receipts but starts no wrapper or quota refresh process. Its stdout includes the literal task in the argument list; it writes no dispatch receipt or run log. Use it to inspect the call before sending it.

Use `--allow FILE_OR_LIST` with either command to restrict selection to measured benchmark options or council seats, for example `--allow 'codex:gpt-6.1-sol:high,claude:claude-sonnet-5-5:*'`. Files accept one `pool:model_key:level` entry per line or comma-separated entries; blank lines are ignored. Only the level accepts `*`, meaning any admitted level for that pool and model. Other options are rejected with `not in allow list` before scoring, exploration and fallback. Existing admission gates still apply. An empty list or a list with no eligible options produces an error. Every selection receipt records the normalized entries in `allow` (`null` when unrestricted), so changing the file cannot change the recorded restriction.

## Quality evidence

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

```text
score = stakes_weight * quality - pool_lambda * task_pool_percent - mu * latency_seconds
pool_lambda = 0                                           when projected_usage <= target
pool_lambda = lambda0 * (projected_usage - target) / (100 - target) otherwise
pool_lambda = lambda_unknown                              when projection is unavailable
```

Default stakes weights are low 1, normal 2 and high 4. Unknown quality receives an uncertainty discount. For exploitation, an unknown cell with the same mean as a measured cell is dominated by it. The binding quota window is the window with the strongest projected pressure; an unknown projection takes precedence because that window could be binding. `lambda_unknown` defaults to half of `lambda0`, and the option is flagged `quota_projection_unknown`.

`mu` defaults to 0.001 per second of median time to first answer, halves for high stakes and becomes zero for irreversible work. Evidence uses measured `ttfa_s` or `time_to_first_answer_s`, then the publisher's first-answer measurement, then a flagged 120-second placeholder. Total run duration is a separate measure.

`irreversible` selects the highest admitted known quality estimate, ignoring price and latency. All-unknown quality causes refusal.

## Task cost

Measured subscription consumption takes precedence. Evidence rows store `own_cost.<pool>.percent_per_task` and optional `requests_per_task` medians per model and level. A ledger task can provide `pool` and `own_cost: {percent_per_task, requests_per_task}`; linked identity, usage and proof records count once. Estimated selector costs never become measured observations.

When a USD allowance matches the binding window, token medians and API-equivalent input/output prices estimate task percent as `100 * task_usd / allowance_usd`. Configure the allowance in `quota_pools.<pool>.plan.allowance`, including amount, currency and window. An explicitly different window is never used.

Set `quota_pools.<pool>.plan.limit` to `"none-known"` for a pool with no known quota limit: its price is zero, quota snapshots and refresh sources are ignored, and brief/console show "no known limit"; explicit off switches and provider quota errors still block dispatch.

Without that allowance, Go uses `quota_pools.opencode-go.request_estimates.per_5h_week_month.<model>`, an array of request counts for the 5-hour, weekly and monthly limits. A level-keyed object of those arrays is also accepted. Task percent is `100 / binding_request_count`, multiplied by measured requests per task, or 1 if unmeasured.

Other options use API-equivalent task USD divided by the median USD across the pool's admitted options, multiplied by `quota_pools.<pool>.nominal_percent_per_task` (default 0.5). This is flagged `cost_relative_proxy`. Missing prices leave `cost.percent` unknown; scoring uses the nominal percent and flags `cost_unknown_nominal`. Cost uncertainty alone never rejects an option. Each result names its cost basis and the percent used for the penalty.

## Exploration and replay

At low stakes, `policy.selector.explore` defaults to 0.1. That fraction of choices samples uniformly from all admitted options with unknown quality. Other stakes always exploit their highest score.

Every choice and immutable receipt records `selection_probability`, the unconditional probability under the mixture policy. With N unknown options and exploration rate e, an unknown option receives e/N, plus 1-e if it is also the exploitation winner. A measured exploitation winner receives 1-e. With exploration disabled, the chosen option receives 1. This probability survives into wrapper and AFK ledger metadata for later replay.

The JSON result includes the chosen option, the top three options ranked by exploitation score, all pool prices and exact wrapper commands. An exploratory choice can be outside that top three and has its own receipt. Replace the command's `<task>` placeholder and retain its `FLEET_SELECTION_FILE` assignment. The wrapper records the effort that actually ran. A receipt that disagrees with the actual model, lane, effort, role or family is omitted with a `selection_error`. Each alternative command has its own receipt; its probability describes the selector policy, so a manually chosen alternative is not a randomized policy observation.

`policy.selector` also accepts `allowed_pools_by_role`, `stakes_weights`, `target`, `lambda0`, `families_by_role`, `mode_by_role` and `uncertainty_weight`. Source overrides must supply their assumption text and exactly one anchor per anchored family. See the [example overlay](../examples/access-overlay.example.json).

Synthetic regression tests establish these mechanics. A held-out comparison against fixed policies is still needed to establish whether the selector improves real task outcomes.
