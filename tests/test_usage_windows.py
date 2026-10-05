"""Applicable actual limits constrain forecasts, routing, and spare advice."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import selector


class UsageWindowTests(unittest.TestCase):
    def setUp(self):
        self.now = fleetctl.utc_now()
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(fleetctl, "utc_now", return_value=self.now).start()
        patch.object(fleetctl, "_POLICY_CACHE", ("clock_aware", "test")).start()
        patch.dict(fleetctl.os.environ, {"FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0"}).start()

    def window(self, used, minutes=300, left=600, **extra):
        return {"used_percent": used, "window_minutes": minutes,
                "reset_at": fleetctl.iso(self.now + dt.timedelta(seconds=left)),
                "will_last_to_reset": True, **extra}

    def runtime(self, short=5, weekly=99, pool="claude"):
        return {"quota_snapshots": {pool: {"source": "fixture", "observed_at": fleetctl.iso(self.now),
                "windows": {"primary": self.window(short, label="5-hour"),
                            "secondary": self.window(weekly, 10080, label="Weekly")}}}}

    def test_either_actual_window_binds_at_existing_critical_threshold(self):
        for used in (90, 99, 100):
            for short, weekly, name in ((5, used, "secondary"), (used, 5, "primary")):
                with self.subTest(short=short, weekly=weekly):
                    runtime = self.runtime(short, weekly)
                    original = copy.deepcopy(runtime)
                    state, evidence = fleetctl.current_pool_state(runtime, "claude", roster=self.roster)
                    self.assertEqual(state, "EXHAUSTED" if used == 100 else "CRITICAL")
                    self.assertEqual(evidence["bottleneck_used_percent"], used)
                    self.assertEqual(evidence["spend_down"], [])
                    self.assertNotIn(name, evidence.get("non_binding", []))
                    self.assertEqual(fleetctl.task_band(state, evidence), "critical")
                    if used < 100:
                        self.assertEqual(fleetctl.effective_cap(
                            {"quality_tier": "frontier", "max_parallel": 3}, state, evidence), 1)
                    else:
                        self.assertIn(name, evidence["limit_names"])
                    self.assertEqual(runtime, original)

    def test_low_usage_forecast_still_relaxes_conservation(self):
        runtime = self.runtime(short=80, weekly=5)
        state, evidence = fleetctl.current_pool_state(runtime, "claude", roster=self.roster)
        self.assertEqual(state, "CONSERVE")
        self.assertEqual(evidence["routing_state"], "ABUNDANT")
        self.assertEqual(fleetctl.task_band(state, evidence), "quality_first")
        self.assertEqual(evidence["spend_down"], ["primary", "secondary"])

    def test_projection_pressure_never_falls_below_actual_usage(self):
        for short, weekly, name in ((5, 99, "secondary"), (99, 5, "primary")):
            runtime = self.runtime(short, weekly)
            for window in runtime["quota_snapshots"]["claude"]["windows"].values():
                window["projected_used_percent_at_reset"] = 10
            with self.subTest(short=short, weekly=weekly):
                price = selector._pool_price(self.roster, runtime, "claude", {}, fleetctl)
                self.assertEqual(price["binding_window"], name)
                self.assertEqual(price["pressure_percent"], 99)
                self.assertEqual(price["projected_used_percent_at_reset"], 10)
                self.assertAlmostEqual(price["lambda"], .9)

    def test_unknown_projection_price_has_an_actual_usage_floor(self):
        runtime = self.runtime()
        for window in runtime["quota_snapshots"]["claude"]["windows"].values():
            window.pop("window_minutes")
        price = selector._pool_price(self.roster, runtime, "claude", {"lambda_unknown": 0}, fleetctl)
        self.assertEqual(price["binding_window"], "secondary")
        self.assertTrue(price["projection_unknown"])
        self.assertAlmostEqual(price["lambda"], .9)

    def test_unknown_window_price_cannot_be_erased_by_another_window(self):
        runtime = self.runtime(short=1, weekly=99)
        windows = runtime["quota_snapshots"]["claude"]["windows"]
        windows["primary"].pop("window_minutes")
        windows["secondary"]["projected_used_percent_at_reset"] = 99
        policy = {"lambda_unknown": 5}
        combined = selector._pool_price(self.roster, runtime, "claude", policy, fleetctl)
        separate = []
        for name, window in windows.items():
            individual = copy.deepcopy(runtime)
            individual["quota_snapshots"]["claude"]["windows"] = {name: window}
            separate.append(selector._pool_price(self.roster, individual, "claude", policy, fleetctl)["lambda"])
        self.assertEqual(combined["lambda"], max(separate))
        self.assertEqual(combined["lambda"], 5)
        self.assertEqual(combined["binding_window"], "primary")
        self.assertTrue(combined["projection_unknown"])
        self.assertEqual(combined["state"], "CRITICAL")

    def test_higher_predictive_pressure_is_preserved(self):
        runtime = self.runtime(short=5, weekly=80)
        windows = runtime["quota_snapshots"]["claude"]["windows"]
        windows["primary"]["projected_used_percent_at_reset"] = 120
        windows["secondary"]["projected_used_percent_at_reset"] = 85
        price = selector._pool_price(self.roster, runtime, "claude", {}, fleetctl)
        self.assertEqual(price["binding_window"], "primary")
        self.assertEqual(price["pressure_percent"], 120)
        self.assertAlmostEqual(price["lambda"], 3)

    def test_model_only_exhaustion_blocks_matching_model_and_keeps_other_models(self):
        runtime = self.runtime(short=5, weekly=5)
        runtime["quota_snapshots"]["claude"]["windows"]["claude-weekly-scoped-sonnet"] = self.window(
            100, 10080, label="Sonnet only")
        for model, expected in ((None, "ABUNDANT"), ("claude-opus-5-5", "ABUNDANT"),
                                ("claude-sonnet-5-5", "EXHAUSTED")):
            with self.subTest(model=model):
                state, evidence = fleetctl.current_pool_state(runtime, "claude", roster=self.roster,
                                                              model_key=model)
                self.assertEqual(state, expected)
                self.assertIn("claude-weekly-scoped-sonnet", evidence["windows"])
                self.assertEqual("claude-weekly-scoped-sonnet" in evidence["applicable_windows"],
                                 expected == "EXHAUSTED")
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["claude"]}}
        options, rejected = selector.enumerate_options(self.roster, runtime, "review", fleet=fleetctl)
        self.assertTrue(any(option["model_key"] == "claude-opus-5-5" for option in options))
        self.assertFalse(any("sonnet" in option["model_key"] for option in options))
        self.assertTrue(any("sonnet" in reason and "exhausted" in reason for reason in rejected))

    def test_model_only_critical_window_suppresses_matching_model_spare_advice(self):
        runtime = self.runtime(short=5, weekly=5)
        runtime["quota_snapshots"]["claude"]["windows"]["claude-weekly-scoped-sonnet"] = self.window(
            99, 10080, label="Sonnet only", projected_used_percent_at_reset=10)
        _, opus = fleetctl.current_pool_state(runtime, "claude", roster=self.roster, model_key="claude-opus-5-5")
        state, sonnet = fleetctl.current_pool_state(runtime, "claude", roster=self.roster, model_key="claude-sonnet-5-5")
        self.assertTrue(opus["spend_down"])
        self.assertEqual(state, "CRITICAL")
        self.assertEqual(sonnet["spend_down"], [])
        price = selector._pool_price(self.roster, runtime, "claude", {}, fleetctl, model_key="claude-sonnet-5-5")
        self.assertEqual(price["binding_window"], "claude-weekly-scoped-sonnet")
        self.assertAlmostEqual(price["lambda"], .9)

    def test_model_only_reading_remains_visible_without_a_general_window(self):
        runtime = self.runtime()
        runtime["quota_snapshots"]["claude"]["windows"] = {
            "scoped-sonnet": self.window(99, 10080, label="Sonnet only")}
        overview = fleetctl.fleet_overview(self.roster, runtime, self.state)
        pool = next(row for row in overview["pools"] if row["pool"] == "claude")
        self.assertEqual(pool["state"], "UNKNOWN")
        self.assertIsNone(pool["quota"])
        self.assertEqual(pool["limits"][0]["used_percent"], 99)
        self.assertFalse(pool["limits"][0]["spend_down"])

    def test_overview_and_brief_cannot_advertise_spare_through_critical_window(self):
        for short, weekly, window in ((5, 99, "7d"), (99, 5, "5h")):
            with self.subTest(short=short, weekly=weekly):
                runtime = self.runtime(short, weekly)
                overview = fleetctl.fleet_overview(self.roster, runtime, self.state)
                pool = next(row for row in overview["pools"] if row["pool"] == "claude")
                self.assertEqual(pool["state"], "CRITICAL")
                self.assertEqual(pool["quota"]["used_percent"], 99)
                self.assertEqual(pool["quota"]["window"], window)
                self.assertFalse(pool["quota"]["spend_down"])
                self.assertFalse(any(limit["spend_down"] for limit in pool["limits"]))
                line = fleetctl._brief_entry(pool)
                self.assertNotIn("spare", line.lower())
                self.assertNotIn("spend down", line.lower())

    def test_router_and_lease_use_matching_scoped_window(self):
        runtime = self.runtime(short=5, weekly=5, pool="opencode-go")
        runtime["quota_snapshots"]["opencode-go"]["windows"]["scoped-kimi"] = self.window(
            100, label="Kimi only")
        routed = copy.deepcopy(self.roster)
        candidates = [lane["lane_id"] for lane in routed["lanes"]
                      if lane.get("quota_pool") == "opencode-go" and lane["model_key"] in {
                          "kimi-k3", "deepseek-v4-flash"}]
        candidates.sort(key=lambda value: "kimi" not in value)
        routed["routing"]["roles"]["default"] = {"quality_first": candidates}
        selected = fleetctl.choose_lane(routed, runtime, "default", "read-only", "text")
        self.assertEqual(selected["model_key"], "deepseek-v4-flash")
        (self.state / "runtime.json").write_text(json.dumps(runtime))
        with self.assertRaisesRegex(fleetctl.FleetError, "exhausted"):
            fleetctl.acquire_lease(self.state, routed, candidates[0], 600)

    def test_routed_model_critical_scope_applies_its_role_allowlist(self):
        runtime = self.runtime(short=5, weekly=5, pool="opencode-go")
        runtime["quota_snapshots"]["opencode-go"]["windows"]["scoped-kimi"] = self.window(
            99, label="Kimi only")
        routed = copy.deepcopy(self.roster)
        kimi = next(lane["lane_id"] for lane in routed["lanes"] if lane["model_key"] == "kimi-k3")
        flash = next(lane["lane_id"] for lane in routed["lanes"] if lane["model_key"] == "deepseek-v4-flash")
        routed["routing"]["roles"]["default"] = {"quality_first": [kimi], "critical": [flash]}
        for used in (99, 100):
            with self.subTest(used=used):
                runtime["quota_snapshots"]["opencode-go"]["windows"]["scoped-kimi"]["used_percent"] = used
                selected = fleetctl.choose_lane(routed, runtime, "default", "read-only", "text")
                self.assertEqual(selected["lane_id"], flash)

    def test_scoped_routing_keeps_unrelated_quality_candidates_ahead_of_fallbacks(self):
        runtime = self.runtime(short=5, weekly=5, pool="opencode-go")
        runtime["quota_snapshots"]["opencode-go"]["windows"]["scoped-kimi"] = self.window(
            99, label="Kimi only")
        routed = copy.deepcopy(self.roster)
        keys = {lane["model_key"]: lane["lane_id"] for lane in routed["lanes"]
                if lane.get("quota_pool") == "opencode-go"}
        routed["routing"]["roles"]["default"] = {
            "quality_first": [keys["kimi-k3"], keys["deepseek-v4-pro"]],
            "critical": [keys["deepseek-v4-flash"]]}
        selected = fleetctl.choose_lane(routed, runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], keys["deepseek-v4-pro"])

    def test_exact_scopes_do_not_match_shared_vendor_or_version_words(self):
        for pool, name, label, matching, unrelated in (
            ("codex", "scoped-gpt-6.1-sol", "gpt-6.1-sol only", "gpt-6.1-sol",
             ["gpt-6-astra", "gpt-6-luna", "gpt-6-sol"]),
            ("opencode-go", "scoped-deepseek-v4-pro", "DeepSeek V4 Pro only", "deepseek-v4-pro",
             ["deepseek-v4-flash"]),
            ("claude", "claude-weekly-scoped-sonnet-5-5", "Claude Sonnet 5.5 only", "claude-sonnet-5-5",
             ["claude-sonnet-5", "claude-opus-5-5"]),
            ("claude", "claude-weekly-scoped-sonnet-5-5", "Sonnet 5.5 only", "claude-sonnet-5-5",
             ["claude-sonnet-5", "claude-opus-5-5"]),
        ):
            with self.subTest(pool=pool, matching=matching):
                window = self.window(100, label=label)
                self.assertTrue(fleetctl.quota_window_applies(self.roster, pool, name, window, matching))
                self.assertFalse(fleetctl.quota_window_applies(self.roster, pool, name, window))
                for model in unrelated:
                    self.assertFalse(fleetctl.quota_window_applies(self.roster, pool, name, window, model), model)
                    runtime = self.runtime(short=5, weekly=5, pool=pool)
                    runtime["quota_snapshots"][pool]["windows"][name] = window
                    state, _ = fleetctl.current_pool_state(runtime, pool, roster=self.roster, model_key=model)
                    self.assertEqual(state, "ABUNDANT", model)

    def test_unknown_numeric_scopes_do_not_fall_back_to_known_families(self):
        for pool, name, label, model in (
            ("codex", "scoped-gpt-6-2-sol", "GPT6.2Sol only", "gpt-6.1-sol"),
            ("codex", "scoped-gpt-6-2-sol", "GPT Sol only", "gpt-6.1-sol"),
            ("claude", "scoped-sonnet-6", "Sonnet6 only", "claude-sonnet-5-5"),
            ("claude", "scoped-sonnet-5-6", "Sonnet 5.6 only", "claude-sonnet-5"),
        ):
            with self.subTest(label=label, model=model):
                window = self.window(100, label=label)
                self.assertFalse(fleetctl.quota_window_applies(self.roster, pool, name, window, model))
                runtime = self.runtime(short=5, weekly=5, pool=pool)
                runtime["quota_snapshots"][pool]["windows"][name] = window
                state, _ = fleetctl.current_pool_state(runtime, pool, roster=self.roster, model_key=model)
                self.assertEqual(state, "ABUNDANT")

    def test_combined_family_scope_binds_each_named_family(self):
        for name, label in (("scoped-combined", "Sonnet and Opus only"),
                            ("scoped-combined", "Sonnet or Opus only"),
                            ("claude-weekly-scoped-sonnet-and-opus", ""),
                            ("claude_weekly_scoped_sonnet_or_opus", "Weekly")):
            for model in ("claude-sonnet-5-5", "claude-opus-5-5"):
                with self.subTest(label=label, model=model):
                    window = self.window(100, label=label)
                    self.assertTrue(fleetctl.quota_window_applies(self.roster, "claude", name, window, model))
                    self.assertFalse(fleetctl.quota_window_applies(self.roster, "claude", name, window,
                                                                  "claude-fable-5-1"))
                    runtime = self.runtime(short=5, weekly=5)
                    runtime["quota_snapshots"]["claude"]["windows"][name] = window
                    state, _ = fleetctl.current_pool_state(runtime, "claude", roster=self.roster, model_key=model)
                    self.assertEqual(state, "EXHAUSTED")

    def test_paused_pro_does_not_change_primary_pool_scope_checks(self):
        runtime = self.runtime(short=5, weekly=5, pool="opencode-go")
        runtime["quota_snapshots"]["opencode-go"]["windows"]["scoped-kimi"] = self.window(99, label="Kimi only")
        routed = copy.deepcopy(self.roster)
        keys = {lane["model_key"]: lane["lane_id"] for lane in routed["lanes"]
                if lane.get("quota_pool") == "opencode-go"}
        routed["quota_pools"]["chatgpt-work"] = {"label": "ChatGPT"}
        routed["lanes"].append({"lane_id": "paused-pro", "model_key": "chatgpt:latest-pro",
                               "harness": "chatgpt-chat", "worker_level": "pro", "quota_pool": "chatgpt-work",
                               "gateway_status": {"quota_blocked": True}})
        routed["routing"]["roles"]["default"] = {
            "quality_first": ["paused-pro", keys["kimi-k3"]], "critical": [keys["deepseek-v4-flash"]]}
        selected = fleetctl.choose_lane(routed, runtime, "default", "read-only", "text")
        self.assertEqual(selected["lane_id"], keys["deepseek-v4-flash"])


if __name__ == "__main__":
    unittest.main()
