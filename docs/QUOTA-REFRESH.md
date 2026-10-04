# Quota refresh

Quota sources are declared per pool in the overlay. Refresh at the point where usage, selection, dispatch or lease acquisition consumes pool state, so a long session does not route from a stale snapshot.

Each `quota_refresh` block declares an oracle (`codexbar`, `command`, `file` or `http`), TTL and optional window filter. Null means no percentage source. Missing measurements stay UNKNOWN; measured exhaustion, provider-error circuits and paid caps still refuse dispatch. Paid per-token pools use spend-cap admission.

Fetch outside the runtime lock. Under lock, replace only with a strictly newer observation, preserving manual overrides and concurrent writers. A failed refresh retains the previous observation and its real age. Cooldowns limit repeated source failures. Synthetic tests use no-refresh to keep local telemetry out of fixtures.

Model pools at the metering boundary. One source can feed independent pools through disjoint window filters; fetch it once per refresh. Selection refreshes every candidate pool it consults. Local cost and token telemetry remain estimates, never reconstructed subscription percentages.

A fresh percentage does not clear a circuit opened by a provider quota refusal. Use the supported recovery/snapshot path deliberately; a gauge cannot prove service availability.

HTTP oracles are GET-only and use supported schemes. Bearer credentials must not travel over plaintext. Keep key values out of logs and manifests. Test declarations and independent pool gating before changing quota boundaries or refresh behavior.
