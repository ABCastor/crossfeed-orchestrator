"""Shadow telemetry never routes and only an external check marks acceptance."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import test_selector as fixtures

fleetctl = fixtures.fleetctl
selector = fixtures.selector
import rating_log


class RatingLoggingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rating logging ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime.json"
        now = fleetctl.utc_now()
        self.reset = fleetctl.iso(now + dt.timedelta(hours=2))
        self.snapshot(20, fleetctl.iso(now - dt.timedelta(seconds=2)))
        self.dispatch_id = None
        self.calls = []

    def snapshot(self, percent, observed):
        self.runtime.write_text(json.dumps({"quota_snapshots": {"claude": {
            "source": "synthetic-gauge", "observed_at": observed, "windows": {"weekly": {
                "used_percent": percent, "reset_at": self.reset, "window_minutes": 10080}}}}}))

    def option(self, name, price, pool="claude"):
        return {"model_key": name, "model_identity": name, "pool": pool, "harness": "claude",
                "level": "high", "cash": {"funding": "included"}, "cost": {"percent": price},
                "capacity": {"weekly_usd_equivalent": 100},
                "pool_price": {"lambda": 0, "window_minutes": 10080},
                "q": {"mean": .7, "unknown": False}, "selection_file": f"{name}.json",
                "command_argv": ["fake", name, "--prompt", "<task>"]}

    def dispatch(self, supplied=(), codes=(0,)):
        before = fleetctl.load_json(self.runtime)["quota_snapshots"]["claude"]["windows"]["weekly"]["used_percent"]
        args = fleetctl.build_parser().parse_args(["dispatch", "--role", "review", "--prompt", "private task",
                                                   "--dir", str(self.root), *supplied])
        items = [self.option(f"model-{i}", 4-i) for i in range(4)]
        selection = {"choice": items[0], "top3": items[:3], "eligible_options": items}
        codes = iter(codes)

        def start(argv, *, env, **kwargs):
            self.calls.append(argv[1])
            self.dispatch_id = env["CROSSFEED_DISPATCH_ID"]
            last = argv[argv.index("--last") + 1]
            def wait():
                self.snapshot(before + 7, fleetctl.iso())
                Path(last + ".crossfeed.json").write_text(json.dumps({"schema": "crossfeed-model-run/v1",
                    "dispatch_id": self.dispatch_id, "run_id": "verified-" + self.dispatch_id}))
                return next(codes)
            return mock.Mock(wait=wait)

        with mock.patch.object(fleetctl.subprocess, "Popen", side_effect=start), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fleetctl.dispatch_selection(selection, args, self.root, args.prompt)

    def records(self):
        return [json.loads(line) for line in (self.root / rating_log.LEDGER).read_text().splitlines()]

    def accept(self, run_id, *extra):
        return subprocess.run([sys.executable, str(fixtures.ROOT / "scripts/fleetctl.py"), "--state-dir", str(self.root),
                               "accept", run_id, "--verdict", "pass", "--check", "unit tests pass", *extra],
                              capture_output=True, text=True, check=False)

    def test_omitted_flags_and_acceptance_round_trip(self):
        self.assertEqual(self.dispatch(), 0)
        row, = self.records()
        self.assertEqual((row["work_kind"], row["difficulty"], row["stakes"]), ("unknown",) * 3)
        self.assertEqual(row["shadow_status"], "unknown_task_labels")
        self.assertIsNone(row["shadow_pick"])
        self.assertIsNone(row["live_pick"]["predicted_success"])
        self.assertTrue(row["task_id"])
        self.assertEqual(row["run_id"], self.dispatch_id)
        self.assertNotIn("verdict", row)
        self.assertNotIn("private task", (self.root / rating_log.LEDGER).read_text())
        self.assertNotEqual(self.accept("nonexistent").returncode, 0)
        self.assertNotEqual(self.accept(row["run_id"], "--check", " ").returncode, 0)
        result = self.accept(row["worker_run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        acceptance = json.loads(result.stdout)
        self.assertEqual(self.records(), [row, acceptance])
        self.assertEqual((acceptance["run_id"], acceptance["task_id"], acceptance["verdict"], acceptance["check"]),
                         (row["run_id"], row["task_id"], "pass", "unit tests pass"))
        self.assertEqual(self.accept(row["run_id"], "--verdict", "fail").returncode, 0)
        self.assertEqual(self.records()[-1]["verdict"], "fail")

    def test_forecast_prices_eligible_set_and_quota_delta(self):
        self.assertEqual(self.dispatch(), 0)  # prior priced task seeds typical price
        flags = ("--task-id", "trial-42", "--work-kind", "review", "--difficulty", "easy", "--rating-stakes", "important")
        self.assertEqual(self.dispatch(flags), 0)
        row = self.records()[-1]
        self.assertEqual((row["task_id"], row["work_kind"], row["difficulty"], row["stakes"], row["typical_price"]),
                         ("trial-42", "review", "easy", "important", 4))
        self.assertEqual(row["shadow_status"], "scored")
        self.assertEqual(len(row["eligible_options"]), 4)
        for name, model, price in [("live_pick", "model-0", 4), ("shadow_pick", "model-3", 1)]:
            self.assertEqual(row[name]["estimated_price"], price)
            self.assertEqual(row[name]["model_key"], model)
            self.assertGreater(row[name]["predicted_success"], .5)
        observed, = row["observed_quota_debit"]
        self.assertEqual(observed["delta_percentage_points"], 7)
        self.assertEqual(observed["attribution"], "pool_window_not_isolated")
        self.assertEqual(self.calls, ["model-0", "model-0"])

    def test_live_price_epoch_does_not_mix_shadow_prices_or_retry_cost(self):
        self.assertEqual(self.dispatch(), 0)
        basis = rating_log.rating.LIVE_PRICE_BASIS
        self.assertIsNone(rating_log.typical_price(self.root, pricing_basis=basis))
        old = self.option

        def live_option(name, price, pool="claude"):
            return dict(old(name, price, pool), pricing_basis=basis, estimated_price=price / 100,
                        p_success=.82)

        with mock.patch.object(self, "option", side_effect=live_option):
            self.assertEqual(self.dispatch(codes=(5, 0)), 0)
        first, retry = self.records()[-2:]
        self.assertEqual(first["live_pick"]["pricing_basis"], basis)
        self.assertEqual(first["live_pick"]["estimated_price"], .04)
        self.assertEqual(first["live_pick"]["predicted_success"], .82)
        self.assertEqual(retry["live_pick"]["estimated_price"], .03)
        self.assertEqual(rating_log.typical_price(self.root, pricing_basis=basis), .04)
        self.assertEqual(rating_log.typical_price(self.root), 4)

    def test_scoring_failure_and_retries_keep_live_choices(self):
        self.assertEqual(self.dispatch(), 0)
        with mock.patch.object(rating_log.rating, "choose", side_effect=RuntimeError("broken ratings")) as choose:
            self.assertEqual(self.dispatch(("--work-kind", "coding", "--difficulty", "ordinary", "--rating-stakes", "routine"),
                                           codes=(5, 0)), 0)
        self.assertTrue(choose.called)
        first, retry = self.records()[-2:]
        self.assertEqual(first["task_id"], retry["task_id"])
        self.assertNotEqual(first["run_id"], retry["run_id"])
        self.assertEqual((first["shadow_status"], retry["shadow_status"]), ("rating_error", "rating_error"))
        self.assertEqual(self.calls, ["model-0", "model-0", "model-1"])
        self.assertEqual(retry["live_pick"]["estimated_price"], 3)

    def test_select_parser_and_live_fixture_are_unchanged(self):
        parse = fleetctl.build_parser().parse_args
        old = parse(["select", "--role", "review"])
        labelled = parse(["select", "--role", "review", "--task-id", "same", "--work-kind", "review",
                          "--difficulty", "hard", "--rating-stakes", "critical"])
        self.assertEqual(old.stakes, labelled.stakes)
        self.assertEqual(rating_log.labels(labelled), {"task_id": "same", "work_kind": "review",
                                                        "difficulty": "hard", "stakes": "critical"})
        fixture = fixtures.SelectorTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.only(["codex"])
        fixture.one_level("codex")
        fixture.evidence([fixtures.quality_row("gpt-6.1-sol")])
        runtime = fixture.snapshot("codex", 40)
        with mock.patch.object(selector.random, "random", return_value=.5):
            baseline = fixture.select(runtime, persist_receipts=False)
            instrumented = fixture.select(runtime, persist_receipts=False, collect_eligible=True)
        self.assertTrue(instrumented.pop("eligible_options"))
        self.assertEqual(baseline, instrumented)

    def test_debit_requires_a_new_comparable_reading(self):
        before = fleetctl.load_json(self.runtime)["quota_snapshots"]["claude"]
        start = fleetctl.iso()
        self.snapshot(25, fleetctl.iso())
        after = fleetctl.load_json(self.runtime)["quota_snapshots"]["claude"]
        end = fleetctl.iso()
        self.assertEqual(rating_log.debit(before, before, start, end), [])
        after["windows"]["weekly"]["reset_at"] = fleetctl.iso(fleetctl.utc_now() + dt.timedelta(hours=4))
        self.assertEqual(rating_log.debit(before, after, start, end), [])


if __name__ == "__main__":
    unittest.main()
