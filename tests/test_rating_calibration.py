"""Probability and subscription price priors are planning assumptions, not observed rates."""
import unittest

from scripts import rating


class LiveCalibration(unittest.TestCase):
    # BR-SELECT-003
    def test_ordinary_prior_keeps_pass_rate_and_missing_is_neutral(self):
        for rate in (.4, .5, .6):
            self.assertEqual(rating.success_prior(rate), rate)
            self.assertLess(rating.success_prior(rate, "hard"), rate)
            self.assertGreater(rating.success_prior(rate, "easy"), rate)
        self.assertEqual(rating.success_prior(None), .5)

    # BR-SELECT-005
    def test_subscription_price_uses_window_value_and_scarcity(self):
        price = lambda capacity: rating.subscription_price(10, capacity, .25, window_minutes=10080)
        self.assertAlmostEqual(price(2), .25)
        self.assertAlmostEqual(price(4), .50)
        self.assertAlmostEqual(price(40), 5)
        self.assertAlmostEqual(rating.subscription_price(10, 4, .25, window_minutes=5040, scarcity=.75),
                               .40)
        self.assertIsNone(price(None))
        self.assertIsNone(rating.subscription_price(None, 46, 0, window_minutes=10080))


if __name__ == "__main__":
    unittest.main()
