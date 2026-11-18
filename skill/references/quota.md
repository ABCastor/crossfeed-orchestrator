Load when quota gauges look wrong, the owner requests emergency mode, or the lead's pool reaches CRITICAL.

# Quota and reset clocks

UNKNOWN means missing or stale measurement, not exhaustion: quota admission fails open. EXHAUSTED and an open quota-error circuit fail closed. `forced` overrides a gauge only, never a known refusal or paid daily cap. Other capability, access, switch, mode and lease gates still apply. Reduce parallelism before judgment quality.

With UNKNOWN quota, keep each frontier lane to one slot: one best eligible worker, no duplicate frontier swarm, no automatic same-route retry.

Read `fleetctl.py usage --json` for every measured window and reset. Quota does not roll over. Prefer useful quality-sensitive work on expiring surplus, but do not start unresumable work that cannot finish before reset. Say the reset countdown when it motivates routing. Return to normal discipline after reset.

A pool that has just reset is at its cheapest: front-load its expensive work rather than trickling it out.

`surplus projected` means a window is unlikely to consume its allowance; `SPEND DOWN` adds an imminent reset. They are different. The projection uses an oracle forecast first, otherwise observed burn after enough of the window has elapsed, otherwise the short end-of-window fallback. Selector lambda is zero for surplus, rises with binding-window pressure, and has a configured fallback when projections are unknown.

`fleetctl.py policy` inspects `clock_aware`, `strict` or `off`. Clock-aware ignores projected-surplus windows when determining pressure; strict uses raw percentages; off disables quota gating. Measurement still reports its real state. Changing policy to bypass an owner restriction is forbidden.

A declared `quota_refresh` source can use a provider's own reader (`codex`, `claude`, `antigravity`, `copilot`, `opencode-go`), `command`, `file`, `http` or the optional third-party `codexbar`, with TTL and optional window filtering; null means no percentage source by design. Lazy refresh occurs at consumption points. Fetch outside runtime locks, replace only with strictly newer observations, preserve honest age after failures, and never auto-clear a quota-error circuit. See engine `docs/QUOTA-REFRESH.md` before modifying this mechanism.

A configured oracle failure is separate from UNKNOWN: reuse the last successful active reading for up to six hours, marked stale; otherwise report GAUGE_ERROR with its reason. Selection applies an uncertainty price (at least `lambda0`, default 1.00), retaining the pool as a fallback. `doctor` reports the oracle failure even when a stale reading is usable.

One provider can fund several pools. Antigravity Gemini and third-party models have separate metering windows; never collapse them into one shortage. A healthy measured gauge cannot prove an unreported provider limit is open: before a multi-task agy campaign, run one bounded canary and confirm a real response.

Go `step_finish.cost`, token totals and `opencode stats` are local equivalent-cost estimates, not authoritative Go debit. Do not reconstruct used percentages from them. Legacy `gemini-metered` uses recorded estimated spend under an estimate-only daily cap outside the hard ledger; it can overshoot and is explicit-only, never an automatic fallback. Bounded paid text uses a separate account envelope and reservation, not quota lambda. Keep OpenCode Use balance off.

At lead-pool CRITICAL, check the reset clock first. If no near-reset surplus applies, minimize lead input: summarize long inputs externally, cap digests, batch questions and defer nonessential lead work. Announce the change once. Owner-specific away-window/emergency rules belong in optional live policy, not this engine.

Antigravity Argon uses `antigravity-argon`, separate from Flash's `antigravity-gemini` and Claude's `antigravity-3p`; the Antigravity CLI reports Argon in its own weekly bucket, so a full Argon never means Antigravity is full. Provider reset durations override the generic quota-error cooldown for all AGY pools, including a five-hour Claude limit hidden by a low shared percentage. Run-correlated refusals, plus caller-verified overlay `quota_error` seeds, block until reset and expire automatically. Global CLI logs retain their legacy empty-output diagnostic but cannot seed persistent pool circuits.
