"""The r14 live-overlay migration adds the text roles once and never writes its source."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import admit_chatgpt_roles as admission


class RoleMigrationTests(unittest.TestCase):
    def old_overlay(self):
        roster = json.loads((ROOT / "examples/access-overlay.example.json").read_text())
        roster["chatgpt_gateway"]["lane_template"]["roles"] = ["default", "lookup", "hard-reasoning", "review", "long-context"]
        roster["routing"]["roles"]["writing"] = {"quality_first": ["antigravity-gemini-4-argon-high"], "conserve": [], "critical": ["x"]}
        return roster

    def test_adds_roles_and_leads_writing_with_the_chatgpt_slot_once(self):
        old = self.old_overlay()
        new = admission.migrate(old)
        roles = new["chatgpt_gateway"]["lane_template"]["roles"]
        self.assertTrue({"research", "judgment", "writing", "audit"} <= set(roles))
        self.assertEqual(new["routing"]["roles"]["writing"]["quality_first"][0], "chatgpt")
        self.assertEqual(admission.migrate(new), new)
        self.assertEqual(old, self.old_overlay())

    def test_cli_prints_a_diff_and_refuses_to_overwrite_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "overlay.json"
            source.write_text(json.dumps(self.old_overlay(), indent=2) + "\n")
            before = source.read_text()
            out = Path(directory) / "migrated.json"
            run = subprocess.run([sys.executable, str(ROOT / "scripts/admit_chatgpt_roles.py"), str(source), "--out", str(out)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('+        "research",', run.stdout)
            self.assertEqual(source.read_text(), before)
            self.assertIn("research", json.loads(out.read_text())["chatgpt_gateway"]["lane_template"]["roles"])
            refused = subprocess.run([sys.executable, str(ROOT / "scripts/admit_chatgpt_roles.py"), str(source), "--out", str(source)],
                                     capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(source.read_text(), before)


if __name__ == "__main__":
    unittest.main()
