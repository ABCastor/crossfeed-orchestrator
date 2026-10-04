"""Break Pi's supervisor in a disposable mirror and require the guard to go red."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PiGuardSabotageTests(unittest.TestCase):
    def test_python_watchdog_and_group_escalation_are_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            mirror = Path(directory) / "scripts"
            mirror.mkdir()
            for source in (ROOT / "scripts").iterdir():
                if source.is_file():
                    (mirror / source.name).symlink_to(source)
            supervisor = mirror / "pi_runner.py"
            # Only replace a scratch symlink, never a project file.
            supervisor.unlink()
            original = (ROOT / "scripts" / "pi_runner.py").read_text()
            supervisor.write_text(original)
            env = {**os.environ, "ACCESS_OVERLAY": str(ROOT / "tests/fixtures/access-overlay.test.json"),
                   "FLEET_STATE_DIR": str(Path(directory) / "state"), "FLEET_NO_AUTO_REFRESH": "1"}

            def scan():
                return subprocess.run(["bash", str(ROOT / "scripts/check-dispatch-invariants.sh"),
                                       str(mirror), str(ROOT / "skill/SKILL.md")],
                                      capture_output=True, text=True, env=env, timeout=30)

            baseline = scan()
            self.assertEqual(baseline.returncode, 0, baseline.stdout + baseline.stderr)
            for old, new, message in (
                ("signal_group(child, signal.SIGKILL)", "signal_group(child, signal.SIGTERM)",
                 "Python supervisor must send TERM then KILL"),
                ("os.killpg(child.pid, sig)", "os.kill(child.pid, sig)",
                 "Python supervisor must send TERM then KILL"),
                ("args.idle and now - activity >= args.idle", "False",
                 "lost its running Python idle watchdog"),
            ):
                self.assertIn(old, original)
                supervisor.write_text(original.replace(old, new))
                broken = scan()
                self.assertNotEqual(broken.returncode, 0, broken.stdout)
                self.assertIn(message, broken.stdout)
                supervisor.write_text(original)
                restored = scan()
                self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
