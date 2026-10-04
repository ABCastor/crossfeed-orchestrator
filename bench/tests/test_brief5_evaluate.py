"""Frozen task split, partial-grid coverage and paired numerical checks."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree

from bench import evaluate as evaluator
from bench.split import task_split


class PreregEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = {}
        for split in ("fit", "heldout"):
            self.ids[split] = ["21:gen-fix-%d" % i for i in range(100)
                               if task_split("21:gen-fix-%d" % i) == split][:4]
        ids = ["ds41-flash-high", "opus55-xhigh", "sol-high", "sol-xhigh"]
        self.options = {"options": [{"id": ident, "model": "sol" if ident.startswith("sol") else ident,
                                     "level": "xhigh" if ident.endswith("xhigh") else "high",
                                     "pool": "codex" if ident.startswith("sol") else "other",
                                     "price_1m": {"in": 1 if ident == ids[0] else 3, "out": 0}}
                                    for ident in ids]}
        self.config = {"options_file": "options.json", "split_method": "task-name-sha256",
                       "expected_task_ids": sorted(self.ids["fit"] + self.ids["heldout"]),
                       "selector": {task: "sol-high" for task in self.ids["heldout"]}}
        self.rows = []
        for split, tasks in self.ids.items():
            for i, task in enumerate(tasks):
                for ident in ids:
                    passed = (ident == ids[0] and i < 3) if split == "fit" else (
                        i % 2 == 1 if ident == ids[0] else i < 3)
                    self.rows.append({"task": task, "option": ident, "seed": 21, "family": "fix",
                                      "difficulty": "easy", "pass": passed, "duration_s": i+1,
                                      "exit_code": 0, "reason": "synthetic", "tokens_in": 1000,
                                      "tokens_out": 0, "pool_percent": .25,
                                      "pool": "codex" if ident.startswith("sol") else "other"})
        self.save()

    def save(self):
        (self.root / "options.json").write_text(json.dumps(self.options))
        (self.root / "policies.json").write_text(json.dumps(self.config))
        paths = [self.root / "results-a.jsonl", self.root / "results-b.jsonl"]
        for p, rows in zip(paths, (self.rows[::2], self.rows[1::2])):
            p.write_text("".join(json.dumps(row)+"\n" for row in rows))
        return paths

    def evaluate(self, **kwargs):
        return evaluator.evaluate(self.save(), self.root / "policies.json", resamples=150, **kwargs)

    def pair(self, summary, left, right):
        return next(p for p in summary["comparisons"] if (p["left"], p["right"]) == (left, right))

    def test_hash_split_uses_folder_not_seed_and_reports_only_heldout(self):
        summary = self.evaluate()
        self.assertEqual(summary["task_ids"], sorted(self.ids["heldout"]))
        self.assertEqual(summary["fit_task_ids"], sorted(self.ids["fit"]))
        for task in self.ids["heldout"]:
            self.assertEqual(task_split(task), task_split(task.replace("21:", "99:")))
        with self.assertRaisesRegex(evaluator.EvaluationError, "held-out only"):
            self.evaluate(split="fit")
        with self.assertRaisesRegex(evaluator.EvaluationError, "fixed at 7"):
            self.evaluate(seed=0)

    def test_fit_tuning_and_hull_cannot_use_heldout_outcomes_or_costs(self):
        original = self.evaluate()
        for row in self.rows:
            if row["task"] in self.ids["heldout"]:
                row["pass"] = row["option"] != "ds41-flash-high"
                row["tokens_in"] = 99999999
        changed = self.evaluate()
        for key in ("single_best_option", "fit_option_hull", "single_best_eligible_options"):
            self.assertEqual(original[key], changed[key])
        self.assertEqual(original["single_best_option"], "ds41-flash-high")
        self.assertEqual(original["policies"]["zero"]["choices"], changed["policies"]["zero"]["choices"])
        self.assertEqual(original["bootstrap"]["seed"], 7)
        self.assertEqual(original["policy_seed"], 7)

    def test_known_metrics_oracle_area_and_paired_statistics(self):
        summary = self.evaluate()
        cheap = summary["policies"]["always-cheapest"]
        maximum = summary["policies"]["always-max"]
        self.assertEqual(cheap["pass_rate"], .5)
        self.assertEqual(maximum["pass_rate"], .75)
        self.assertEqual(cheap["pass_regret"], .5)
        self.assertAlmostEqual(cheap["cost_per_task_usd"], .001)
        self.assertEqual(cheap["median_latency_s"], 2.5)
        self.assertAlmostEqual(cheap["p90_latency_s"], 3.7)
        self.assertEqual(cheap["pool_percent_per_task"]["other"], .25)
        self.assertAlmostEqual(summary["cost_quality"]["area_usd_pass_rate"], .00125)
        pair = self.pair(summary, "always-max", "always-cheapest")
        self.assertEqual(pair["paired_task_count"], 4)
        self.assertEqual(pair["differences"]["pass_rate"]["difference"], .25)
        self.assertEqual(pair["mcnemar"]["left_only"], 2)
        self.assertEqual(pair["mcnemar"]["right_only"], 1)
        identical = self.pair(summary, "fixed-seats", "selector")
        self.assertEqual(identical["differences"]["pass_rate"]["ci95"], [0, 0])
        self.assertEqual(summary, self.evaluate())

    def test_missing_selected_measurement_keeps_full_score_unknown(self):
        absent = self.ids["heldout"][0]
        self.rows = [r for r in self.rows if not (r["task"] == absent and r["option"] == "opus55-xhigh")]
        summary = self.evaluate()
        maximum = summary["policies"]["always-max"]
        self.assertIsNone(maximum["pass_rate"])
        self.assertEqual(maximum["measured_tasks"], 3)
        self.assertEqual(maximum["missing_task_ids"], [absent])
        self.assertEqual(maximum["measured_subset"]["metrics"]["pass_rate"], 2/3)
        self.assertIn({"task": absent, "option": "opus55-xhigh"}, summary["missing_grid_cells"])
        pair = self.pair(summary, "always-max", "always-cheapest")
        self.assertEqual(pair["paired_task_count"], 3)
        self.assertNotIn(absent, pair["paired_task_ids"])
        self.assertIn("exploratory", pair["scope"])
        self.assertNotIn("always-max", [p["id"] for p in summary["cost_quality"]["frontier"]])

    def test_entirely_absent_task_remains_in_frozen_universe(self):
        absent = self.ids["heldout"][0]
        self.rows = [r for r in self.rows if r["task"] != absent]
        summary = self.evaluate()
        self.assertIn(absent, summary["task_ids"])
        self.assertIsNone(summary["policies"]["always-cheapest"]["pass_rate"])
        self.assertEqual(summary["policies"]["oracle"]["missing_task_ids"], [absent])

    def test_incomplete_fit_option_is_ineligible_and_zero_is_missing(self):
        self.rows = [r for r in self.rows if not (r["task"] == self.ids["fit"][0] and
                                                  r["option"] == "ds41-flash-high")]
        summary = self.evaluate()
        self.assertNotIn("ds41-flash-high", summary["single_best_eligible_options"])
        self.assertTrue(summary["single_best_provisional"])
        self.assertIsNone(summary["fit_option_hull"])
        self.assertIsNone(summary["policies"]["zero"]["pass_rate"])
        self.assertEqual(summary["policies"]["zero"]["measured_tasks"], 0)
        self.assertIsNone(self.pair(summary, "always-max", "zero")["mcnemar"]["p_exact"])

    def test_external_full_selector_document_null_and_unmeasured_cells(self):
        choices = dict(self.config["selector"])
        choices[self.ids["heldout"][0]] = None
        (self.root / "selector.json").write_text(json.dumps({"mapping": choices, "selections": {"a": {"why": "full"}}}))
        self.config["selector"] = "selector.json"
        summary = self.evaluate()
        policy = summary["policies"]["selector"]
        self.assertIsNone(policy["pass_rate"])
        self.assertEqual(policy["missing_measurements"][self.ids["heldout"][0]], "unmeasured_choice")
        self.assertEqual(policy["measured_tasks"], 3)
        self.config["selector"] = {task: "sol-high" for task in self.ids["heldout"]}
        self.rows = [r for r in self.rows if not (r["task"] == self.ids["heldout"][0] and r["option"] == "sol-high")]
        self.assertEqual(self.evaluate()["policies"]["selector"]["measured_tasks"], 3)

    def test_missing_cost_stays_unknown_and_oracle_cannot_guess_cheapest(self):
        for row in self.rows:
            if row["option"] == "ds41-flash-high":
                row.pop("tokens_in")
        summary = self.evaluate()
        self.assertEqual(summary["policies"]["always-cheapest"]["pass_rate"], .5)
        self.assertIsNone(summary["policies"]["always-cheapest"]["cost_per_task_usd"])
        self.assertIsNone(summary["policies"]["oracle"]["cost_per_task_usd"])
        self.assertIsNone(summary["policies"]["always-max"]["cost_delta_vs_oracle_usd"])
        self.assertIsNone(summary["fit_option_hull"])

    def test_uniform_task_exclusions_filter_before_payload_validation(self):
        excluded = [self.ids["fit"][0], self.ids["heldout"][0]]
        (self.root / "excluded-hook-tasks.txt").write_text("\n".join(task.split(":", 1)[1] for task in excluded) + "\n")
        for row in self.rows:
            if row["task"] in excluded:
                row["pass"] = {"malformed": "excluded payload must not be inspected"}
                row["excluded"] = True
                row["option"] = "not-a-configured-option"
        self.config["heldout_task_ids"] = list(self.ids["heldout"])
        summary = self.evaluate()
        self.assertEqual(summary["fit_task_count"], 3)
        self.assertEqual(len(summary["task_ids"]), 3)
        self.assertEqual(summary["exclusions"]["excluded_task_ids"], sorted(excluded))
        self.assertTrue(all(task not in summary["task_ids"] + summary["fit_task_ids"] for task in excluded))
        self.assertTrue(all(cell["task"] not in excluded for cell in summary["missing_grid_cells"]))
        self.assertEqual(summary["policies"]["always-max"]["measured_tasks"], 3)
        (self.root / "explicit-exclusions.txt").write_text(self.ids["heldout"][1].split(":", 1)[1] + "\n")
        self.config["exclude_tasks_file"] = "explicit-exclusions.txt"
        explicit = self.evaluate()
        self.assertEqual(len(explicit["task_ids"]), 2)

    def test_unknown_cheapest_oracle_identity_has_no_latency(self):
        for row in self.rows:
            row.pop("tokens_in")
            row["duration_s"] = 100 if row["option"] == "ds41-flash-high" else 3
        oracle = self.evaluate()["policies"]["oracle"]
        self.assertEqual(oracle["pass_rate"], 1)
        self.assertIsNone(oracle["cost_per_task_usd"])
        self.assertIsNone(oracle["median_latency_s"])
        self.assertIsNone(oracle["p90_latency_s"])

    def test_declared_heldout_subset_keeps_fit_universe_and_scarce_ineligible(self):
        subset = self.ids["heldout"][:2]
        self.config["heldout_task_ids"] = list(reversed(subset))
        self.options["options"].append({"id": "scarce-high", "model": "scarce", "level": "high",
                                        "pool": "other", "price_1m": {"in": 1, "out": 0}})
        for i, task in enumerate(subset):
            self.rows.append(dict(self.rows[0], task=task, option="scarce-high", **{"pass": i == 0}))
        summary = self.evaluate()
        self.assertEqual(summary["task_ids"], sorted(subset))
        self.assertEqual(summary["fit_task_ids"], sorted(self.ids["fit"]))
        self.assertEqual(summary["heldout_universe_task_count"], 4)
        self.assertTrue(summary["heldout_subset_declared"])
        self.assertNotIn("scarce-high", summary["single_best_eligible_options"])
        self.assertEqual(summary["policies"]["fixed-level:scarce-high"]["pass_rate"], .5)
        self.assertEqual(summary["policies"]["fixed-level:scarce-high"]["measured_tasks"], 2)
        self.assertTrue(all(cell["task"] in self.ids["fit"] + subset for cell in summary["missing_grid_cells"]))
        (self.root / "heldout-subset.json").write_text(json.dumps(subset))
        self.config["heldout_task_ids"] = "heldout-subset.json"
        self.assertEqual(summary, self.evaluate())
        self.config["heldout_task_ids"] = [self.ids["fit"][0]]
        with self.assertRaisesRegex(evaluator.EvaluationError, "only held-out"):
            self.evaluate()
        self.config["heldout_task_ids"] = ["21:gen-fix-9999"]
        with self.assertRaisesRegex(evaluator.EvaluationError, "Unknown heldout_task_ids"):
            self.evaluate()
        self.config["heldout_task_ids"] = [subset[0], subset[0]]
        with self.assertRaisesRegex(evaluator.EvaluationError, "unique"):
            self.evaluate()

    def test_cost_report_is_measured_batch_average_and_unknown_remains_unknown(self):
        path = self.root / "costs.json"
        path.write_text(json.dumps({"schema": "crossfeed-quota-cost/v1", "options": [
            {"option": "ds41-flash-high", "pool": "other", "percent_binding_window_per_task": .7}]}))
        summary = self.evaluate(costs_path=path)
        self.assertEqual(summary["policies"]["always-cheapest"]["pool_percent_per_task"]["other"], .7)
        self.assertIsNone(summary["policies"]["always-max"]["pool_percent_per_task"]["other"])
        self.assertAlmostEqual(summary["policies"]["always-cheapest"]["cost_per_task_usd"], .001)

    def test_multi_options_files_and_duplicate_conflicts(self):
        (self.root / "extra.json").write_text(json.dumps(self.options))
        self.config["options_files"] = ["options.json", "extra.json"]
        self.evaluate()
        self.rows.append(dict(self.rows[0], duration_s=99))
        with self.assertRaisesRegex(evaluator.EvaluationError, "Conflicting duplicate"):
            self.evaluate()

    def test_tasks_directory_includes_absent_tasks_and_rejects_unexpected(self):
        tasks = self.root / "tasks"
        for task in self.config["expected_task_ids"]:
            folder = tasks / task.split(":", 1)[1]
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text(json.dumps({"seed": 21, "family": "fix", "difficulty": "easy"}))
        absent = self.ids["heldout"][0]
        self.rows = [r for r in self.rows if r["task"] != absent]
        summary = self.evaluate(tasks_path=tasks)
        self.assertIn(absent, summary["task_ids"])
        self.assertEqual(summary["task_universe"], "frozen tasks directory")
        self.rows.append(dict(self.rows[0], task="21:gen-fix-999"))
        with self.assertRaisesRegex(evaluator.EvaluationError, "Unexpected task"):
            self.evaluate(tasks_path=tasks)

    def test_gemini_embedded_level_is_one_random_level_model(self):
        for level in ("medium", "high"):
            self.options["options"].append({"id": "gem38-" + level,
                "model": "gemini-3.8-flash-" + level, "level": level, "pool": "agy"})
        summary = self.evaluate()
        name = "random-level:gemini-3.8-flash"
        self.assertIn(name, summary["policies"])
        self.assertNotIn(name + "-medium", summary["policies"])
        self.assertNotIn(name + "-high", summary["policies"])
        self.assertEqual(summary["policies"][name]["choices"],
            {task: evaluator.random_choice(7, name, task, ["gem38-high", "gem38-medium"])
             for task in sorted(self.ids["heldout"])})

    def test_standalone_svg_and_cli_preserve_legacy_command_shape(self):
        summary = self.evaluate()
        svg = ElementTree.fromstring(evaluator.cost_quality_svg(summary))
        self.assertTrue(svg.tag.endswith("svg"))
        self.assertTrue(any(e.tag.endswith("polyline") for e in svg.iter()))
        argv = [sys.executable, str(Path(evaluator.__file__)), *map(str, self.save()),
                "--policies", str(self.root / "policies.json"), "--split", "heldout", "--prereg",
                "--resamples", "20", "--json-out", str(self.root / "summary.json"),
                "--svg-out", str(self.root / "curve.svg")]
        result = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Coverage (measured / declared held-out)", result.stdout)
        self.assertEqual(json.loads((self.root / "summary.json").read_text())["bootstrap"]["seed"], 7)
        ElementTree.parse(self.root / "curve.svg")


if __name__ == "__main__":
    unittest.main()
