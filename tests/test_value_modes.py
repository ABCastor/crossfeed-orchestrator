"""Expected-cost admission, the critical alias, and lead reserve regressions."""
import datetime as dt
import unittest
from unittest import mock

from tests import test_selector as fixtures

fleet = fixtures.fleetctl
selector = fixtures.selector
quality_row = fixtures.quality_row


class ValueModes(unittest.TestCase):
    evidence = fixtures.SelectorTests.evidence
    select = fixtures.SelectorTests.select

    def setUp(self):
        fixtures.SelectorTests.setUp(self)
        self.roster["policy"]["selector"] = {
            "quality_floors_by_role": {"review": .6}, "entitled_only": True, "exploration_cap": 0,
        }
        self.codex = "codex:gpt-6.1-sol:high"
        self.claude = "claude:claude-opus-5-5:high"
        self.allow = self.codex + "," + self.claude

    # BR-SELECT-003
    def test_legacy_role_floor_does_not_gate_weak_or_missing_grades(self):
        cheap = quality_row("gpt-6.1-sol", mean=.99, latency=0)
        weak = {"mean": .59, "sd": .01, "n_sources": 1}
        cheap["q_by_stakes"] = {"high": {"review": weak}, "low": cheap["q"]}
        self.evidence([cheap, quality_row("claude-opus-5-5", mean=.80)])
        with mock.patch.object(selector, "routing_price", return_value=.3):
            result = self.select(allow=self.allow, stakes="high", value_mode="absolute-best")
        self.assertEqual(result["choice"]["pool"], "claude")
        weak_option = next(item for item in result["top3"] if item["pool"] == "codex")
        self.assertEqual(weak_option["q"]["mean"], .59)
        self.assertGreater(weak_option["expected_cost"], result["choice"]["expected_cost"])
        self.assertEqual(self.select(allow=self.codex, value_mode="absolute-best",
                                     stakes="high")["choice"]["pool"], "codex")
        self.assertEqual(self.select(allow=self.codex, stakes="low")["choice"]["q"]["mean"], .59)
        self.assertTrue(self.select(allow=self.codex, family="reasoning")["choice"]["q"]["unknown"])

    # BR-SELECT-003
    def test_absolute_best_is_critical_and_keeps_admission(self):
        self.evidence([quality_row("gpt-6.1-sol", mean=.92, latency=1000),
                       quality_row("claude-opus-5-5", mean=.7, latency=1)])
        opts = dict(allow=self.allow, persist_receipts=False)
        result = self.select(value_mode="absolute-best", **opts)
        critical = self.select(rating_stakes="critical", **opts)
        self.assertEqual(result["rating_stakes"], "critical")
        self.assertEqual(result["choice"], critical["choice"])
        self.assertEqual(self.select(allow=self.claude, value_mode="absolute-best")["choice"]["pool"], "claude")

    # BR-SELECT-003
    def test_removed_soft_claude_first_does_not_remove_pressured_lead_reserve(self):
        self.evidence([quality_row("gpt-6.1-sol", mean=.65), quality_row("claude-opus-5-5", mean=.95)])
        now = fleet.utc_now()
        window = {"used_percent": 45, "window_minutes": 10080,
                  "reset_at": fleet.iso(now + dt.timedelta(hours=48))}
        runtime = {"quota_snapshots": {"claude": {"observed_at": fleet.iso(now), "windows": {"weekly": window}}}}
        window["projected_used_percent_at_reset"] = 79.9
        self.assertEqual(self.select(runtime, lead="claude", allow=self.allow)["choice"]["pool"], "claude")
        window.update(used_percent=92, projected_used_percent_at_reset=108)
        for rating_stakes in ("routine", "critical"):
            result = self.select(runtime, lead="claude", allow=self.allow, rating_stakes=rating_stakes)
            self.assertEqual(result["choice"]["pool"], "codex")
            self.assertEqual(result["lead_pressure"]["pool"], "claude")
            self.assertTrue(any("pressured lead" in row for row in result["rejected"]))
