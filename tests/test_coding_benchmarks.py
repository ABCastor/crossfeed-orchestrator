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
