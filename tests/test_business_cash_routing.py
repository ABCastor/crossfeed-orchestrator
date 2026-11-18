"""Cash versus quota at the selection, brief, dry-run and receipt boundaries."""
import argparse
import contextlib
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
import run_identity
import selector


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

    def select(self, **kwargs):
        return selector.select_option(self.roster, {}, self.state, kwargs.pop("role", "review"),
                                      fleet=fleetctl, **kwargs)

    def included(self):
        return self.select(allow="claude:claude-sonnet-5-5:high")

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
