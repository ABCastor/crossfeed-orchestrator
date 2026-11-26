"""Round 7 BENCH: candidate-only models are never offered as usable; benchmark joins and Neon eligibility."""
import copy
import json
import unittest

from tests.test_selector import ROOT, fleetctl, selector

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


class BenchmarkPriorTests(unittest.TestCase):
    """Configured outside rows grade a provider-default cell the free feed leaves empty."""

    def rows(self, existing=None):
        import tempfile
        from pathlib import Path
        roster = fixture()
        roster["benchmark_priors"] = {"mimo-v2.6-pro": {"provider-default": {
            "evaluations": {"terminal_bench_4": 0.35, "intelligence_index": 29.9}, "name": "MiMo V2.6 Pro",
            "read_on": "2026-11-26", "slug": "mimo-v2-6-pro", "url": "https://artificialanalysis.ai/models/mimo-v2-6-pro"}}}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "evidence").mkdir()
            (state / "evidence/levels.json").write_text(json.dumps({"rows": existing or []}))
            return fleetctl.comparison_evidence(roster, state)

    # BR-BENCH-001
    def test_prior_grades_the_cell_with_outside_scores(self):
        row = self.rows()[("mimo-v2.6-pro", "provider-default")]
        self.assertAlmostEqual(row["q"]["coding-agent"]["mean"], 0.35, places=2)
        self.assertAlmostEqual(row["q"]["reasoning"]["mean"], 0.299, places=2)
        self.assertTrue(row["q"]["review"]["n_sources"])
        self.assertNotIn("extraction", row["q"])  # no instruction-following score was published, so none is invented

    # BR-BENCH-001 control: a measured or feed row with sources is never overwritten by a prior.
    def test_prior_does_not_replace_a_sourced_row(self):
        sourced = {"model_key": "mimo-v2.6-pro", "model_identity": "mimo-v2.6-pro", "level": "provider-default",
                   "q": {"coding-agent": {"mean": 0.9, "sd": .05, "n_sources": 1}},
                   "sources": [{"mapping": "aa_terminal", "value": 90}]}
        row = self.rows([sourced])[("mimo-v2.6-pro", "provider-default")]
        self.assertEqual(row["q"]["coding-agent"]["mean"], 0.9)


class NeonEligibilityTests(unittest.TestCase):
    """Why Neon is rarely picked: it is a code-ok lane and callers default to private material."""

    def roster(self):
        roster = fixture()
        lane = copy.deepcopy(next(l for l in roster["lanes"] if l["quota_pool"] == "antigravity-gemini"
                                  and l["admission_status"] == "active"))
        lane.update(lane_id="test-neon-high", model_key="gemini-4-neon", roles=["implementation"],
                    data_policy="code-ok", allowed_modes=["write"])
        roster["lanes"].append(lane)
        roster["effort"]["gemini-4-neon"] = {"default": "high", "levels": {"agy": ["high"]}, "by_role": {}}
        return roster

    def lanes(self, data_class):
        options, rejected = selector.enumerate_options(self.roster(), {}, "implementation", fleet=fleetctl,
                                                       data_class=data_class, mode="write")
        return [o.get("lane_id") for o in options], rejected

    # BR-DATA-001 (MUST NOT): a private task is never sent to a code-ok lane.
    def test_private_task_refuses_the_code_ok_neon_lane(self):
        lanes, rejected = self.lanes("private")
        self.assertNotIn("test-neon-high", lanes)
        self.assertIn("test-neon-high: code-ok lane refuses private material", rejected)

    # BR-DATA-001 control: the same lane is offered once the caller declares code material.
    def test_probe_sees_the_neon_lane_for_code_material(self):
        lanes, _ = self.lanes("code")
        self.assertIn("test-neon-high", lanes)

    # BR-DATA-001: the dispatch instructions tell the caller how to declare code material.
    def test_skill_tells_callers_to_declare_code_material(self):
        text = (ROOT / "skill/SKILL.md").read_text()
        self.assertIn("--data-class code", text)
        self.assertIn("BR-DATA-001", text)


if __name__ == "__main__":
    unittest.main()
