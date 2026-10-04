"""Prove both cf5 guards fail when their production mechanism is deliberately removed."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SabotageTests(unittest.TestCase):
    def prove(self, filename, original, broken, test):
        with tempfile.TemporaryDirectory(prefix="crossfeed-sabotage-") as folder:
            root = Path(folder)
            # The installed copy has no skill/ folder (its manual is SKILL.md at the top): copy what exists.
            for name in ("scripts", "tests", "examples", "skill"):
                if (ROOT / name).exists():
                    shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "runs"))
            command = ["python3", "-m", "unittest", test]
            healthy = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(healthy.returncode, 0, healthy.stderr)
            path = root / filename
            text = path.read_text()
            self.assertIn(original, text)
            path.write_text(text.replace(original, broken, 1))
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("FAIL:", result.stderr)
            self.assertNotIn("ERROR:", result.stderr)
            print(f"EXPECTED FAILURE: {filename}: {test.rsplit('.', 1)[-1]}")

    def test_provider_identity_loss_is_detected(self):
        self.prove("scripts/run_identity.py",
                   'record["actual_model"] = observed_model(record["harness"], events, database)',
                   'record["actual_model"] = None',
                   "tests.test_run_identity.IdentityWrapperTests.test_codex_fallback_reaches_every_side_with_native_thread_identity")

    def test_older_default_on_is_detected(self):
        self.prove("scripts/fleetctl.py",
                   '            and (not model_is_older(roster, pool, model) or bool(older_model_reason(roster, pool, model)))\n',
                   '',
                   "tests.test_older_model_rule.OlderRuleTests.test_no_reason_is_off_by_default_even_with_legacy_all_on_state")


if __name__ == "__main__":
    unittest.main()
