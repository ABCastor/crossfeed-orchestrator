"""Owner spend presets and lead preservation, using only isolated synthetic state."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import selector
from tests.test_selector import quality_row

NOW = dt.datetime(2026, 10, 3, 10, tzinfo=dt.timezone.utc)


def snapshot(pool="claude", used=80):
    return {"quota_snapshots": {pool: {"observed_at": fleetctl.iso(NOW), "windows": {
        "weekly": {"used_percent": used, "window_minutes": 10080,
                   "reset_at": fleetctl.iso(NOW + dt.timedelta(days=3))}}}}}


def profile_roster():
    return {"schema_version": 3, "lanes": [], "quota_pools": {
        "claude": {}, "codex": {}, "antigravity-gemini": {}, "chatgpt-work": {},
        "opencode-go": {}, "gemini-metered": {"daily_usd_cap": 1}}}


class LeadDetectionTests(unittest.TestCase):
    def test_environment_and_explicit_precedence(self):
        cases = [({}, None), ({"CLAUDECODE": "1"}, "claude"),
                 ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, "claude"),
                 ({"CODEX_HOME": "/synthetic"}, "codex"),
                 ({"CLAUDECODE": "1", "CODEX_HOME": "/synthetic"}, "claude"),
                 ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "synthetic"}, "codex"),
                 ({"PI_CODING_AGENT_DIR": "/synthetic"}, None),
                 ({"PI_PROVIDER": "openai"}, None), ({"PI_PROVIDER": "google"}, None),
                 ({"PI_PROVIDER": "openai-codex"}, "codex"),
                 ({"PI_PROVIDER": "anthropic"}, None), ({"PI_PROVIDER": "claude-bridge"}, "claude"),
                 ({"PI_PROVIDER": "google-antigravity"}, "antigravity-gemini"),
                 ({"PI_QUOTA_POOL": "chatgpt-work"}, "chatgpt-work")]
        for env, expected in cases:
            with self.subTest(env=env):
                self.assertEqual(fleetctl.detect_lead_pool(env=env), expected)
                self.assertEqual(fleetctl.detect_lead_pool("opencode-go", env), "opencode-go")

    def test_golden_line_known_quota_and_api_render_is_env_neutral(self):
        runtime = snapshot()
        data = {"pools": []}
        expected = ("lead: you are spending claude (80% of weekly): delegate every task external "
                    "via dispatch, keep your own turns short, say so")
        with mock.patch.object(fleetctl, "utc_now", return_value=NOW), \
                mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True):
            text = fleetctl.render_brief(data, runtime=runtime, lead="claude")
            self.assertEqual(text.splitlines()[-2], expected)
            self.assertEqual(text.count("lead:"), 1)
            self.assertNotIn("lead:", fleetctl.render_brief(data, runtime=runtime))
            runtime["switches"] = {"claude": "on"}
            self.assertEqual(fleetctl.lead_directive(fleetctl.lead_pressure(runtime, "claude")), expected)

    # BR-SELECT-004 / BR-SELECT-006
    def test_pi_fable_lead_preserves_scoped_headroom_without_excluding_opus(self):
        fable, opus = "claude-fable-5-2", "claude-opus-5-5"
        roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        roster["model_cards"][fable] = {"pool": "claude", "status": "current", "aliases": ["fable"]}
        roster["effort"][fable] = {"levels": {"claude": ["high"]}, "default": "high"}
        roster["quota_pools"]["claude"]["value_capacity"] = {
            "weekly_usd_equivalent": 46, "source": "synthetic subscription"}
        runtime = snapshot(used=12)
        windows = runtime["quota_snapshots"]["claude"]["windows"]
        windows["weekly-fable"] = dict(windows["weekly"], label="Fable only", used_percent=92)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(fleetctl, "utc_now", return_value=NOW), \
                mock.patch.dict(os.environ, {"PI_PROVIDER": "claude-bridge", "PI_MODEL": fable}, clear=True):
            state = Path(d)
            (state / "evidence").mkdir()
            # This control must actually prefer Fable under expected cost before
            # the lead gate runs. Give both options a debit; selecting critical
            # alone does not bypass quota costs.
            rows = [dict(quality_row(key, mean=grade, pass_rate=grade),
                         own_cost={"claude": {"percent_per_task": .01}})
                    for key, grade in ((fable, .99), (opus, .80))]
            (state / "evidence/levels.json").write_text(json.dumps({"rows": rows}))
            allowed = f"claude:{opus}:high,claude:{fable}:high"
            lead = fleetctl.resolve_lead_pool(roster)
            unaffiliated = selector.select_option(roster, runtime, state, "review", allow=allowed,
                                                   value_mode="absolute-best", persist_receipts=False)
            self.assertEqual(unaffiliated["choice"]["model_key"], fable)
            self.assertLess(unaffiliated["choice"]["expected_cost"], unaffiliated["top3"][1]["expected_cost"])
            protected = selector.select_option(roster, runtime, state, "review", allow=allowed,
                                                lead=lead, value_mode="absolute-best", persist_receipts=False)
            self.assertEqual(protected["choice"]["model_key"], opus)
            self.assertEqual(protected["lead"], "claude")
            self.assertEqual(protected["lead_pressure"]["scoped_window"], "weekly-fable")
            self.assertTrue(any("scoped window excluded" in reason for reason in protected["rejected"]))

    # BR-SELECT-006
    def test_claude_lead_uses_the_at_risk_window_not_the_most_used(self):
        fable, opus = "claude-fable-5-2", selector.OPUS_HARD_REASONING
        runtime = snapshot(used=76)
        windows = runtime["quota_snapshots"]["claude"]["windows"]
        weekly = windows["weekly"]
        weekly["reset_at"] = fleetctl.iso(NOW + dt.timedelta(hours=17))
        windows["weekly-fable"] = dict(weekly, label="Fable only", used_percent=70,
                                       reset_at=fleetctl.iso(NOW + dt.timedelta(days=3)))
        with mock.patch.object(fleetctl, "utc_now", return_value=NOW), \
                mock.patch.dict(os.environ, {"PI_PROVIDER": "claude-bridge", "PI_MODEL": fable}, clear=True):
            pressure = fleetctl.lead_pressure(runtime, "claude")
            self.assertEqual(pressure["scoped_window"], "weekly-fable")
            self.assertEqual(pressure["used_percent"], 70)
            self.assertTrue(fleetctl.lead_blocks(runtime, None, pressure, {"pool": "claude", "model_key": fable}))
            self.assertFalse(fleetctl.lead_blocks(runtime, None, pressure, {"pool": "claude", "model_key": opus}))

            windows["primary"] = {"used_percent": 35, "window_minutes": 300,
                                  "reset_at": fleetctl.iso(NOW + dt.timedelta(minutes=240))}
            pressure = fleetctl.lead_pressure(runtime, "claude")
            self.assertNotIn("scoped_window", pressure)
            self.assertEqual(pressure["used_percent"], 35)

            # Remove the scoped deficit. The new five-hour reading alone is too
            # early to project, so it cannot reserve the pool just for a deficit.
            windows["weekly-fable"]["reset_at"] = weekly["reset_at"]
            windows["primary"]["reset_at"] = fleetctl.iso(NOW + dt.timedelta(minutes=299))
            self.assertIsNone(fleetctl.lead_pressure(runtime, "claude"))

    def test_low_off_unknown_and_stale_truth_boundary(self):
        for level in ("low", "off"):
            pressure = fleetctl.lead_pressure({"switches": {"claude": level}}, "claude")
            self.assertEqual(fleetctl.lead_directive(pressure),
                             f"lead: you are spending claude (quota unknown; level {level}): "
                             "delegate every task external via dispatch, keep your own turns short, say so")
        self.assertIsNone(fleetctl.lead_pressure({}, "claude"))
        stale = snapshot()
        stale["quota_snapshots"]["claude"]["observed_at"] = fleetctl.iso(NOW - dt.timedelta(hours=2))
        with mock.patch.object(fleetctl, "utc_now", return_value=NOW):
            self.assertIsNone(fleetctl.lead_pressure(stale, "claude"))


class ProfileTests(unittest.TestCase):
    def test_every_profile_golden_and_per_change_audit(self):
        goldens = {
            "save-claude": "antigravity-gemini normal -> high; chatgpt-work normal -> high; claude normal -> low; codex normal -> high",
            "save-codex": "antigravity-gemini normal -> high; chatgpt-work normal -> high; claude normal -> high; codex normal -> low",
            "balanced": "antigravity-gemini high -> normal; chatgpt-work high -> normal; claude high -> normal; codex high -> normal; gemini-metered high -> normal; opencode-go high -> normal",
            "max-quality": "antigravity-gemini normal -> high; chatgpt-work normal -> high; claude normal -> high; codex normal -> high; opencode-go normal -> high",
            "reset": "antigravity-gemini high -> normal; chatgpt-work high -> normal; claude high -> normal; codex high -> normal; gemini-metered high -> normal; opencode-go high -> normal",
        }
        roster = profile_roster()
        for name, golden in goldens.items():
            with self.subTest(profile=name), mock.patch.object(fleetctl, "utc_now", return_value=NOW):
                runtime = {"model_switches": {"synthetic-model": False}}
                if name in {"balanced", "reset"}:
                    runtime["switches"] = {pool: "high" for pool in roster["quota_pools"]}
                result = fleetctl.apply_profile(roster, runtime, name, who="codex:synthetic", because="owner phrase")
                self.assertEqual(fleetctl.profile_report(result), f"profile {name}: {golden}")
                self.assertEqual(runtime["profile_changes"], result["changes"])
                for change in runtime["profile_changes"]:
                    self.assertEqual((change["who"], change["when"], change["phrase"], change["profile"]),
                                     ("codex:synthetic", fleetctl.iso(NOW), "owner phrase", name))
                    self.assertEqual(fleetctl.pool_level(runtime, change["pool"]), change["after"])
                self.assertEqual(runtime["model_switches"], {"synthetic-model": False})

    def test_every_profile_preserves_unnamed_off_pools(self):
        roster = profile_roster()
        for name in fleetctl.PROFILES:
            runtime = {"switches": {pool: "off" for pool in roster["quota_pools"]}}
            with self.subTest(profile=name):
                result = fleetctl.apply_profile(roster, runtime, name, who="synthetic", because="use others")
                self.assertEqual(result["changes"], [])
                self.assertNotIn("profile_changes", runtime)
                self.assertTrue(all(fleetctl.pool_level(runtime, pool) == "off" for pool in roster["quota_pools"]))
                self.assertTrue(fleetctl.profile_report(result).startswith(f"profile {name}: no level changes; kept off:"))

    def test_only_named_off_pool_changes_and_ambiguous_names_do_not_lift_off(self):
        roster = profile_roster()
        cases = [("use Claude", {"claude"}), ("use codexes", set()),
                 ("use Gemini", set()), ("use ChatGPT", set()),
                 ("enable antigravity-gemini and ChatGPT Work", {"antigravity-gemini", "chatgpt-work"})]
        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                runtime = {"switches": {pool: "off" for pool in roster["quota_pools"]}}
                result = fleetctl.apply_profile(roster, runtime, "balanced", who="synthetic", because=phrase)
                self.assertEqual({change["pool"] for change in result["changes"]}, expected)
        del roster["quota_pools"]["gemini-metered"]
        result = fleetctl.apply_profile(roster, {"switches": {"antigravity-gemini": "off"}},
                                        "save-claude", who="synthetic", because="use Gemini")
        self.assertIn("antigravity-gemini", {change["pool"] for change in result["changes"]})

    def test_noop_and_paid_pool_levels_stay_unchanged(self):
        roster = profile_roster()
        result = fleetctl.apply_profile(roster, {}, "reset", who="synthetic")
        self.assertEqual(fleetctl.profile_report(result), "profile reset: no level changes")
        roster["quota_pools"].update({"paid-api": {"billing": "per-token"},
                                     "paid-plan": {"plan": {"billing": "metered"}}})
        runtime = {"switches": {"gemini-metered": "low", "paid-api": "low", "paid-plan": "low"}}
        fleetctl.apply_profile(roster, runtime, "max-quality", who="synthetic")
        for pool in ("gemini-metered", "paid-api", "paid-plan"):
            self.assertEqual(fleetctl.pool_level(runtime, pool), "low")


class LeadSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": ["claude", "codex"]}}
        (self.state / "evidence").mkdir()
        rows = [quality_row("gpt-6.1-sol", mean=.99), quality_row("claude-opus-5-5", mean=.8)]
        (self.state / "evidence/levels.json").write_text(json.dumps({"rows": rows}))
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(fleetctl, "utc_now", return_value=NOW).start()
        mock.patch.dict(os.environ, {"FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0"}).start()

    def select(self, runtime, **kwargs):
        return selector.select_option(self.roster, runtime, self.state, "review", lead="codex", fleet=fleetctl, **kwargs)

    def cli(self, argv):
        with mock.patch.object(sys, "argv", ["fleetctl.py", *argv]):
            return fleetctl.main()

    def test_pressure_excludes_before_scoring_and_is_saved_in_receipt(self):
        for runtime in (snapshot("codex", 80), snapshot("codex", 95), {"switches": {"codex": "low"}}):
            with self.subTest(runtime=runtime):
                result = self.select(runtime)
                self.assertTrue(all(option["pool"] != "codex" for option in result["top3"] + [result["choice"]]))
                self.assertTrue(any("pressured lead pool excluded" in row for row in result["rejected"]))
                receipt = json.loads(Path(result["selection_file"]).read_text())
                self.assertEqual(receipt["lead"], "codex")
                self.assertEqual(receipt["lead_pressure"], result["lead_pressure"])

    # BR-SELECT-004 / BR-SELECT-006
    def test_claude_lead_hard_reasoning_reserves_only_a_tight_window(self):
        opus, astra = selector.OPUS_HARD_REASONING, selector.ASTRA_HARD_REASONING
        self.roster["policy"]["selector"]["prefer_opus_hard_reasoning"] = True
        self.roster["model_cards"][astra] = self.roster["model_cards"]["gpt-6-astra"]
        self.roster["effort"][astra] = self.roster["effort"]["gpt-6-astra"]
        allowed = f"claude:{opus}:high,codex:{astra}:high"
        for used, reset_hours, expected, reason in (
            (76, 17, opus, None),          # surplus
            (80, 34, opus, None),          # on pace
            (80, 72, astra, "Opus not admitted"),  # deficit with run-out before reset
            (90, 12, astra, "Claude weekly window at least 90% used"),  # even in surplus
        ):
            with self.subTest(used=used, reset_hours=reset_hours):
                runtime = snapshot(used=used)
                weekly = runtime["quota_snapshots"]["claude"]["windows"]["weekly"]
                weekly["reset_at"] = fleetctl.iso(NOW + dt.timedelta(hours=reset_hours))
                controls = selector.select_option(self.roster, runtime, self.state, "hard-reasoning",
                                                  allow=allowed, no_explore=True, persist_receipts=False)
                self.assertEqual(controls["choice"]["model_key"], astra if used >= 90 else opus)
                result = selector.select_option(self.roster, runtime, self.state, "hard-reasoning",
                                                lead="claude", allow=allowed, no_explore=True,
                                                persist_receipts=False)
                self.assertEqual(result["choice"]["model_key"], expected)
                preference = result["hard_reasoning_preference"]
                if reason is None:
                    self.assertIsNone(result["lead_pressure"])
                    self.assertEqual(preference["route"], "opus")
                else:
                    self.assertEqual(result["lead_pressure"]["used_percent"], used)
                    self.assertEqual(preference, {"route": "gpt-fallback", "reason": reason})

    # BR-SELECT-004 / BR-SELECT-006
    def test_claude_lead_reserve_applies_only_when_it_helps(self):
        opus, astra = selector.OPUS_HARD_REASONING, selector.ASTRA_HARD_REASONING
        self.roster["policy"]["selector"]["prefer_opus_hard_reasoning"] = True
        self.roster["model_cards"][astra] = self.roster["model_cards"]["gpt-6-astra"]
        self.roster["effort"][astra] = self.roster["effort"]["gpt-6-astra"]
        allowed = f"claude:{opus}:high,codex:{astra}:high"

        def window(used, left_hours, **extra):
            return {"used_percent": used, "window_minutes": 10080,
                    "reset_at": fleetctl.iso(NOW + dt.timedelta(hours=left_hours)), **extra}

        def pick(claude, codex=None):
            runtime = {"quota_snapshots": {"claude": {"observed_at": fleetctl.iso(NOW), "windows": {"weekly": claude}}}}
            if codex is not None:
                runtime["quota_snapshots"]["codex"] = {"observed_at": fleetctl.iso(NOW), "windows": {"weekly": codex}}
            return selector.select_option(self.roster, runtime, self.state, "hard-reasoning", lead="claude",
                                          allow=allowed, no_explore=True, persist_receipts=False)

        # The live case: Claude is 14% used one day into its window, the oracle projects a run-out in
        # 5d 3h, and Codex is at 87% running out in 10 hours. Moving work there only drains the
        # pool that is nearer to empty, so Opus keeps hard reasoning.
        live_claude = window(14, 147, eta_seconds=int(5.1 * 86400))
        live_codex = window(87, 83, eta_seconds=10 * 3600)
        result = pick(live_claude, live_codex)
        self.assertEqual(result["choice"]["model_key"], opus)
        self.assertIsNone(result["lead_pressure"])
        self.assertEqual(result["hard_reasoning_preference"], {"route": "opus"})

        # Early projections never reserve, whatever the other pool looks like.
        self.assertIsNone(fleetctl.lead_pressure(
            {"quota_snapshots": {"claude": {"observed_at": fleetctl.iso(NOW), "windows": {"weekly": live_claude}}}},
            "claude"))

        # The same run-out later in the window is real. The reserve waives only when the alternative
        # runs out first: Codex 10 h away beats Claude 5 d away, a healthy Codex does not.
        late_claude = window(80, 72, eta_seconds=int(2 * 86400))
        waived = pick(late_claude, live_codex)
        self.assertEqual(waived["choice"]["model_key"], opus)
        self.assertEqual(waived["lead_pressure"]["reserve"], "waived")
        self.assertIn("runs out in", waived["lead_pressure"]["waived_because"])
        healthy = pick(late_claude, window(30, 83))
        self.assertEqual(healthy["choice"]["model_key"], astra)
        self.assertEqual(healthy["hard_reasoning_preference"], {"route": "gpt-fallback", "reason": "Opus not admitted"})
        self.assertEqual(healthy["lead_pressure"]["reason"], "projected")
        self.assertNotIn("reserve", healthy["lead_pressure"])
        # Alternatives with no reading at all are not projected to run out.
        self.assertEqual(pick(late_claude)["choice"]["model_key"], astra)
        # A Codex run-out later than Claude's still leaves the reserve in force.
        self.assertEqual(pick(late_claude, window(60, 83, eta_seconds=int(2.9 * 86400)))["choice"]["model_key"], astra)

        # At 90% used the reserve is unconditional, however bad the alternative looks (BR-SELECT-004 fallback).
        for codex in (live_codex, window(30, 83)):
            tight = pick(window(90, 12), codex)
            self.assertEqual(tight["choice"]["model_key"], astra)
            self.assertEqual(tight["lead_pressure"]["reason"], "tight")
            self.assertEqual(tight["hard_reasoning_preference"],
                             {"route": "gpt-fallback", "reason": "Claude weekly window at least 90% used"})

    def test_unknown_normal_lead_stays_eligible(self):
        result = self.select({})
        self.assertIsNone(result["lead_pressure"])
        result = self.select({}, allow="codex:gpt-6.1-sol:high")
        self.assertEqual(result["choice"]["pool"], "codex")

    def test_irreversible_bypasses_only_lead_exclusion(self):
        allowed = "codex:gpt-6.1-sol:high"
        result = self.select(snapshot("codex", 80), stakes="irreversible", allow=allowed)
        self.assertEqual(result["choice"]["pool"], "codex")
        blocked = [{"switches": {"codex": "off"}}, snapshot("codex", 100),
                   {"pool_circuits": {"codex": {"until": fleetctl.iso(NOW + dt.timedelta(hours=1))}}}]
        for runtime in blocked:
            with self.subTest(runtime=runtime), self.assertRaises(fleetctl.FleetError):
                self.select(runtime, stakes="irreversible", allow=allowed)
        self.roster["quota_pools"]["codex"]["daily_usd_cap"] = 0
        with self.assertRaises(fleetctl.FleetError):
            self.select({}, stakes="irreversible", allow=allowed)

    def test_cli_detects_and_dispatch_propagates_explicit_lead(self):
        overlay = self.state / "overlay.json"
        overlay.write_text(json.dumps(self.roster))
        (self.state / "runtime.json").write_text(json.dumps({"switches": {"codex": "low"}}))
        prefix = ["--overlay", str(overlay), "--state-dir", str(self.state)]
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "synthetic", "FLEET_NO_AUTO_REFRESH": "1"}, clear=True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(self.cli(prefix + ["select", "--role", "review", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["choice"]["pool"], "claude")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(self.cli(prefix + ["brief", "--lead", "codex"]), 0)
            self.assertIn("lead: you are spending codex (quota unknown; level low)", output.getvalue())
            with mock.patch.object(fleetctl, "dispatch_selection", return_value=0) as dispatch:
                self.assertEqual(self.cli(prefix + ["dispatch", "--role", "review", "--lead", "codex",
                                                       "--prompt", "synthetic", "--dir", str(self.state)]), 0)
                self.assertEqual(dispatch.call_args.args[0]["choice"]["pool"], "claude")
                self.assertEqual(dispatch.call_args.args[0]["lead"], "codex")

    def test_cli_profile_and_json_brief_are_auditable(self):
        overlay = self.state / "overlay.json"
        overlay.write_text(json.dumps(profile_roster()))
        prefix = ["--overlay", str(overlay), "--state-dir", str(self.state)]
        output = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(self.cli(prefix + ["profile", "save-codex", "--who", "synthetic",
                                                   "--because", "save Codex"]), 0)
        runtime = json.loads((self.state / "runtime.json").read_text())
        self.assertEqual({row["who"] for row in runtime["profile_changes"]}, {"synthetic"})
        self.assertEqual({row["phrase"] for row in runtime["profile_changes"]}, {"save Codex"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.cli(prefix + ["brief", "--lead", "codex", "--json", "--no-refresh"]), 0)
        self.assertEqual(json.loads(output.getvalue())["lead"]["pressure"]["level"], "low")


if __name__ == "__main__":
    unittest.main()
