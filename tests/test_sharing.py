"""sharing: what each person owes and what was measured, with every gap named rather than guessed."""
import datetime as dt
import unittest

from scripts import sharing

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.timezone.utc).timestamp()
RESET = NOW + 3 * 86400
START = RESET - 7 * 86400


def entry(key, name, monthly, share, other="Sam", currency="EUR", pools=None, value=None):
    return {"id": key, "name": name, "amount": monthly, "monthly": monthly, "currency": currency, "period": "monthly",
            "operator_share_percent": share, "other_payer": other, "pools": pools or [key],
            "pool_value": {key: {"used_usd_equivalent": value, "capacity_usd_equivalent": "65.00", "source": "fixture"}}
            if value else {}}


def weekly(percent, reset=RESET):
    iso = dt.datetime.fromtimestamp(reset, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"kind": "window", "minutes": 10080, "scope": None, "used_percent": percent, "reset_at": iso, "state": "x"}]


def hours(*pairs):  # (hours after window start, usd)
    return [[int((START + hour * 3600) // 3600), 100, usd] for hour, usd in pairs]


def overview(**pools):
    return {"pools": [{"pool": key, **value} for key, value in pools.items()]}


class Sharing(unittest.TestCase):
    def build(self, entries, pools):
        return sharing.build({"subscriptions": entries}, overview(**pools), NOW)

    def test_measured_here_against_the_provider_total_and_the_remainder(self):
        model = self.build([entry("claude", "Claude Max", "100.00", "50", value="16.90")],
                           {"claude": {"limits": weekly(26), "usage_cost": {"hourly": hours((5, 4.0), (30, 5.8)),
                                                                            "last_7d_usd": 9.8, "stale": False}}})
        use = model["plans"][0]["use"]
        self.assertAlmostEqual(use["measured_usd"], 9.8)
        self.assertAlmostEqual(use["rest_usd"], 7.1)
        self.assertAlmostEqual(use["measured_share"], 58.0, places=0)
        text = sharing.card_text(model, "sam")
        self.assertIn("½ of €100.00 a month · yours €50.00", text)
        self.assertIn("The account used 26% of its weekly allowance, about $16.90 at API list prices", text)
        self.assertIn("Measured on my Mac since the weekly reset on", text)
        self.assertIn("$9.80", text)
        self.assertIn("Not measured on my Mac: about $7.10 (yours, anyone else's, or mine on other devices and the web)", text)
        self.assertIn("You owe €50.00 a month", text)
        self.assertIn("never split by person", text)
        mine = sharing.card_text(model, "me")
        self.assertIn("I owe €50.00 a month", mine)
        self.assertIn("Not measured on this Mac: about $7.10 (other people, or my other devices and the web)", mine)

    def test_only_use_inside_the_weekly_window_counts(self):
        old = [[int((START - 7200) // 3600), 1, 50.0]]
        model = self.build([entry("claude", "Claude", "10", "50", value="5")],
                           {"claude": {"limits": weekly(10), "usage_cost": {"hourly": old + hours((1, 2.0)), "last_7d_usd": 2.0}}})
        self.assertAlmostEqual(model["plans"][0]["use"]["measured_usd"], 2.0)

    def test_an_unpriced_model_makes_the_figure_a_floor_and_the_rest_a_ceiling(self):
        model = self.build([entry("claude", "Claude", "10", "50", value="5")],
                           {"claude": {"limits": weekly(10), "usage_cost": {"hourly": hours((1, 2.0)), "last_7d_usd": None}}})
        text = sharing.card_text(model, "sam")
        self.assertIn("at least $2.00", text)
        self.assertIn("at most $3.00", text)

    def test_measured_above_the_provider_total_says_the_conversion_is_rough_and_never_goes_negative(self):
        model = self.build([entry("claude", "Claude", "10", "50", value="5")],
                           {"claude": {"limits": weekly(10), "usage_cost": {"hourly": hours((1, 8.0)), "last_7d_usd": 8.0}}})
        use = model["plans"][0]["use"]
        self.assertEqual(use["rest_usd"], 0.0)
        self.assertTrue(use["overshoot"])
        self.assertIn("only approximate", sharing.card_text(model, "sam"))

    def test_without_a_dollar_value_or_a_usage_log_it_says_so_and_invents_nothing(self):
        model = self.build([entry("opencode-go", "OpenCode Go", "10", "100", other=None),
                            entry("claude", "Claude", "20", "50")],
                           {"opencode-go": {"limits": weekly(8)},
                            "claude": {"limits": weekly(20), "usage_cost": {"hourly": hours((1, 2.0)), "last_7d_usd": 2.0}}})
        go, claude = model["plans"]
        self.assertIn("no usage log on this Mac", go["use"]["reason"])
        self.assertIsNone(go["use"]["rest_usd"])
        self.assertIsNone(claude["use"]["total_usd"])
        text = sharing.card_text(model, "sam")
        self.assertNotIn("Not measured on my Mac", text)       # no dollar total, so no remainder
        self.assertIn("Measured on my Mac", text)
        self.assertNotIn("OpenCode Go", text)                  # the operator keeps 100%: not on Sam's card

    def test_a_plan_with_several_weekly_allowances_uses_the_rolling_week_and_states_no_account_total(self):
        model = self.build([entry("antigravity", "Google AI Pro", "20", "70", pools=["agy-a", "agy-b"])],
                           {"agy-a": {"limits": weekly(30), "usage_cost": {"hourly": hours((1, 2.0)), "last_7d_usd": 2.5}},
                            "agy-b": {"limits": weekly(60)}})
        use = model["plans"][0]["use"]
        self.assertIsNone(use["percent"])
        self.assertEqual(use["basis"], "in the last 7 days")
        self.assertAlmostEqual(use["measured_usd"], 2.5)
        text = sharing.card_text(model, "sam")
        self.assertNotIn("weekly allowance", text)
        self.assertIn("in the last 7 days: $2.50", text)

    def test_people_totals_per_currency_and_unsplit_plans_stay_with_the_operator(self):
        model = self.build([entry("a", "A", "100", "50", other="Sam"), entry("b", "B", "30", "40", other="Sam"),
                            entry("c", "C", "10", "50", other="Alex", currency="USD"), entry("d", "D", "5", None)], {})
        self.assertEqual([p["name"] for p in model["people"]], ["Me", "Alex", "Sam"])
        owed = {p["key"]: p["owed"] for p in model["people"]}
        self.assertEqual(owed["sam"], {"EUR": "68.00"})
        self.assertEqual(owed["alex"], {"USD": "5.00"})
        self.assertEqual(owed["me"], {"EUR": "62.00", "USD": "5.00"})   # d has no share, so it adds nothing to "owed"
        self.assertIn("Share not set", sharing.card_text(model, "me"))

    def test_unpriced_unavailable_and_unknown_person(self):
        self.assertEqual(sharing.build({"unavailable": True}, {}, NOW)["people"], [])
        self.assertEqual(self.build([{**entry("x", "X", None, None), "amount": None, "monthly": None}], {})["people"], [])
        model = self.build([entry("a", "A", "10", "50")], {})
        self.assertEqual(sharing.card_lines(model, "nobody"), [])

    def test_money_and_names_are_formatted_for_a_message(self):
        self.assertEqual(sharing.money("EUR", "1234.5"), "€1,234.50")
        self.assertEqual(sharing.money("CHF", "5"), "CHF 5.00")
        self.assertEqual(sharing.slug("Sam B."), "sam-b")
        self.assertEqual(sharing.usd(0.0004), "$0.0004")


if __name__ == "__main__":
    unittest.main()
