"""BR-SUBS-001 and BR-SUBS-002: plan prices come from the access overlay; the operator's own value wins."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts import sharing, subscriptions
from tests.test_console import console

ROSTER = {"schema_version": 3, "quota_pools": {
    "claude": {"plan": {"billing": "subscription", "name": "Claude Max 20x", "price": 183, "currency": "GBP",
                        "as_of": "2026-09", "price_source": "his receipt"}},
    "codex": {"plan": {"billing": "subscription", "name": "ChatGPT Pro, shared with Edo", "price": 200,
                       "currency": "USD", "as_of": "2026-09", "price_source": "the 200-a-month plan, shared with Edo"}},
    "antigravity-gemini": {"plan": {"billing": "subscription", "name": "Google AI Pro (second account, promo)",
                                    "price": 5.5, "currency": "EUR", "as_of": "2026-10",
                                    "price_source": "read as EUR 5.50/month for the first three months"}},
    "github-copilot-student": {"plan": {"billing": "free", "name": "Copilot Student", "price": 0, "currency": "USD"}},
    "bare": {"plan": {"billing": "subscription", "name": "No price"}},
}, "lanes": []}


class PlanPrices(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "subscriptions.json"  # never created

    def rows(self):
        return {r["id"]: r for r in subscriptions.view(self.path, ROSTER, {})["subscriptions"]}

    # BR-SUBS-001
    def test_prices_come_from_the_plan_when_the_operator_set_none(self):
        rows = self.rows()
        self.assertEqual([(rows[k]["amount"], rows[k]["currency"], rows[k]["period"], rows[k]["source"])
                          for k in ("claude", "codex", "antigravity")],
                         [("183", "GBP", "monthly", "default"), ("200", "USD", "monthly", "default"),
                          ("5.50", "EUR", "monthly", "default")])
        self.assertIsNone(rows["bare"]["amount"])  # no price in the overlay: nothing is invented
        view = subscriptions.view(self.path, ROSTER, {})
        self.assertEqual(view["unpriced"], 1)
        self.assertFalse(self.path.exists())
        self.assertEqual(view["free"], [{"id": "github-copilot-student", "name": "Copilot Student"}])
        self.assertEqual(console.plan_summary(rows["claude"]), "£183 a month")

    # BR-SUBS-001
    def test_an_operator_edit_wins_and_is_saved_alone(self):
        subscriptions.save(self.path, ROSTER, "claude", {"amount": "150", "currency": "gbp", "period": "monthly"})
        rows = self.rows()
        self.assertEqual((rows["claude"]["amount"], rows["claude"]["source"]), ("150", "operator"))
        self.assertEqual(rows["codex"]["source"], "default")
        saved = {r["id"]: r for r in json.loads(self.path.read_text())["subscriptions"]}
        self.assertIsNone(saved["codex"]["amount"], "defaults are not copied into the private file")
        # control: the probe sees an operator value when one exists, and a default re-entered stays a default
        subscriptions.save(self.path, ROSTER, "codex", {"amount": "200", "currency": "USD", "period": "monthly"})
        self.assertEqual(self.rows()["codex"]["source"], "default")

    # BR-SUBS-002
    def test_chatgpt_pro_is_assumed_half_with_edo_until_the_operator_sets_it(self):
        row = self.rows()["codex"]
        self.assertEqual((row["operator_share_percent"], row["other_payer"], row["share_source"]), ("50", "Edo", "default"))
        self.assertEqual(console.plan_summary(row), "$200 a month · shared with Edo · assumed 50/50 · change")
        subscriptions.save(self.path, ROSTER, "codex", {"operator_share_percent": "60", "other_payer": "Edo"})
        row = self.rows()["codex"]
        self.assertEqual((row["operator_share_percent"], row["share_source"]), ("60", "operator"))
        self.assertEqual(console.plan_summary(row), "$200 a month · shared with Edo · 60/40")

    def test_edo_card_shows_his_share_and_names_the_assumption(self):
        model = sharing.build(subscriptions.view(self.path, ROSTER, {}), {"pools": []}, 1.7e9)
        text = sharing.card_text(model, "edo")
        self.assertIn("Your share 50% of $200.00 a month: $100.00", text)
        self.assertIn("The split is assumed until it is agreed", text)
        self.assertIn("never split by person", text)

    def test_promo_shows_its_end_when_known_and_plain_promo_otherwise(self):
        row = self.rows()["antigravity"]
        self.assertEqual(console.plan_summary(row), "€5.50 a month · promo until Jan 2027")
        self.assertEqual(console.plan_summary({**row, "promo_until": None}), "€5.50 a month · promo")
        self.assertEqual(subscriptions._promo({"name": "X promo", "promo_until": "2027-02-14"}), (True, "2027-02"))
        self.assertEqual(subscriptions._promo({"name": "Plain", "price_source": "three months"}), (False, None))

    def test_free_plan_reads_free(self):
        self.assertEqual(console.plan_summary({"amount": "0.00", "currency": "USD", "period": "monthly"}), "Free")


if __name__ == "__main__":
    unittest.main()
