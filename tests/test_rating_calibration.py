"""Probability and subscription price priors are planning assumptions, not observed rates."""
import unittest

from scripts import rating


class LiveCalibration(unittest.TestCase):
    def test_anchor_is_ordinary_route_not_a_raw_grade(self):
        self.assertAlmostEqual(rating.success_prior(.5), .825)
        self.assertAlmostEqual(rating.success_prior(None), .825)
        self.assertLess(rating.success_prior(.4), rating.success_prior(.5))
        self.assertGreater(rating.success_prior(.6), rating.success_prior(.5))
        self.assertLess(rating.success_prior(.6) - rating.success_prior(.4), .04)
        self.assertLess(rating.success_prior(.5, "hard"), .825)
        self.assertGreater(rating.success_prior(.5, "easy"), .825)

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
