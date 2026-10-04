Load for OpenCode worker attachments, direct calls, context modes, research-scout or detailed failures.

# OpenCode worker

Default is read-only, `--context lean`: compact global worker contract, project instructions preserved, skill catalogue disabled. `--context shared` restores synchronized skills/policy only when needed. Project config suppression and isolated per-run profiles prevent arbitrary project MCP/provider policy from redefining the worker boundary. `--pure` disables plugins; wrapper verification is explicit.

Choose automatic `--role` or one explicit `--lane`, `--model-key` or `--model`, never conflicting selectors. `--variant` overrides a supported level; `--effort-role` selects role-level resolution with an explicit lane. Writes need `--write` and a clean disposable git worktree. Plan denies shell/edit/nested agents/external dirs; it is a tool-policy boundary, not an OS sandbox. Build retains denied nested/external tools; the caller owns isolation and full proof.

Each wrapper run has a unique XDG_DATA_HOME and opencode.db, copying only required auth. Success cleans it; failure may keep it and name the path. Raw parallel opencode calls can share the default DB and collide. Use wrapper isolation, not a global concurrency myth.

Automatic routing can skip busy lanes. An explicitly named lane is never silently substituted: it waits for OPENCODE_LANE_WAIT_S (default 900), then refuses. The roster `timeout_s` remains a hard budget without explicit --timeout because leases lack atomic renewal; idle watchdog still defaults to 2400. For work expected to run past the lane's `timeout_s`, pass an explicit `--timeout` or raise the lane's `timeout_s` in the overlay; never add an outer timeout.

`--direct --lane <lane>` is one deny-all text-only model call with no repository/project context or tools. It rejects writes, attachments, injection, shared context and web search. Fanout does not expose a direct field; use an explicit wrapper call when appropriate.

Media requires `--file` and `--modality` and an admitted input capability. Advertised input is not a live transport/quality proof. Treat audio/video and other route transport as unverified until your own transport tests establish them. Native Gemini is the supported audio/video path.

`--role research-scout` enables hosted Exa search in an empty generated directory, with no local filesystem/shell/edit/skill/question/todo/nested tools, no attachments and a bounded iteration count. Its prompt goes to Go and generated queries to Exa: public material only. No private project facts, personal data, credentials or unreleased names. A citation counts only after the lead opens it. This is source discovery, not a vendor-native deep-research product; the lead owns coverage, weighting, contradiction handling and synthesis. Arbitrary URL fetch stays denied to avoid private-network access.

Ordinary local read-only workers do not need web search. Never transmit secrets/customer/production/regulated records. Sensitive evidence stays on approved private transports unless the owner authorizes this specific model/provider.

Go cost telemetry is local equivalent cost, not quota debit. Keep Use balance disabled. Paid Google lanes are explicit-only with the daily cap, not swarm fallback. See [quota](quota.md) for measurement truth boundaries.

Exit 4 is overloaded: lease/capacity refusal or invalid JSON events. Exit 5 may be a prelaunch gate or session error. Exit 6 means no successful terminal step, 7 empty text, 8 telemetry failure (deliverable may exist). The wrapper rejects a native exit 0 with error/no-success/empty terminal state. Inspect evidence before retrying; raw 4/5 is never enough to justify a second worker.
