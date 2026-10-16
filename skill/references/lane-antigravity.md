Load before an agy worker, especially when checking transport, workspace or quota failures.

# Antigravity worker

Always pass `--lane` or `--role`; a bare call uses agy's own settings model, not the roster's intended lane. Wrapper flags include --prompt (no --prompt-file), dir, lane/role/model, effort, sandbox, timeout, idle-timeout, kill-after, last and raw. Selector dispatch sets the chosen model/level; console gates still apply.

The wrapper runs `agy --print` under a PTY because non-TTY stdout can be empty even after successful work. It passes `--add-dir`: changing cwd alone does not add a workspace. Missing workspace can look like file blindness; distinguish wrapper configuration from model capability before recording a limitation. The PTY passes an argv array, not shell-interpolated prompt text. Avoid newest-global-transcript recovery under concurrency; it may identify another run.

Gemini and third-party models spend separate pools, antigravity-gemini and antigravity-3p, with separate windows. A measured healthy gauge can omit an individual provider limit. Before a multi-task campaign, run a small canary and require actual output. Treat empty output and provider quota refusal as different failures; an exit 0 with nothing is not success.

Keep long analytical writes off this transport until its write-step failure is disproved by current evidence. Wrapper watchdog settings cannot repair a cap inside agy. Use an admitted transport proved for long work, preserving the task's quality floor; do not relabel agy as unable to read/write local files.

No attachment flag here: advertised model media capability is not a delivered file transport. Use the media reference for audio/video. Prompt for a terse digest when narration is unwanted. Every writable report belongs inside the work root and is written incrementally.

Exits: 3 may mean exhausted quota; 4 empty output; 5 no route/lease/control gate; 124 wall budget; 125 idle kill. Inspect correlated diagnostics and partial writes before retrying. The idle watchdog defaults to 0 (diagnostics only; a positive `--idle-timeout` opts into killing), no default wall clock. Follow the core never-detach and writer-claim rules.

## Current catalogue and provisional routing

AGY 1.4.0, caller-verified on 16 October 2026. The supported selectors below are the declared admitted list. Tests compare it with the offline catalogue fixture and the example roster's active model/effort combinations.

<!-- agy-models:start -->
| Model | Pool | Selectors |
|---|---|---|
| gemini-4-argon | antigravity-gemini | `gemini-4-argon-low`, `gemini-4-argon-medium`, `gemini-4-argon-high` |
| gemini-3.8-flash | antigravity-gemini | `gemini-3.8-flash-low`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-high` |
| claude-opus-5-5 | antigravity-3p | `claude-opus-5-5-low`, `claude-opus-5-5-medium`, `claude-opus-5-5-high` |
| claude-sonnet-5-5 | antigravity-3p | `claude-sonnet-5-5-low`, `claude-sonnet-5-5-medium`, `claude-sonnet-5-5-high` |
<!-- agy-models:end -->

GPT-OSS 120B medium is visible but excluded: no demonstrated job advantage on the shared third-party pool. The removed Pro, Flash 3.6/3.7 and Claude 4.6 routes are superseded, with named current replacements. Native Claude's Opus, Sonnet and Haiku 5.5 remain separate from AGY controls and quotas. Haiku 5.5 is not in this AGY catalogue.

The owner's Argon design and writing preference is provisional. Manual `route` rankings put it first for design, visual, frontend-visual and writing, subject to switches and quota gates. `select` and `dispatch` continue to score measured task evidence; a ranking is no numeric quality bonus. An unknown family stays unknown. Writing has its own evidence family, so coding or visual scores cannot silently establish writing quality. Choose write mode for AGY, including prompt-only writing, because this transport has no proven read-only boundary. The existing restriction on long analytical writes remains in force.

The caller's Argon-high and Opus-high canaries returned OK. Other levels and Sonnet have catalogue evidence only. None of those canaries proves task quality, long-write reliability or native image delivery. Recheck those boundaries before a campaign.

Outside evidence checked 16 October: [Text Arena](https://arena.ai/leaderboard/chat/text) places Argon high first with preliminary results; [Writing, Literature and Language](https://arena.ai/leaderboard/chat/text/industry-writing-and-literature-and-language) is a distinct category; [WebDev Arena](https://arena.ai/leaderboard/code/webdev) places Argon high ninth, preliminary, below native Claude Opus 5.5 max and Sonnet 5.5 xhigh. This does not establish a design lead. These are preference priors, not local task checks. [Artificial Analysis](https://artificialanalysis.ai/models/gemini-4-argon) has a high-level model record; raw AA scores belong only in local runtime state. Refresh through the existing market tooling with the updated overlay. Do not copy high-level scores to low or medium. Existing evidence gives each source a small weighted pseudo-sample budget; every mechanically checked local fit trial adds one observation. Own trials therefore progressively outweigh priors without erasing provenance.
