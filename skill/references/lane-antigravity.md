Load before an agy worker, especially when checking transport, workspace or quota failures.

# Antigravity worker

Always pass `--lane` or `--role`; a bare call uses agy's own settings model, not the roster's intended lane. Wrapper flags include --prompt (no --prompt-file), dir, lane/role/model, effort, sandbox, timeout, idle-timeout, kill-after, last and raw. Selector dispatch sets the chosen model/level; console gates still apply.

The wrapper runs `agy --print` under a PTY because non-TTY stdout can be empty even after successful work. It passes `--add-dir`: changing cwd alone does not add a workspace. Missing workspace can look like file blindness; distinguish wrapper configuration from model capability before recording a limitation. The PTY passes an argv array, not shell-interpolated prompt text. Avoid newest-global-transcript recovery under concurrency; it may identify another run.

Gemini and third-party models spend separate pools, antigravity-gemini and antigravity-3p, with separate windows. A measured healthy gauge can omit an individual provider limit. Before a multi-task campaign, run a small canary and require actual output. Treat empty output and provider quota refusal as different failures; an exit 0 with nothing is not success.

Keep long analytical writes off this transport until its write-step failure is disproved by current evidence. Wrapper watchdog settings cannot repair a cap inside agy. Use an admitted transport proved for long work, preserving the task's quality floor; do not relabel agy as unable to read/write local files.

No attachment flag here: advertised model media capability is not a delivered file transport. Use the media reference for audio/video. Prompt for a terse digest when narration is unwanted. Every writable report belongs inside the work root and is written incrementally.

Exits: 3 may mean exhausted quota; 4 empty output; 5 no route/lease/control gate; 124 wall budget; 125 idle kill. Inspect correlated diagnostics and partial writes before retrying. The idle watchdog defaults to 2400 seconds, no default wall clock. Follow the core never-detach and writer-claim rules.
