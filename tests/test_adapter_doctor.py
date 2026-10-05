"""Adapter drift guards use synthetic CLI output, configs and receipt ledgers."""
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_fleetctl import fleetctl, lane


class AdapterDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.primary = self.root / "primary.jsonc"
        self.worker = self.root / "worker.jsonc"
        self.now = dt.datetime(2026, 10, 2, 12, tzinfo=dt.timezone.utc)
        self.roster = {
            "schema_version": 3,
            "lanes": [lane("k3", "kimi-k3")],
            "quota_pools": {},
            "model_cards": {"claude-opus-5-5": {
                "status": "current", "pool": "claude", "aliases": ["opus"],
                "min_cli": {"claude": "2.1.280"}}},
        }
        self.env = patch.dict(os.environ, {
            "FLEET_OPENCODE_CONFIG": str(self.primary),
            "FLEET_OPENCODE_WORKER_CONFIG": str(self.worker),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.which = patch.object(fleetctl.shutil, "which", side_effect=lambda command: "/synthetic/bin/" + command)
        self.which.start()
        self.addCleanup(self.which.stop)

    def version(self, output, status=0, stderr=""):
        return patch.object(fleetctl.subprocess, "run", return_value=
                            subprocess.CompletedProcess([], status, output, stderr))

    def whitelist(self, path, models):
        path.write_text(json.dumps({"provider": {"opencode-go": {"whitelist": models}}}))

    def receipt(self, **overrides):
        record = {"schema": "crossfeed-model-run/v1", "run_id": "synthetic-run",
                  "started_at": fleetctl.iso(self.now - dt.timedelta(hours=1)),
                  "actual_model": "claude-opus-5-5", "selected_model": "claude-opus-5-5"}
        record.update(overrides)
        return record

    def ledger(self, records):
        (self.root / "runs.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))

    def doctor(self):
        overlay = self.root / "overlay.json"
        overlay.write_text(json.dumps(self.roster))
        out = io.StringIO()
        with patch.object(fleetctl, "effort_problems", return_value=[]), \
                patch.object(fleetctl, "utc_now", return_value=self.now), contextlib.redirect_stdout(out):
            result = fleetctl.doctor_command(overlay, self.root)
        return result, out.getvalue()

    def test_missing_harness_is_a_failing_diagnostic(self):
        self.whitelist(self.primary, ["kimi-k3"])
        self.whitelist(self.worker, ["kimi-k3"])
        with self.version("2.1.280"), patch.object(fleetctl.shutil, "which", return_value=None):
            result, output = self.doctor()
        self.assertEqual(result, 1)
        self.assertIn("MISSING", output)

    def test_cli_minimum_sabotage_and_restore_through_doctor(self):
        for version, status in [("2.1.280", 0), ("2.1.221", 1), ("2.1.280", 0)]:
            with self.subTest(version=version), self.version(version):
                result, output = self.doctor()
                self.assertEqual(result, status, output)
                self.assertEqual("FAIL" in output, bool(status), output)
                if status:
                    self.assertIn("claude-opus-5-5 requires claude >= 2.1.280", output)

    def test_version_ordering_and_release_edges(self):
        for output, failed in [("Claude Code 2.1.280", False), ("v2.10.1", False),
                               ("2.1.280+build.12", False), ("2.1.280-beta.2", True),
                               ("2.1.281-beta.1", False), ("2.1", True), ("2.1.9", True)]:
            with self.subTest(output=output), self.version(output):
                self.assertEqual(bool(fleetctl.cli_version_problems(self.roster)), failed)
        self.roster["model_cards"]["claude-opus-5-5"]["min_cli"]["claude"] = ">=2.1.280-beta.2"
        for output, failed in [("2.1.280-beta.11", False), ("2.1.280-beta.1", True),
                               ("2.1.280", False), ("2.1.280-alpha", True)]:
            with self.subTest(output=output), self.version(output):
                self.assertEqual(bool(fleetctl.cli_version_problems(self.roster)), failed)

    def test_version_probes_each_required_harness_once(self):
        self.roster["model_cards"]["claude-sonnet-5-5"] = {
            "status": "current", "min_cli": {"claude": "2.1.200", "codex": "2.2.0"}}
        with self.version("2.1.280") as run:
            issues = fleetctl.cli_version_problems(self.roster)
        self.assertEqual(run.call_count, 2)
        self.assertEqual({call.args[0][-1] for call in run.call_args_list}, {"--version"})
        self.assertTrue(all(call.kwargs["timeout"] == 5 for call in run.call_args_list))
        self.assertEqual(len(issues), 1)
        self.assertIn("codex", issues[0])

    def test_no_requirements_or_older_cards_do_not_probe(self):
        self.roster["model_cards"]["claude-opus-5-5"]["status"] = "older"
        with self.version("bad") as run:
            self.assertEqual(fleetctl.cli_version_problems(self.roster), [])
            run.assert_not_called()
        del self.roster["model_cards"]["claude-opus-5-5"]["status"]
        del self.roster["model_cards"]["claude-opus-5-5"]["min_cli"]
        with self.version("bad") as run:
            self.assertEqual(fleetctl.cli_version_problems(self.roster), [])
            run.assert_not_called()

    def test_malformed_minimums_fail_without_running(self):
        for minimum in ["2.1.280", None, [], {"claude": None}, {"claude": "2"},
                        {"claude": "2.1.280..3"}, {"claude": "2.1.280-a..b"},
                        {"claude --print": "2.1.280"}]:
            with self.subTest(minimum=minimum), self.version("2.1.280") as run:
                self.roster["model_cards"]["claude-opus-5-5"]["min_cli"] = minimum
                self.assertTrue(fleetctl.cli_version_problems(self.roster))
                run.assert_not_called()

    def test_version_unavailable_missing_and_probe_errors_fail(self):
        for output in ["unknown", "2.1.280.9", "2.1.280_thing", "2.1.280-a..b"]:
            with self.subTest(output=output), self.version(output):
                self.assertIn("unparseable", fleetctl.cli_version_problems(self.roster)[0])
        with self.version("2.1.280", status=1):
            self.assertIn("unavailable", fleetctl.cli_version_problems(self.roster)[0])
        with self.version("", stderr="claude 2.1.280"):
            self.assertEqual(fleetctl.cli_version_problems(self.roster), [])
        with patch.object(fleetctl.shutil, "which", return_value=None):
            self.assertIn("missing", fleetctl.cli_version_problems(self.roster)[0])
        for error in [OSError(), subprocess.TimeoutExpired("synthetic", 5)]:
            with patch.object(fleetctl.subprocess, "run", side_effect=error):
                self.assertIn("probe failed", fleetctl.cli_version_problems(self.roster)[0])

    def test_whitelist_sabotage_and_restore_checks_both_configs(self):
        self.whitelist(self.primary, ["kimi-k3"])
        self.whitelist(self.worker, ["kimi-k3"])
        with self.version("2.1.280"):
            self.assertEqual(self.doctor()[0], 0)
            for path, label in [(self.primary, "primary"), (self.worker, "fleet-worker")]:
                self.whitelist(path, ["retired-model"])
                result, output = self.doctor()
                self.assertEqual(result, 1)
                self.assertIn(f"OpenCode {label} whitelist", output)
                self.assertIn("missing admitted models kimi-k3", output)
                self.assertIn("stale models retired-model", output)
                self.assertEqual(output.count("FAIL"), 2)
                self.whitelist(path, ["kimi-k3"])
                self.assertEqual(self.doctor()[0], 0)

    def test_whitelist_uses_every_admitted_go_native_selector(self):
        self.roster["lanes"] += [lane("pro", "canonical-pro"), lane("watch", "watched", admission="watch")]
        self.roster["lanes"][1]["selector"] = "opencode-go/provider-pro"
        self.roster["lanes"] += [dict(lane("other", "other"), provider="other-provider")]
        self.whitelist(self.primary, ["kimi-k3"])
        self.whitelist(self.worker, ["kimi-k3"])
        issues = fleetctl.opencode_whitelist_problems(self.roster)
        self.assertEqual(len(issues), 2)
        self.assertTrue(all("missing admitted models provider-pro" in issue for issue in issues))
        self.assertTrue(all("watched" not in issue and "other" not in issue for issue in issues))
        for path in [self.primary, self.worker]:
            self.whitelist(path, ["kimi-k3", "provider-pro"])
        self.assertEqual(fleetctl.opencode_whitelist_problems(self.roster), [])

    def test_whitelist_missing_config_or_unrestricted_provider_is_allowed(self):
        self.assertEqual(fleetctl.opencode_whitelist_problems(self.roster), [])
        for config in [{}, {"provider": {}}, {"provider": {"opencode-go": {}}}]:
            self.primary.write_text(json.dumps(config))
            self.assertEqual(fleetctl.opencode_whitelist_problems(self.roster), [])
        self.whitelist(self.primary, [])
        self.assertIn("missing admitted", fleetctl.opencode_whitelist_problems(self.roster)[0])

    def test_jsonc_comments_urls_quotes_and_trailing_commas(self):
        self.primary.write_text('''{
          // Picker policy
          "endpoint": "https://example.invalid/path//literal",
          "quoted": "an escaped \\"quote\\" /* literal */",
          "provider": { /* policy */ "opencode-go": {
            "whitelist": ["kimi-k3",],
          },},
        }''')
        self.assertEqual(fleetctl.opencode_whitelist_problems(self.roster), [])
        self.assertEqual(fleetctl._load_jsonc(self.primary)["quoted"], 'an escaped "quote" /* literal */')

    def test_malformed_jsonc_and_whitelist_fail_without_payload_leak(self):
        for config in ["{private_payload", "[]", '{"provider":null}',
                       '{"provider":{"opencode-go":null}}',
                       '{"provider":{"opencode-go":[]}}',
                       '{"provider":{"opencode-go":"no-whitelist"}}',
                       '{"provider":{"opencode-go":{"whitelist":null}}}',
                       '{"provider":{"opencode-go":{"whitelist":"kimi-k3"}}}',
                       '{"provider":{"opencode-go":{"whitelist":[4]}}}',
                       '{"provider":{"opencode-go":{"whitelist":["private payload"]}}}']:
            with self.subTest(config=config):
                self.primary.write_text(config)
                issues = fleetctl.opencode_whitelist_problems(self.roster)
                self.assertEqual(len(issues), 1)
                self.assertIn("invalid JSONC or Go whitelist", issues[0])
                self.assertNotIn("private", issues[0])

    def test_whitelist_xdg_paths_when_overrides_unset(self):
        primary = self.root / "config/opencode/opencode.jsonc"
        worker = self.root / "config/opencode/fleet-worker/opencode.jsonc"
        worker.parent.mkdir(parents=True)
        self.whitelist(primary, ["kimi-k3"])
        self.whitelist(worker, [])
        with patch.dict(os.environ, dict(os.environ), clear=True):
            os.environ.pop("FLEET_OPENCODE_CONFIG", None)
            os.environ.pop("FLEET_OPENCODE_WORKER_CONFIG", None)
            issues = fleetctl.opencode_whitelist_problems(self.roster)
        self.assertEqual(len(issues), 1)
        self.assertIn(str(worker), issues[0])

    def test_recent_true_identity_drift_fails_names_run_and_versions(self):
        self.ledger([self.receipt(actual_model="claude-opus-5", run_id="opus-downgrade")])
        issues = fleetctl.identity_drift_problems(self.roster, self.root, self.now)
        self.assertEqual(len(issues), 1)
        self.assertIn("run opus-downgrade", issues[0])
        self.assertIn("selected claude-opus-5-5, provider reported claude-opus-5", issues[0])
        with self.version("2.1.280"):
            result, output = self.doctor()
        self.assertEqual(result, 1)
        self.assertIn("FAIL", output)
        self.assertIn("opus-downgrade", output)

    def test_aliases_prefixes_and_native_lane_identity_are_normalized(self):
        self.roster["lanes"][0]["model_key"] = "canonical-k3"
        self.ledger([self.receipt(actual_model="anthropic/claude-opus-5-5", selected_model="opus"),
                     self.receipt(actual_model="opencode-go/kimi-k3", selected_model="canonical-k3")])
        self.assertEqual(fleetctl.identity_drift_problems(self.roster, self.root, self.now), [])

    def test_dynamic_router_selection_is_exempt(self):
        self.ledger([self.receipt(selected_model=model) for model in
                     ["auto", "default", "unknown", "openrouter/free", "github-copilot-auto",
                      "openrouter-free-router", "service-chosen"]])
        self.assertEqual(fleetctl.identity_drift_problems(self.roster, self.root, self.now), [])

    def test_identity_window_includes_seven_day_boundary_uses_ended_at(self):
        cutoff = self.now - dt.timedelta(days=7)
        self.ledger([self.receipt(run_id="boundary", actual_model="claude-opus-5",
                                started_at=fleetctl.iso(cutoff)),
                     self.receipt(run_id="stale", actual_model="claude-opus-5",
                                  started_at=fleetctl.iso(cutoff - dt.timedelta(seconds=1))),
                     self.receipt(run_id="ended-recent", actual_model="claude-opus-5",
                                  started_at=fleetctl.iso(cutoff - dt.timedelta(days=1)),
                                  ended_at=fleetctl.iso(self.now)),
                     self.receipt(run_id="future", actual_model="claude-opus-5",
                                  started_at=fleetctl.iso(self.now + dt.timedelta(seconds=1)))])
        issues = fleetctl.identity_drift_problems(self.roster, self.root, self.now)
        self.assertEqual(len(issues), 2)
        self.assertTrue(any("run boundary:" in issue for issue in issues))
        self.assertTrue(any("run ended-recent:" in issue for issue in issues))

    def test_malformed_records_and_unknown_identity_do_not_crash(self):
        self.ledger([None, [], "text", 5, {"schema": "other"},
                     self.receipt(started_at="broken"), self.receipt(started_at=None),
                     self.receipt(started_at="2026-10-02T11:00:00"),
                     self.receipt(actual_model=None), self.receipt(selected_model=[]),
                     self.receipt(actual_model="private payload"), self.receipt(actual_model={})])
        with (self.root / "runs.jsonl").open("a") as handle:
            handle.write("{truncated\n")
            handle.write(json.dumps(self.receipt(actual_model="claude-opus-5", run_id="valid-after-bad")) + "\n")
        issues = fleetctl.identity_drift_problems(self.roster, self.root, self.now)
        self.assertEqual(len(issues), 1)
        self.assertIn("valid-after-bad", issues[0])

    def test_no_ledger_and_invalid_run_id_are_safe(self):
        self.assertEqual(fleetctl.identity_drift_problems(self.roster, self.root, self.now), [])
        self.ledger([self.receipt(actual_model="claude-opus-5", run_id="private\ntext")])
        issues = fleetctl.identity_drift_problems(self.roster, self.root, self.now)
        self.assertIn("run unnamed", issues[0])
        self.assertNotIn("private", issues[0])


if __name__ == "__main__":
    unittest.main()
