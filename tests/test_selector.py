"""Behavioral selector checks using synthetic evidence and isolated local state."""
import copy
import datetime as dt
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import selector


def quality_row(model, level="high", mean=.8, sd=.01, price=1, latency=1, sources=1, family="review"):
    return {"model_key": model, "model": model, "level": level,
            "q": {family: {"mean": mean, "sd": sd, "n_sources": sources, "sources": []}},
            "own": {}, "tokens_per_task": {"in": 1000, "out": 1000},
            "price_1m": {"in": price, "out": price}, "latency_s": latency, "flags": []}


def claude_binding_fixture(now):
    def window(used, hours, minutes, label):
        return {"used_percent": used, "reset_at": fleetctl.iso(now + dt.timedelta(hours=hours)),
                "window_minutes": minutes, "label": label}
    return {"quota_snapshots": {"claude": {"observed_at": fleetctl.iso(now), "windows": {
        "primary": window(2, 3, 300, "5-hour"),
        "secondary": window(72, 87, 10080, "Weekly"),
        "claude-weekly-scoped-fable": window(0, 87, 10080, "Fable only"),
    }}}}


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="selector test ")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0"}).start()
        mock.patch.object(fleetctl, "_POLICY_CACHE", ("clock_aware", "test")).start()

    def evidence(self, rows):
        directory = self.state / "evidence"
        directory.mkdir(exist_ok=True)
        (directory / "levels.json").write_text(json.dumps({"schema": "fleet-evidence-levels/v1", "rows": rows}))

    def select(self, runtime=None, **kwargs):
        return selector.select_option(self.roster, runtime or {}, self.state,
                                      kwargs.pop("role", "review"), fleet=fleetctl, **kwargs)

    def only(self, pools):
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": pools}}

    def snapshot(self, pool, projected, used=40):
        now = fleetctl.utc_now()
        return {"quota_snapshots": {pool: {"observed_at": fleetctl.iso(now), "windows": {
            "rolling": {"used_percent": used, "window_minutes": 120,
                        "reset_at": fleetctl.iso(now + dt.timedelta(hours=1)),
                        "projected_used_percent_at_reset": projected}}}}, "leases": []}

    def one_level(self, harness):
        for entry in self.roster["effort"].values():
            if entry["levels"].get(harness):
                entry["levels"][harness] = ["high"]
                entry["default"] = "high"
                entry.pop("stand_in_ceiling", None)

    def test_allow_filters_before_scoring_and_records_every_receipt(self):
        self.only(["codex"])
        self.evidence([quality_row("gpt-6-astra", mean=.99), quality_row("gpt-6.1-sol", mean=.6)])
        allowed = "codex:gpt-6.1-sol:high,codex:gpt-6.1-sol:medium"
        with mock.patch.object(selector, "_task_cost", wraps=selector._task_cost) as score_cost:
            result = self.select(allow=allowed)
        self.assertEqual(result["choice"]["model_key"], "gpt-6.1-sol")
        self.assertEqual({call.args[1]["level"] for call in score_cost.call_args_list}, {"high", "medium"})
        self.assertTrue(all(call.args[1]["model_key"] == "gpt-6.1-sol" for call in score_cost.call_args_list))
        self.assertIn("codex:gpt-6-astra:high: not in allow list", result["rejected"])
        for option in result["top3"]:
            receipt = json.loads(Path(option["selection_file"]).read_text())
            self.assertEqual(receipt["allow"], allowed.split(","))

    def test_allow_file_wildcard_keeps_all_levels_and_restricts_pool(self):
        allowed = self.state / "allow file.txt"
        allowed.write_text("  codex:gpt-6.1-sol:*\n\n codex:gpt-6.1-sol:*\n")
        options, _ = selector.enumerate_options(self.roster, {}, "review", fleet=fleetctl)
        levels = {o["level"] for o in options if o["pool"] == "codex" and o["model_key"] == "gpt-6.1-sol"}
        with mock.patch.object(selector, "_task_cost", wraps=selector._task_cost) as score_cost:
            result = self.select(allow=allowed, stakes="low")
        self.assertEqual({call.args[1]["level"] for call in score_cost.call_args_list}, levels)
        self.assertTrue(all(o["pool"] == "codex" and o["model_key"] == "gpt-6.1-sol"
                            for o in result["top3"] + [result["choice"]]))
        self.assertEqual(result["allow"], ["codex:gpt-6.1-sol:*"])

    def test_long_inline_allow_list_is_not_treated_as_a_filename(self):
        entries = [f"codex:unmeasured-{i}:high" for i in range(30)] + ["codex:gpt-6.1-sol:high"]
        result = self.select(allow=",".join(entries))
        self.assertEqual(result["allow"], entries)
        self.assertEqual(result["choice"]["model_key"], "gpt-6.1-sol")

    def test_allow_empty_invalid_and_unmatched_are_clear_errors(self):
        empty = self.state / "empty.txt"
        empty.write_text("")
        for value, error in [(empty, "allow list is empty"), ("codex:missing:*", "no eligible selector option in allow list"),
                             ("wrong:gpt-6.1-sol:*", "no eligible selector option in allow list"),
                             ("codex:gpt-6.1-sol", "invalid allow entry"),
                             (self.state / "missing.txt", "cannot read allow list")]:
            with self.subTest(value=value), self.assertRaisesRegex(fleetctl.FleetError, error):
                self.select(allow=value)
        self.assertFalse((self.state / "selections").exists())

    def test_lambda_zero_and_rises(self):
        self.assertEqual(selector.pool_lambda(20), 0)
        self.assertEqual(selector.pool_lambda(90), 0)
        self.assertAlmostEqual(selector.pool_lambda(95), .5)
        self.assertGreater(selector.pool_lambda(105), selector.pool_lambda(95))
        self.assertAlmostEqual(selector.pool_lambda(85, target=80, lambda0=2), .5)
        self.assertIsNone(selector.pool_lambda(None))

    def test_reported_claude_binding_and_selection_price(self):
        self.only(["claude"])
        self.one_level("claude")
        now = fleetctl.utc_now()
        runtime = claude_binding_fixture(now)
        # The 5h observation has no trustworthy pace; weekly extrapolates to 149.3%.
        runtime["quota_snapshots"]["claude"]["windows"]["primary"]["will_last_to_reset"] = True
        with mock.patch.object(fleetctl, "utc_now", return_value=now):
            result = self.select(runtime)
        price = result["pool_prices"]["claude"]
        self.assertEqual(price["binding_window"], "secondary")
        self.assertAlmostEqual(price["lambda"], 5.93)
        self.assertFalse(price["projection_unknown"])
        self.assertAlmostEqual(result["choice"]["pool_price"]["lambda"], 5.93)

    def test_unknown_and_known_windows_rank_by_pressure(self):
        now = fleetctl.utc_now()
        for unknown_used, expected in [(2, "known"), (98, "unknown")]:
            runtime = {"quota_snapshots": {"claude": {"observed_at": fleetctl.iso(now), "windows": {
                "unknown": {"used_percent": unknown_used, "reset_at": fleetctl.iso(now + dt.timedelta(hours=1))},
                "known": {"used_percent": 40, "reset_at": fleetctl.iso(now + dt.timedelta(hours=1)),
                          "projected_used_percent_at_reset": 95},
            }}}}
            with self.subTest(unknown_used=unknown_used), mock.patch.object(fleetctl, "utc_now", return_value=now):
                price = selector._pool_price(self.roster, runtime, "claude", {}, fleetctl)
            self.assertEqual(price["binding_window"], expected)
            self.assertEqual(price["projection_unknown"], expected == "unknown")

    def test_fable_scope_prices_only_matching_options_and_changes_choice(self):
        self.only(["claude"])
        self.one_level("claude")
        self.evidence([quality_row("claude-opus-5-5"), quality_row("claude-sonnet-5-5", mean=.9)])
        now = fleetctl.utc_now()
        runtime = claude_binding_fixture(now)
        windows = runtime["quota_snapshots"]["claude"]["windows"]
        windows["secondary"]["projected_used_percent_at_reset"] = 80
        windows["claude-weekly-scoped-fable"].update(used_percent=90, projected_used_percent_at_reset=120)
        with mock.patch.object(fleetctl, "utc_now", return_value=now):
            scoped = selector._pool_price(self.roster, runtime, "claude", {}, fleetctl,
                                          model_key="claude-fable-5-1")
            general = selector._pool_price(self.roster, runtime, "claude", {}, fleetctl)
            self.assertEqual(scoped["binding_window"], "claude-weekly-scoped-fable")
            self.assertEqual(scoped["lambda"], 3)
            self.assertEqual(general["binding_window"], "secondary")
            # Use admitted models to prove scoped price reaches scoring, not just this helper.
            window = windows.pop("claude-weekly-scoped-fable")
            window["label"] = "Sonnet only"
            windows["claude-weekly-scoped-sonnet"] = window
            result = self.select(runtime)
        self.assertEqual(result["choice"]["model_key"], "claude-opus-5-5")
        sonnet = next(o for o in result["top3"] if o["model_key"] == "claude-sonnet-5-5")
        self.assertEqual(sonnet["pool_price"]["lambda"], 3)
        self.assertEqual(result["lambdas"]["claude"], 0)
        receipt = json.loads(Path(sonnet["selection_file"]).read_text())
        self.assertEqual(receipt["choice"]["pool_price"], sonnet["pool_price"])

    def test_fable_option_records_its_own_binding_window(self):
        self.only(["claude"])
        self.one_level("claude")
        self.roster["model_cards"]["claude-fable-5-1"] = {
            "pool": "claude", "name": "Fable 5.1", "status": "current",
        }
        self.evidence([quality_row("claude-opus-5-5"), quality_row("claude-sonnet-5-5", mean=.7),
                       quality_row("claude-fable-5-1", mean=.9)])
        now = fleetctl.utc_now()
        runtime = claude_binding_fixture(now)
        windows = runtime["quota_snapshots"]["claude"]["windows"]
        windows["secondary"]["projected_used_percent_at_reset"] = 80
        windows["claude-weekly-scoped-fable"].update(used_percent=90, projected_used_percent_at_reset=120)
        with mock.patch.object(fleetctl, "utc_now", return_value=now):
            result = self.select(runtime)
        fable = next(o for o in result["top3"] if o["model_key"] == "claude-fable-5-1")
        self.assertEqual(fable["pool_price"]["binding_window"], "claude-weekly-scoped-fable")
        self.assertEqual(fable["pool_price"]["lambda"], 3)
        self.assertEqual(result["choice"]["model_key"], "claude-opus-5-5")

    def test_all_admitted_harnesses_and_supported_levels(self):
        options, _ = selector.enumerate_options(self.roster, {}, "review", mode="read-only", fleet=fleetctl)
        writable, _ = selector.enumerate_options(self.roster, {}, "review", mode="write", fleet=fleetctl)
        options += writable
        self.assertEqual({o["harness"] for o in options}, {"codex", "claude", "agy", "copilot", "opencode"})
        self.assertTrue({"codex", "claude", "opencode-go", "antigravity-gemini", "antigravity-3p", "github-copilot-student"}
                        <= {o["pool"] for o in options})
        for model, harness in (("gpt-6-astra", "codex"), ("claude-opus-5-5", "claude"),
                               ("deepseek-v4-flash", "opencode"), ("gemini-3.8-flash", "agy")):
            self.assertEqual({o["level"] for o in options if o["model_key"] == model},
                             set(self.roster["effort"][model]["levels"][harness]))
        copilot = next(o for o in options if o["harness"] == "copilot")
        self.assertEqual(copilot["effort"], "service-chosen")
        self.assertFalse(any(o["pool"] in {"gemini-metered", "openrouter-free"} for o in options))

    def test_switched_off_model_never_selected(self):
        self.only(["codex"])
        self.evidence([quality_row("gpt-6-astra", mean=.99), quality_row("gpt-6.1-sol", mean=.7)])
        for runtime in ({"model_toggles": {"codex": ["gpt-6-astra"]}},
                        {"model_choices": {"codex": "gpt-6.1-sol"}},
                        {"model_preferences": {"gpt-6-astra": "off"}}):
            with self.subTest(runtime=runtime):
                result = self.select(runtime)
                self.assertNotEqual(result["choice"]["model_key"], "gpt-6-astra")
                self.assertFalse(any(o["model_key"] == "gpt-6-astra" for o in result["top3"]))

    def test_critical_role_refusal_cannot_be_bypassed_by_level_enumeration(self):
        self.only(["codex"])
        runtime = self.snapshot("codex", 190, used=95)
        with self.assertRaisesRegex(fleetctl.FleetError, "CRITICAL|Parallel builder"):
            self.select(runtime, role="builder")

    def test_clock_aware_surplus_softens_critical_role_guard(self):
        self.only(["codex"])
        self.evidence([quality_row("gpt-6-astra", family="coding-agent")])
        runtime = self.snapshot("codex", 96, used=95)
        runtime["quota_snapshots"]["codex"]["windows"]["rolling"]["will_last_to_reset"] = True
        result = self.select(runtime, role="builder", stakes="irreversible")
        self.assertEqual(result["choice"]["pool"], "codex")

    def test_stand_in_ceiling_is_actual_level_ceiling(self):
        options, _ = selector.enumerate_options(self.roster, {"model_toggles": {"codex": ["gpt-6-astra"]}},
                                               "review", fleet=fleetctl)
        levels = {o["level"] for o in options if o["model_key"] == "gpt-6.1-sol"}
        self.assertIn("xhigh", levels)
        self.assertFalse({"max", "ultra"} & levels)

    def test_irreversible_ignores_price_and_latency(self):
        self.only(["codex"])
        self.one_level("codex")
        self.roster["policy"]["selector"]["mu"] = 10
        self.roster["quota_pools"]["codex"]["plan"]["allowance"] = {"amount": 10, "currency": "USD", "window": "rolling"}
        self.evidence([quality_row("gpt-6-astra", mean=.9, price=1000, latency=500),
                       quality_row("gpt-6.1-sol", mean=.7, price=.01, latency=.01)])
        runtime = self.snapshot("codex", 95)
        self.assertEqual(self.select(runtime)["choice"]["model_key"], "gpt-6.1-sol")
        self.assertEqual(self.select(runtime, stakes="irreversible")["choice"]["model_key"], "gpt-6-astra")

    def test_irreversible_refuses_all_unknown_and_uses_highest_measured_mean(self):
        self.only(["codex"])
        self.one_level("codex")
        with self.assertRaises(fleetctl.FleetError):
            self.select(stakes="irreversible")
        self.evidence([quality_row("gpt-6-astra", mean=.9, sd=.2, price=1000, latency=1000),
                       quality_row("gpt-6.1-sol", mean=.8, sd=.001, price=.001, latency=.001)])
        result = self.select(stakes="irreversible")
        self.assertEqual(result["choice"]["model_key"], "gpt-6-astra")
        self.assertAlmostEqual(result["choice"]["score"], .9)

    def test_surplus_pool_wins_equal_quality_against_priced_pressure(self):
        self.only(["codex", "claude"])
        self.one_level("codex")
        self.one_level("claude")
        for pool in ("codex", "claude"):
            self.roster["quota_pools"][pool]["plan"]["allowance"] = {"amount": 10, "currency": "USD", "window": "rolling"}
        self.evidence([quality_row("gpt-6-astra"), quality_row("claude-opus-5-5")])
        runtime = self.snapshot("claude", 95)
        runtime["quota_snapshots"].update(self.snapshot("codex", 70)["quota_snapshots"])
        result = self.select(runtime)
        self.assertEqual(result["choice"]["pool"], "codex")
        self.assertEqual(result["lambdas"]["codex"], 0)
        self.assertGreater(result["lambdas"]["claude"], 0)
        self.assertAlmostEqual(result["choice"]["cost"]["percent"], .02)
        self.assertTrue(result["choice"]["cost"]["estimated"])

    def test_allowed_pool_policy_and_excluded_lineage(self):
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"review": ["codex", "claude"], "default": ["opencode-go"]}}
        result = self.select(exclude_lineage="anthropic")
        self.assertEqual(result["choice"]["pool"], "codex")
        self.assertTrue(all(o["lineage"] != "anthropic" for o in result["top3"]))
        result = self.select(role="lookup")
        self.assertEqual(result["choice"]["pool"], "opencode-go")

    def test_unknown_uncertainty_cannot_replace_measured_equal_mean_even_when_cheaper(self):
        self.only(["codex"])
        self.one_level("codex")
        # This comparison concerns these two equal-mean cells only.
        self.roster["model_cards"]["gpt-6-luna"]["hidden"] = True
        self.roster["quota_pools"]["codex"]["plan"]["allowance"] = {"amount": 10, "currency": "USD", "window": "rolling"}
        self.evidence([quality_row("gpt-6-astra", price=1000),
                       quality_row("gpt-6.1-sol", price=.001, sd=0, sources=0)])
        result = self.select(self.snapshot("codex", 95))
        self.assertEqual(result["choice"]["model_key"], "gpt-6-astra")
        self.evidence([quality_row("gpt-6-astra", sd=0, sources=0)])
        unknown = self.select()["choice"]
        self.assertTrue(unknown["q"]["unknown"])
        self.assertGreaterEqual(unknown["q"]["sd"], .15)

    def test_missing_cost_stays_admitted_and_wrong_allowance_window_is_unused(self):
        self.only(["codex"])
        self.one_level("codex")
        plan = self.roster["quota_pools"]["codex"]["plan"]
        plan["allowance"] = {"amount": 10, "currency": "USD", "window": "rolling"}
        expensive_unknown = quality_row("gpt-6-astra", mean=.99)
        expensive_unknown.pop("price_1m")
        self.evidence([expensive_unknown, quality_row("gpt-6.1-sol", mean=.7)])
        result = self.select(self.snapshot("codex", 95))
        self.assertEqual(result["choice"]["model_key"], "gpt-6-astra")
        self.assertTrue(result["choice"]["cost"]["unknown"])
        plan["allowance"]["window"] = "monthly"
        self.assertIsNone(self.select(self.snapshot("codex", 95))["choice"]["cost"]["allowance_usd"])
        # A matching duration cannot override an explicitly different name.
        plan["allowance"]["window_minutes"] = 120
        self.assertIsNone(self.select(self.snapshot("codex", 95))["choice"]["cost"]["allowance_usd"])

    def test_agy_has_no_proven_read_only_boundary_but_write_is_admitted(self):
        self.only(["antigravity-gemini"])
        with self.assertRaisesRegex(fleetctl.FleetError, "read-only boundary"):
            self.select(mode="read-only")
        selected = self.select(mode="write")
        self.assertEqual(selected["choice"]["harness"], "agy")
        self.assertEqual(selected["choice"]["mode"], "write")

    def test_critical_go_implementation_only_uses_critical_allowlist(self):
        self.only(["opencode-go"])
        runtime = self.snapshot("opencode-go", 190, used=95)
        options, _ = selector.enumerate_options(self.roster, runtime, "implementation", fleet=fleetctl)
        critical_ids = set(self.roster["routing"]["roles"]["implementation"]["critical"])
        go_ids = {o["lane_id"] for o in options if o["pool"] == "opencode-go"}
        self.assertTrue(go_ids)
        self.assertTrue(go_ids <= critical_ids)
        self.assertNotIn("opencode-go-kimi-k2.7-code", go_ids)
        self.evidence([quality_row("deepseek-v4-flash", "high", family="coding-agent"),
                       quality_row("kimi-k2.7-code", "provider-default", mean=.99, family="coding-agent")])
        chosen = self.select(runtime, role="implementation", stakes="irreversible")["choice"]
        self.assertIn(chosen["lane_id"], critical_ids)

    def test_modes_modality_off_pool_and_capacity_are_admission_gates(self):
        self.only(["opencode-go"])
        result = self.select(role="implementation")
        self.assertIn("--write", result["choice"]["command_argv"])
        with self.assertRaises(fleetctl.FleetError):
            self.select({"switches": {"opencode-go": "off"}})
        with self.assertRaises(fleetctl.FleetError):
            self.select(modality="audio")
        runtime = {"switches": {"opencode-go": "low"}, "leases": [{"pool": "opencode-go", "lane_id": None,
                   "expires_at": fleetctl.iso(fleetctl.utc_now() + dt.timedelta(minutes=5))}]}
        with self.assertRaisesRegex(fleetctl.FleetError, "capacity"):
            self.select(runtime)

    def test_receipts_and_shell_commands_are_specific_to_each_alternative(self):
        result = self.select()
        self.assertEqual(len(result["top3"]), 3)
        self.assertEqual(len({o["selection_file"] for o in result["top3"]}), 3)
        for option in result["top3"]:
            argv = shlex.split(option["command"])
            self.assertEqual(argv, option["command_argv"])
            self.assertEqual(argv[:2], ["env", "FLEET_SELECTION_FILE=" + option["selection_file"]])
            payload = json.loads(Path(option["selection_file"]).read_text())
            self.assertEqual(payload["choice"]["model_key"], option["model_key"])
            self.assertEqual(payload["choice"]["level"], option["level"])
            self.assertEqual(payload["lambdas"], result["lambdas"])
            self.assertTrue(all("command" not in o for o in payload["top3"]))
            self.assertFalse(Path(option["selection_file"]).stat().st_mode & 0o222)

    def test_existing_route_and_brief_cli_are_preserved(self):
        overlay = self.state / "overlay.json"
        overlay.write_text(json.dumps(self.roster))
        env = dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=str(self.state), FLEET_NO_AUTO_REFRESH="1")
        expected = fleetctl.choose_lane(self.roster, {}, "review", "read-only", "text")["lane_id"]
        route = subprocess.run([sys.executable, str(ROOT / "scripts/fleetctl.py"), "route", "--role", "review", "--no-refresh"],
                               env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(route.returncode, 0, route.stderr)
        self.assertEqual(route.stdout.strip(), expected)
        brief = subprocess.run([sys.executable, str(ROOT / "scripts/fleetctl.py"), "brief", "--no-refresh"],
                               env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(brief.returncode, 0, brief.stderr)
        self.assertIn("fleetctl.py select --role R", brief.stdout)

    def test_select_cli_outputs_json_choice_and_replay_receipt(self):
        self.only(["codex"])
        self.evidence([quality_row("gpt-6-astra", mean=.99)])
        overlay = self.state / "overlay.json"
        overlay.write_text(json.dumps(self.roster))
        env = dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=str(self.state), FLEET_NO_AUTO_REFRESH="1")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/fleetctl.py"), "select", "--role", "review", "--json", "--no-refresh",
                                 "--allow", "codex:gpt-6-astra:high"],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["choice"]["model_key"], "gpt-6-astra")
        self.assertEqual(data["choice"]["level"], "high")
        self.assertEqual(data["allow"], ["codex:gpt-6-astra:high"])
        self.assertEqual(json.loads(Path(data["selection_file"]).read_text())["choice"]["score"], data["choice"]["score"])

    def test_select_no_refresh_does_not_call_refresh_without_environment_override(self):
        self.only(["codex"])
        self.evidence([quality_row("gpt-6-astra", mean=.99)])
        overlay = self.state / "overlay.json"
        overlay.write_text(json.dumps(self.roster))
        args = ["fleetctl.py", "--overlay", str(overlay), "--state-dir", str(self.state),
                "select", "--role", "review", "--json", "--no-refresh"]
        env = dict(os.environ)
        env.pop("FLEET_NO_AUTO_REFRESH", None)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", args), \
             mock.patch.object(fleetctl, "refresh_stale_pools", side_effect=AssertionError("refresh was invoked")) as refresh, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(fleetctl.main(), 0)
            refresh.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["choice"]["model_key"], "gpt-6-astra")


if __name__ == "__main__":
    unittest.main()
