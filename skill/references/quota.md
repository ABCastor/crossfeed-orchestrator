Load when quota gauges look wrong, the owner requests emergency mode, or the lead's pool reaches CRITICAL.

# Quota and reset clocks

UNKNOWN means missing or stale measurement, not exhaustion: quota admission fails open. EXHAUSTED and an open quota-error circuit fail closed. `forced` overrides a gauge only, never a known refusal or paid daily cap. Other capability, access, switch, mode and lease gates still apply. Reduce parallelism before judgment quality.

With UNKNOWN quota, keep each frontier lane to one slot: one best eligible worker, no duplicate frontier swarm, no automatic same-route retry.

Read `fleetctl.py usage --json` for every measured window and reset. Quota does not roll over. Prefer useful quality-sensitive work on expiring surplus, but do not start unresumable work that cannot finish before reset. Say the reset countdown when it motivates routing. Return to normal discipline after reset.

A pool that has just reset is at its cheapest: front-load its expensive work rather than trickling it out.

`surplus projected` means a window is unlikely to consume its allowance; `SPEND DOWN` adds an imminent reset. They are different. The projection uses an oracle forecast first, otherwise observed burn after enough of the window has elapsed, otherwise the short end-of-window fallback. Selector lambda is zero for surplus, rises with binding-window pressure, and has a configured fallback when projections are unknown.

`fleetctl.py policy` inspects `clock_aware`, `strict` or `off`. Clock-aware ignores projected-surplus windows when determining pressure; strict uses raw percentages; off disables quota gating. Measurement still reports its real state. Changing policy to bypass an owner restriction is forbidden.

A declared `quota_refresh` source can use `codexbar`, `command`, `file` or `http`, with TTL and optional window filtering; null means no percentage source by design. Lazy refresh occurs at consumption points. Fetch outside runtime locks, replace only with strictly newer observations, preserve honest age after failures, and never auto-clear a quota-error circuit. See engine `docs/QUOTA-REFRESH.md` before modifying this mechanism.

One provider can fund several pools. Antigravity Gemini and third-party models have separate metering windows; never collapse them into one shortage. A healthy measured gauge cannot prove an unreported provider limit is open: before a multi-task agy campaign, run one bounded canary and confirm a real response.

Go `step_finish.cost`, token totals and `opencode stats` are local equivalent-cost estimates, not authoritative Go debit. Do not reconstruct used percentages from them. Paid `gemini-metered` instead uses recorded estimated spend under its daily USD cap; its cap is a safety rail, not authoritative billing. Keep OpenCode Use balance off.

At lead-pool CRITICAL, check the reset clock first. If no near-reset surplus applies, minimize lead input: summarize long inputs externally, cap digests, batch questions and defer nonessential lead work. Announce the change once. Owner-specific away-window/emergency rules belong in optional live policy, not this engine.
