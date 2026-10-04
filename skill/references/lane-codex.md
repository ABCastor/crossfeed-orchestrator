Load for a Codex worker needing sandbox, schema, resume or native-subagent details beyond a plain dispatch.

# Codex worker

`codex-agent.sh` wraps official `codex exec` on the ChatGPT login. Core flags: `--prompt`/`--prompt-file`, `--dir`, `--model`, `--role`, `--reasoning`, `--sandbox read-only|workspace-write`, `--schema`, `--events`, `--last`, `--log`, `--timeout`, `--idle-timeout`, `--kill-after`, `--dry-run`. It passes the working root, closes stdin, always sets resolved model_reasoning_effort and resolves console stand-ins before effort.

`read-only` denies writes. `workspace-write` permits work-root/configured temp writes, not arbitrary home/system writes. Codex cannot write `.git`; the lead reviews, commits and runs full suites outside the sandbox. Thread-dependent aiosqlite/asyncio.to_thread tests can hang and system ps may be blocked. Run meaningful targeted checks within the sandbox, report exact limits, then verify affected full behavior outside it.

Give a real project/worktree root, never bare home. Its report must be writable inside `--dir`; incremental writes retain value after a kill. Use unique artifact paths. Exit 4 means blank/whitespace deliverable despite a successful CLI exit, not a retryable launch refusal. Exit 5 can be a prelaunch control gate; 8 can mean identity/ledger failure with a deliverable. Read stderr and sidecars before deciding recovery.

Schema mode constrains final output; stdout and `--last` preserve the original schema JSON unchanged. The receipt lives in the sidecar. Other runs print final text; model receipts are on stderr and the `.crossfeed.json` sidecar. Do not infer an actual identity from a requested or selected model when confirmation is absent.

The worker can inherit configured MCP reach. Include only required context/tool access and disable a live tool connection when rebuilding that server. Resume by explicit session ID when available; `codex exec resume --last` is best-effort and unsafe as an identity for concurrent work.

For decomposable work, explicitly request native subagents in the brief if their harness permits it: map scope, give one disjoint build unit to each builder, fresh-context diff review, then run the named proof command. Do not hard-code historical seats; use installed agent definitions and console-aware selection. Native subagents share vendor lineage and never replace independent cross-vendor review. The lead verifies returned claims and owns acceptance.
