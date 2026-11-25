"""The operator can ignore quota estimates without overriding hard limits."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_selector import ROOT, fleetctl, quality_row, selector

NOW = dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc)
MODEL = "gpt-6.1-sol"


class ForcedQuotaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["codex", "claude"]}}
        self.roster["quota_pools"]["codex"]["value_capacity"] = {
            "weekly_usd_equivalent": 50, "source": "test plan"}
        (self.state / "evidence").mkdir()
        rows = [dict(quality_row(MODEL, pass_rate=.6), own_cost={"codex": {"percent_per_task": 1}}),
                quality_row("claude-opus-5-5", pass_rate=.5)]
        (self.state / "evidence/levels.json").write_text(json.dumps({"rows": rows}))
        for patch in (mock.patch.object(fleetctl, "utc_now", return_value=NOW),
                      mock.patch.dict("os.environ", {"FLEET_QUOTA_POLICY": "clock_aware",
                                                     "FLEET_IGNORE_QUOTA": "0"})):
            patch.start()
            self.addCleanup(patch.stop)

    def runtime(self, level="forced", used=91, *, refusal=False, expired=False, age_hours=0):
        reset = NOW + dt.timedelta(hours=-1 if expired else 96)
        runtime = {"switches": {"codex": level}, "quota_snapshots": {"codex": {
            "source": "codex-oauth-usage", "observed_at": fleetctl.iso(NOW - dt.timedelta(hours=age_hours)),
            "windows": {"weekly": {"used_percent": used, "window_minutes": 10080,
                                   "reset_at": fleetctl.iso(reset), "projected_used_percent_at_reset": 180}},
        }}}
        if refusal:
            runtime["pool_circuits"] = {"codex": {"until": fleetctl.iso(reset), "limit_name": "weekly"}}
        return runtime

    def select(self, runtime, *, lead="codex", constrained=False):
        return selector.select_option(self.roster, runtime, self.state, "review", lead=lead, fleet=fleetctl,
                                      no_explore=True, persist_receipts=False, collect_eligible=True,
                                      allow=f"codex:{MODEL}:high" if constrained else None)

    # BR-SELECT-007
    def test_forced_codex_ignores_estimates_not_hard_stops(self):
        runtime = self.runtime()
        result = self.select(runtime, constrained=True)
        self.assertIsNone(result["lead_pressure"])
        option = result["choice"]
        self.assertEqual(option["pool"], "codex")
        self.assertEqual(option["pool_price"]["lambda"], 0)
        self.assertEqual(option["estimated_price"], .50)
        for runtime in (self.runtime(used=100), self.runtime(used=100, age_hours=2),
                        self.runtime(refusal=True), self.runtime(level="off")):
            with self.subTest(runtime=runtime), self.assertRaisesRegex(fleetctl.FleetError, "pool exhausted or switched off"):
                self.select(runtime, lead=None, constrained=True)
        self.assertEqual(self.select(self.runtime(used=100, expired=True), constrained=True)["choice"]["pool"], "codex")
        rounded = self.runtime(used=100)
        rounded["quota_snapshots"]["codex"]["provider_used_percent"] = {"weekly": 99.8}
        self.assertEqual(self.select(rounded, constrained=True)["choice"]["pool"], "codex")

    # BR-SELECT-007: the same selector sees reserve and inflated price under both other levels.
    def test_normal_and_high_still_reserve_and_price_pressure(self):
        for level in ("normal", "high"):
            for used in (76, 91):
                with self.subTest(level=level, used=used):
                    runtime = self.runtime(level, used=used)
                    protected = self.select(runtime)
                    self.assertEqual(protected["lead_pressure"]["used_percent"], used)
                    self.assertNotIn("codex", {item["pool"] for item in protected["eligible_options"]})
                    unreserved = self.select(runtime, lead=None, constrained=True)["choice"]
                    self.assertGreater(unreserved["pool_price"]["lambda"], 0)
                    self.assertGreater(unreserved["estimated_price"], .50)
