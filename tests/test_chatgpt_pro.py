"""Pro accounting and fallback behavior, using isolated ledgers and fake HTTP."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import chatgpt_catalog as catalog
import chatgpt_pro as pro
import chatgpt_workers as workers
import console
import fleetctl
import selector
from tests import test_chatgpt as chat_fixture
from tests.test_selector import quality_row


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = json.loads((ROOT / "examples/access-overlay.example.json").read_text())
        self.template = self.roster["chatgpt_gateway"]["lane_template"]
        self.template["auth"]["key_file"] = str(self.root / "key")
        (self.root / "key").write_text("test-key")
        models = {"object": "list", "data": [
            {"object": "model", "id": "chatgpt:latest-" + level, "saved": True, "row": "Latest", "level": number}
            for number, level in enumerate(("instant", "medium", "high", "xhigh", "pro"))]}
        self.status = {"workers": [{"label": "latest-" + level, "contact": "recent", "polling": True}
                                  for level in ("instant", "medium", "high", "xhigh", "pro")]}
        with patch.object(catalog, "request", side_effect=lambda b, k, path, **kw: models if path == "/models" else self.status):
            self.roster = catalog.expand(self.roster, self.root)
        self.lanes = fleetctl.lane_map(self.roster)
        self.now = dt.datetime(2026, 10, 4, 12, tzinfo=dt.timezone.utc)
        self.runtime = {}

    def ledger(self, rows):
        (self.root / "runs.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n{torn")

    def answer(self, age=0, **extra):
        return {"harness": "chatgpt-chat", "run_id": str(age), "selector": "chatgpt:latest-pro",
                "returncode": 0, "ended_at": fleetctl.iso(self.now - dt.timedelta(seconds=age)), **extra}

    def test_rolling_boundary_failed_requests_duplicates_and_wake_logs(self):
        self.ledger([self.answer(), self.answer(), self.answer(1, returncode=4),
                     self.answer(2, selector="chatgpt:latest-medium"), self.answer(pro.WEEK),
                     self.answer(-1), self.answer(3)])
        path = workers.wake_log_file(self.template)
        rows = [dict(ts=self.now.timestamp() - age, label=label, outcome=outcome, stage=stage, **extra)
                for age, label, outcome, stage, extra in [
                    (1, "latest-pro", "ok", "previous conversation archive", {}),
                    (2, "latest-pro", "failed", "worker registration", {}),
                    (3, "latest-pro", "failed", "wake admission", {}),
                    (3, "latest-pro", "ok", "saved worker lookup", {}),
                    (4, "latest-pro", "ok", "worker registration", {"refunded": True}),
                    (5, "latest-medium", "ok", "worker registration", {}),
                    (pro.WEEK, "latest-pro", "ok", "worker registration", {})]]
        path.write_text("\n".join(json.dumps(row) for row in rows))
        meter = pro.usage(self.roster, self.root, "chatgpt-work", now=self.now)
        self.assertEqual((meter["requests"], meter["wakes"], meter["used"], meter["remaining"]), (2, 2, 4, 196))
        self.assertTrue(meter["estimate"])
        self.assertFalse(meter["spent"])

    def test_console_and_both_briefs_show_owner_allowance(self):
        self.roster["quota_pools"]["chatgpt-work"]["pro_weekly_allowance"] = 3
        self.ledger([self.answer(2), self.answer(1)])
        overview = fleetctl.fleet_overview(self.roster, self.runtime, self.root, self.now)
        for rendered in (console.render_page(overview, "fixture"), fleetctl.render_brief(overview),
                         fleetctl.render_brief(overview, verbose=True)):
            self.assertIn("Pro estimate: 2/3 in rolling 7 days", rendered)
            self.assertIn("2 answered requests", rendered)

    def test_quota_pause_estimate_and_switch_route_extra_high_then_high(self):
        ranked = ["chatgpt:latest-pro", "chatgpt:latest-medium", "chatgpt:latest-high"]
        self.roster["routing"]["roles"]["hard-reasoning"] = {band: ranked for band in ("quality_first", "conserve", "critical")}
        for reason in ("quota", "estimate", "off"):
            with self.subTest(reason=reason):
                runtime = {}
                lane = self.lanes["chatgpt:latest-pro"]
                lane["gateway_status"] = {"quota_blocked": reason == "quota"}
                self.roster["chatgpt_pro_usage"] = {"chatgpt-work": {"spent": reason == "estimate"}}
                if reason == "off":
                    fleetctl.set_model_toggle(runtime, self.roster, "chatgpt-work", lane["model_key"], False)
                choice = fleetctl.choose_lane(self.roster, runtime, "hard-reasoning", "read-only", "text", "chatgpt-chat")
                self.assertEqual(choice["lane_id"], "chatgpt:latest-xhigh")
                self.assertEqual(choice["pro_fallback"]["requested_model"], "chatgpt:latest-pro")
                fleetctl.set_model_toggle(runtime, self.roster, "chatgpt-work", "chatgpt:latest-xhigh", False)
                self.assertEqual(fleetctl.choose_lane(self.roster, runtime, "hard-reasoning", "read-only", "text", "chatgpt-chat")["lane_id"], "chatgpt:latest-high")
                fleetctl.set_model_toggle(runtime, self.roster, "chatgpt-work", "chatgpt:latest-high", False)
                with self.assertRaises(fleetctl.FleetError):
                    fleetctl.choose_lane(self.roster, runtime, "hard-reasoning", "read-only", "text", "chatgpt-chat")

    def test_target_selector_prefers_extra_high_even_with_better_high_score(self):
        self.lanes["chatgpt:latest-pro"]["gateway_status"] = {"quota_blocked": True}
        self.roster["policy"]["selector"]["allowed_pools_by_role"] = {"hard-reasoning": ["chatgpt-work"]}
        evidence = self.root / "evidence/levels.json"
        evidence.parent.mkdir()
        evidence.write_text(json.dumps({"rows": [quality_row("chatgpt:latest-" + level, level, mean=mean, family="reasoning")
                                               for level, mean in (("xhigh", .6), ("high", .9))]}))
        selected = selector.select_option(self.roster, {}, self.root, "hard-reasoning", mode="read-only",
                                          target=("chatgpt-chat", "chatgpt:latest-pro"))
        self.assertEqual(selected["choice"]["worker_level"], "xhigh")
        receipt = json.loads(Path(selected["selection_file"]).read_text())
        self.assertIn("Pro paused", receipt["choice"]["pro_fallback"]["reason"])
        # Missing quality evidence must not override the owner's fallback order.
        evidence.write_text(json.dumps({"rows": [quality_row("chatgpt:latest-high", "high", mean=.5, family="reasoning")]}))
        selected = selector.select_option(self.roster, {}, self.root, "hard-reasoning", mode="read-only",
                                          target=("chatgpt-chat", "chatgpt:latest-pro"))
        self.assertEqual(selected["choice"]["worker_level"], "xhigh")

    def test_reset_and_unknown_expiry_stay_pro_specific(self):
        lane = self.lanes["chatgpt:latest-pro"]
        until = pro.reset_at("Try again in 3 days", now=self.now)
        self.assertEqual(until, "2026-10-07T12:00:00Z")
        self.assertEqual(pro.reset_at("unknown", now=self.now), "2026-10-05T12:00:00Z")
        runtime = {"chatgpt_pro_blocks": {"chatgpt-work": {"until": until}}}
        self.assertTrue(pro.blocked(self.roster, runtime, lane, now=self.now))
        self.assertIsNone(pro.blocked(self.roster, runtime, self.lanes["chatgpt:latest-xhigh"], now=self.now))
        self.assertIsNone(pro.blocked(self.roster, runtime, lane, now=self.now + dt.timedelta(days=4)))

    def test_answer_about_limits_is_not_a_downgrade_report(self):
        self.assertIsNone(pro.LIMIT_REPORT.search("The Pro rate limit is estimated from a rolling seven-day window."))
        self.assertTrue(pro.LIMIT_REPORT.search("You've reached the Pro quota limit. Try again in 3 days."))

    def test_idempotent_retry_does_not_count_as_another_pro_use(self):
        self.ledger([self.answer(3, idempotency_key="same-job"), self.answer(1, idempotency_key="same-job")])
        self.assertEqual(pro.usage(self.roster, self.root, "chatgpt-work", now=self.now)["requests"], 1)

    def test_corrupt_pro_wake_state_preserves_other_providers(self):
        for contents in ("{torn", "[]", "null"):
            with self.subTest(contents=contents):
                (self.root / "wake-state.json").write_text(contents)
                overview = fleetctl.fleet_overview(self.roster, {}, self.root, self.now)
                meter = next(pool["pro_usage"] for pool in overview["pools"] if pool["pool"] == "chatgpt-work")
                self.assertTrue(meter["unavailable"])
                self.assertTrue(pro.blocked(self.roster, {}, self.lanes["chatgpt:latest-pro"]))
                self.assertTrue(any(pool["pool"] != "chatgpt-work" for pool in overview["pools"]))
                self.assertIn("Pro estimate unavailable", console.render_page(overview, "fixture"))


class ProRunnerTests(unittest.TestCase):
    # Reuse the existing HTTP/socket-pair fixture without inheriting its tests.
    save = chat_fixture.ChatGPTTests.save
    command = chat_fixture.ChatGPTTests.command
    run_adapter = chat_fixture.ChatGPTTests.run_adapter

    def setUp(self):
        chat_fixture.ChatGPTTests.setUp(self)
        self.lane["lane_id"] = "chatgpt:latest-pro"
        self.gateway.catalog_ids = ["chatgpt:latest-" + level for level in ("medium", "high", "xhigh", "pro")]
        self.gateway.catalog_levels = {"chatgpt:latest-" + level: number for number, level in enumerate(("instant", "medium", "high", "xhigh", "pro"))}
        self.gateway.mode_by_selector = {}
        template = self.roster["chatgpt_gateway"]["lane_template"]
        template["transport"] = self.lane["transport"]
        template["auth"] = self.lane["auth"]
        self.save()

    def record(self):
        return json.loads((self.work / "last.crossfeed.json").read_text())

    def test_downgrade_rate_limit_and_reset_trigger_extra_high(self):
        for mode in ("downgraded", "429", "rate-text"):
            with self.subTest(mode=mode):
                # Use independent state so each case really submits to Pro first.
                self.env["FLEET_STATE_DIR"] = str(self.work / mode)
                self.gateway.payloads.clear()
                self.gateway.mode_by_selector = {"chatgpt:latest-pro": mode}
                result = self.run_adapter()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([row["model"] for row in self.gateway.payloads], ["chatgpt:latest-pro", "chatgpt:latest-xhigh"])
                self.assertEqual(result.stdout, "PONG\n")
                record = self.record()
                self.assertEqual(record["requested_model"], "chatgpt:latest-pro")
                self.assertEqual(record["selected_model"], "chatgpt:latest-xhigh")
                self.assertIn("Pro fallback", result.stderr)
                self.assertIsNone(record["actual_model"])
                runtime = json.loads((Path(self.env["FLEET_STATE_DIR"]) / "runtime.json").read_text())
                delta = fleetctl.parse_iso(runtime["chatgpt_pro_blocks"]["chatgpt-work"]["until"]) - fleetctl.utc_now()
                expected = 3 * 86400 if mode == "rate-text" else 86400
                self.assertAlmostEqual(delta.total_seconds(), expected, delta=10)
                self.assertEqual(len(set(self.gateway.keys[-2:])), 2)

    def test_extra_high_failure_uses_high_and_never_medium(self):
        self.gateway.quota_blocked = ["chatgpt:latest-pro"]
        self.gateway.mode_by_selector = {"chatgpt:latest-xhigh": "503"}
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row["model"] for row in self.gateway.payloads], ["chatgpt:latest-xhigh", "chatgpt:latest-high"])
        self.assertEqual(self.record()["selected_model"], "chatgpt:latest-high")

    def test_pause_discovered_after_health_avoids_pro_submission(self):
        self.gateway.pro_block_after = 4
        result = self.run_adapter()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row["model"] for row in self.gateway.payloads], ["chatgpt:latest-xhigh"])

    def test_medium_picker_in_replacement_is_refused(self):
        self.gateway.quota_blocked = ["chatgpt:latest-pro"]
        self.gateway.mode_by_selector = {"chatgpt:latest-xhigh": "downgraded", "chatgpt:latest-high": "downgraded"}
        result = self.run_adapter()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_spent_estimate_and_owner_off_avoid_pro_submission(self):
        for reason in ("estimate", "off"):
            with self.subTest(reason=reason):
                self.env["FLEET_STATE_DIR"] = str(self.work / reason)
                self.gateway.payloads.clear()
                self.roster["quota_pools"]["chatgpt-work"]["pro_weekly_allowance"] = 0 if reason == "estimate" else 200
                state = Path(self.env["FLEET_STATE_DIR"])
                if reason == "off":
                    state.mkdir()
                    (state / "runtime.json").write_text(json.dumps({"model_preferences": {"chatgpt:latest-pro": "off"}}))
                self.save()
                result = self.run_adapter()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([row["model"] for row in self.gateway.payloads], ["chatgpt:latest-xhigh"])


if __name__ == "__main__":
    unittest.main()
