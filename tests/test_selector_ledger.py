"""Run selector commands against fake vendor CLIs and verify actual ledger replay."""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_selector import quality_row
from tests.test_switch_respected import Sandbox, FAKE_CODEX

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fleetctl
import run_identity
import selector


class StandaloneReceiptTests(unittest.TestCase):
    def test_receipt_helper_runs_without_evidence_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "run_identity.py"
            shutil.copyfile(Path(run_identity.__file__), helper)
            receipt = root / "receipt.json"
            env = dict(os.environ, ACCESS_OVERLAY=str(root / "absent-overlay.json"),
                       FLEET_STATE_DIR=str(root / "state"))
            env.pop("FLEET_SELECTION_FILE", None)
            result = subprocess.run(
                [sys.executable, str(helper), "begin", "--path", str(receipt),
                 "--wrapper", "codex", "--requested", "synthetic", "--selected", "synthetic",
                 "--role", "review", "--effort", "low"],
                env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(receipt.read_text())
            self.assertEqual((record["role"], record["family"], record["effort"]),
                             ("review", "review", "low"))


class SelectorLedgerTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.env["FLEET_QUOTA_POLICY"] = "clock_aware"
        self.env["FLEET_IGNORE_QUOTA"] = "0"
        self.env.pop("FLEET_SELECTION_FILE", None)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"FLEET_QUOTA_POLICY": "clock_aware", "FLEET_IGNORE_QUOTA": "0"}).start()
        mock.patch.object(fleetctl, "_POLICY_CACHE", ("clock_aware", "test")).start()

    def selection(self, pool, harness=None, mode=None):
        self.roster["policy"]["selector"] = {"allowed_pools_by_role": {"default": [pool]}}
        self.write_roster(self.roster)
        options, _ = selector.enumerate_options(self.roster, {}, "review", mode=mode, fleet=fleetctl)
        option = next(o for o in options if harness is None or o["harness"] == harness)
        directory = self.state / "evidence"
        directory.mkdir(exist_ok=True)
        (directory / "levels.json").write_text(json.dumps({"rows": [quality_row(option["model_key"], option["level"], mean=.99)]}))
        return selector.select_option(self.roster, {}, self.state, "review", mode=mode, fleet=fleetctl)

    def records(self):
        return [json.loads(line) for line in (self.state / "runs.jsonl").read_text().splitlines()]

    def execute(self, selection, extra=()):
        argv = shlex.split(selection["choice"]["command"])
        argv[argv.index("--prompt") + 1] = "synthetic task"
        argv += ["--dir", str(self.root), "--idle-timeout", "0", *extra]
        return subprocess.run(argv, env=self.env, cwd=self.root, capture_output=True, text=True, timeout=30)

    def assert_replay(self, record, selection):
        choice = selection["choice"]
        self.assertEqual(record["effort"], choice["effort"])
        self.assertEqual(record["role"], selection["role"])
        self.assertEqual(record["family"], selection["family"])
        replay = record["selection"]
        self.assertIsNotNone(replay)
        self.assertEqual(replay["choice"]["model_key"], choice["model_key"])
        self.assertEqual(replay["choice"]["effort"], choice["effort"])
        self.assertEqual(replay["choice"]["score"], choice["score"])
        self.assertEqual(replay["lambdas"], selection["lambdas"])
        self.assertEqual(replay["selection_probability"], selection["selection_probability"])
        self.assertEqual(replay["choice"]["selection_probability"], choice["selection_probability"])
        self.assertEqual([(o["model_key"], o["level"], o["score"]) for o in replay["top3"]],
                         [(o["model_key"], o["level"], o["score"]) for o in selection["top3"]])
        self.assertNotIn("command", replay["choice"])

    def test_generated_codex_command_executes_and_records_effort_and_replay(self):
        args = self.fake_cli("codex", FAKE_CODEX)
        selection = self.selection("codex", "codex")
        result = self.execute(selection)
        self.assertEqual(result.returncode, 0, result.stderr)
        native = args.read_text().splitlines()
        self.assertIn('model_reasoning_effort="' + selection["choice"]["level"] + '"', native)
        self.assert_replay(self.records()[-1], selection)

    def test_generated_claude_command_executes_supported_effort_flag(self):
        args = self.fake_cli("claude", "echo '{\"type\":\"result\",\"is_error\":false,\"result\":\"answer\"}'\n")
        selection = self.selection("claude", "claude")
        result = self.execute(selection)
        self.assertEqual(result.returncode, 0, result.stderr)
        native = args.read_text().splitlines()
        self.assertEqual(native[native.index("--effort") + 1], selection["choice"]["level"])
        self.assert_replay(self.records()[-1], selection)

    def test_generated_agy_command_executes_exact_level_and_quota_lane(self):
        args = self.fake_cli("agy", "echo answer\n")
        selection = self.selection("antigravity-gemini", "agy", mode="write")
        result = self.execute(selection)
        self.assertEqual(result.returncode, 0, result.stderr)
        native = args.read_text().splitlines()
        self.assertIn(selection["choice"]["run_as"], native)
        self.assertEqual(native[native.index("--effort") + 1], selection["choice"]["level"])
        self.assert_replay(self.records()[-1], selection)

    def test_generated_copilot_command_keeps_auto_and_service_chosen(self):
        self.fake_cli("copilot", "echo '{\"type\":\"assistant.message\",\"data\":{\"content\":\"answer\"}}'\necho '{\"type\":\"result\",\"exitCode\":0}'\n")
        selection = self.selection("github-copilot-student", "copilot")
        self.assertNotIn("--effort", selection["choice"]["command_argv"])
        result = self.execute(selection)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(selection["choice"]["effort"], "service-chosen")
        self.assert_replay(self.records()[-1], selection)

    def test_generated_opencode_command_records_identity_and_priced_effort(self):
        args = self.fake_cli("opencode", "echo '{\"type\":\"text\",\"part\":{\"text\":\"answer\"}}'\necho '{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}'\n")
        auth = self.home / ".local/share/opencode/auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text('{"opencode-go":{"type":"api","key":"synthetic-test-only"}}')
        self.env["AGENT_SYNC_VERIFY"] = "/usr/bin/true"
        selection = self.selection("opencode-go", "opencode")
        self.assertNotIn("--role", selection["choice"]["command_argv"])
        result = self.execute(selection, extra=("--direct",))
        self.assertEqual(result.returncode, 0, result.stderr)
        native = args.read_text().splitlines()
        if selection["choice"]["level"] != "provider-default":
            self.assertEqual(native[native.index("--variant") + 1], selection["choice"]["level"])
        records = self.records()
        identity = next(r for r in records if r["schema"] == "crossfeed-model-run/v1")
        priced = next(r for r in records if r["schema"] == fleetctl.RUN_SCHEMA)
        self.assertEqual(identity["run_id"], priced["run_id"])
        self.assert_replay(identity, selection)
        self.assert_replay(priced, selection)

    def test_stale_selector_receipt_is_stripped_but_actual_wrapper_identity_survives(self):
        self.fake_cli("codex", FAKE_CODEX)
        selection = self.selection("codex", "codex")
        chosen = selection["choice"]["model_key"]
        alternative = next(key for key in fleetctl.choosable_models(self.roster, "codex")
                           if key != chosen and not fleetctl.model_is_older(self.roster, "codex", key))
        changed = copy.deepcopy(selection)
        argv = changed["choice"]["command_argv"]
        argv[argv.index("--model") + 1] = alternative
        changed["choice"]["command"] = shlex.join(argv)
        result = self.execute(changed)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual(record["selected_model"], alternative)
        self.assertIsNone(record["selection"])
        self.assertIn("selection_error", record)
        self.assertEqual(record["effort"], selection["choice"]["effort"])

    def test_regular_and_linked_afk_ledgers_preserve_selector_replay(self):
        selection = self.selection("opencode-go", "opencode")
        choice = selection["choice"]
        events = self.root / "events.jsonl"
        events.write_text(json.dumps({"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 20, "output": 10}, "cost": .01}}) + "\n")
        with mock.patch.dict(os.environ, {"AFK_ATTEMPT_ID": "synthetic-attempt"}):
            regular = fleetctl.record_run(self.state, self.roster, choice["lane_id"], events, None, 0, fleetctl.iso(),
                                         effort=choice["effort"], selection=Path(selection["selection_file"]), role="review", family="review")
        self.assert_replay(regular, selection)
        attempt = fleetctl.record_afk_attempt(self.state, self.roster, choice["lane_id"], "synthetic-attempt", fleetctl.iso(),
                                             1000, "verified", None, "synthetic-proof-hash", 0, 0)
        self.assert_replay(attempt, selection)
        self.assertEqual(attempt["proof"]["returncode"], 0)
        self.assertNotIn("outcome", regular)
        self.assertEqual(attempt["selection"], regular["selection"])

    def test_selection_binding_rejects_changed_model_lane_effort_role_or_family(self):
        selection = self.selection("opencode-go", "opencode")
        choice = selection["choice"]
        actual = {"model": choice["model_key"], "lane_id": choice["lane_id"], "effort": choice["effort"],
                  "pool": choice["pool"], "harness": choice["harness"], "role": "review", "family": "review"}
        good = run_identity.selection_metadata(Path(selection["selection_file"]), **actual)
        self.assert_replay(good, selection)
        for field, changed in (("model", "another-model"), ("lane_id", "another-lane"), ("effort", "unsupported"),
                               ("pool", "another-pool"), ("harness", "agy"), ("role", "lookup"), ("family", "repo-qa")):
            with self.subTest(field=field):
                result = run_identity.selection_metadata(Path(selection["selection_file"]), **dict(actual, **{field: changed}))
                self.assertIsNone(result["selection"])
                self.assertIn("selection_error", result)
        self.assertTrue(all(value == .5 for value in good["selection"]["lambdas"].values()))

    def test_afk_explicit_role_override_derives_its_new_family(self):
        selection = self.selection("opencode-go", "opencode")
        choice = selection["choice"]
        with mock.patch.dict(os.environ, {"AFK_ATTEMPT_ID": "changed-role"}):
            fleetctl.record_run(self.state, self.roster, choice["lane_id"], None, None, 0, fleetctl.iso(),
                                effort=choice["effort"], selection=Path(selection["selection_file"]), role="review", family="review")
        attempt = fleetctl.record_afk_attempt(self.state, self.roster, choice["lane_id"], "changed-role", fleetctl.iso(),
                                             1000, "verified", None, "proof-hash", 0, 0, role="lookup")
        self.assertEqual(attempt["role"], "lookup")
        self.assertEqual(attempt["family"], "repo-qa")
        self.assertIsNone(attempt["selection"])


if __name__ == "__main__":
    unittest.main()
