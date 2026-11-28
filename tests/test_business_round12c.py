"""BR-QUOTA-005 and BR-QUOTA-006: held limit resets count as capacity, and the operator may let one be used.

Every Claude claim here is a fake `claim` callable: no suite ever reaches Anthropic.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import panel_prefs
import selector
from tests.test_menubar import menubar

NOW = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.timezone.utc)


def grant(clears=("weekly",), left=1, key=None, nxt=False, expires_days=12):
    return {"label": "One full limit reset (November 2026)", "left": left, "total": left, "clears": list(clears),
            "expires_at": fleetctl.iso(NOW + dt.timedelta(days=expires_days)), "usable_now": True,
            "next": nxt, "kind": "claude_grant", "key": key or clears[0]}


def runtime(weekly_used, resets=None, reset_days=3, extra_windows=None):
    windows = {"weekly": {"used_percent": weekly_used, "window_minutes": 10080, "label": "Weekly",
                          "reset_at": fleetctl.iso(NOW + dt.timedelta(days=reset_days))}}
    windows.update(extra_windows or {})
    snapshot = {"source": "claude-oauth-usage", "observed_at": fleetctl.iso(NOW - dt.timedelta(minutes=2)),
                "windows": windows}
    if resets is not None:
        snapshot["resets"] = resets
    return {"quota_snapshots": {"claude": snapshot}}


def fable_window(used):
    return {"claude-weekly-scoped-fable": {"used_percent": used, "window_minutes": 10080, "label": "Fable only",
                                           "reset_at": fleetctl.iso(NOW + dt.timedelta(days=3))}}


class HeldResetsRoutingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fleetctl, "utc_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def pressure(self, state):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True):
            return fleetctl.lead_pressure(state, "claude")

    def refusal(self, state):
        return selector._opus_quota_refusal({"quota_pools": {"claude": {}}, "model_cards": {}, "effort": {}}, state, fleetctl)

    # BR-QUOTA-005 (a)
    def test_claude_at_95_with_a_held_weekly_reset_is_not_avoided_where_without_it_it_is(self):
        without = runtime(95)
        held = runtime(95, [grant(("weekly",))])
        self.assertEqual(self.pressure(without)["reason"], "tight")
        self.assertIsNone(self.pressure(held))
        self.assertEqual(self.refusal(without), "Claude weekly window at least 90% used")
        self.assertIsNone(self.refusal(held))
        # The pool is neither CRITICAL nor tight: its routing state follows the widened capacity.
        state, evidence = fleetctl.current_pool_state(held, "claude")
        self.assertNotIn(state, {"CRITICAL", "EXHAUSTED"})
        self.assertEqual(evidence["windows"]["weekly"]["resets_held"], 1)
        self.assertEqual(evidence["windows"]["weekly"]["raw_used_percent"], 95)
        self.assertEqual(evidence["windows"]["weekly"]["capacity_left_percent"], 105.0)
        # What the operator reads is still the provider's figure.
        self.assertEqual(evidence["bottleneck_used_percent"], 95)

    def test_a_deficit_that_one_held_reset_covers_is_not_a_run_out(self):
        # 80% used with half the week gone: at this rate the week runs out in under a day.
        late = runtime(80, reset_days=3.5)
        self.assertLess(fleetctl.pool_run_out_seconds(late, "claude"), 86400)
        covered = runtime(80, [grant(("weekly",))], reset_days=3.5)
        self.assertIsNone(fleetctl.pool_run_out_seconds(covered, "claude"))

    # BR-QUOTA-005 (b)
    def test_the_fable_reset_does_not_lift_the_all_models_weekly_window(self):
        state = runtime(95, [grant(("claude-weekly-scoped-fable",), key="claude-weekly-scoped-fable")],
                        extra_windows=fable_window(40))
        self.assertEqual(self.pressure(state)["reason"], "tight")
        self.assertEqual(self.refusal(state), "Claude weekly window at least 90% used")
        _state, evidence = fleetctl.current_pool_state(state, "claude")
        self.assertNotIn("resets_held", evidence["windows"]["weekly"])
        self.assertEqual(evidence["windows"]["claude-weekly-scoped-fable"]["resets_held"], 1)

    def test_an_expired_or_spent_reset_is_not_held(self):
        spent = grant(("weekly",), left=0)
        expired = grant(("weekly",), expires_days=-1)
        for rows in ([spent], [expired], [spent, expired]):
            with self.subTest(rows=len(rows)):
                self.assertEqual(self.pressure(runtime(95, rows))["reason"], "tight")

    # BR-QUOTA-005 (c)
    def test_a_used_up_window_with_a_reset_held_is_unavailable_now_but_says_a_reset_is_held(self):
        state = runtime(100, [grant(("weekly",))])
        self.assertEqual(fleetctl.current_pool_state(state, "claude")[0], "EXHAUSTED")
        _state, evidence = fleetctl.current_pool_state(state, "claude")
        self.assertEqual(evidence["windows"]["weekly"]["resets_held"], 1)
        self.assertEqual(evidence["windows"]["weekly"]["capacity_left_percent"], 100.0)
        self.assertEqual(self.refusal(state), "Claude weekly window at least 90% used")
        self.assertEqual(self.pressure(state)["state"], "EXHAUSTED")
        self.assertEqual(self.pressure(state)["resets_held"], 1)
        directive = fleetctl.lead_directive(self.pressure(state))
        self.assertIn("🎟️ 1 held", directive)
        self.assertNotIn("runs out", directive)


class HeldResetsGlanceTests(unittest.TestCase):
    def setUp(self):
        self.roster = json.loads((ROOT / "examples" / "access-overlay.example.json").read_text())
        self.roster.pop("provider_sources", None)
        self.roster.pop("chatgpt_gateway", None)
        folder = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def glance(self, state):
        with mock.patch.object(fleetctl, "utc_now", return_value=NOW):
            overview = fleetctl.fleet_overview(self.roster, state, self.dir, NOW)
            return overview, fleetctl.glance_payload(self.dir / "o.json", self.roster, overview, "claude", self.dir)

    def claude(self, glance):
        return next(p for p in glance["pools"] if p["pool"] == "claude")

    def test_each_limit_carries_resets_held_and_the_pool_the_capacity_they_add(self):
        state = runtime(96, [grant(("weekly",))], extra_windows=fable_window(10))
        _overview, glance = self.glance(state)
        claude = self.claude(glance)
        by_name = {limit["name"]: limit for limit in claude["limits"]}
        self.assertEqual(by_name["weekly"]["resets_held"], 1)
        self.assertEqual(by_name["claude-weekly-scoped-fable"]["resets_held"], 0)
        self.assertEqual(by_name["weekly"]["used_percent"], 96)          # the provider's figure, untouched
        self.assertEqual((claude["resets_held"], claude["reset_capacity_percent"]), (1, 100))
        self.assertNotEqual(by_name["weekly"]["state"], "critical")

    def test_a_pool_without_a_reset_reports_zero_capacity(self):
        _overview, glance = self.glance(runtime(40, []))
        claude = self.claude(glance)
        self.assertEqual((claude["resets_held"], claude["reset_capacity_percent"]), (0, 0))

    def test_the_menu_title_and_dropdown_say_held_and_never_runs_out(self):
        # 80% used with half the week gone runs out in under a day without a reset.
        state = runtime(80, [grant(("weekly",))], reset_days=3.5)
        overview, glance = self.glance(state)
        self.assertIn("Claude 80% · 🎟️ 1 held", glance["title"])
        weekly = next(l for l in self.claude(glance)["limits"] if l["name"] == "weekly")
        self.assertNotIn("runs out", (weekly["pace"] or {}).get("words", ""))
        self.assertNotIn("runs out", menubar.swiftbar(overview, "claude"))
        self.assertIn("🎟️ 1 held", menubar.swiftbar(overview, "claude"))
        without = self.glance(runtime(80, [], reset_days=3.5))[1]
        self.assertIn("runs out", next(l for l in self.claude(without)["limits"] if l["name"] == "weekly")["pace"]["words"])

    def test_the_glance_carries_the_automatic_use_notice_while_it_is_recent(self):
        (self.dir / "auto-resets.json").write_text(json.dumps({"notice": {
            "text": "Used the weekly reset automatically", "at": fleetctl.iso(NOW - dt.timedelta(hours=1)), "window": "weekly"}}))
        _overview, glance = self.glance(runtime(10, []))
        self.assertEqual(glance["notices"], [{"kind": "auto_reset", "text": "Used the weekly reset automatically",
                                              "at": fleetctl.iso(NOW - dt.timedelta(hours=1))}])
        (self.dir / "auto-resets.json").write_text(json.dumps({"notice": {
            "text": "Used the weekly reset automatically", "at": fleetctl.iso(NOW - dt.timedelta(hours=30)), "window": "weekly"}}))
        self.assertEqual(self.glance(runtime(10, []))[1]["notices"], [])

    def test_a_used_up_window_with_a_reset_held_reads_used_up_plus_held(self):
        _overview, glance = self.glance(runtime(100, [grant(("weekly",))]))
        self.assertTrue(glance["title"].startswith("Claude used up · 🎟️ 1 held"), glance["title"])


class AutoUseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name)
        self.calls = []
        env = mock.patch.dict(os.environ, {"FLEET_NO_AUTO_USE_RESETS": ""})
        env.start()
        self.addCleanup(env.stop)

    def claim(self, ok=True, sent=True):
        def fake(key, state_dir, overlay):
            self.calls.append(key)
            return {"ok": ok, "sent": sent, "key": key, "error": None if ok else "refused"}
        return fake

    def reading(self, used=100, reset_hours=60, resets=None):
        windows = {"weekly": {"used_percent": used, "window_minutes": 10080, "label": "Weekly",
                              "reset_at": fleetctl.iso(NOW + dt.timedelta(hours=reset_hours))}}
        return {"claude": {"available": True, "windows": windows,
                           "resets": [grant(("weekly",))] if resets is None else resets}}

    def run_auto(self, reading, mode="when_used_up", at=NOW, claim=None):
        return fleetctl.auto_use_resets(self.state, reading, now=at, prefs={"auto_use_resets": mode},
                                        claim=claim or self.claim())

    # BR-QUOTA-006
    def test_off_is_the_default_and_spends_nothing(self):
        self.assertEqual(panel_prefs.normalize({})["auto_use_resets"], "off")
        self.assertEqual(self.run_auto(self.reading(), mode="off"), [])
        self.assertEqual(panel_prefs.normalize({"auto_use_resets": "always"})["auto_use_resets"], "off")
        self.assertEqual(panel_prefs.normalize({"auto_use_resets": "when_used_up"})["auto_use_resets"], "when_used_up")
        self.assertEqual(self.calls, [])

    def test_a_used_up_window_far_from_its_reset_spends_exactly_one_reset_and_leaves_a_notice(self):
        result = self.run_auto(self.reading())
        self.assertEqual(self.calls, ["weekly"])
        self.assertEqual([(r["window"], r["ok"]) for r in result], [("weekly", True)])
        notice = fleetctl.auto_reset_notice(self.state, NOW + dt.timedelta(minutes=5))
        self.assertEqual(notice["text"], "Used the weekly reset automatically")
        self.assertIn("weekly used weekly: ok", (self.state / "auto-resets.log").read_text())
        self.assertNotIn("grant", (self.state / "auto-resets.log").read_text())
        self.assertIsNone(fleetctl.auto_reset_notice(self.state, NOW + dt.timedelta(hours=25)))

    def test_it_waits_for_99_5_percent_and_for_a_natural_reset_more_than_12_hours_away(self):
        self.assertEqual(self.run_auto(self.reading(used=99)), [])
        self.assertEqual(self.run_auto(self.reading(reset_hours=12)), [])
        self.assertEqual(self.run_auto(self.reading(reset_hours=11)), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.run_auto(self.reading(used=99.5, reset_hours=13))), 1)

    def test_it_needs_a_held_reset_that_clears_that_window(self):
        self.assertEqual(self.run_auto(self.reading(resets=[])), [])
        fable_only = [grant(("claude-weekly-scoped-fable",))]
        self.assertEqual(self.run_auto(self.reading(resets=fable_only)), [])
        expired = [grant(("weekly",), expires_days=-1)]
        self.assertEqual(self.run_auto(self.reading(resets=expired)), [])
        self.assertEqual(self.calls, [])

    def test_at_most_once_per_window_per_12_hours(self):
        self.run_auto(self.reading())
        self.assertEqual(self.run_auto(self.reading(), at=NOW + dt.timedelta(hours=11)), [])
        self.assertEqual(len(self.run_auto(self.reading(), at=NOW + dt.timedelta(hours=12, minutes=1))), 1)
        self.assertEqual(self.calls, ["weekly", "weekly"])

    def test_claude_codes_next_grant_goes_first_else_the_one_expiring_soonest(self):
        soon = grant(("weekly",), key="weekly", expires_days=3)
        later = grant(("weekly",), key="weekly-2", expires_days=20, nxt=True)
        self.run_auto(self.reading(resets=[soon, later]))
        self.assertEqual(self.calls, ["weekly-2"])
        other = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(other.cleanup)
        self.state = Path(other.name)
        later["next"] = False
        self.run_auto(self.reading(resets=[later, soon]))
        self.assertEqual(self.calls[-1], "weekly")

    def test_a_claim_that_was_not_sent_is_logged_and_not_counted(self):
        self.run_auto(self.reading(), claim=self.claim(ok=False, sent=False))
        self.assertIn("refused", (self.state / "auto-resets.log").read_text())
        self.assertIsNone(fleetctl.auto_reset_notice(self.state, NOW))
        self.assertEqual(len(self.run_auto(self.reading(), at=NOW + dt.timedelta(minutes=5))), 1)

    def test_tests_and_the_environment_switch_it_off(self):
        with mock.patch.dict(os.environ, {"FLEET_NO_AUTO_USE_RESETS": "1"}):
            self.assertEqual(self.run_auto(self.reading()), [])
        self.assertEqual(self.calls, [])

    def test_the_quota_refresh_path_calls_it_for_fresh_claude_readings_only(self):
        sources = {"claude": {"oracle": "claude"}, "codex": {"oracle": "codex"}}
        observed = {"available": True, "source": "claude-oauth-usage", "observed_at": fleetctl.iso(NOW),
                    "windows": self.reading()["claude"]["windows"], "resets": [grant(("weekly",))]}
        adapters = {"claude": lambda cfg, timeout=None: dict(observed),
                    "codex": lambda cfg, timeout=None: dict(observed)}
        with mock.patch.dict(fleetctl.ORACLE_REGISTRY, adapters), \
                mock.patch.object(fleetctl, "auto_use_resets") as spend, \
                mock.patch.object(fleetctl, "adopt_remote_readings"):
            fleetctl.refresh_stale_pools(self.state, ["claude", "codex"], sources=sources, force=True)
            self.assertEqual(list(spend.call_args.args[1]), ["claude"])
            spend.reset_mock()
            fleetctl.refresh_stale_pools(self.state, ["claude"], sources=sources, force=True, auto_use=False)
            spend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
