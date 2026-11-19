"""Hard-grade gating, cost independence, and weekly reserve regressions."""
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
            "quality_floors_by_role": {"review": .6}, "entitled_only": True, "variety": False,
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

    def test_absolute_best_ignores_cost_but_not_admission(self):
        self.roster["policy"]["selector"]["mu"] = .1
        self.evidence([quality_row("gpt-6.1-sol", mean=.92, latency=1000),
                       quality_row("claude-opus-5-5", mean=.7, latency=1)])
        self.assertEqual(self.select(allow=self.allow)["choice"]["pool"], "claude")
        result = self.select(allow=self.allow, value_mode="absolute-best")
        self.assertEqual((result["choice"]["pool"], result["choice"]["score"]), ("codex", .92))
        self.assertEqual(result["selection_probability"], 1)
        self.assertEqual(self.select(allow=self.claude, value_mode="absolute-best")["choice"]["pool"], "claude")

    def test_weekly_reserve_uses_strict_threshold_and_unknown_does_not_prefer(self):
        self.evidence([quality_row("gpt-6.1-sol", mean=.95), quality_row("claude-opus-5-5", mean=.65)])
        now = fleet.utc_now()
        window = {"used_percent": 45, "window_minutes": 10080,
                  "reset_at": fleet.iso(now + dt.timedelta(hours=48))}
        snapshot = {"observed_at": fleet.iso(now), "windows": {"weekly": window}}
        runtime = {"quota_snapshots": {"claude": snapshot}}
        for projected, expected in ((79.9, "claude"), (80, "codex"), (None, "codex")):
            with self.subTest(projected=projected):
                window["projected_used_percent_at_reset"] = projected
                window["will_last_to_reset"] = True
                result = self.select(runtime, allow=self.allow)
                self.assertEqual(result["choice"]["pool"], expected)
                self.assertEqual(result["claude_preference"], expected == "claude")
                self.assertEqual(self.select(runtime, allow=self.allow, value_mode="absolute-best")["choice"]["pool"], "codex")
