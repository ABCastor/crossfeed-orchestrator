"""Cash versus quota at the selection, brief, dry-run and receipt boundaries."""
import argparse
import contextlib
import copy
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fleetctl
import openrouter_budget
import run_identity
import selector
from tests.cash_fixtures import ACCOUNT, configure, seed


class CashVisibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cash-routing-")
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.roster = json.loads((ROOT / "tests/fixtures/access-overlay.test.json").read_text())
        self.overlay = self.state / "overlay.json"
        self.roster["quota_pools"]["claude"]["funding"] = "included"
        self.roster["model_cards"]["claude-sonnet-5-5"]["price_1m"] = {"input": 3, "output": 15}
        evidence = self.state / "evidence"
        evidence.mkdir()
        (evidence / "levels.json").write_text(json.dumps({"rows": [{
            "model_key": "claude-sonnet-5-5", "level": "high",
            "q": {"review": {"mean": .8, "sd": .01, "n_sources": 1}},
            "tokens_per_task": {"in": 2000, "out": 1000},
        }]}))
        self.env = patch.dict(os.environ, {"FLEET_QUOTA_POLICY": "off", "FLEET_IGNORE_QUOTA": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def select(self, runtime=None, **kwargs):
        return selector.select_option(self.roster, runtime or {}, self.state, kwargs.pop("role", "review"),
                                      fleet=fleetctl, **kwargs)

    def included(self):
        return self.select(allow="claude:claude-sonnet-5-5:high")

    def paid_fixture(self):
        configure(self.roster, "http://127.0.0.1:9/v1")
        self.ledger = self.state / "cash-budget.json"
        seed(self.ledger)
        patcher = patch.object(openrouter_budget, "ledger_path", return_value=self.ledger)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.roster["policy"]["selector"].update(lambda0=0, lambda_unknown=0, explore=1)
        self.roster["policy"]["selector"].pop("allowed_pools_by_role", None)
        self.route("other", "1000")
        self.choices = "claude:claude-sonnet-5-5:high,fixture-other:fixture-other:*"
        self.evidence_rows(other=.81)

    def route(self, key, rate):
        template = next(lane for lane in self.roster["lanes"] if lane["lane_id"] == "openrouter-mimo-v2.6-flash")
        lane = copy.deepcopy(template)
        lane.pop("paid_mimo")
        ident = "fixture-" + key
        lane.update(lane_id=ident, model_key=ident, provider="fixture", selector="fixture/" + key, quota_pool=ident)
        lane["paid_route"].update(model=lane["selector"], quality_floors={"reasoning": .65})
        lane["paid_route"]["tariff"].update(input=rate, output=rate)
        self.roster["quota_pools"][ident] = {"funding": "metered", "plan": {"limit": "none-known"}}
        self.roster["effort"][ident] = copy.deepcopy(self.roster["effort"][template["model_key"]])
        self.roster["lanes"].append(lane)
        return lane

    def evidence_rows(self, included=.8, codex=.8, **paid):
        def row(model, level, q):
            return {"model_key": model, "level": level, "latency_s": 1,
                    "q": {"reasoning": {"mean": q, "sd": .01, "n_sources": 1}},
                    "tokens_per_task": {"in": 10, "out": 10}}
        rows = [row("claude-sonnet-5-5", "high", included), row("gpt-6.1-sol", "high", codex)]
        rows += [row("fixture-" + key, "service-chosen", quality) for key, quality in paid.items()]
        (self.state / "evidence/levels.json").write_text(json.dumps({"rows": rows}))

    def reasoning(self, allow, runtime=None, **kwargs):
        return self.select(runtime, role="hard-reasoning", data_class="public",
                           prompt="Return PONG", allow=allow, **kwargs)

    # BR-CASH-004
    def test_cap_and_allow_are_not_permission_to_auto_select(self):
        self.paid_fixture()
        for permission in (None, False):
            if permission is not None:
                self.roster["cash_accounts"][ACCOUNT]["automatic"] = permission
            result = self.reasoning(self.choices)
            self.assertEqual(result["choice"]["pool"], "claude")
            self.assertNotIn("fixture-other", [row["pool"] for row in result["top3"]])
            self.assertIn("automatic selection", " ".join(result["rejected"]))
        self.roster["cash_accounts"][ACCOUNT]["automatic"] = True
        self.evidence_rows(included=.6, other=.81)
        result = self.reasoning(self.choices)
        self.assertEqual(result["choice"]["pool"], "fixture-other")
        self.assertEqual(result["choice"]["cash"]["worst_case_usd"], .2)
        self.assertAlmostEqual(result["choice"]["cash"]["estimate_usd"], .02)
        budget = openrouter_budget.Budget(ACCOUNT, self.roster["cash_accounts"][ACCOUNT])
        token = budget.reserve("fixture/other", "5")
        budget.settle(token, "5")
        result = self.reasoning(self.choices)
        self.assertEqual(result["choice"]["pool"], "claude")
        self.assertIn("cap", " ".join(result["rejected"]))

    # BR-CASH-004
    def test_equal_quality_and_score_band_prefers_expiring_included_then_cash(self):
        self.paid_fixture()
        self.roster["cash_accounts"][ACCOUNT]["automatic"] = True
        self.route("cheap", "500")
        self.evidence_rows(other=.81, cheap=.8)
        now = fleetctl.utc_now()
        runtime = {"quota_snapshots": {"claude": {"observed_at": fleetctl.iso(now), "windows": {
            "primary": {"used_percent": 10, "window_minutes": 300,
                        "reset_at": fleetctl.iso(now + dt.timedelta(minutes=30))}}}}}
        allow = self.choices + ",fixture-cheap:fixture-cheap:*,codex:gpt-6.1-sol:high"
        with patch.object(fleetctl, "utc_now", return_value=now):
            result = self.reasoning(allow, runtime)
        self.assertEqual(result["choice"]["pool"], "claude")
        self.assertEqual(result["cash_preference"], "expiring-included-quota")
        result = self.reasoning("fixture-other:fixture-other:*,fixture-cheap:fixture-cheap:*")
        self.assertEqual(result["choice"]["pool"], "fixture-cheap")
        self.assertEqual(result["cash_preference"], "lower-marginal-cash")
        self.assertFalse(json.loads(self.ledger.read_text())["accounts"][ACCOUNT]["calls"])

    # BR-CASH-004
    def test_paid_quality_must_be_measured_and_at_the_route_floor_never_explored(self):
        self.paid_fixture()
        self.roster["cash_accounts"][ACCOUNT]["automatic"] = True
        for measure in ({}, {"other": .64}):
            with self.subTest(measure=measure):
                self.evidence_rows(**measure)
                with self.assertRaisesRegex(fleetctl.FleetError, "paid quality"):
                    self.reasoning("fixture-other:fixture-other:*", stakes="low")
        self.evidence_rows(other=.65)
        result = self.reasoning("fixture-other:fixture-other:*", stakes="low")
        self.assertEqual(result["choice"]["pool"], "fixture-other")
        self.assertFalse(result["exploration"])

    # BR-CASH-001
    def test_normalized_rate_is_api_equivalent_not_incremental_cash(self):
        pick = self.included()
        choice = pick["choice"]
        self.assertAlmostEqual(choice["cost"]["estimated_usd"], .021)
        self.assertEqual(choice["cash"]["funding"], "included")
        self.assertEqual(choice["cash"]["estimate_usd"], 0)
        self.assertEqual(choice["cash"]["worst_case_usd"], 0)
        self.assertEqual(pick["top3"][0]["cash"], choice["cash"])
        payload = json.loads(Path(choice["selection_file"]).read_text())
        self.assertEqual(payload["choice"]["cash"], choice["cash"])
        meta = run_identity.selection_metadata(payload, model=choice["model_key"],
                                               effort="high", pool="claude", harness="claude")
        self.assertEqual(meta["selection"]["top3"][0]["cash"], choice["cash"])

    # BR-CASH-001
    def test_ambiguous_or_invalid_tariff_never_looks_free(self):
        self.roster["model_cards"]["claude-sonnet-5-5"]["price_1m"] = {
            "input": 0, "output": 0, "in": 4, "out": 15,
        }
        self.assertIsNone(self.included()["choice"]["cost"]["estimated_usd"])
        pro = next(lane for lane in self.roster["lanes"] if lane["lane_id"] == "openrouter-mimo-v2.6-pro")
        pro["price_1m"] = {"input": "bogus", "output": 0}
        pick = self.select(role="hard-reasoning", data_class="public",
                           allow="claude:claude-sonnet-5-5:high,openrouter-mimo:mimo-v2.6-pro:*")
        self.assertNotIn("openrouter-mimo", [option["pool"] for option in pick["top3"]])
        self.assertIn("invalid tariff", " ".join(pick["rejected"]))
        self.assertIsNone(pick["pool_cash"]["openrouter-mimo"]["estimate_usd"])
        self.assertIsNone(pick["pool_cash"]["openrouter-mimo"]["worst_case_usd"])

    # BR-CASH-001
    def test_brief_and_dispatch_dry_run_report_cash_separately(self):
        brief = fleetctl.fleet_overview(self.roster, {}, self.state)
        text = fleetctl.render_brief(brief)
        self.assertIn("Cash (USD", text)
        self.assertIn("claude: included, estimated 0", text)
        self.assertIn("gemini-metered: metered, estimated unknown", text)
        self.assertIn("estimate-only", text)
        self.overlay.write_text(json.dumps(self.roster))
        selection = self.included()
        output, error = io.StringIO(), io.StringIO()
        args = argparse.Namespace(dir=self.state, last=None, overlay=self.overlay, mode="read-only",
                                  dry_run=True, data_class=None)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(fleetctl.dispatch_selection(selection, args, self.state, "fixture task"), 0)
        self.assertIn("cash included", error.getvalue())
        self.assertEqual(json.loads(output.getvalue())["cash"], selection["choice"]["cash"])
        self.assertFalse((self.state / "dispatches").exists())


if __name__ == "__main__":
    unittest.main()
