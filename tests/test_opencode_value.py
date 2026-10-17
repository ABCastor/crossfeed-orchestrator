"""Value-routing patch behavior against a synthetic overlay fixture."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch as mock_patch

from scripts import fleetctl, selector
from scripts import patch_opencode_go_roles as patcher


ROOT = Path(__file__).resolve().parents[1]


def value_fixture():
    """Start from the checked-in schema example and add the current shortlist lanes."""
    roster = json.loads((ROOT / "examples/access-overlay.example.json").read_text())
    template = copy.deepcopy(next(lane for lane in roster["lanes"]
                                  if lane.get("provider") == "opencode-go"
                                  and lane.get("model_key") == "deepseek-v4-flash"))
    for model in patcher.SHORTLIST:
        if any(lane.get("provider") == "opencode-go" and lane.get("model_key") == model
               for lane in roster["lanes"]):
            continue
        lane = copy.deepcopy(template)
        lane.update(lane_id=f"opencode-go-{model}", model_key=model,
                    selector=f"opencode-go/{model}")
        roster["lanes"].append(lane)
        roster.setdefault("effort", {})[model] = copy.deepcopy(roster["effort"]["deepseek-v4-flash"])
    return roster


class OpenCodeValuePatchTests(unittest.TestCase):
    def setUp(self):
        self.roster = value_fixture()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)

    def test_migration_is_idempotent_and_only_routes_shortlisted_go_models(self):
        before = copy.deepcopy(self.roster)
        migrated = patcher.migrate(before)
        self.assertEqual(migrated, patcher.migrate(migrated))
        self.assertEqual(before, self.roster)

        before_lanes = {lane["lane_id"]: lane for lane in before["lanes"]}
        after_lanes = {lane["lane_id"]: lane for lane in migrated["lanes"]}
        self.assertEqual(set(before_lanes), set(after_lanes))
        for lane_id, lane in before_lanes.items():
            after = after_lanes[lane_id]
            if lane.get("quota_pool") == patcher.POOL:
                expected = patcher.SHORTLIST.get(lane["model_key"], [])
                self.assertEqual(after["roles"], expected, lane_id)
                for key in lane.keys() - {"roles"}:
                    self.assertEqual(after.get(key), lane.get(key), (lane_id, key))
            else:
                self.assertEqual(after, lane, lane_id)

        go_ids = {lane["lane_id"] for lane in migrated["lanes"]
                  if lane.get("quota_pool") == patcher.POOL}
        for role, bands in migrated["routing"]["roles"].items():
            for band, entries in bands.items():
                automatic_go = [lane_id for lane_id in entries if lane_id in go_ids]
                allowed = [lane["lane_id"] for model in patcher.ROUTE_ORDER.get(role, [])
                           for lane in migrated["lanes"]
                           if lane.get("quota_pool") == patcher.POOL
                           and lane.get("model_key") == model]
                self.assertEqual(automatic_go, allowed, (role, band))
                original_non_go = [lane_id for lane_id in before["routing"]["roles"][role][band]
                                   if lane_id not in go_ids]
                result_non_go = [lane_id for lane_id in entries if lane_id not in go_ids]
                self.assertEqual(result_non_go, original_non_go, (role, band))

    def test_shortlist_routes_automatically_and_nonshortlist_stays_explicit(self):
        migrated = patcher.migrate(self.roster)
        selected = fleetctl.choose_lane(migrated, {}, "default", "read-only", "text",
                                        harness="opencode")
        self.assertIn(selected["model_key"], patcher.ROUTE_ORDER["default"])

        options, _ = selector.enumerate_options(migrated, {}, "review", mode="read-only",
                                                 fleet=fleetctl)
        self.assertFalse(any(row["harness"] == "opencode"
                             and row["model_key"] == "deepseek-v4-pro" for row in options))
        self.assertTrue(any(row["harness"] == "opencode"
                            and row["model_key"] == "deepseek-v4.1-flash" for row in options))

        overlay = self.tmp / "overlay.json"
        overlay.write_text(patcher.dump(migrated))
        env = dict(os.environ, ACCESS_OVERLAY=str(overlay),
                   FLEET_STATE_DIR=str(self.tmp / "fleet-state"), FLEET_NO_AUTO_REFRESH="1")
        resolved = subprocess.run(["bash", str(ROOT / "scripts/roster.sh"),
                                   "resolve-lane", "deepseek-v4-pro", "opencode"],
                                  env=env, text=True, capture_output=True)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(resolved.stdout.strip(), "opencode-go-deepseek-v4-pro")

    def test_dry_run_writes_patch_validates_candidate_and_preserves_source_bytes(self):
        overlay = self.tmp / "overlay.json"
        patch = self.tmp / "roles.patch"
        overlay.write_text(patcher.dump(self.roster))
        original = overlay.read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/patch_opencode_go_roles.py"),
                                 "--overlay", str(overlay), "--patch", str(patch)],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(overlay.read_bytes(), original)
        self.assertTrue(patch.is_file())
        self.assertIn("Candidate validated", result.stdout)
        self.assertIn("Dry run", result.stdout)
        self.assertIn("opencode-go-mimo-v2.6-flash", patch.read_text())

    def test_apply_on_temp_overlay_creates_backup_and_validates_result(self):
        overlay = self.tmp / "overlay.json"
        patch = self.tmp / "roles.patch"
        original = patcher.dump(self.roster).encode()
        overlay.write_bytes(original)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/patch_opencode_go_roles.py"),
                                 "--overlay", str(overlay), "--patch", str(patch), "--apply"],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        backup_line = next(line for line in result.stdout.splitlines() if line.startswith("Backup: "))
        backup = Path(backup_line.removeprefix("Backup: "))
        self.assertEqual(backup.read_bytes(), original)
        self.assertNotEqual(overlay.read_bytes(), original)
        self.assertEqual(json.loads(overlay.read_text()), patcher.migrate(self.roster))

        env = dict(os.environ, ACCESS_OVERLAY=str(overlay),
                   FLEET_STATE_DIR=str(self.tmp / "validate-state"), FLEET_NO_AUTO_REFRESH="1")
        checked = subprocess.run(["bash", str(ROOT / "scripts/roster.sh"), "validate"],
                                 env=env, text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_failed_post_apply_validation_restores_the_backup(self):
        overlay = self.tmp / "overlay.json"
        patch_path = self.tmp / "roles.patch"
        original = patcher.dump(self.roster).encode()
        overlay.write_bytes(original)
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with mock_patch.object(patcher, "validate_overlay",
                                   side_effect=[None, patcher.PatchError("synthetic post-write failure")]):
                result = patcher.main(["--overlay", str(overlay), "--patch", str(patch_path), "--apply"])
        self.assertEqual(result, 1)
        self.assertEqual(overlay.read_bytes(), original)
        backup_line = next(line for line in output.getvalue().splitlines() if line.startswith("Backup: "))
        self.assertEqual(Path(backup_line.removeprefix("Backup: ")).read_bytes(), original)

    def test_symlink_source_is_refused_without_changing_target(self):
        target = self.tmp / "overlay-target.json"
        link = self.tmp / "overlay-link.json"
        target.write_text(patcher.dump(self.roster))
        original = target.read_bytes()
        link.symlink_to(target)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/patch_opencode_go_roles.py"),
                                 "--overlay", str(link), "--patch", str(self.tmp / "roles.patch")],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing a symlink source", result.stderr)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(link.is_symlink())

    def test_patch_cannot_overwrite_its_source(self):
        overlay = self.tmp / "overlay.json"
        overlay.write_text(patcher.dump(self.roster))
        original = overlay.read_bytes()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/patch_opencode_go_roles.py"),
                                 "--overlay", str(overlay), "--patch", str(overlay)],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--patch must differ", result.stderr)
        self.assertEqual(overlay.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
