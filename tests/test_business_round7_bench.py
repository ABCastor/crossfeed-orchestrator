"""Round 7 BENCH: candidate-only models are never offered as usable; benchmark joins and Neon eligibility."""
import copy
import json
import unittest

from tests.test_selector import ROOT, fleetctl

CANDIDATE = "gpt-9.9-terra"
USABLE = "gpt-6-sol"


def fixture():
    return json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())


def codex_options(roster):
    pools = {"codex"}
    models = fleetctl.model_overview(roster, {}, pools)
    return {row["model"]: row for row in fleetctl.pool_model_options(roster, {}, "codex", models, {})}


class CandidateListingTests(unittest.TestCase):
    def roster(self):
        roster = fixture()
        roster["quota_pools"]["codex-catalog"] = {"label": "Codex catalog candidates"}
        roster["lanes"].append({
            "lane_id": "catalog-codex-catalog-" + CANDIDATE, "harness": "codex", "provider": "openai",
            "quota_pool": "codex-catalog", "selector": CANDIDATE, "model_key": CANDIDATE,
            "access_status": "unverified", "admission_status": "candidate", "roles": [],
            "allowed_modes": ["read-only"], "data_policy": "public-only"})
        return roster

    # BR-REFRESH-001
    def test_catalog_candidate_is_not_listed_as_a_usable_codex_model(self):
        options = codex_options(self.roster())
        self.assertNotIn(CANDIDATE, list(options))

    # BR-REFRESH-001: control, the probe must see a model the pool really runs.
    def test_probe_can_see_a_runnable_codex_model(self):
        options = codex_options(self.roster())
        self.assertIn(USABLE, list(options))
        self.assertTrue(options[USABLE]["run_as"])

    # BR-REFRESH-001: admitting the candidate (card active, access verified) makes it appear.
    def test_probe_sees_the_candidate_once_admitted(self):
        roster = self.roster()
        roster["model_cards"][CANDIDATE] = {"name": "GPT 9.9 Terra", "pool": "codex", "access_status": "verified",
                                            "admission_status": "active", "status": "current"}
        self.assertIn(CANDIDATE, list(codex_options(roster)))


if __name__ == "__main__":
    unittest.main()
