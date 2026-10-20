"""Exercise real launch paths with a blocking gate and local wrappers only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ResourceGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        for name in ("fanout.sh", "swarm.sh", "outcome-taxonomy.sh"):
            shutil.copy2(ROOT / "scripts" / name, self.scripts / name)
        self.overlay = self.root / "overlay.json"
        self.overlay.write_text('{"lanes": []}')
        self.gate = self.root / "gate.py"
        self.gate.write_text('''import os, time
from pathlib import Path
root = Path(os.environ['GATE_TEST_ROOT'])
with (root / 'calls').open('a') as f:
    f.write('gate\\n')
while not (root / 'release').exists():
    time.sleep(0.02)
''')
        self.gate.chmod(0o755)
        # Absolute interpreter avoids relying on the machine's model CLI/config.
        wrapper = '''#!/usr/bin/python3
import os, sys
from pathlib import Path
with (Path(os.environ['GATE_TEST_ROOT']) / 'launched').open('a') as f:
    f.write('worker\\n')
if '--last' in sys.argv:
    Path(sys.argv[sys.argv.index('--last') + 1]).write_text('stub deliverable')
print('stub deliverable')
'''
        for name in ("codex-agent.sh", "gemini-media.sh"):
            path = self.scripts / name
            path.write_text(wrapper)
            path.chmod(0o755)
        self.env = {**os.environ, "ACCESS_OVERLAY": str(self.overlay),
                    "FLEET_RESOURCE_GATE": str(self.gate), "RESOURCE_GATE": "on",
                    "GATE_TEST_ROOT": str(self.root),
                    "FLEET_STATE_DIR": str(self.root / "state")}
        self.out = self.root / "out"

    def fanout_command(self):
        tasks = self.root / "tasks.jsonl"
        tasks.write_text(''.join(json.dumps({"id": f"task{i}", "agent": "codex",
                                           "prompt": "stub only", "dir": str(self.root)}) + '\n'
                                 for i in range(3)))
        return ["bash", str(self.scripts / "fanout.sh"), str(tasks), "--out", str(self.out)]

    def media_command(self):
        media = self.root / "audio.wav"
        media.touch()
        return ["bash", str(self.scripts / "swarm.sh"), "media-review", "--prompt", "stub only",
                "--file", str(media), "--modality", "audio", "--out", str(self.out)]

    def assert_blocked_then_released(self, command, count):
        process = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "calls").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail('dispatcher did not enter gate')
                time.sleep(0.02)
            time.sleep(0.15)
            self.assertIsNone(process.poll())
            self.assertFalse((self.root / "launched").exists(), 'worker launched while gate blocked')
            self.assertEqual((self.root / "calls").read_text().splitlines(), ['gate'])
            self.assertEqual((self.out / "dispatch.log").read_text().count('checking for room'), 1)
            (self.root / "release").touch()
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(len((self.root / "calls").read_text().splitlines()), count)
            self.assertEqual(len((self.root / "launched").read_text().splitlines()), count)
            self.assertEqual((self.out / "dispatch.log").read_text().count('checking for room'), count)
            return stdout, stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_fanout_blocks_all_three_workers_then_completes(self):
        _, stderr = self.assert_blocked_then_released(self.fanout_command(), 3)
        self.assertIn('fixed batches up to 8', stderr)
        rows = (self.out / "summary.tsv").read_text().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.startswith('SUCCEEDED\t') for row in rows), rows)

    def test_swarm_direct_media_waits(self):
        self.assert_blocked_then_released(self.media_command(), 1)

    def test_absent_nonexecutable_and_disabled_gate_preserve_dispatch(self):
        for mode in ('absent', 'nonexecutable', 'off'):
            with self.subTest(mode=mode):
                self.out = self.root / mode
                env = dict(self.env)
                if mode == 'absent':
                    env['FLEET_RESOURCE_GATE'] = str(self.root / 'missing')
                elif mode == 'nonexecutable':
                    self.gate.chmod(0o644)
                else:
                    self.gate.chmod(0o755)
                    env['RESOURCE_GATE'] = 'off'
                result = subprocess.run(self.fanout_command(), env=env, text=True,
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len((self.out / 'summary.tsv').read_text().splitlines()), 3)
                self.assertNotIn('checking for room', result.stderr)
                self.assertFalse((self.root / 'calls').exists())

    def test_dry_run_keeps_preflight_only_contract(self):
        result = subprocess.run([*self.fanout_command(), '--dry-run'], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 3)
        self.assertFalse((self.root / 'calls').exists())
        self.assertFalse((self.root / 'launched').exists())


if __name__ == '__main__':
    unittest.main()
