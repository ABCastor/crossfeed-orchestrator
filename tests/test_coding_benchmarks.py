import io
import json
import os
import unittest
from unittest.mock import patch

from tests import test_evidence as fixtures


evidence = fixtures.evidence
market = fixtures.market


class CodingBenchmarksTests(unittest.TestCase):
    def test_match_exact_snapshot_effort_and_non_null_metric(self):
        rows = [
            {"slug": "synthetic-model-high", "name": "Synthetic (High)",
             "evaluations": {"terminalbench_v4_0": None, "terminal_bench_4": .63}},
            {"slug": "synthetic-model-high-preview", "name": "Synthetic Preview (High)"},
            {"slug": "synthetic-model-1017-low", "name": "Synthetic (Low)"},
            {"slug": "synthetic-model-max", "name": "Synthetic (High)"},
            {"slug": "synthetic-model", "name": "Synthetic (Max, Default Fallback)"},
        ]
        payload = {"data": rows, "pagination": {"page": 1, "total_pages": 1, "has_more": False}}
        with patch.dict(os.environ, {"AA_API_KEY": "fixture"}), patch.object(
                market.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            cell = market.fetch_artificial_analysis({"synthetic-model": ["syntheticmodel"]})["synthetic-model"]
        self.assertEqual(len(cell["variants"]), 4)
        self.assertEqual(cell["levels"]["high"]["terminal_bench_4"], .63)
        self.assertNotIn("low", cell["levels"])
        self.assertEqual(cell["levels"]["max"]["slug"], "synthetic-model")

    def test_published_max_does_not_certify_high_or_a_changed_snapshot(self):
        model = "gpt-6-luna"
        overlay = {"effort": {model: {"levels": {"codex": ["high", "max"]}}}, "lanes": []}
        rows = {r["level"]: r for r in evidence.build_evidence(overlay, {}, [], "2026-11-19")["rows"]}
        self.assertFalse(rows["max"]["q_by_stakes"]["high"]["coding-agent"]["unknown"])
        self.assertTrue(rows["high"]["q_by_stakes"]["high"]["coding-agent"]["unknown"])
        overlay["model_cards"] = {model: {"served_snapshot": "20261120"}}
        rows = evidence.build_evidence(overlay, {}, [], "2026-11-20")["rows"]
        self.assertTrue(all(r["q_by_stakes"]["high"]["coding-agent"]["unknown"] for r in rows))

    def test_claude_page_cells_are_exact_and_effort_tokens_feed_cost(self):
        from scripts import selector
        opus, sonnet = "claude-opus-5-5", "claude-sonnet-5-5"
        overlay = {"effort": {m: {"levels": {"claude": ["medium", "high", "max"]}}
                              for m in (opus, sonnet)}}
        catalog = {"artificial_analysis": {m: {"variants": [
            {"effort": l, "price_1m_input": price / 5, "price_1m_output": price}
            for l in ("medium", "high", "max")]}
            for m, price in ((opus, 20), (sonnet, 10))}}
        def build(telemetry=None):
            with patch.object(evidence, "_observations", return_value=({}, telemetry or {})):
                return {(r["model_key"], r["level"]): r for r in
                        evidence.build_evidence(overlay, catalog, [], "2026-11-19")["rows"]}
        rows = build()
        med, high, max_ = rows[opus, "medium"], rows[opus, "high"], rows[sonnet, "max"]
        self.assertEqual((med["sources"][0]["value"], high["sources"][0]["value"]), (52.5253, 56.5657))
        self.assertTrue(med["sources"][0]["url"].endswith(opus))
        self.assertEqual(max_["sources"][0]["source"], "terminal_bench")
        def cost(row):
            return selector._task_cost({}, {"pool": "claude"}, row, {}, .05)[0]
        self.assertLess(cost(med)["percent"], cost(high)["percent"])
        self.assertLess(cost(high)["percent"], cost(max_)["percent"])
        self.assertIn("not known Claude Max weights", cost(med)["effort_proxy"]["weight_basis"])
        observed = build({(opus, "medium"): {"out": [3100, 5000, 4000]}})[opus, "medium"]
        self.assertEqual(observed["tokens_per_task"]["out"], 4000)
        self.assertNotIn("token_estimate", observed)
        overlay["model_cards"] = {opus: {"served_snapshot": "20261120"}}
        changed = build()[opus, "medium"]
        self.assertNotIn("token_estimate", changed)
        self.assertFalse(changed["sources"])

    def test_general_reasoning_fit_does_not_train_hard_benchmark_grade(self):
        counts = {("synthetic-model", "high", "reasoning"): {"passes": 32, "trials": 32}}
        catalog = fixtures.catalog({"intelligence_index": 42})
        baseline = fixtures.cell(evidence.build_evidence(fixtures.overlay(), catalog, []))
        with patch.object(evidence, "_observations", return_value=(counts, {})):
            row = fixtures.cell(evidence.build_evidence(fixtures.overlay(), catalog, []))
        self.assertGreater(row["q"]["reasoning"]["mean"], baseline["q"]["reasoning"]["mean"])
        self.assertEqual(row["q_by_stakes"]["high"]["reasoning"]["mean"],
                         baseline["q_by_stakes"]["high"]["reasoning"]["mean"])

    def test_reasoning_index_needs_versioned_coding_calibration(self):
        models = [f"m{i}" for i in range(6)]
        overlay = {"effort": {m: {"levels": {"codex": ["high"]}} for m in models}, "lanes": []}
        variants = {m: {"levels": {"high": {"effort": "high", "index_version": 5,
                    "evaluations": {"intelligence_index": 20 + i * 10 if i < 5 else 35,
                                    "terminal_bench_4": .2 + i * .1 if i < 5 else None}}}}
                    for i, m in enumerate(models)}
        def grade():
            result = evidence.build_evidence(overlay, {"artificial_analysis": variants}, [])
            return next(r for r in result["rows"] if r["model_key"] == "m5")["q"]["coding-agent"]
        self.assertEqual(grade()["imputation"]["predictor_mapping"], "aa_v5_coding_proxy")
        self.assertFalse(grade()["imputation"]["boundary_clamped"])
        values = variants["m5"]["levels"]["high"]["evaluations"]
        values["intelligence_index"] = 15
        self.assertTrue(grade()["imputation"]["boundary_clamped"])
        values["intelligence_index"] = 5
        self.assertTrue(grade()["unknown"])
        variant = variants["m5"]["levels"]["high"]
        values["intelligence_index"] = 35
        variant["index_version"] = 4
        self.assertTrue(grade()["unknown"])

    def test_go_flash_1017_uses_dated_catalog_identity(self):
        model = "deepseek-v4.1-flash"
        overlay = {"effort": {model: {"levels": {"opencode": ["max"]}}}, "lanes": []}
        generic = evidence.build_evidence(overlay, {}, [], "2026-11-19")["rows"][0]
        self.assertTrue(generic["q_by_stakes"]["high"]["coding-agent"]["unknown"])
        catalog = {"go_models": {model: {"name": "DeepSeek V4.1 Flash 1017", "release_date": "2026-10-17"}}}
        row = evidence.build_evidence(overlay, catalog, [], "2026-11-19")["rows"][0]
        self.assertEqual(row["sources"][0]["served_snapshot"], "1017")
        self.assertEqual(row["sources"][0]["value"], 38.5)

    def test_fit_success_does_not_train_hard_or_debug(self):
        overlay = fixtures.overlay()
        counts = {("synthetic-model", "high", "coding-agent"): {"passes": 32, "trials": 32}}
        with patch.object(evidence, "_observations", return_value=(counts, {})):
            row = fixtures.cell(evidence.build_evidence(overlay, {}, []))
        self.assertAlmostEqual(row["q_by_stakes"]["low"]["coding-agent"]["mean"], 33 / 34)
        self.assertTrue(row["q_by_stakes"]["high"]["coding-agent"]["unknown"])
        self.assertEqual(row["own"]["debug"]["trials"], 0)
