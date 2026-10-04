"""Pi campaigns use the same admission and outcome boundary as other workers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PiDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        for name in ("fanout.sh", "outcome-taxonomy.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.scripts / name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.dispatch_count = 0
        self.overlay = self.root / "overlay.json"
        self.lane = {"lane_id": "pi-test", "harness": "pi", "model_key": "test-model",
                     "selector": "google/test-model", "quota_pool": "gemini-metered",
                     "access_status": "verified", "admission_status": "active",
                     "allowed_modes": ["read-only", "write"],
                     "capabilities": {"input": ["text"]}, "timeout_s": 1800,
                     "max_tasks_per_run": 1, "max_parallel": 1}
        self.save_overlay()
        self.env = {**os.environ, "ACCESS_OVERLAY": str(self.overlay),
                    "FLEET_STATE_DIR": str(self.root / "state"), "FLEET_NO_AUTO_REFRESH": "1"}

    def save_overlay(self):
        self.overlay.write_text(json.dumps({"lanes": [self.lane]}))

    def dispatch(self, changes=None, dry=True, extra_tasks=None):
        task = {"id": "test", "agent": "pi", "lane_id": "pi-test", "prompt": "inspect",
                "dir": str(self.work), "mode": "read-only", "effort": "high"}
        task.update(changes or {})
        tasks = self.root / "tasks.jsonl"
        tasks.write_text("".join(json.dumps(t) + "\n" for t in [task, *(extra_tasks or [])]))
        self.dispatch_count += 1
        self.out = self.root / f"out-{self.dispatch_count}"
        args = ["bash", str(self.scripts / "fanout.sh"), str(tasks), "--out", str(self.out)]
        if dry:
            args.append("--dry-run")
        return subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=30)

    def test_explicit_lane_accepted_without_hidden_wall_cap(self):
        run = self.dispatch()
        self.assertEqual(run.returncode, 0, run.stderr)
        manifest = json.loads(run.stdout)
        self.assertEqual((manifest["model"], manifest["quota_pool"], manifest["timeout"]),
                         ("google/test-model", "gemini-metered", 0))

    def test_selector_and_key_are_supported_but_conflicts_refuse(self):
        for changes in ({"lane_id": "", "model_key": "test-model"},
                        {"lane_id": "", "model": "google/test-model"}):
            run = self.dispatch(changes)
            self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.dispatch({"model": "google/other"}).returncode, 4)

    def test_inactive_lane_and_unsupported_input_refuse_before_dispatch(self):
        self.lane["admission_status"] = "candidate"
        self.save_overlay()
        self.assertEqual(self.dispatch().returncode, 4)
        self.lane["admission_status"] = "active"
        self.save_overlay()
        self.assertEqual(self.dispatch({"modality": "image"}).returncode, 4)
        self.lane["allowed_modes"] = ["write"]
        self.save_overlay()
        self.assertEqual(self.dispatch().returncode, 4)

    def test_lane_task_cap_applies(self):
        extra = {"id": "second", "agent": "pi", "lane_id": "pi-test", "prompt": "inspect",
                 "dir": str(self.work)}
        self.assertEqual(self.dispatch(extra_tasks=[extra]).returncode, 4)

    def test_dispatch_passes_effort_mode_and_events_to_pi_wrapper(self):
        wrapper = self.scripts / "pi-agent.sh"
        wrapper.write_text("""#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
pathlib.Path(__file__).with_name('received.json').write_text(json.dumps(args))
pathlib.Path(args[args.index('--last') + 1]).write_text('final text')
""")
        wrapper.chmod(0o755)
        run = self.dispatch(dry=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        args = json.loads((self.scripts / "received.json").read_text())
        self.assertIn("--read-only", args)
        self.assertEqual(args[args.index("--lane") + 1], "pi-test")
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertTrue(args[args.index("--events") + 1].endswith("test.events.jsonl"))
        self.assertNotIn("--timeout", args)
        self.assertIn("SUCCEEDED", (self.out / "summary.tsv").read_text())

    def test_native_failures_do_not_suppress_an_entire_pool(self):
        expected = {3: "lane-or-modality-not-admitted", 4: "lease-refused",
                    5: "provider-or-terms-error", 6: "no-terminal-event", 7: "empty-final-output"}
        for code, reason in expected.items():
            command = 'source "$1"; classify_wrapper_outcome pi "$2" missing missing; printf "%s|%s" "$WRAPPER_OUTCOME_REASON" "$WRAPPER_OUTCOME_SUPPRESS_POOL"'
            run = subprocess.run(["bash", "-c", command, "test", str(self.scripts / "outcome-taxonomy.sh"), str(code)],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(run.stdout, reason + "|0")
