"""Older models must earn an on switch with a pool-specific comparative reason."""
import copy
import json
import unittest
from pathlib import Path

from tests.test_console import fleetctl, console
from tests.test_switch_respected import Sandbox
from tests.test_limits_and_older import gemini_roster


def reason(current="gpt-6.1-sol"):
    return {"job": "short repo maps", "advantage": "faster", "compared_to": current,
            "reason": "2 s versus 5 s on map-fixture-v1", "evidence": "tests/map-fixture-v1 (2026-10-02)"}


class OlderRuleTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.roster["model_cards"]["gpt-6-sol"]["best_for"] = "repo maps"
        self.write_roster(self.roster)

    def keep(self):
        self.roster["model_cards"]["gpt-6-sol"]["older_model_reasons"] = {"codex": reason()}
        self.write_roster(self.roster)

    def test_no_reason_is_off_by_default_even_with_legacy_all_on_state(self):
        for runtime in ({}, {"model_toggles": {"codex": []}}, {"model_choices": {"codex": "gpt-6-sol"}}):
            self.assertFalse(fleetctl.pool_switches(self.roster, runtime, "codex")["gpt-6-sol"])
        self.assertEqual(self.fleet("model-run", "codex", "gpt-6-sol").stdout.strip(), "gpt-6-luna")
        result = self.fleet("model-toggle", "codex", "gpt-6-sol", "on", check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("older model needs a roster reason", result.stderr)
        with self.assertRaisesRegex(ValueError, "older model needs"):
            fleetctl.set_model_choice({}, self.roster, "codex", "gpt-6-sol")

    def test_good_reason_allows_on_but_never_reenables_an_owner_off(self):
        self.keep()
        self.assertTrue(fleetctl.pool_switches(self.roster, {}, "codex")["gpt-6-sol"])
        self.off("codex", "gpt-6-sol")
        self.assertFalse(fleetctl.pool_switches(self.roster, json.loads((self.state / "runtime.json").read_text()), "codex")["gpt-6-sol"])
        self.assertEqual(self.fleet("model-toggle", "codex", "gpt-6-sol", "on").stdout.strip(), "on")
        self.assertEqual(self.fleet("model-run", "codex", "gpt-6-sol").stdout, "")

    def test_reason_is_scoped_to_current_comparator_job_evidence_and_pool(self):
        self.keep()
        for field, bad in (("job", ""), ("advantage", "served"), ("compared_to", "gpt-6-sol"),
                           ("evidence", ""), ("reason", ""), ("advantage", []), ("compared_to", [])):
            fixture = copy.deepcopy(self.roster)
            fixture["model_cards"]["gpt-6-sol"]["older_model_reasons"]["codex"][field] = bad
            self.assertIsNone(fleetctl.older_model_reason(fixture, "codex", "gpt-6-sol"), field)
        fixture = copy.deepcopy(self.roster)
        fixture["model_cards"]["gpt-6-sol"]["older_model_reasons"] = []
        self.assertFalse(fleetctl.pool_switches(fixture, {}, "codex")["gpt-6-sol"])
        fixture["model_cards"]["gpt-6-sol"]["older_model_reasons"] = {"other": reason()}
        self.assertFalse(fleetctl.pool_switches(fixture, {}, "codex")["gpt-6-sol"])
        # Generic best_for/lane descriptions never qualify as a comparative advantage.
        fixture["model_cards"]["gpt-6-sol"].pop("older_model_reasons")
        self.assertIsNone(fleetctl.older_model_reason(fixture, "codex", "gpt-6-sol"))

    def test_existing_sourced_successor_comparison_counts_and_disappears_after_supersession(self):
        card = self.roster["model_cards"]["gpt-6-sol"]
        card["better_than_successor_at"] = [{"capability": "map-fixture-v1 latency", "this": "2 s",
            "successor": "5 s", "margin": "lower", "source": "tests/map-fixture-v1"}]
        self.assertTrue(fleetctl.pool_switches(self.roster, {}, "codex")["gpt-6-sol"])
        card["better_than_successor_at"][0]["margin"] = "small"  # Existing console-card format.
        self.assertTrue(fleetctl.pool_switches(self.roster, {}, "codex")["gpt-6-sol"])
        card["better_than_successor_at"][0].pop("source")
        self.assertFalse(fleetctl.pool_switches(self.roster, {}, "codex")["gpt-6-sol"])
        self.keep()
        self.roster["model_cards"]["gpt-6.1-sol"]["status"] = "older"
        self.assertFalse(fleetctl.pool_switches(self.roster, {}, "codex")["gpt-6-sol"])

    def test_list_console_dry_run_apply_and_idempotence(self):
        self.keep()
        self.off("codex", "gpt-6-astra")
        before = (self.state / "runtime.json").read_bytes()
        rows = json.loads(self.fleet("older-models", "list", "--json").stdout)
        sol = next(row for row in rows if row["model"] == "gpt-6-sol")
        self.assertTrue(sol["on"])
        self.assertIn("Faster than gpt-6.1-sol", sol["reason"])
        overview = fleetctl.fleet_overview(self.roster, {}, self.state)
        page = console.render_page(overview, "test-token")
        self.assertIn("Faster than gpt-6.1-sol", page)
        self.assertIn("Off: no recorded advantage", page)
        result = self.fleet("older-models", "apply", "--dry-run")
        self.assertIn("would switch off", result.stdout)
        self.assertEqual((self.state / "runtime.json").read_bytes(), before)
        self.fleet("older-models", "apply")
        runtime = json.loads((self.state / "runtime.json").read_text())
        self.assertIn("claude-sonnet-5", runtime["model_toggles"]["claude"])
        self.assertEqual(runtime["model_toggles"]["codex"], ["gpt-6-astra"])
        self.assertTrue(fleetctl.pool_switches(self.roster, runtime, "codex")["gpt-6-sol"])
        rows = json.loads(self.fleet("older-models", "apply", "--json").stdout)
        self.assertFalse(any(row["action"] == "off" for row in rows))

    def test_dry_run_does_not_create_missing_state(self):
        import shutil
        shutil.rmtree(self.state)  # Temporary test fixture only.
        self.fleet("older-models", "apply", "--dry-run")
        self.assertFalse(self.state.exists())

    def test_routed_older_models_need_the_same_reason(self):
        fixture = gemini_roster()
        self.assertFalse(fleetctl.pool_switches(fixture, {}, "gem")["gemini-3.7-flash"])
        fixture["model_cards"]["gemini-3.7-flash"] = {"older_model_reasons": {"gem": reason("gemini-3.8-flash")}}
        self.assertTrue(fleetctl.pool_switches(fixture, {}, "gem")["gemini-3.7-flash"])
        # Kept older models can be ranked explicitly; they never stand in for a current one.
        fixture["routing"]["roles"]["default"]["quality_first"] = ["flash-37"]
        self.assertEqual(fleetctl.choose_lane(fixture, {}, "default", "read-only", "text")["lane_id"], "flash-37")
        runtime = {"model_toggles": {"gem": ["gemini-3.8-flash", "gemini-3.1-pro"]}}
        kept, _ = fleetctl.apply_model_toggles(["flash-38"], fixture, runtime)
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
