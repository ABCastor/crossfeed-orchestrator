"""Known paired outcomes, costs and inferential edge cases, without models."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "evaluate.py"
SPEC = importlib.util.spec_from_file_location("bench_evaluate", MODULE_PATH)
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = {"options": [
            {"id": "a", "model": "model", "level": "low", "pool": "shared",
             "price_1m": {"in": 1, "out": 2}},
            {"id": "b", "model": "model", "level": "high", "pool": "shared",
             "price_1m": {"in": 3, "out": 4}}]}
        self.config = {"options_file": "options.json", "fit_seeds": [1], "heldout_seeds": [2],
                       "always_max": "b", "always_cheapest": "a", "fixed_seats": {"fix": "a"},
                       "selector": {"%d:fix-%d" % (seed, k): "b" for seed in (1, 2) for k in range(4)}}
        passes = {1: {"a": [True, True, True, False], "b": [True, False, False, False]},
                  2: {"a": [False, True, False, True], "b": [True, True, True, False]}}
        self.rows = []
        for seed in (1, 2):
            for k in range(4):
                for option in ("a", "b"):
                    self.rows.append({"task": "%d:fix-%d" % (seed, k), "family": "fix",
                                      "difficulty": "easy", "seed": seed, "option": option,
                                      "pass": passes[seed][option][k], "reason": "synthetic",
                                      "duration_s": (k + 1) * (1 if option == "a" else 2),
                                      "exit_code": 0, "tokens_in": 1000, "tokens_out": 500,
                                      "pool_percent": (k + 1) * (1 if option == "a" else 2)})
        self.save()

    def save(self):
        (self.root / "options.json").write_text(json.dumps(self.options), encoding="utf-8")
        (self.root / "policies.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.root / "results.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")

    def evaluate(self, split="heldout", seed=17, resamples=150):
        self.save()
        return evaluator.evaluate(self.root / "results.jsonl", self.root / "policies.json",
                                  split, seed, resamples)

    def test_known_pass_cost_latency_regret_and_pool_metrics(self):
        summary = self.evaluate()
        cheap = summary["policies"]["always-cheapest"]
        maximum = summary["policies"]["always-max"]
        oracle = summary["policies"]["oracle"]
        self.assertEqual(cheap["pass_rate"], .5)
        self.assertEqual(maximum["pass_rate"], .75)
        self.assertEqual(oracle["pass_rate"], 1)
        self.assertAlmostEqual(cheap["cost_per_task_usd"], .002)
        self.assertAlmostEqual(maximum["cost_per_task_usd"], .005)
        self.assertAlmostEqual(oracle["cost_per_task_usd"], .0035)
        self.assertEqual(cheap["median_latency_s"], 2.5)
        self.assertAlmostEqual(cheap["p90_latency_s"], 3.7)
        self.assertEqual(maximum["median_latency_s"], 5)
        self.assertAlmostEqual(maximum["p90_latency_s"], 7.4)
        self.assertEqual(cheap["pass_regret"], .5)
        self.assertAlmostEqual(cheap["cost_delta_vs_oracle_usd"], -.0015)
        self.assertEqual(cheap["pool_percent_used"], {"shared": 10})
        self.assertEqual(maximum["collapse_index"], 1)

    def test_expert_tasks_are_evaluated(self):
        for row in self.rows:
            row['difficulty'] = 'expert'
            row['tier'] = 'expert'
            row['template_id'] = 'fixture'
        summary = self.evaluate()
        self.assertEqual(summary['policies']['always-max']['pass_rate'], .75)

    def test_history_hash_ids_and_preregistered_tasks_are_evaluated(self):
        mapping = {}
        for row in self.rows:
            old = row['task']
            k = int(old.rsplit('-', 1)[1])
            new = '%d:history-fix-%012x' % (row['seed'], 0xabcdef000000+k)
            mapping[old] = new
            row['task'] = new
            row['family'] = 'history-fix'
        self.config['selector'] = {mapping[k]: v for k, v in self.config['selector'].items()}
        self.config['fixed_seats'] = {'history-fix': 'a'}
        self.config['expected_task_ids'] = sorted(set(mapping.values()))
        summary = self.evaluate()
        self.assertEqual(summary['policies']['always-max']['pass_rate'], .75)

    def test_single_best_uses_fit_and_not_heldout(self):
        summary = self.evaluate()
        self.assertEqual(summary["single_best_option"], "a")
        self.assertEqual(summary["policies"]["single-best"]["pass_rate"], .5)
        self.assertEqual(self.evaluate("fit")["policies"]["single-best"]["pass_rate"], .75)
        for row in self.rows:
            if row["seed"] == 2:
                row["pass"] = row["option"] == "b"
        self.assertEqual(self.evaluate()["single_best_option"], "a")

    def test_single_best_tie_breaks_by_option_id(self):
        for row in self.rows:
            if row["seed"] == 1:
                row["pass"] = True
        self.options["options"].reverse()
        self.assertEqual(self.evaluate()["single_best_option"], "a")

    def test_missing_costs_do_not_drop_passes_or_impute(self):
        for row in self.rows:
            if row["task"] == "2:fix-1" and row["option"] == "a":
                row.pop("tokens_out")
        summary = self.evaluate()
        cheap = summary["policies"]["always-cheapest"]
        self.assertEqual(cheap["pass_rate"], .5)
        self.assertIsNone(cheap["cost_per_task_usd"])
        self.assertEqual(cheap["cost_known_tasks"], 3)
        self.assertIsNone(summary["policies"]["oracle"]["cost_per_task_usd"])
        self.assertIsNone(summary["policies"]["always-max"]["cost_delta_vs_oracle_usd"])
        pair = next(pair for pair in summary["comparisons"]
                    if pair["left"] == "always-max" and pair["right"] == "always-cheapest")
        self.assertIsNone(pair["differences"]["cost_per_task_usd"]["ci95"])
        self.assertIsNotNone(pair["differences"]["pass_rate"]["ci95"])

    def test_null_prices_and_missing_fit_tokens_disable_zero(self):
        self.options["options"][0]["price_1m"] = {"in": None, "out": None}
        summary = self.evaluate()
        self.assertIsNone(summary["policies"]["always-cheapest"]["cost_per_task_usd"])
        self.assertIsNone(summary["fit_option_hull"])
        self.assertNotIn("zero", summary["policies"])
        self.assertTrue(any("Zero baseline unavailable" in warning for warning in summary["warnings"]))

    def test_exact_zero_tokens_are_measured_zero_cost(self):
        for row in self.rows:
            row["tokens_in"] = row["tokens_out"] = 0
        summary = self.evaluate()
        self.assertEqual(summary["policies"]["always-max"]["cost_per_task_usd"], 0)

    def test_bootstrap_reproducible_and_paired_identical_policies(self):
        first = self.evaluate(seed=31)
        second = self.evaluate(seed=31)
        self.assertEqual(first, second)
        pair = next(pair for pair in first["comparisons"]
                    if pair["left"] == "always-cheapest" and pair["right"] == "fixed-seats")
        for metric in pair["differences"].values():
            self.assertEqual(metric, {"difference": 0, "ci95": [0, 0]})
        self.assertEqual(pair["mcnemar"]["p_exact"], 1)
        max_pair = next(pair for pair in first["comparisons"]
                        if pair["left"] == "always-max" and pair["right"] == "always-cheapest")
        self.assertEqual(max_pair["differences"]["pass_rate"]["difference"], .25)
        self.assertEqual(max_pair["mcnemar"]["left_only"], 2)
        self.assertEqual(max_pair["mcnemar"]["right_only"], 1)

    def test_mcnemar_known_cases_and_large_balanced_counts(self):
        self.assertEqual(evaluator.mcnemar_exact(0, 0), 1)
        self.assertEqual(evaluator.mcnemar_exact(1, 0), 1)
        self.assertAlmostEqual(evaluator.mcnemar_exact(4, 0), .125)
        self.assertAlmostEqual(evaluator.mcnemar_exact(9, 1), .021484375)
        self.assertEqual(evaluator.mcnemar_exact(50000, 50000), 1)
        self.assertEqual(evaluator.mcnemar_exact(10000, 0), 0)

    def test_grid_seed_and_duplicate_validation(self):
        original = copy.deepcopy(self.rows)
        self.rows.pop()
        with self.assertRaisesRegex(evaluator.EvaluationError, "Incomplete paired grid"):
            self.evaluate()
        self.rows = original
        self.rows.append(dict(self.rows[0]))
        self.evaluate()
        self.rows[-1]["duration_s"] = 99
        with self.assertRaisesRegex(evaluator.EvaluationError, "Conflicting duplicate"):
            self.evaluate()
        self.rows = original
        self.rows[1]["difficulty"] = "hard"
        with self.assertRaisesRegex(evaluator.EvaluationError, "Inconsistent metadata"):
            self.evaluate()

    def test_expected_task_ids_detect_entirely_absent_task(self):
        self.config["expected_task_ids"] = sorted({row["task"] for row in self.rows})
        self.evaluate()
        self.rows = [row for row in self.rows if row["task"] != "2:fix-3"]
        with self.assertRaisesRegex(evaluator.EvaluationError, "1 absent"):
            self.evaluate()
        (self.root / "expected.json").write_text(json.dumps(self.config["expected_task_ids"]), encoding="utf-8")
        self.config["expected_task_ids"] = "expected.json"
        with self.assertRaisesRegex(evaluator.EvaluationError, "1 absent"):
            self.evaluate()

    def test_oracle_uses_cheapest_fallback_when_no_option_passes(self):
        summary = self.evaluate("fit")
        self.assertEqual(summary["policies"]["oracle"]["choices"]["1:fix-3"], "a")
        self.assertEqual(summary["policies"]["oracle"]["pass_rate"], .75)

    def test_overlap_excluded_invalid_telemetry_and_missing_seed_rejected(self):
        self.config["heldout_seeds"] = [1]
        with self.assertRaisesRegex(evaluator.EvaluationError, "overlap"):
            self.evaluate()
        self.config["heldout_seeds"] = [3]
        with self.assertRaisesRegex(evaluator.EvaluationError, "No measured tasks"):
            self.evaluate()
        self.config["heldout_seeds"] = [2]
        self.rows[0]["excluded"] = True
        with self.assertRaisesRegex(evaluator.EvaluationError, "must be rerun"):
            self.evaluate()
        self.rows[0].pop("excluded")
        self.rows[0]["tokens_in"] = -1
        with self.assertRaisesRegex(evaluator.EvaluationError, "nonnegative"):
            self.evaluate()

    def test_model_and_level_provenance_must_match_option(self):
        self.rows[0]["model"] = "substituted-model"
        with self.assertRaisesRegex(evaluator.EvaluationError, "model disagrees"):
            self.evaluate()
        self.rows[0]["model"] = "model"
        self.rows[0]["level"] = "high"
        with self.assertRaisesRegex(evaluator.EvaluationError, "level disagrees"):
            self.evaluate()
        self.rows[0]["level"] = "low"
        self.evaluate()

    def test_digests_cannot_mix_tasks_or_option_configurations(self):
        self.rows[0]["task_digest"] = "fixture-one"
        self.rows[1]["task_digest"] = "fixture-two"
        with self.assertRaisesRegex(evaluator.EvaluationError, "Inconsistent task_digest"):
            self.evaluate()
        self.rows[1]["task_digest"] = "fixture-one"
        self.rows[0]["option_digest"] = "option-config-one"
        self.rows[2]["option_digest"] = "option-config-two"
        with self.assertRaisesRegex(evaluator.EvaluationError, "Inconsistent option_digest"):
            self.evaluate()
        self.rows[2]["option_digest"] = "option-config-one"
        self.evaluate()

    def test_external_selector_file_and_missing_choice(self):
        choices = self.config["selector"]
        (self.root / "selection.json").write_text(json.dumps(choices), encoding="utf-8")
        self.config["selector"] = {"mapping_file": "selection.json"}
        self.assertEqual(self.evaluate()["policies"]["selector"]["pass_rate"], .75)
        choices.pop("2:fix-1")
        (self.root / "selection.json").write_text(json.dumps(choices), encoding="utf-8")
        with self.assertRaisesRegex(evaluator.EvaluationError, "Missing or unknown choice"):
            self.evaluate()

    def test_random_baselines_and_zero_fit_hull(self):
        summary = self.evaluate()
        self.assertIn("fixed-level:a", summary["policies"])
        self.assertIn("fixed-level:b", summary["policies"])
        self.assertIn("random-level:model", summary["policies"])
        self.assertEqual([point["id"] for point in summary["fit_option_hull"]], ["a"])
        self.assertEqual(set(summary["policies"]["zero"]["choices"].values()), {"a"})
        self.assertEqual(summary["policies"]["random-level:model"]["choices"],
                         self.evaluate(seed=999)["policies"]["random-level:model"]["choices"])
        self.assertAlmostEqual(summary["cost_quality"]["area_usd_pass_rate"], .001875)

    def test_cost_quality_hull_removes_points_beaten_by_mixtures(self):
        points = [{"id": "a", "cost_per_task_usd": 0, "pass_rate": 0},
                  {"id": "b", "cost_per_task_usd": 1, "pass_rate": .2},
                  {"id": "c", "cost_per_task_usd": 2, "pass_rate": 1}]
        self.assertEqual([point["id"] for point in evaluator.cost_quality_hull(points)], ["a", "c"])
        points[1]["pass_rate"] = .8
        self.assertEqual([point["id"] for point in evaluator.cost_quality_hull(points)], ["a", "b", "c"])

    def test_holm_correction_and_declared_variant_validation(self):
        self.config["number_variants"] = 3
        summary = self.evaluate()
        selected = [pair for pair in summary["comparisons"] if "selector" in (pair["left"], pair["right"])]
        self.assertEqual(summary["selector_multiplicity"]["holm_family_size"], len(selected) * 3)
        for pair in selected:
            self.assertGreaterEqual(pair["mcnemar"]["p_holm_selector"], pair["mcnemar"]["p_exact"])
        self.config["number_variants"] = 0
        with self.assertRaisesRegex(evaluator.EvaluationError, ">= declared"):
            self.evaluate()

    def test_quota_replay_measured_use_reset_and_missing_telemetry(self):
        choices = {"2:fix-%d" % k: "b" if k < 2 else "a" for k in range(4)}
        self.config["quota_scenarios"] = [{"id": "reset", "selector": choices,
            "pools": {"shared": {"remaining_percent": 10, "reset_after_tasks": 2,
                                  "reset_allowance_percent": 100}}}]
        summary = self.evaluate()
        pool = summary["quota_scenarios"]["reset"]["pools"]["shared"]
        self.assertEqual(pool["percent_used"], 13)
        self.assertEqual(pool["expired_unspent_percent"], 4)
        self.assertEqual(pool["remaining_percent"], 93)
        self.assertEqual(pool["overdrawn_percent"], 0)
        self.assertTrue(pool["reset_observed"])
        for row in self.rows:
            if row["task"] == "2:fix-0" and row["option"] == "b":
                row.pop("pool_percent")
        pool = self.evaluate()["quota_scenarios"]["reset"]["pools"]["shared"]
        self.assertIsNone(pool["expired_unspent_percent"])
        self.assertIsNone(pool["percent_used"])

    def test_quota_overdraw_is_reported_without_rerouting(self):
        self.config["quota_scenarios"] = [{"id": "critical", "selector": self.config["selector"],
            "pools": {"shared": {"remaining_percent": 5}}}]
        summary = self.evaluate()
        pool = summary["quota_scenarios"]["critical"]["pools"]["shared"]
        self.assertEqual(pool["overdrawn_percent"], 15)
        self.assertEqual(pool["expired_unspent_percent"], 0)
        self.assertEqual(pool["remaining_percent"], 0)
        self.assertEqual(set(summary["policies"]["selector-quota:critical"]["choices"].values()), {"b"})

    def test_cli_markdown_and_delimited_json(self):
        result = subprocess.run([sys.executable, str(MODULE_PATH), str(self.root / "results.jsonl"),
            "--policies", str(self.root / "policies.json"), "--split", "heldout", "--resamples", "10",
            "--json-out", "-"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Policy | Pass rate", result.stdout)
        encoded = result.stdout.split("--- BEGIN JSON SUMMARY ---\n", 1)[1].split("\n--- END JSON SUMMARY ---", 1)[0]
        self.assertEqual(json.loads(encoded)["single_best_option"], "a")

    def test_default_json_sidecar_and_cli_failure(self):
        argv = [sys.executable, str(MODULE_PATH), str(self.root / "results.jsonl"),
                "--policies", str(self.root / "policies.json"), "--split", "heldout", "--resamples", "5"]
        result = subprocess.run(argv, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "results.summary.json").is_file())
        self.rows.pop()
        self.save()
        result = subprocess.run(argv, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Incomplete paired grid", result.stderr)


if __name__ == "__main__":
    unittest.main()
