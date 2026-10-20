import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from opencode_v2 import worker_config
from migrate_opencode_v2 import migrate
import fleetctl


class OpenCodeV2Tests(unittest.TestCase):
    def test_doctor_checks_v2_json_model_disables_in_both_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roster = {"lanes": [{"lane_id": "go", "selector": "opencode-go/admitted",
                "provider": "opencode-go", "harness": "opencode", "access_status": "verified",
                "admission_status": "active", "allowed_modes": ["read-only"]}]}
            primary, worker = root / "opencode.jsonc", root / "worker/opencode.jsonc"
            worker.parent.mkdir()
            env = {"FLEET_OPENCODE_CONFIG": str(primary), "FLEET_OPENCODE_WORKER_CONFIG": str(worker)}
            with patch.dict(os.environ, env):
                for source in (primary, worker):
                    path = source.with_suffix(".json")
                    path.write_text(json.dumps({"providers": {"opencode-go": {"models": {"admitted": {"disabled": True}}}}}))
                    self.assertEqual(len(fleetctl.opencode_whitelist_problems(roster)), 1)
                    # Explicit .json overrides must not inspect that same file twice.
                    key = "FLEET_OPENCODE_CONFIG" if source == primary else "FLEET_OPENCODE_WORKER_CONFIG"
                    with patch.dict(os.environ, {key: str(path)}):
                        self.assertEqual(len(fleetctl.opencode_whitelist_problems(roster)), 1)
                    path.write_text(json.dumps({"providers": {"opencode-go": {"models": {"admitted": {"disabled": False}}}}}))
                    self.assertEqual(fleetctl.opencode_whitelist_problems(roster), [])

    # BR-OPENCODE-WORKER-001
    def test_no_external_plugins_skills_or_mcp_in_lean_and_direct(self):
        self.assertEqual(worker_config(search=True)["agents"]["fleet-research"]["steps"], 6)
        for config in (worker_config(), worker_config(direct=True), worker_config(search=True)):
            self.assertEqual(config["mcp"], {"servers": {}})
            for integration in ("plugin:*", "mcp:*", "skill:*"):
                self.assertIn({"action": "integration.use", "resource": integration, "effect": "deny"}, config["policies"])
            self.assertIn("-opencode.config.skill", config["plugins"])
            self.assertFalse(config["warming"])

    def test_profile_imports_only_named_instructions_and_shared_skill_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source, work = root / "source", root / "work"
            source.mkdir(); work.mkdir()
            (source / "AGENTS.md").write_text("Synchronized contract")
            (source / "skills").mkdir()
            (source / "opencode.jsonc").write_text('{"plugins":["hostile"],"mcp":{"servers":{"hostile":{}}},"agents":{"plan":{"permissions":[{"action":"*","resource":"*","effect":"allow"}]}}}')
            (work / "AGENTS.md").write_text("Project contract")
            destination = root / "profile.json"
            for context in ("lean", "shared"):
                result = subprocess.run([sys.executable, str(ROOT / "scripts/opencode_v2.py"),
                    str(destination), "--source", str(source), "--directory", str(work),
                    "--context", context], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                profile = json.loads(destination.read_text())
                self.assertEqual(set(profile["instructions"]), {str(source / "AGENTS.md"), str(work / "AGENTS.md")})
                self.assertEqual(profile.get("skills", []), [str(source / "skills")] if context == "shared" else [])
                self.assertNotIn("hostile", destination.read_text())
                self.assertEqual(profile["permissions"][0]["effect"], "deny")

    def test_usage_with_v2_missing_total_and_optional_step_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events"
            events.write_text((ROOT / "tests/fixtures/opencode-v2-run-ok-events.jsonl").read_text())
            parsed = fleetctl.parse_events(events)
            self.assertIsNotNone(parsed["session_id"])
            self.assertIsNone(parsed["finish"])
            self.assertTrue(parsed["completed"])
            self.assertFalse(parsed["usage_reported"])
            receipt = fleetctl.record_run(Path(tmp) / "state", {"lanes": [{"lane_id": "go",
                "provider": "opencode-go", "model_key": "mimo-v2.6-flash"}]}, "go", events, None, 0, None)
            self.assertEqual(receipt["status"], "ok")
            self.assertFalse(receipt["usage_reported"])
            with events.open("w") as stream:
                for event in ({"type": "step_start"}, {"type": "step_finish", "part": {"reason": "tool-calls"}},
                              {"type": "step_start"}, {"type": "text", "part": {"text": "final"}}):
                    stream.write(json.dumps(event) + "\n")
            self.assertTrue(fleetctl.parse_events(events)["completed"])
            self.assertEqual(parsed["tokens"]["total"], 0)
            usage = {"input": 10, "output": 3, "reasoning": 4, "cache": {"read": 5, "write": 6}}
            for kind in ("step_finish", "session.step.ended"):
                events.write_text(json.dumps({"type": kind, "part" if kind == "step_finish" else "data":
                    {"tokens": usage, "cost": 0.001, "reason" if kind == "step_finish" else "finish": "stop"}}) + "\n")
                parsed = fleetctl.parse_events(events)
                self.assertEqual(parsed["tokens"], {"input": 10, "output": 3, "reasoning": 4, "cache_read": 5, "cache_write": 6, "total": 28})
                self.assertEqual(parsed["estimated_cost_usd"], 0.001)
                self.assertEqual(parsed["finish"], "stop")
                self.assertTrue(parsed["usage_reported"])

    def test_picker_migration_preserves_source_and_is_idempotent(self):
        source = {"provider": {"opencode-go": {"whitelist": ["old"], "models": {"custom": {"name": "Custom"}}}}, "share": "manual"}
        original = copy.deepcopy(source)
        result = migrate(source, {"admitted"}, {"old": {}, "admitted": {}, "new": {}})
        self.assertEqual(source, original)
        self.assertNotIn("whitelist", result["provider"]["opencode-go"])
        self.assertEqual(result["providers"]["opencode-go"]["models"], {"admitted": {"disabled": False}, "old": {"disabled": True}, "new": {"disabled": True}, "custom": {"disabled": True}})
        self.assertEqual(migrate(result, {"admitted"}, {"old": {}, "admitted": {}, "new": {}}), result)
        self.assertEqual(result["share"], "manual")

    def test_migration_cli_writes_separate_candidates_for_both_config_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            overlay = root / "overlay.json"
            overlay.write_text(json.dumps({"lanes": [{"lane_id": "test-go", "allowed_modes": ["read-only"], "selector": "opencode-go/admitted", "harness": "opencode", "provider": "opencode-go", "access_status": "verified", "admission_status": "active"}]}))
            catalog = root / "catalog.json"
            catalog.write_text(json.dumps({"opencode-go": {"models": {"admitted": {}, "other": {}}}}))
            configs = [root / "opencode.json", root / "fleet-worker/opencode.jsonc"]
            configs[1].parent.mkdir()
            for path in configs:
                path.write_text('{"provider":{"opencode-go":{"whitelist":["admitted"]}}}')
            before = [p.read_bytes() for p in configs]
            command = [sys.executable, str(ROOT / "scripts/migrate_opencode_v2.py"), "--overlay", str(overlay), "--catalog", str(catalog), "--out", str(root / "candidates")]
            for path in configs:
                command += ["--config", str(path)]
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual([p.read_bytes() for p in configs], before)
            candidates = list((root / "candidates").iterdir())
            self.assertEqual(len(candidates), 2)
            for path in candidates:
                self.assertEqual(json.loads(path.read_text())["providers"]["opencode-go"]["models"]["other"]["disabled"], True)


if __name__ == "__main__":
    unittest.main()
