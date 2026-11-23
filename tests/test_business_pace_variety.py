"""BR-SELECT-002: visible quota pace and bounded, checked-outcome exploration.

Synthetic readings and isolated state; dispatch booking uses the real shared cap."""
import collections
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import rating_log
import selector
from tests.test_menubar import load_menubar
from tests.test_selector import quality_row

menubar = load_menubar()
NOW = dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)
THREE = "codex:gpt-6-astra:high,codex:gpt-6.1-sol:high,claude:claude-opus-5-5:high"


def window(used, minutes, left_s, now, **extra):
    return dict({"used_percent": used, "window_minutes": minutes,
                 "reset_at": fleetctl.iso(now + dt.timedelta(seconds=left_s))}, **extra)


def snapshot(windows, now):
    return {"observed_at": fleetctl.iso(now - dt.timedelta(minutes=2)), "windows": windows}


class PaceReadingTests(unittest.TestCase):
    """Every limit with a reading shows its pace, in CodexBar's words."""

    # BR-SELECT-002
    def test_window_pace_deficit_surplus_on_pace_and_run_out(self):
        week = 10080
        # 4 h before a weekly reset with 20% unused: 98% elapsed, so 18 points of surplus.
        pace = fleetctl.window_pace({"used_percent": 80, "window_minutes": week, "seconds_to_reset": 4 * 3600})
        self.assertEqual((pace["status"], pace["points"], pace["words"]), ("surplus", 18, "18% surplus"))
        # Halfway through with 60% used: 10 points ahead; the flat rate runs out in 2.8 days.
        half = week * 30
        pace = fleetctl.window_pace({"used_percent": 60, "window_minutes": week, "seconds_to_reset": half})
        self.assertEqual((pace["status"], pace["points"]), ("deficit", 10))
        self.assertEqual(pace["runs_out_in_s"], int(half * 40 / 60))
        self.assertEqual(pace["words"], "10% in deficit, runs out in 2d 8h")
        # The oracle's own ETA outranks the flat average.
        pace = fleetctl.window_pace({"used_percent": 60, "window_minutes": week, "seconds_to_reset": half,
                                     "eta_seconds": 3600})
        self.assertEqual((pace["runs_out_in_s"], pace["run_out_basis"]), (3600, "observed_pace"))
        # Too early in the window to extrapolate a run-out time, but the pace is still said.
        pace = fleetctl.window_pace({"used_percent": 20, "window_minutes": week, "seconds_to_reset": week * 60 * .95})
        self.assertEqual(pace["words"], "15% in deficit, too early to project")
        self.assertEqual(fleetctl.window_pace({"used_percent": 50, "window_minutes": 300,
                                               "seconds_to_reset": 150 * 60})["words"], "on pace")
        # No length, no pace: never a guess.
        self.assertIsNone(fleetctl.window_pace({"used_percent": 50, "seconds_to_reset": 60}))

    def overview(self, runtime, roster=None):
        roster = roster or json.loads((ROOT / "examples" / "access-overlay.example.json").read_text())
        roster.pop("provider_sources", None)
        roster.pop("chatgpt_gateway", None)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp, mock.patch.object(fleetctl, "utc_now", return_value=NOW):
            return fleetctl.fleet_overview(roster, runtime, Path(tmp), NOW), roster

    def readings(self, now=NOW):
        return {"quota_snapshots": {
            "claude": snapshot({"weekly": window(80, 10080, 4 * 3600, now, label="Weekly")}, now),
            "codex": snapshot({"weekly": window(60, 10080, 10080 * 30, now, label="Weekly")}, now),
            "antigravity-gemini": snapshot({
                "antigravity-quota-summary-gemini-5h": window(50, 300, 150 * 60, now, label="Gemini 5-hour")}, now),
        }}

    # BR-SELECT-002
    def test_glance_and_brief_show_pace_for_every_limit_with_a_reading(self):
        overview, _ = self.overview(self.readings())
        text = menubar.text(overview, "claude")
        self.assertIn("Weekly 80% · resets", text)
        self.assertIn("· 18% surplus", text)
        self.assertIn("10% in deficit, runs out in 2d 8h", text)
        self.assertIn("· on pace", text)
        swift = menubar.swiftbar(overview, "claude")
        self.assertRegex(swift.splitlines()[2], r"^Claude  80% · resets .* · 18% surplus")
        data = menubar.as_json(overview, "claude")
        read = [limit for row in data["pools"] for limit in row["limits"]
                if limit["kind"] == "window" and limit.get("reset_at")]
        self.assertEqual(len(read), 3)
        self.assertTrue(all(limit["pace"]["words"] for limit in read), read)
        brief = fleetctl._render_verbose_brief(overview)
        self.assertRegex(brief, r"claude: weekly 80% CONSERVE, resets .*, 18% surplus")
        self.assertRegex(brief, r"codex: weekly 60%, resets .*, 10% in deficit, runs out in 2d 8h")

    # BR-SELECT-002
    def test_a_stale_reading_kept_through_a_failed_read_shows_no_pace(self):
        runtime = self.readings()
        runtime["quota_refresh_errors"] = {"claude": {"attempted_at": fleetctl.iso(NOW), "reason": "HTTP 500"}}
        runtime["quota_snapshots"]["claude"]["observed_at"] = fleetctl.iso(NOW - dt.timedelta(hours=2))
        overview, _ = self.overview(runtime)
        claude = next(pool for pool in overview["pools"] if pool["pool"] == "claude")
        self.assertTrue(claude["gauge_error"])
        self.assertTrue(claude["limits"])
        self.assertTrue(all(limit["pace"] is None for limit in claude["limits"]))
        # Control: the same reading without the failed read does carry its pace.
        fresh, _ = self.overview(self.readings())
        self.assertEqual(next(p for p in fresh["pools"] if p["pool"] == "claude")["limits"][0]["pace"]["words"],
                         "18% surplus")

    # BR-SELECT-002
    def test_glance_command_prints_pace(self):
        roster = json.loads((ROOT / "examples" / "access-overlay.example.json").read_text())
        roster.pop("provider_sources", None)
        roster.pop("chatgpt_gateway", None)
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            (state / "runtime.json").write_text(json.dumps(self.readings(fleetctl.utc_now())))
            overlay = Path(tmp) / "overlay.json"
            overlay.write_text(json.dumps(roster))
            env = dict(os.environ, ACCESS_OVERLAY=str(overlay), FLEET_STATE_DIR=str(state), FLEET_NO_AUTO_REFRESH="1")
            done = subprocess.run([sys.executable, str(ROOT / "scripts" / "fleetctl.py"), "glance", "--lead", "claude"],
                                  capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("18% surplus", done.stdout)
        self.assertIn("10% in deficit, runs out in", done.stdout)


class CheckedRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pace variety ")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0"}).start()
        mock.patch.object(fleetctl, "_POLICY_CACHE", ("clock_aware", "test")).start()
        for pool in ("codex", "claude"):
            self.roster["quota_pools"][pool]["value_capacity"] = {
                "weekly_usd_equivalent": 46, "source": "synthetic weekly estimate"}
        self.evidence([self.row(model) for model in ("gpt-6-astra", "gpt-6.1-sol", "claude-opus-5-5")])

    @staticmethod
    def row(model, debit=.1):
        pool = "claude" if model.startswith("claude") else "codex"
        return dict(quality_row(model), own_cost={pool: {"percent_per_task": debit}})

    def evidence(self, rows):
        directory = self.state / "evidence"
        directory.mkdir(exist_ok=True)
        (directory / "levels.json").write_text(json.dumps({"schema": "fleet-evidence-levels/v1", "rows": rows}))

    def select(self, runtime=None, allow=THREE, **kwargs):
        return selector.select_option(self.roster, runtime or {}, self.state, kwargs.pop("role", "review"),
                                      allow=allow, fleet=fleetctl, **kwargs)

    def ran(self, option, when=None):
        """What a wrapper appends to runs.jsonl for a run of this option."""
        record = {"schema": "crossfeed-model-run/v1", "quota_pool": option["pool"],
                  "selected_model": option["model_key"], "harness": option["harness"],
                  "started_at": fleetctl.iso(when or fleetctl.utc_now())}
        with (self.state / "runs.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def dispatches(self, count, runtime=None, **kwargs):
        picks = collections.Counter()
        for n in range(count):
            result = self.select(runtime, explore_seed=n, **kwargs)
            booked = fleetctl._book_rating_trial(result, self.state, f"task-{n}", f"run-{n}")
            choice = booked or result["choice"]
            picks[(choice["pool"], choice["model_key"])] += 1
        return picks

    def pace_readings(self):
        """Codex: its 5-hour limit resets in 30 min with 80% unused, its weekly one just reset.
        Claude: 20% of the week gone in its first 13 hours, 12 points ahead of pace. Both carry
        a young window, so the quota price alone (an unknown projection on each) cannot tell
        them apart."""
        now = fleetctl.utc_now()
        week = 10080 * 60
        return {"quota_snapshots": {
            "codex": snapshot({"primary": window(10, 300, 30 * 60, now),
                               "secondary": window(1, 10080, week * .97, now)}, now),
            "claude": snapshot({"weekly": window(20, 10080, week * .92, now)}, now),
        }, "leases": []}

    def healthy(self):
        now = fleetctl.utc_now()
        good = window(10, 10080, 2 * 86400, now, projected_used_percent_at_reset=20)
        return {"quota_snapshots": {pool: snapshot({"weekly": good}, now)
                                    for pool in ("codex", "claude")}}

    # BR-SELECT-002
    def test_surplus_pool_about_to_reset_is_preferred_over_equal_quality_deficit_pool(self):
        allow = "codex:gpt-6-astra:high,claude:claude-opus-5-5:high"
        result = self.select(self.pace_readings(), allow=allow, no_explore=True)
        self.assertEqual(result["lambdas"]["codex"], result["lambdas"]["claude"])
        codex, claude = result["top3"]
        self.assertGreater(codex["quota_pace"]["value"], .7)
        self.assertLess(claude["quota_pace"]["value"], -.1)
        self.assertEqual(result["choice"]["pool"], "codex")
        self.assertLess(codex["estimated_price"], claude["estimated_price"])
        receipt = json.loads(Path(codex["selection_file"]).read_text())
        self.assertEqual(receipt["choice"]["quota_pace"], codex["quota_pace"])

    # BR-SELECT-002
    def test_fifty_dispatches_cap_non_greedy_starts_across_lanes(self):
        runtime = self.healthy()
        mean = self.select(runtime, no_explore=True)["choice"]
        picks = self.dispatches(50, runtime)
        self.assertEqual(sum(picks.values()), 50)
        total, explored = selector.rating_explore.read_totals(self.state)
        self.assertEqual(total, 50)
        self.assertGreater(explored, 0)
        self.assertLessEqual(explored / total, .15)
        self.assertEqual(explored, total - picks[mean["pool"], mean["model_key"]])

    # BR-SELECT-002
    def test_execution_receipts_do_not_change_the_prior_but_independent_verdicts_do(self):
        mean = self.select(self.healthy(), no_explore=True)["choice"]
        self.ran(mean)
        again = self.select(self.healthy(), no_explore=True)["choice"]
        self.assertEqual(again["posterior"], mean["posterior"])
        with (self.state / rating_log.LEDGER).open("a") as ledger:
            for row in (dict(schema=rating_log.DISPATCH, run_id="r1", task_id="t1", live_pick=mean),
                        dict(schema=rating_log.ACCEPTANCE, run_id="r1", task_id="t1", verdict="fail", check="test")):
                ledger.write(json.dumps(row) + "\n")
        ranked = self.select(self.healthy(), no_explore=True)["top3"]
        updated = next(item for item in ranked if item["model_key"] == mean["model_key"])
        self.assertEqual(updated["checked_failures"], 1)
        self.assertLess(updated["p_success"], mean["p_success"])

    # BR-SELECT-002
    def test_option_above_cost_band_is_never_sampled(self):
        allow = THREE + ",codex:gpt-6-luna:high"
        rows = [self.row(model) for model in ("gpt-6-astra", "gpt-6.1-sol", "claude-opus-5-5")]
        runtime = self.healthy()
        self.evidence(rows + [self.row("gpt-6-luna", debit=4)])
        self.assertNotIn(("codex", "gpt-6-luna"), self.dispatches(50, runtime, allow=allow))
        # Control: the same admitted option becomes sampleable at equal cost.
        self.evidence(rows + [self.row("gpt-6-luna")])
        self.assertIn(("codex", "gpt-6-luna"), self.dispatches(50, runtime, allow=allow))

    # BR-SELECT-002
    def test_hard_gates_hold_for_a_surplus_lane(self):
        runtime = self.pace_readings()          # codex is the surplus pool every draw favours
        runtime["model_preferences"] = {"gpt-6-astra": "off"}
        self.roster["effort"]["gpt-6.1-sol"]["refuse_roles"] = {"review": "synthetic refusal"}
        picks = self.dispatches(50, runtime, allow=THREE)
        self.assertEqual(set(picks), {("claude", "claude-opus-5-5")}, picks)
        result = self.select(runtime)
        self.assertTrue(any("gpt-6-astra" in line for line in result["rejected"]))
        self.assertTrue(any("synthetic refusal" in line for line in result["rejected"]))
        # The allow list is a gate too: an unlisted equal-quality lane never enters the draw.
        self.assertNotIn(("claude", "claude-opus-5-5"),
                         self.dispatches(20, allow="codex:gpt-6-astra:high,codex:gpt-6.1-sol:high"))

    # BR-SELECT-002
    def test_irreversible_and_explicit_targets_are_never_sampled(self):
        self.roster["policy"]["selector"]["exploration_cap"] = 1
        with mock.patch.object(selector.rating_explore, "sample_choice",
                               side_effect=AssertionError("sampled a protected choice")):
            result = self.select(self.healthy(), stakes="irreversible")
            target = self.select(self.healthy(), target=("codex", "gpt-6-astra"), allow=None)
        self.assertEqual(target["choice"]["model_key"], "gpt-6-astra")
        for selection in (result, target):
            self.assertEqual(selection["selection_probability"], 1)
            self.assertIsNone(selection["rating_sampling"]["sampled_pick"])

    # BR-SELECT-002
    def test_thompson_draw_records_sample_not_a_selection_probability(self):
        self.roster["policy"]["selector"]["exploration_cap"] = 1
        result = self.select(self.healthy(), explore_seed=3)
        self.assertIsNotNone(result["rating_sampling"]["sampled_pick"])
        self.assertIsNone(result["selection_probability"])
        payload = json.loads(Path(result["selection_file"]).read_text())
        self.assertIsNone(payload["selection_probability"])
        self.assertEqual(payload["rating_sampling"], result["rating_sampling"])

    # BR-SELECT-002
    def test_exploration_can_be_switched_off_by_policy(self):
        self.roster["policy"]["selector"]["exploration_cap"] = 0
        with mock.patch.object(selector.rating_explore, "sample_choice",
                               side_effect=AssertionError("sampled with cap off")):
            picks = self.dispatches(10, self.healthy())
        self.assertEqual(len(picks), 1, picks)


if __name__ == "__main__":
    unittest.main()
