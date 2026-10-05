"""Synthetic publisher and mechanically checked outcome evidence, without IO to providers."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evidence

SPEC = importlib.util.spec_from_file_location("selector_test_market", ROOT / "scripts/market-refresh.py")
market = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market)
READ_ON = "2026-10-02T10:00:00Z"


def overlay():
    return {"effort": {"synthetic-model": {"levels": {"codex": ["low", "high"]}, "default": "high"}}, "lanes": []}


def catalog(metrics=None):
    return {"fetched_at": READ_ON, "artificial_analysis": {"synthetic-model": {"levels": {
        "high": {"id": "synthetic-high", "effort": "high", "release_date": "2026-09-01",
                 "evaluations": metrics or {"terminalbench_v4_0": .8},
                 "price_1m_input": 2, "price_1m_output": 4}}}}}


def cell(result, level="high"):
    return next(row for row in result["rows"] if row["model_key"] == "synthetic-model" and row["level"] == level)


class EvidenceTests(unittest.TestCase):
    def test_aa_fetch_preserves_all_variants_ids_dates_and_evaluations(self):
        metrics = {"artificial_analysis_intelligence_index": 65, "artificial_analysis_coding_index": 75,
                   "terminalbench_v4_0": .8, "tau2": .7, "lcr": .6, "ifbench": .9, "future_eval": .123}
        raw = {"data": [{"id": "fixture-" + level, "slug": "synthetic-model-" + level,
                          "name": "Synthetic " + level, "release_date": "2026-09-01",
                          "evaluations": metrics, "pricing": {"price_1m_input_tokens": 2, "price_1m_output_tokens": 4}}
                         for level in ("low", "high")]}
        with mock.patch.dict(os.environ, {"AA_API_KEY": "synthetic-not-a-secret"}), \
             mock.patch.object(market.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(raw).encode())):
            result = market.fetch_artificial_analysis({"synthetic-model": ["syntheticmodel"]})
        self.assertEqual(set(result["synthetic-model"]["levels"]), {"low", "high"})
        self.assertEqual(len(result["synthetic-model"]["variants"]), 2)
        for row in result["synthetic-model"]["variants"]:
            self.assertEqual(row["evaluations"], metrics)
            self.assertEqual(row["release_date"], "2026-09-01")
            self.assertEqual(row["id"], "fixture-" + row["effort"])

    def test_fraction_and_percent_maps_have_same_quality_and_source_metadata(self):
        fractional = {"terminalbench_v4_0": .8, "tau2": .7, "lcr": .6, "ifbench": .9}
        percent = {key: value * 100 for key, value in fractional.items()}
        first = cell(evidence.build_evidence(overlay(), catalog(fractional), [], READ_ON))
        second = cell(evidence.build_evidence(overlay(), catalog(percent), [], READ_ON))
        for family in evidence.FAMILIES:
            self.assertAlmostEqual(first["q"][family]["mean"], second["q"][family]["mean"])
        self.assertEqual({s["metric"] for s in first["sources"]}, set(fractional))
        self.assertTrue(all(s["id"] == "synthetic-high" and s["release_date"] == "2026-09-01" for s in first["sources"]))
        self.assertTrue(all(s["assumption"] and s["heuristic"] for s in first["sources"]))

    def test_missing_level_is_unknown_without_borrowing_another_level_mean(self):
        result = evidence.build_evidence(overlay(), catalog(), [], READ_ON)
        high, low = cell(result), cell(result, "low")
        self.assertGreater(high["q"]["coding-agent"]["mean"], .5)
        for family in evidence.FAMILIES:
            self.assertEqual(low["q"][family]["mean"], .5)
            self.assertGreaterEqual(low["q"][family]["sd"], .15)
            self.assertEqual(low["q"][family]["n_sources"], 0)
            self.assertTrue(low["q"][family]["unknown"])
            self.assertEqual(low["q"][family]["sources"], [])
        self.assertFalse(any("interpolated" in flag for flag in low["flags"]))

    def test_mechanical_pass_and_fail_update_beta_but_wrapper_success_does_not(self):
        records = [{"run_id": "transport", "model": "synthetic-model", "effort": "low", "role": "implementation", "status": "ok", "returncode": 0},
                   {"run_id": "pass", "model": "synthetic-model", "effort": "low", "role": "implementation", "outcome": {"mechanically_verified": True, "passed": True}},
                   {"run_id": "fail", "model": "synthetic-model", "effort": "low", "role": "implementation", "outcome": {"mechanically_verified": True, "passed": False}}]
        row = cell(evidence.build_evidence(overlay(), {}, records, READ_ON), "low")
        self.assertEqual(row["own"]["coding-agent"], {"passes": 1, "trials": 2})
        self.assertAlmostEqual(row["q"]["coding-agent"]["mean"], .5)
        self.assertLess(row["q"]["coding-agent"]["sd"], (1 / 12) ** .5)
        pass_only = cell(evidence.build_evidence(overlay(), {}, records[:2], READ_ON), "low")
        self.assertAlmostEqual(pass_only["q"]["coding-agent"]["mean"], 2 / 3)

    def test_linked_identity_telemetry_and_afk_proof_count_one_trial(self):
        identity = {"schema": "crossfeed-model-run/v1", "run_id": "run-1", "selected_model": "synthetic-model", "effort": "low", "role": "implementation", "status": "ok"}
        telemetry = {"schema": "opencode-run-usage/v2", "run_id": "run-1", "afk_attempt_id": "attempt-1", "model": "synthetic-model", "effort": "low",
                     "tokens": {"input": 123, "output": 456}, "duration_ms": 9000, "ttfa_s": 9}
        proof = {"schema": "afk-attempt/v1", "attempt_id": "attempt-1", "model": "synthetic-model", "effort": "low", "role": "implementation",
                 "result": "verified", "failure_class": None, "proof": {"returncode": 0, "output_sha256": "synthetic-hash"}}
        records = [identity, telemetry, proof, copy.deepcopy(proof)]
        row = cell(evidence.build_evidence(overlay(), {}, records, READ_ON), "low")
        self.assertEqual(row["own"]["coding-agent"], {"passes": 1, "trials": 1})
        self.assertAlmostEqual(row["q"]["coding-agent"]["mean"], 2 / 3)
        self.assertEqual(row["tokens_per_task"], {"in": 123, "out": 456})
        self.assertEqual(row["latency_s"], 9)
        direct = cell(evidence.build_evidence(overlay(), {}, [dict(identity, outcome={"mechanically_verified": True, "passed": True})], READ_ON), "low")
        self.assertEqual(direct["own"]["coding-agent"], {"passes": 1, "trials": 1})

    def test_external_posterior_moves_with_mechanical_outcomes(self):
        baseline = cell(evidence.build_evidence(overlay(), catalog(), [], READ_ON))["q"]["coding-agent"]["mean"]
        for passed in (True, False):
            records = [{"run_id": "mechanical", "model": "synthetic-model", "effort": "high", "family": "coding-agent",
                        "outcome": {"mechanically_verified": True, "passed": passed}}]
            updated = cell(evidence.build_evidence(overlay(), catalog(), records, READ_ON))["q"]["coding-agent"]["mean"]
            self.assertGreater(updated, baseline) if passed else self.assertLess(updated, baseline)

    def test_verified_evidence_limits_and_completion_latency_survive_refresh(self):
        flags = ['coverage_incomplete', 'calibration_unmeasured', 'model_identity_unconfirmed',
                 'synthetic_coding_only', 'model_harness_specific']
        record = {'run_id': 'limited-proof', 'model': 'synthetic-model', 'effort': 'high',
                  'family': 'coding-agent', 'completion_time_s': 340.8,
                  'outcome': {'mechanically_verified': True, 'passed': True},
                  'evidence_flags': flags + ['untrusted-payload', {'invalid': 'flag'}]}
        row = cell(evidence.build_evidence(overlay(), {}, [record], READ_ON))
        self.assertTrue(set(flags) <= set(row['flags']))
        self.assertNotIn('untrusted-payload', row['flags'])
        self.assertEqual(row['latency_s'], 340.8)
        self.assertIn('latency_completion_measured', row['flags'])
        record.pop('outcome')
        row = cell(evidence.build_evidence(overlay(), {}, [record], READ_ON))
        self.assertTrue(set(flags).isdisjoint(row['flags']))

    def test_ambiguous_variants_never_become_a_measured_point(self):
        data = catalog()
        original = data["artificial_analysis"]["synthetic-model"]["levels"]["high"]
        data["artificial_analysis"]["synthetic-model"]["levels"]["high"] = {"ambiguous": True, "variants": [original, dict(original, id="different", evaluations={"terminalbench_v4_0": .2})]}
        row = cell(evidence.build_evidence(overlay(), data, [], READ_ON))
        self.assertIn("ambiguous_variants", row["flags"])
        self.assertTrue(row["q"]["coding-agent"]["unknown"])
        self.assertGreaterEqual(row["q"]["coding-agent"]["sd"], .15)

    def test_measured_token_latency_and_api_equivalent_price_flags(self):
        records = [{"run_id": "a", "model": "synthetic-model", "effort": "high", "tokens": {"input": 100, "output": 200}, "duration_ms": 1000, "ttfa_s": 1},
                   {"run_id": "b", "model": "synthetic-model", "effort": "high", "tokens": {"input": 300, "output": 600}, "duration_ms": 3000, "ttfa_s": 3}]
        result = evidence.build_evidence(overlay(), catalog(), records, READ_ON)
        row = cell(result)
        self.assertEqual(row["tokens_per_task"], {"in": 200, "out": 400})
        self.assertEqual(row["latency_s"], 2)
        self.assertEqual(row["price_1m"], {"in": 2, "out": 4})
        self.assertTrue({"tokens_in_measured", "tokens_out_measured", "latency_measured", "price_api_equivalent_estimated"} <= set(row["flags"]))
        self.assertTrue({"tokens_in_estimated", "latency_estimated", "price_unknown"} <= set(cell(result, "low")["flags"]))

    def test_arena_absence_skips_cleanly_and_json_attaches_only_exact_level(self):
        self.assertFalse(evidence.load_lmarena()["available"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "arena.json"
            path.write_text(json.dumps({"rows": [{"model": "synthetic-model", "level": "low", "elo": 1200}]}))
            arena = evidence.load_lmarena(path)
            self.assertTrue(arena["available"])
            self.assertEqual(arena["license"], "CC-BY")
            result = evidence.build_evidence(overlay(), {"lmarena": arena}, [], READ_ON)
            self.assertGreater(cell(result, "low")["q"]["visual"]["mean"], .5)
            self.assertTrue(cell(result)["q"]["visual"]["unknown"])
            self.assertTrue(cell(result, "low")["q"]["reasoning"]["unknown"])
            self.assertFalse(evidence.load_lmarena(Path(d) / "missing.json")["available"])

    def test_write_evidence_ignores_torn_ledger_and_publishes_json(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            state.joinpath("runs.jsonl").write_text('{"torn":\n' + json.dumps({"model": "synthetic-model", "effort": "low", "role": "implementation", "outcome": {"mechanically_verified": True, "passed": True}}) + "\n")
            result = evidence.write_evidence(state, overlay(), catalog())
            self.assertEqual(json.loads((state / "evidence/levels.json").read_text()), result)
            self.assertEqual(cell(result, "low")["own"]["coding-agent"]["trials"], 1)


if __name__ == "__main__":
    unittest.main()
