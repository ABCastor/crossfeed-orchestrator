"""Hard-grade gating, the critical alias, and lead reserve regressions."""
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

    def test_hard_gate_precedes_value_and_cannot_use_low_fit_or_relabel(self):
        cheap = quality_row("gpt-6.1-sol", mean=.99, latency=0)
        weak = {"mean": .59, "sd": .01, "n_sources": 1}
        cheap["q_by_stakes"] = {"high": {"review": weak}, "low": cheap["q"]}
        self.evidence([cheap, quality_row("claude-opus-5-5", mean=.61)])
        with mock.patch.object(selector, "_task_cost", wraps=selector._task_cost) as cost:
            result = self.select(allow=self.allow, stakes="low")
        self.assertEqual(result["choice"]["pool"], "claude")
        self.assertEqual([call.args[1]["pool"] for call in cost.call_args_list], ["claude"])
        with self.assertRaisesRegex(fleet.FleetError, r"grade 0\.590 does not clear review floor 0\.600"):
            self.select(allow=self.codex, value_mode="absolute-best")
        with self.assertRaisesRegex(fleet.FleetError, "quality floor requires task family review"):
            self.select(allow=self.allow, family="reasoning")

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
