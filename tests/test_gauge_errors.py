"""Configured oracle failures must not masquerade as unmeasured surplus."""
import argparse
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_selector import ROOT, fleetctl, quality_row, selector


class GaugeErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.fixture = ROOT / "tests/fixtures/codexbar-provider-error.py"
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.roster["quota_pools"] = {pool: self.roster["quota_pools"][pool]
                                     for pool in ("opencode-go", "codex")}
        self.roster["lanes"] = [lane for lane in self.roster["lanes"]
                                if lane["model_key"] in {"kimi-k2.7-code", "gpt-6.1-sol"}]
        self.roster["quota_pools"]["unconfigured"] = {"quota_refresh": None}
        home = self.state / "home"
        home.mkdir()
        self.env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
                    "OPENCODE_DB": str(home / "opencode.db"), "CODEXBAR_BIN": str(self.fixture),
                    "ACCESS_OVERLAY": str(self.state / "overlay.json"),
                    "FLEET_STATE_DIR": str(self.state), "FLEET_NO_AUTO_REFRESH": "0",
                    "FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0",
                    "CODEXBAR_GO_RESPONSE": "error"}
        for key, value in (("CODEXBAR_BIN", str(self.fixture)),
                           ("_POLICY_CACHE", ("clock_aware", "test"))):
            guard = patch.object(fleetctl, key, value)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, self.env)
        guard.start()
        self.addCleanup(guard.stop)
        (self.state / "overlay.json").write_text(json.dumps(self.roster))

    def refresh(self, **kwargs):
        return fleetctl.refresh_stale_pools(self.state, ["opencode-go", "codex"],
                                            roster=self.roster, **kwargs)

    def runtime(self):
        return fleetctl.load_json(self.state / "runtime.json", {})

    def seed(self, age_s, used=90):
        now = fleetctl.utc_now()
        reading = {"source": "codexbar", "observed_at": fleetctl.iso(now - dt.timedelta(seconds=age_s)),
                   "windows": {"weekly": {"used_percent": used, "window_minutes": 10080,
                                           "reset_at": fleetctl.iso(now + dt.timedelta(days=2))}}}
        fleetctl.atomic_json(self.state / "runtime.json", {"quota_snapshots": {"opencode-go": reading}})
        return reading

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts/fleetctl.py"), *args],
                              env=self.env, text=True, capture_output=True, timeout=30)

    def test_failed_provider_is_distinct_from_unconfigured_and_carries_reason(self):
        self.refresh()
        runtime = self.runtime()
        state, evidence = fleetctl.current_pool_state(runtime, "opencode-go", roster=self.roster)
        self.assertEqual(state, "GAUGE_ERROR")
        self.assertIn("HTTP 500", evidence["gauge_error"])
        self.assertEqual(fleetctl.current_pool_state(runtime, "codex", roster=self.roster)[0], "ABUNDANT")
        self.assertEqual(fleetctl.current_pool_state(runtime, "unconfigured", roster=self.roster)[0], "UNKNOWN")
        self.assertNotIn("opencode-go", runtime["quota_snapshots"])
        self.refresh()  # cooldown keeps the failure, rather than replacing it with UNKNOWN
        self.assertEqual(fleetctl.current_pool_state(self.runtime(), "opencode-go")[0], "GAUGE_ERROR")

    def test_stderr_and_nonzero_exit_cannot_be_successful_readings(self):
        for mode in ("stderr", "nonzero-usage"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"CODEXBAR_GO_RESPONSE": mode}):
                result = fleetctl.codexbar_observed("opencodego", binary=str(self.fixture))
                self.assertFalse(result["available"])
                self.assertIn("500", result["reason"])

    def test_last_success_is_stale_for_at_most_six_hours(self):
        reading = self.seed(7200)
        self.refresh()
        runtime = self.runtime()
        observed = fleetctl.parse_iso(reading["observed_at"])
        for age_s, expected in ((7200, "CRITICAL"), (21600, "CRITICAL"), (21601, "GAUGE_ERROR")):
            with self.subTest(age_s=age_s):
                state, evidence = fleetctl.current_pool_state(
                    runtime, "opencode-go", observed + dt.timedelta(seconds=age_s), roster=self.roster)
                self.assertEqual(state, expected)
                self.assertEqual(evidence["confidence"], "stale")
                self.assertIn("HTTP 500", evidence["gauge_error"])
        self.assertEqual(runtime["quota_snapshots"]["opencode-go"], reading)
        result = self.cli("brief", "--no-refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(line for line in result.stdout.splitlines() if line.startswith("opencode-go |"))
        self.assertIn("90% (stale", line)

    def test_error_without_usable_cache_is_visible_in_brief_and_doctor(self):
        for age_s in (None, 21601):
            with self.subTest(age_s=age_s):
                if age_s is not None:
                    self.seed(age_s)
                result = self.cli("brief")
                self.assertEqual(result.returncode, 0, result.stderr)
                line = next(line for line in result.stdout.splitlines() if line.startswith("opencode-go |"))
                self.assertIn("gauge error:", line)
                self.assertIn("HTTP 500", line)
                self.assertIn("~1.00", line)
                result = self.cli("doctor")
                self.assertEqual(result.returncode, 1, result.stderr)
                line = next(line for line in result.stdout.splitlines() if "opencode-go" in line and "oracle" in line)
                self.assertIn("FAIL", line)
                self.assertIn("HTTP 500", line)

    def test_recent_fallback_still_reports_failing_oracle(self):
        self.seed(7200)
        self.refresh()
        result = self.cli("doctor")
        line = next(line for line in result.stdout.splitlines() if "opencode-go" in line and "oracle" in line)
        self.assertIn("FAIL", line)
        self.assertIn("HTTP 500", line)

    def test_uncertainty_price_prefers_measured_similar_quality_but_keeps_fallback(self):
        self.refresh()
        runtime = self.runtime()
        runtime["quota_snapshots"]["codex"]["windows"]["primary"]["projected_used_percent_at_reset"] = 95
        evidence = self.state / "evidence"
        evidence.mkdir()
        rows = [quality_row("kimi-k2.7-code", level="provider-default", mean=.8, family="coding-agent"),
                quality_row("gpt-6.1-sol", mean=.799, family="coding-agent")]
        for row, pool in zip(rows, ("opencode-go", "codex")):
            row["own_cost"] = {pool: {"percent_per_task": .1}}
        (evidence / "levels.json").write_text(json.dumps({"schema": "fleet-evidence-levels/v1", "rows": rows}))
        allow = "opencode-go:kimi-k2.7-code:provider-default,codex:gpt-6.1-sol:high"
        unmeasured = copy.deepcopy(runtime)
        unmeasured.pop("quota_refresh_errors")
        control = selector.select_option(self.roster, unmeasured, self.state, "default", allow=allow, fleet=fleetctl)
        # BR-SELECT-002: the two are within the variety band, so the pick is varied; the gauge
        # price still decides the ranking and tilts the draw.
        self.assertEqual(control["top3"][0]["pool"], "opencode-go")
        result = selector.select_option(self.roster, runtime, self.state, "default", allow=allow, fleet=fleetctl)
        self.assertEqual(result["top3"][0]["pool"], "codex")
        odds = {item["pool"]: item["probability"] for item in result["variety"]["candidates"]}
        self.assertGreater(odds["codex"], odds["opencode-go"])
        self.assertEqual(result["lambdas"]["codex"], .5)
        self.assertEqual(result["lambdas"]["opencode-go"], 1.0)
        fallback = selector.select_option(self.roster, runtime, self.state, "default",
                                          allow="opencode-go:kimi-k2.7-code:provider-default", fleet=fleetctl)
        self.assertEqual(fallback["choice"]["pool"], "opencode-go")
        price = fallback["choice"]["pool_price"]
        self.assertEqual(price["state"], "GAUGE_ERROR")
        self.assertTrue(price["projection_unknown"])
        self.assertEqual(price["projection_basis"], "gauge_error")
        unknown = selector._pool_price(self.roster, {}, "opencode-go", {}, fleetctl)
        self.assertEqual(unknown["lambda"], .5)  # never measured retains the fail-open fallback
        fleetctl.atomic_json(self.state / "runtime.json", runtime)
        result = self.cli("select", "--role", "default", "--allow", allow, "--json", "--no-refresh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["top3"][0]["pool"], "codex")

    def test_success_clears_error_even_when_keeping_a_newer_snapshot(self):
        self.refresh()
        with patch.dict(os.environ, {"CODEXBAR_GO_RESPONSE": "ok"}):
            self.refresh(force=True)
        runtime = self.runtime()
        self.assertNotIn("opencode-go", runtime.get("quota_refresh_errors", {}))
        self.assertEqual(fleetctl.current_pool_state(runtime, "opencode-go")[0], "ABUNDANT")
        self.refresh(force=True)
        with patch.dict(os.environ, {"CODEXBAR_GO_RESPONSE": "ok"}):
            reading = self.seed(-60, used=20)
            runtime = self.runtime()
            runtime["quota_refresh_errors"] = {"opencode-go": {"reason": "HTTP 500", "attempted_at": fleetctl.iso()}}
            fleetctl.atomic_json(self.state / "runtime.json", runtime)
            self.refresh(force=True)
        runtime = self.runtime()
        self.assertEqual(runtime["quota_snapshots"]["opencode-go"], reading)
        self.assertNotIn("opencode-go", runtime.get("quota_refresh_errors", {}))

    def test_manual_oracle_refresh_also_records_failure_and_recovery(self):
        args = argparse.Namespace(provider="opencodego", timeout=5, json=True)
        with contextlib.redirect_stdout(io.StringIO()):
            fleetctl.codexbar_snapshot_command(args, self.state, self.roster)
        self.assertEqual(fleetctl.current_pool_state(self.runtime(), "opencode-go")[0], "GAUGE_ERROR")
        with patch.dict(os.environ, {"CODEXBAR_GO_RESPONSE": "ok"}), contextlib.redirect_stdout(io.StringIO()):
            fleetctl.codexbar_snapshot_command(args, self.state, self.roster)
        self.assertNotIn("opencode-go", self.runtime().get("quota_refresh_errors", {}))

    def test_failure_never_reopens_known_exhaustion(self):
        self.seed(25000, used=100)
        self.refresh()
        state, _ = fleetctl.current_pool_state(self.runtime(), "opencode-go", roster=self.roster)
        self.assertEqual(state, "EXHAUSTED")

    def test_error_does_not_reuse_a_window_after_its_reset(self):
        reading = self.seed(7200)
        reading["windows"]["weekly"]["reset_at"] = fleetctl.iso(fleetctl.utc_now() - dt.timedelta(seconds=1))
        fleetctl.atomic_json(self.state / "runtime.json", {"quota_snapshots": {"opencode-go": reading}})
        self.refresh()
        runtime = self.runtime()
        self.assertEqual(fleetctl.current_pool_state(runtime, "opencode-go")[0], "GAUGE_ERROR")
        self.assertEqual(selector._pool_price(self.roster, runtime, "opencode-go", {}, fleetctl)["lambda"], 1)

    def test_older_inflight_failure_preserves_newer_success(self):
        oracle = fleetctl.ORACLE_REGISTRY["codexbar"]

        def racing_oracle(config, timeout):
            if config["provider"] != "opencodego":
                return oracle(config, timeout)
            reading = {**self.seed(0, used=20), "available": True}
            with fleetctl.locked_runtime(self.state) as runtime:
                runtime["quota_snapshots"]["opencode-go"] = reading
                fleetctl.record_gauge_result(runtime, "opencode-go", reading,
                                             fleetctl.iso(fleetctl.utc_now() + dt.timedelta(seconds=1)))
            return {"available": False, "reason": "older HTTP 500"}

        with patch.dict(fleetctl.ORACLE_REGISTRY, {"codexbar": racing_oracle}):
            result = self.refresh()
        self.assertEqual(result["opencode-go"], "kept newer refresh result")
        self.assertNotIn("opencode-go", self.runtime().get("quota_refresh_errors", {}))
        self.assertEqual(fleetctl.current_pool_state(self.runtime(), "opencode-go")[0], "ABUNDANT")

    def test_adapter_exception_is_a_gauge_error(self):
        def broken_oracle(config, timeout):
            raise RuntimeError("oracle transport failed")

        with patch.dict(fleetctl.ORACLE_REGISTRY, {"codexbar": broken_oracle}):
            self.refresh()
        state, evidence = fleetctl.current_pool_state(self.runtime(), "opencode-go")
        self.assertEqual(state, "GAUGE_ERROR")
        self.assertIn("oracle transport failed", evidence["gauge_error"])


if __name__ == "__main__":
    unittest.main()
