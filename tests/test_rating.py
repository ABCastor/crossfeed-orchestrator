import unittest

from scripts.rating import (ASTRA, OPUS, Check, Offer, Route, Task, choose, quota_price, success, update)


class RatingTests(unittest.TestCase):
    def setUp(self):
        self.cheap = Offer(Route("cheap", "low", "subscription-a"), 1, funding="included")
        self.strong = Offer(Route("strong", "high", "subscription-b"), 3, 0.95, "included")
        self.routine = Task("coding", "ordinary", "routine")
        self.critical = Task("coding", "ordinary", "critical")

    def test_stakes_trades_price_for_expected_success(self):
        self.assertEqual(choose([self.strong, self.cheap], self.routine, 1, {}).first, self.cheap.route)
        self.assertEqual(choose([self.cheap, self.strong], self.critical, 1, {}).first, self.strong.route)

    def test_evidence_is_weak_separate_and_bounded(self):
        self.assertAlmostEqual(success(self.cheap, self.routine, {}), 0.5)
        self.assertLess(success(self.strong, self.routine, {}), 0.7)
        original = {}
        corrected = update(self.cheap, self.routine, original, True)
        self.assertEqual(original, {})
        self.assertAlmostEqual(success(self.cheap, self.routine, corrected), 0.55, places=2)
        self.assertLess(success(self.cheap, self.routine, update(self.cheap, self.routine, {}, False)), 0.5)
        for _ in range(200):
            corrected = update(self.cheap, self.routine, corrected, True)
        self.assertLess(success(self.cheap, self.routine, corrected), 0.9)
        self.assertEqual(success(self.cheap, Task("review", "ordinary", "routine"), corrected), 0.5)
        self.assertEqual(success(Offer(Route("cheap", "max", "subscription-a"), 1), self.routine, corrected), 0.5)
        self.assertEqual(success(Offer(Route("cheap", "low", "subscription-b"), 1), self.routine, corrected), 0.5)
        self.assertGreater(success(self.cheap, Task("coding", "easy", "routine"), {}), 0.5)
        self.assertLess(success(self.cheap, Task("coding", "hard", "routine"), {}), 0.5)
        with self.assertRaises(ValueError):
            update(self.cheap, self.routine, {}, 0)

    def test_hard_preference_uses_admitted_opus_or_explicit_astra_fallback(self):
        opus = Offer(Route(OPUS, "low", "claude"), 2)
        astra = Offer(Route(ASTRA, "high", "codex"), 1)
        sonnet = Offer(Route("claude-sonnet-5", "high", "claude"), 0)
        offers = [opus, astra, sonnet]
        self.assertEqual(choose(offers, Task("coding", "hard", "routine", "opus"), 1, {}).first, opus.route)
        self.assertEqual(choose(offers, Task("coding", "hard", "routine", "astra"), 1, {}).first, astra.route)
        self.assertIsNone(choose([astra, sonnet], Task("coding", "hard", "routine", "opus"), 1, {}))
        self.assertIsNone(choose(offers, Task("coding", "hard", "critical"), 1, {}))

    def test_checked_escalation_values_the_full_plan(self):
        pair = self.cheap.route, self.strong.route
        plan = choose([self.cheap, self.strong], self.critical, 1, {}, Check(0.2, {pair: 0.7}))
        self.assertEqual((plan.first, plan.fallback), pair)
        self.assertAlmostEqual(plan.price, 2.8)
        self.assertAlmostEqual(plan.success, 0.85)
        self.assertAlmostEqual(plan.loss, 5.8)
        for conditional in ({}, {pair: 0.05}, {pair: True}, {pair: float("nan")}):
            self.assertIsNone(choose([self.cheap, self.strong], self.critical, 1, {},
                                     Check(0.2, conditional)).fallback)
        paid = Offer(self.strong.route, self.strong.price, self.strong.benchmark_grade, "metered")
        self.assertIsNone(choose([self.cheap, paid], self.critical, 1, {}, Check(0.2, {pair: 0.99})).fallback)

    def test_unpriced_abstains_and_quota_uses_window_and_pace(self):
        self.assertAlmostEqual(quota_price(5, 40, 0.5), 3)
        self.assertAlmostEqual(quota_price(5, 40, 0.5, window_minutes=5040), 1.5)
        for inputs in ((5, None, 0.5), (5, 40, float("inf")), (False, 40, 0.5), (-5, 40, 0.5), (101, 40, 0.5)):
            self.assertIsNone(quota_price(*inputs))
        self.assertIsNone(choose([self.cheap], self.routine, None, {}))
        self.assertIsNone(choose([self.cheap, Offer(self.strong.route, None)], self.routine, 1, {}))
        self.assertIsNone(choose([], self.routine, 1, {}))


if __name__ == "__main__":
    unittest.main()
