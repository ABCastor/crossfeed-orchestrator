"""Round-two regressions, with synthetic data and no provider calls."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from tests import test_selector as fixtures
from tests import test_evidence as ef

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evidence
import fleetctl
import run_identity
import selector


class EvidenceRound2(unittest.TestCase):
    def test_disabled_metric_cannot_impute_known_quality(self):
        overlay = {"effort": {m: {"levels": {"codex": ["high"]}} for m in ("a", "b", "c", "missing")},
                   "policy": {"evidence": {"sources": {"aa_coding": {"weight": 0, "assumption": "Disabled predictor."}}}}}
        catalog = {"artificial_analysis": {}}
        for model, terminal, coding in (("a", .2, 20), ("b", .4, 40), ("c", .6, 60), ("missing", None, 80)):
            values = {"artificial_analysis_coding_index": coding}
            if terminal is not None:
                values["terminalbench_v4_0"] = terminal
            catalog["artificial_analysis"][model] = {"levels": {"high": {"effort": "high", "evaluations": values}}}
        row = next(r for r in evidence.build_evidence(overlay, catalog, [], ef.READ_ON)["rows"] if r["model_key"] == "missing")
        self.assertTrue(row["q"]["coding-agent"]["unknown"], "zero-weight metrics must not turn unknown quality into known evidence")
        self.assertFalse(row["q"]["coding-agent"]["imputed"])

    def test_direct_unverified_and_inactive_cards_do_not_affect_ranks(self):
        for field, value in (("access_status", "unverified"), ("admission_status", "inactive")):
            with self.subTest(field=field):
                overlay = {"quota_pools": {"codex": {}}, "model_cards": {
                    "a": {"pool": "codex"}, "b": {"pool": "codex"},
                    "blocked": {"pool": "codex", field: value}}, "lanes": [],
                    "effort": {m: {"levels": {"codex": ["high"]}} for m in ("a", "b", "blocked")}}
                catalog = {"artificial_analysis": {m: {"levels": {"high": {
                    "effort": "high", "evaluations": {"terminalbench_v4_0": score}}}}
                    for m, score in (("a", .2), ("b", .4), ("blocked", .99))}}
                row = next(r for r in evidence.build_evidence(overlay, catalog, [], ef.READ_ON)["rows"] if r["model_key"] == "a")
                self.assertAlmostEqual(row["q"]["coding-agent"]["mean"], .5)

    def test_full_roster_excludes_effort_only_orphan_from_percentiles(self):
        overlay = {"quota_pools": {"codex": {}}, "model_cards": {
            "model-a": {"pool": "codex"}, "model-b": {"pool": "codex"}}, "lanes": [],
            "effort": {m: {"levels": {"codex": ["high"]}} for m in ("model-a", "model-b", "orphan")}}
        catalog = {"artificial_analysis": {m: {"levels": {"high": {
            "effort": "high", "evaluations": {"terminalbench_v4_0": score}}}}
            for m, score in (("model-a", .2), ("model-b", .4), ("orphan", .99))}}
        rows = {r["model_key"]: r for r in evidence.build_evidence(overlay, catalog, [], ef.READ_ON)["rows"]}
        self.assertAlmostEqual(rows["model-a"]["q"]["coding-agent"]["mean"], .5)
        self.assertAlmostEqual(rows["model-b"]["q"]["coding-agent"]["mean"], 1, places=5)

    def test_declared_snapshot_stem_rejects_preview_with_same_date(self):
        declared = {"model_cards": {"synthetic-model": {"served_snapshot": "synthetic-model-20260813-high"}}}
        served = {"slug": "synthetic-model-20260813-high", "release_date": "2026-08-13"}
        preview = {"slug": "synthetic-model-preview-20260813-high", "release_date": "2026-09-15"}
        self.assertEqual(evidence.current_aa_variants([served, preview], declared, "synthetic-model"), [served])
        low = dict(served, slug="synthetic-model-20260813-low")
        self.assertEqual(evidence.current_aa_variants([low], declared, "synthetic-model"), [low])

    def test_first_answer_alias_survives_null_primary_field(self):
        records = [{"model": "synthetic-model", "effort": "high", "ttfa_s": None,
                    "time_to_first_answer_s": 7, "duration_ms": 900000}]
        self.assertEqual(ef.cell(evidence.build_evidence(ef.overlay(), ef.catalog(), records, ef.READ_ON))["latency_s"], 7)
        catalog = ef.catalog()
        catalog["artificial_analysis"]["synthetic-model"]["levels"]["high"].update(
            ttfa_s=None, median_time_to_first_answer_token=17)
        self.assertEqual(ef.cell(evidence.build_evidence(ef.overlay(), catalog, [], ef.READ_ON))["latency_s"], 17)

    def test_snapshot_filter_and_latest_release_per_level(self):
        rows = [{"id": slug, "slug": slug, "release_date": date,
                 "evaluations": {"terminalbench_v4_0": score}}
                for slug, date, score in [
                    ("synthetic-model-0424-high", "2026-04-24", .99),
                    ("synthetic-model-high", "2026-09-01", .7),
                    ("synthetic-model-high", "2026-10-01", .6),
                    ("synthetic-model-20260813-low", "2026-08-13", .9)]]
        with mock.patch.dict(os.environ, {"AA_API_KEY": "fixture"}), \
             mock.patch.object(ef.market.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"data": rows}).encode())):
            result = ef.market.fetch_artificial_analysis({"synthetic-model": ["syntheticmodel"]})
        high = result["synthetic-model"]["levels"]["high"]
        self.assertEqual(high.get("release_date"), "2026-10-01", "use latest served release, not ambiguous old snapshots")
        self.assertNotIn("low", result["synthetic-model"]["levels"], "undeclared snapshot is not served")
        self.assertEqual(len(result["synthetic-model"]["variants"]), 4, "retain raw variants for audit")
        declared = ef.overlay()
        declared["model_cards"] = {"synthetic-model": {"served_snapshot": "0424"}}
        mapped = evidence.build_evidence(declared, {"artificial_analysis": result}, [], ef.READ_ON)
        self.assertEqual(ef.cell(mapped)["sources"][0]["release_date"], "2026-04-24")

    def test_anchor_percentile_imputation_and_current_population(self):
        overlay = {"effort": {}, "lanes": []}
        catalog = {"fetched_at": ef.READ_ON, "artificial_analysis": {}}
        metrics = [("a", .2, 20), ("b", .4, 40), ("c", .6, 60), ("missing", None, 30), ("unadmitted", .99, 99)]
        for name, terminal, coding in metrics:
            if name != "unadmitted":
                overlay["effort"][name] = {"levels": {"codex": ["high"]}}
            values = {"artificial_analysis_coding_index": coding, "tau2": .99,
                      "artificial_analysis_intelligence_index": 100 - coding, "ifbench": coding / 100}
            if terminal is not None:
                values["terminalbench_v4_0"] = terminal
            catalog["artificial_analysis"][name] = {"levels": {"high": {"effort": "high", "evaluations": values}}}
        result = evidence.build_evidence(overlay, catalog, [], ef.READ_ON)
        rows = {r["model_key"]: r for r in result["rows"]}
        self.assertAlmostEqual(rows["a"]["q"]["coding-agent"]["mean"], 1 / 3, places=5, msg="percentile uses admitted observed anchors")
        q = rows["missing"]["q"]["coding-agent"]
        self.assertAlmostEqual(q["mean"], .5, places=5, msg="OLS from co-observed coding values imputes missing terminal anchor")
        self.assertTrue(q["imputed"])
        self.assertGreaterEqual(q["imputation"]["residual_sd"], .1)
        self.assertGreater(q["sd"], rows["b"]["q"]["coding-agent"]["sd"])
        self.assertEqual(q["anchor_metric"], "terminalbench_v4_0")
        self.assertIn("imputed", rows["missing"]["flags"])
        for family in ("review", "repo-qa"):
            self.assertEqual(rows["a"]["q"][family]["anchor_metric"], "terminalbench_v4_0")
        for family in ("reasoning", "research"):
            self.assertEqual(rows["a"]["q"][family]["anchor_metric"], "artificial_analysis_intelligence_index")
        self.assertEqual(rows["a"]["q"]["extraction"]["anchor_metric"], "ifbench")
        changed = copy.deepcopy(catalog)
        changed["artificial_analysis"]["a"]["levels"]["high"]["evaluations"]["tau2"] = .01
        again = evidence.build_evidence(overlay, changed, [], ef.READ_ON)
        self.assertEqual(rows["a"]["q"]["coding-agent"]["mean"], next(r for r in again["rows"] if r["model_key"] == "a")["q"]["coding-agent"]["mean"])

    def test_ledger_cost_is_pool_scoped_and_ttfa_precedes_duration(self):
        records = [{"run_id": str(i), "model": "synthetic-model", "effort": "high", "pool": pool,
                    "own_cost": {"percent_per_task": cost, "requests_per_task": 2},
                    "ttfa_s": first, "duration_ms": 500000}
                   for i, (pool, cost, first) in enumerate([("codex", .2, 10), ("codex", .4, 30), ("claude", 9, 20)])]
        row = ef.cell(evidence.build_evidence(ef.overlay(), ef.catalog(), records, ef.READ_ON))
        self.assertAlmostEqual(row["own_cost"]["codex"]["percent_per_task"], .3)
        self.assertEqual(row["own_cost"]["claude"]["percent_per_task"], 9)
        self.assertEqual(row["latency_s"], 20)


class SelectorRound2(unittest.TestCase):
    setUp = fixtures.SelectorTests.setUp
    evidence = fixtures.SelectorTests.evidence
    select = fixtures.SelectorTests.select
    only = fixtures.SelectorTests.only
    snapshot = fixtures.SelectorTests.snapshot
    one_level = fixtures.SelectorTests.one_level

    def test_unknown_projection_has_fallback_price(self):
        self.only(["codex"])
        self.one_level("codex")
        self.evidence([fixtures.quality_row("gpt-6-astra")])
        result = self.select()
        self.assertEqual(result["lambdas"]["codex"], .5, "unknown projection must carry a nonzero price")
        self.assertIn("quota_projection_unknown", result["choice"]["flags"])
        self.roster["policy"]["selector"].update(lambda0=4)
        self.assertEqual(self.select()["lambdas"]["codex"], 2)
        self.roster["policy"]["selector"]["lambda_unknown"] = .7
        self.assertEqual(self.select()["lambdas"]["codex"], .7)

    def test_cost_without_allowance_measured_then_relative_proxy(self):
        self.only(["codex"])
        self.one_level("codex")
        rows = [fixtures.quality_row("gpt-6-astra", mean=.9, price=4),
                fixtures.quality_row("gpt-6.1-sol", mean=.8, price=1)]
        rows[0]["own_cost"] = {"codex": {"percent_per_task": 2, "trials": 3}}
        rows[1]["own_cost"] = {"codex": {"percent_per_task": .1, "trials": 3}}
        self.evidence(rows)
        result = self.select(self.snapshot("codex", 95))
        self.assertEqual(result["choice"]["model_key"], "gpt-6.1-sol", "measured percent creates quota pressure without USD allowance")
        self.assertEqual(result["choice"]["cost"]["percent"], .1)
        for row in rows:
            row.pop("own_cost")
        self.evidence(rows)
        result = self.select(self.snapshot("codex", 95))
        self.assertEqual(result["choice"]["model_key"], "gpt-6.1-sol")
        self.assertAlmostEqual(result["choice"]["cost"]["percent"], .2)
        self.assertIn("cost_relative_proxy", result["choice"]["flags"])

    def test_go_cost_uses_binding_request_column_and_measured_requests(self):
        self.only(["opencode-go"])
        self.roster["quota_pools"]["opencode-go"]["request_estimates"] = {"per_5h_week_month": {"deepseek-v4-flash": [100, 500, 2000]}}
        row = fixtures.quality_row("deepseek-v4-flash", "high")
        row["own_cost"] = {"opencode-go": {"requests_per_task": 3}}
        self.evidence([row])
        runtime = self.snapshot("opencode-go", 95)
        window = runtime["quota_snapshots"]["opencode-go"]["windows"].pop("rolling")
        window["window_minutes"] = 10080
        runtime["quota_snapshots"]["opencode-go"]["windows"]["weekly"] = window
        result = self.select(runtime)
        chosen = next(o for o in result["top3"] if o["model_key"] == "deepseek-v4-flash" and o["level"] == "high")
        self.assertAlmostEqual(chosen["cost"]["percent"], .6)
        self.assertEqual(chosen["cost"]["basis"], "go_request_estimate")

    def test_unknown_cost_does_not_reject(self):
        self.only(["codex"])
        self.one_level("codex")
        rows = [fixtures.quality_row("gpt-6-astra", mean=.99), fixtures.quality_row("gpt-6.1-sol", mean=.7)]
        for row in rows:
            row.pop("price_1m")
        self.evidence(rows)
        result = self.select(self.snapshot("codex", 95))
        self.assertEqual(result["choice"]["model_key"], "gpt-6-astra")
        self.assertTrue(result["choice"]["cost"]["unknown"])

    def test_default_latency_penalty_scales_by_stakes(self):
        self.only(["claude"])
        self.roster["effort"]["claude-opus-5-5"]["levels"]["claude"] = ["xhigh", "max"]
        self.evidence([fixtures.quality_row("claude-opus-5-5", "max", mean=.9, latency=462),
                       fixtures.quality_row("claude-opus-5-5", "xhigh", mean=.8, latency=52)])
        normal = self.select()["choice"]
        high = self.select(stakes="high")["choice"]
        irreversible = self.select(stakes="irreversible")["choice"]
        self.assertEqual(normal["level"], "xhigh", "default latency cost rejects small quality gains with long waits")
        self.assertEqual(high["level"], "max")
        self.assertEqual(irreversible["level"], "max")
        self.assertEqual(normal["mu"], .001)
        self.assertEqual(high["mu"], .0005)
        self.assertEqual(irreversible["mu"], 0)

    def test_selector_enforces_older_rule_even_with_stale_switches(self):
        self.only(["codex"])
        self.one_level("codex")
        self.roster["model_cards"]["gpt-5.6-luna"] = {"pool": "codex", "status": "older", "superseded_by": "gpt-6-luna"}
        self.roster["effort"]["gpt-5.6-luna"] = copy.deepcopy(self.roster["effort"]["gpt-6-luna"])
        self.evidence([fixtures.quality_row("gpt-5.6-luna", mean=.99), fixtures.quality_row("gpt-6-luna", mean=.8)])
        switches = {key: True for key in fleetctl.choosable_models(self.roster, "codex")}
        with mock.patch.object(fleetctl, "pool_switches", return_value=switches):
            result = self.select(stakes="irreversible")
            self.assertEqual(result["choice"]["model_key"], "gpt-6-luna", "stale switches cannot admit an unretained older model")
            self.roster["model_cards"]["gpt-5.6-luna"]["older_model_reasons"] = {"codex": {
                "compared_to": "gpt-6-luna", "advantage": "better", "job": "synthetic review",
                "reason": "synthetic measured advantage", "evidence": "fixture"}}
            self.assertEqual(self.select(stakes="irreversible")["choice"]["model_key"], "gpt-5.6-luna")

    def test_low_stakes_exploration_and_unconditional_receipt_probability(self):
        self.only(["codex"])
        self.one_level("codex")
        self.evidence([fixtures.quality_row("gpt-6-astra", mean=.95)])
        options, _ = selector.enumerate_options(self.roster, {}, "review", fleet=fleetctl)
        unknown = [o for o in options if o["model_key"] != "gpt-6-astra"]
        self.assertTrue(unknown)
        with mock.patch("random.random", return_value=0), mock.patch("random.choice", side_effect=lambda seq: seq[-1]):
            result = self.select(stakes="low")
        chosen = result["choice"]
        self.assertTrue(chosen["q"]["unknown"], "low stakes must actually explore unknown options")
        self.assertAlmostEqual(result["selection_probability"], .1 / len(unknown))
        payload = json.loads(Path(chosen["selection_file"]).read_text())
        self.assertAlmostEqual(payload["selection_probability"], .1 / len(unknown))
        metadata = run_identity.selection_metadata(payload, model=chosen["model_key"], effort=chosen["effort"], pool=chosen["pool"], harness=chosen["harness"], role="review")
        self.assertAlmostEqual(metadata["selection"]["selection_probability"], .1 / len(unknown))
        with mock.patch("random.random", return_value=.99):
            exploit = self.select(stakes="low")
        self.assertEqual(exploit["choice"]["model_key"], "gpt-6-astra")
        self.assertAlmostEqual(exploit["selection_probability"], .9)
        for stakes in ("normal", "high", "irreversible"):
            with mock.patch("random.random", side_effect=AssertionError("exploration outside low stakes")):
                result = self.select(stakes=stakes)
            self.assertEqual(result["choice"]["model_key"], "gpt-6-astra")
            self.assertEqual(result["selection_probability"], 1)


if __name__ == "__main__":
    unittest.main()
