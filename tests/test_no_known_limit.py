"""Declared no-known-limit pools ignore gauges, while real refusals still gate them."""
import argparse
import contextlib
import copy
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_console import console
from tests.test_selector import fleetctl, selector
from tests.test_fleetctl import lane


class NoKnownLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.now = fleetctl.utc_now()
        self.roster = {
            "schema_version": 3, "lanes": [], "routing": {"roles": {}},
            "quota_pools": {
                "chatgpt-work": {"label": "ChatGPT", "plan": {"limit": "none-known", "allowance": None}},
                "codex": {"label": "Codex", "plan": {}},
            },
        }
        snapshot = {"observed_at": fleetctl.iso(self.now), "source": "codexbar", "plan": "Codex Pro",
                    "windows": {"weekly": {"used_percent": 21, "window_minutes": 10080,
                        "reset_at": fleetctl.iso(self.now + dt.timedelta(days=6, hours=4)),
                        "projected_used_percent_at_reset": 95}},
                    "idle_windows": {"rolling": {"used_percent": 0, "window_minutes": 300}}}
        self.runtime = {"quota_snapshots": {pool: copy.deepcopy(snapshot) for pool in self.roster["quota_pools"]}}

    def test_zero_price_with_missing_and_borrowed_snapshots_normal_pool_unchanged(self):
        for runtime in [{}, self.runtime]:
            with self.subTest(runtime=bool(runtime)):
                price = selector._pool_price(self.roster, runtime, "chatgpt-work", {"lambda_unknown": 7}, fleetctl)
                self.assertEqual(price["lambda"], 0)
                self.assertIsNone(price["binding_window"])
                self.assertEqual(price["window"], {})
                self.assertFalse(price["projection_unknown"])
        self.assertEqual(selector._pool_price(self.roster, {}, "codex", {}, fleetctl)["lambda"], .5)
        normal = selector._pool_price(self.roster, self.runtime, "codex", {}, fleetctl)
        self.assertEqual(normal["lambda"], .5)
        self.assertEqual(normal["binding_window"], "weekly")
        self.assertEqual(normal["window"]["used_percent"], 21)

    def test_overview_and_both_briefs_and_console_ignore_borrowed_windows(self):
        original = copy.deepcopy(self.runtime)
        overview = fleetctl.fleet_overview(self.roster, self.runtime, self.state, self.now)
        pools = {p["pool"]: p for p in overview["pools"]}
        chat = pools["chatgpt-work"]
        self.assertIsNone(chat["quota"])
        self.assertIsNone(chat["plan"]["name"])  # no Codex plan borrowed from the snapshot
        self.assertEqual(chat["limits"], [])
        self.assertIsNone(chat["stale_age_s"])
        self.assertFalse(chat["measured"])
        self.assertEqual(pools["codex"]["quota"]["used_percent"], 21)
        for context in [{}, {"roster": self.roster, "runtime": self.runtime}]:
            with patch.object(fleetctl, "utc_now", return_value=self.now):
                brief = fleetctl.render_brief(overview, **context)
            self.assertIn("chatgpt-work | normal | no known limit | - | - | 0.00 |", brief)
            self.assertIn("codex | normal | weekly | 21% | 6d 4h |", brief)
        verbose = fleetctl.render_brief(overview, verbose=True)
        self.assertIn("chatgpt-work no known limit", verbose)
        page = console.render_page(overview, "synthetic-token")
        row = page.split('id="pool-chatgpt-work"', 1)[1].split("</article>", 1)[0]
        self.assertIn("no known limit", row)
        self.assertNotIn("21%", row)
        self.assertNotIn("Weekly", row)
        self.assertNotIn("resets", row)
        self.assertEqual(self.runtime, original)

    def test_full_snapshot_does_not_gate_selector_route_lease_or_lead(self):
        roster = json.loads((Path(__file__).parent / "fixtures/access-overlay.test.json").read_text())
        roster["quota_pools"]["codex"]["plan"]["limit"] = "none-known"
        runtime = copy.deepcopy(self.runtime)
        runtime["quota_snapshots"]["codex"]["windows"]["weekly"]["used_percent"] = 100
        options, _ = selector.enumerate_options(roster, runtime, "review", fleet=fleetctl)
        self.assertTrue(any(o["pool"] == "codex" for o in options))
        self.assertIsNone(fleetctl.lead_pressure(runtime, "codex", roster))
        candidate = {**lane("synthetic-codex", "gpt-6.1-sol"), "quota_pool": "codex", "harness": "codex"}
        roster["lanes"].append(candidate)
        roster["routing"]["roles"]["default"]["quality_first"] = [candidate["lane_id"]]
        chosen = fleetctl.choose_lane(roster, runtime, "default", "read-only", "text")
        self.assertEqual(chosen["lane_id"], candidate["lane_id"])
        (self.state / "runtime.json").write_text(json.dumps(runtime))
        self.assertTrue(fleetctl.acquire_lease(self.state, roster, candidate["lane_id"], 60))
        del roster["quota_pools"]["codex"]["plan"]["limit"]
        options, _ = selector.enumerate_options(roster, runtime, "review", fleet=fleetctl)
        self.assertFalse(any(o["pool"] == "codex" for o in options))

    def test_switch_off_and_real_quota_error_still_gate(self):
        for extra in [{"switches": {"chatgpt-work": "off"}},
                      {"pool_circuits": {"chatgpt-work": {"until": fleetctl.iso(self.now + dt.timedelta(hours=1))}}}]:
            with self.subTest(extra=extra):
                runtime = {**self.runtime, **extra}
                state, _ = fleetctl.current_pool_state(runtime, "chatgpt-work", roster=self.roster)
                self.assertEqual(state, "EXHAUSTED")
                self.assertEqual(selector._pool_price(self.roster, runtime, "chatgpt-work", {}, fleetctl)["lambda"], 0)

    def test_declared_refresh_and_explicit_snapshot_do_not_borrow_or_clear_circuit(self):
        self.roster["quota_pools"]["chatgpt-work"].update(
            quota_refresh={"oracle": "codexbar", "provider": "codex"}, shares_limits_with=["codex"])
        self.roster["quota_pools"]["codex"]["quota_refresh"] = {"oracle": "codexbar", "provider": "codex"}
        self.assertEqual(set(fleetctl.quota_sources(self.roster)), {"codex"})
        self.runtime["pool_circuits"] = {"chatgpt-work": {"until": fleetctl.iso(self.now + dt.timedelta(hours=1))}}
        (self.state / "runtime.json").write_text(json.dumps(self.runtime))
        args = argparse.Namespace(provider="chatgpt-work", timeout=1)
        with patch.dict(fleetctl.ORACLE_REGISTRY, {"codexbar": unittest.mock.Mock()}) as registry, contextlib.redirect_stdout(io.StringIO()):
            result = fleetctl.codexbar_snapshot_command(args, self.state, self.roster)
            registry["codexbar"].assert_not_called()
        self.assertFalse(result["chatgpt-work"]["available"])
        self.assertEqual(fleetctl.load_json(self.state / "runtime.json", {})["quota_snapshots"], self.runtime["quota_snapshots"])
        self.assertEqual(fleetctl.load_json(self.state / "runtime.json", {})["pool_circuits"], self.runtime["pool_circuits"])

    def test_doctor_accepts_missing_oracle_only_for_declared_no_known_limit(self):
        self.roster["quota_pools"].pop("codex")
        overlay = self.state / "overlay.json"
        for limit in ["none-known", None]:
            self.roster["quota_pools"]["chatgpt-work"]["plan"]["limit"] = limit
            overlay.write_text(json.dumps(self.roster))
            output = io.StringIO()
            with patch.object(fleetctl, "effort_problems", return_value=[]), contextlib.redirect_stdout(output):
                result = fleetctl.doctor_command(overlay, self.state)
            self.assertEqual(result, 0, output.getvalue())  # missing quota is a warning, not a fatal doctor error
            self.assertEqual("no known limit, no quota oracle needed" in output.getvalue(), limit == "none-known")
            self.assertEqual("NONE     chatgpt-work" in output.getvalue(), limit is None)


if __name__ == "__main__":
    unittest.main()
