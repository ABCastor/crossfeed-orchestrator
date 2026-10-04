import contextlib
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from bench import cost, run_grid


def fleet_usage(pool, used, observed="2026-10-02T10:00:00+00:00", reset="2026-10-03T10:00:00+00:00"):
    # Mirrors fleetctl.show_usage(), including routing-pool versus other-pool nesting.
    quota = {"quota_state": "HEALTHY", "band": "normal", "bottleneck_used_percent": used,
             "binding_used_percent": used, "non_binding": [], "spend_down": [],
             "confidence": "direct", "age_seconds": 0, "observed_at": observed,
             "source": "fixture-console", "windows": {"weekly": {"used_percent": used,
             "reset_at": reset, "seconds_to_reset": 86400, "window_minutes": 10080}}}
    return {"pool": "opencode-go", "quota_state": "HEALTHY", "quota": quota if pool == "opencode-go" else {},
            "quota_pools": {} if pool == "opencode-go" else {pool: quota}, "metered_pools": {},
            "truth_boundary": {"local_cost_is_quota_debit": False}, "local_observed": {},
            "wrapper_run_ledger": {}, "levels": {}, "quota_policy": {"policy": "clock_aware"}}


class CostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.results = self.root / "results.jsonl"

    def snapshots(self, pool="codex", start=30, end=34, name="sample"):
        paths = []
        for when, used, captured in (("before", start, "2026-10-02T10:00:00+00:00"),
                                     ("after", end, "2026-10-02T10:02:00+00:00")):
            path = self.root / (name + "." + when + ".json")
            path.write_text(json.dumps({"status": "ok", "captured_at": captured,
                                        "data": fleet_usage(pool, used, captured)}))
            paths.append(str(path))
        return paths

    def rows(self, before, after, option="high", pool="codex", tasks=2):
        return [{"task": "task%s" % i, "option": option, "pool": pool, "pass": i % 2 == 0,
                 "tokens_in": 20, "tokens_out": 5,
                 "pool_usage": {"before": before, "after": after}} for i in range(tasks)]

    def summarize(self, rows):
        self.results.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return cost.report(self.results)["options"]

    def test_actual_fleet_shape_multiple_pools_options_and_tokens(self):
        rows = []
        for pool, option, debit in (("codex", "high", 4), ("codex", "low", 2), ("opencode-go", "flash", 6)):
            rows += self.rows(*self.snapshots(pool, end=30 + debit, name=option), option=option, pool=pool)
        summaries = self.summarize(rows)
        self.assertEqual([(s["pool"], s["option"], s["percent_binding_window_per_task"]) for s in summaries],
                         [("codex", "high", 2), ("codex", "low", 1), ("opencode-go", "flash", 3)])
        self.assertTrue(all(s["tokens_in"] == 40 and s["tokens_out"] == 10 for s in summaries))
        self.assertTrue(all(s["measured_tasks"] == 2 for s in summaries))

    def test_batch_deltas_are_weighted_by_attempted_tasks_not_batch_means(self):
        rows = self.rows(*self.snapshots(end=32, name="first"), tasks=1)
        more = self.rows(*self.snapshots(end=36, name="second"), tasks=3)
        for row in more:
            row["task"] += "-second"
        summary = self.summarize(rows + more)[0]
        self.assertEqual(summary["percent_binding_window_per_task"], 2)

    def test_missing_failed_reset_unknown_and_cached_snapshots_remain_unknown(self):
        for case in ("missing", "failed", "reset", "unknown", "cached", "decrease", "metered", "invalid", "bottleneck"):
            with self.subTest(case=case):
                before, after = self.snapshots(name=case)
                data = json.loads(Path(after).read_text())
                info = data["data"]["quota_pools"]["codex"]
                if case == "missing":
                    after += ".absent"
                elif case == "failed":
                    data["status"] = "failed"
                elif case == "reset":
                    info["windows"]["weekly"]["reset_at"] = "2026-10-04T10:00:00+00:00"
                elif case == "unknown":
                    info["quota_state"] = "UNKNOWN"
                elif case == "cached":
                    info["observed_at"] = "2026-10-02T10:00:00+00:00"
                elif case == "decrease":
                    info["windows"]["weekly"]["used_percent"] = 29
                elif case == "metered":
                    data["data"]["quota_pools"] = {}
                    data["data"]["metered_pools"] = {"codex": {"estimated_spent_usd_today": 3}}
                elif case == "invalid":
                    info["windows"]["weekly"]["used_percent"] = True
                elif case == "bottleneck":
                    first = json.loads(Path(before).read_text())
                    first["data"]["quota_pools"]["codex"]["windows"]["daily"] = {
                        "used_percent": 20, "reset_at": "2026-10-03T10:00:00+00:00"}
                    Path(before).write_text(json.dumps(first))
                    info["windows"]["daily"] = {"used_percent": 50, "reset_at": "2026-10-03T10:00:00+00:00"}
                if case != "missing":
                    Path(after).write_text(json.dumps(data))
                summary = self.summarize(self.rows(before, after))[0]
                self.assertIsNone(summary["percent_binding_window_per_task"])
                self.assertEqual(summary["unmeasured_tasks"], 2)
                self.assertTrue(summary["batches"][0]["reason"])

    def test_mixed_option_snapshot_pair_does_not_invent_option_cost(self):
        paths = self.snapshots()
        rows = self.rows(*paths, option="high") + self.rows(*paths, option="low")
        self.assertTrue(all(s["percent_binding_window_per_task"] is None for s in self.summarize(rows)))

    def test_shared_noise_and_zero_delta_are_explicit(self):
        rows = self.rows(*self.snapshots(end=30))
        rows[0]["pool_usage"]["shared_pool"] = True
        summary = self.summarize(rows)[0]
        self.assertTrue(summary["shared_pool"])
        self.assertEqual(summary["percent_binding_window_per_task"], 0)
        self.assertTrue(any("another user" in warning for warning in summary["warnings"]))
        self.assertTrue(any("zero delta" in warning for warning in summary["warnings"]))


class PoolRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = self.root / "tasks"
        for name in ("reasoning-1", "reasoning-2"):
            task = self.tasks / name
            (task / "workspace").mkdir(parents=True)
            (task / "PROMPT.md").write_text("Return answer 42.")
            (task / "meta.json").write_text(json.dumps({"seed": 1, "family": "reasoning", "difficulty": "easy"}))
            (task / "check.json").write_text(json.dumps({"kind": "reasoning", "answer": 42}))
        self.options = self.root / "options.json"
        self.out = self.root / "results.jsonl"
        self.config = [{"id": option, "pool": pool, "model": "fixture", "level": "none",
                        "command": [sys.executable, "-c", "print('reply')"]}
                       for option, pool in (("a", "codex"), ("b", "claude"), ("c", "codex"))]
        self.options.write_text(json.dumps({"options": self.config}))

    def cli(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = run_grid.main(["--tasks", str(self.tasks), "--options", str(self.options), "--out", str(self.out), *extra])
        return code, stdout.getvalue()

    def test_pool_option_order_snapshots_resume_and_cell_evidence(self):
        order = []
        def observed(command, path, timeout):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"status": "ok", "data": {}}))
            order.append(path.name)
        def measured(task, option, out):
            order.append(option["id"])
            cell_dir = out / "cells" / (task[0].name + option["id"])
            cell_dir.mkdir(parents=True)
            return {"task": task[2], "option": option["id"], "pool": option["pool"], "pass": True,
                    "task_digest": task[3], "option_digest": run_grid.option_digest(option),
                    "tokens_in": 12, "tokens_out": 3, "cell_dir": str(cell_dir)}
        with mock.patch.object(run_grid, "usage_snapshot", side_effect=observed) as snapshots, \
                mock.patch.object(run_grid, "measure", side_effect=measured):
            self.cli("--pool-batches", "--usage-cmd", "fixture usage --json", "--parallel", "2", "--shared-pool", "codex")
            self.assertEqual(order, ["pool.before.json", "option-a.before.json", "a", "a", "option-a.after.json",
                                     "option-c.before.json", "c", "c", "option-c.after.json", "pool.after.json",
                                     "pool.before.json", "b", "b", "pool.after.json"])
            self.assertEqual(snapshots.call_count, 8)
            self.cli("--pool-batches", "--usage-cmd", "fixture usage --json")
            self.assertEqual(snapshots.call_count, 8)
        rows = list(run_grid.read_results(self.out).values())
        self.assertEqual(len(rows), 6)
        for row in rows:
            refs = row["pool_usage"]
            self.assertTrue(Path(refs["before"]).is_file())
            self.assertTrue(Path(refs["after"]).is_file())
            self.assertEqual(refs["shared_pool"], row["pool"] == "codex")
            self.assertEqual(json.loads((Path(row["cell_dir"]) / "pool-usage.json").read_text()), refs)
            self.assertEqual(row["tokens_in"], 12)

    def test_usage_command_errors_are_persisted_without_raw_error_or_console_output(self):
        for name, command, status in (("valid", [sys.executable, "-c", "print('{}')"], "ok"),
                                      ("invalid", [sys.executable, "-c", "print('invalid')"], "invalid-json"),
                                      ("failure", [sys.executable, "-c", "raise SystemExit(7)"], "failed"),
                                      ("missing", [str(self.root / "not-present")], "failed"),
                                      ("timeout", [sys.executable, "-c", "import time; time.sleep(1)"], "timeout")):
            with self.subTest(name=name):
                path = self.root / (name + ".json")
                run_grid.usage_snapshot(command, path, timeout=0.05 if name == "timeout" else 5)
                self.assertEqual(json.loads(path.read_text())["status"], status)

    def test_dry_run_runs_no_usage_command_and_no_cells(self):
        with mock.patch.object(run_grid, "usage_snapshot", side_effect=AssertionError("executed")), \
                mock.patch.object(run_grid, "measure", side_effect=AssertionError("executed")):
            _, output = self.cli("--pool-batches", "--usage-cmd", "fixture usage", "--dry-run")
        self.assertEqual([json.loads(line)["option"] for line in output.splitlines()], ["a", "a", "c", "c", "b", "b"])
        self.assertFalse(self.out.exists())

    def test_usage_requires_pool_batch(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            self.cli("--usage-cmd", shlex.join([sys.executable, "-c", "print('{}')"]))
        self.assertEqual(failure.exception.code, 2)

    def test_real_offline_cells_preserve_tokens_and_yield_measured_cost(self):
        worker = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('{\"tokens_in\":100,\"tokens_out\":20}'); print('{\"answer\":42}')"
        self.config = [dict(self.config[0], command=[sys.executable, "-c", worker.replace("{", "{{").replace("}", "}}"), "{usage_file}"])]
        self.options.write_text(json.dumps({"options": self.config}))
        oracle = self.root / "usage_oracle.py"
        oracle.write_text("""import json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
state = Path(sys.argv[1])
used = int(state.read_text()) + 2 if state.exists() else 10
state.write_text(str(used))
now = datetime.now(timezone.utc)
print(json.dumps({'pool': 'opencode-go', 'quota': {}, 'quota_pools': {'codex': {
    'quota_state': 'HEALTHY', 'confidence': 'direct', 'source': 'offline-fixture',
    'observed_at': now.isoformat(), 'non_binding': [], 'windows': {'weekly': {
        'used_percent': used, 'reset_at': (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)).isoformat()
    }}}}}))
""")
        command = shlex.join([sys.executable, str(oracle), str(self.root / "counter")])
        self.cli("--pool-batches", "--usage-cmd", command, "--parallel", "2")
        rows = list(run_grid.read_results(self.out).values())
        self.assertTrue(all(row["pass"] for row in rows), rows)
        self.assertTrue(all((row["tokens_in"], row["tokens_out"]) == (100, 20) for row in rows))
        measured = cost.report(self.out)["options"][0]
        self.assertEqual(measured["percent_binding_window_per_task"], 1, measured)
        self.assertEqual(measured["measured_tasks"], 2)


if __name__ == "__main__":
    unittest.main()
