"""Per-lane estimates use the previous measured input, including cached tokens."""
import unittest

from model_gateway.chatgpt.estimate import Estimator


class EstimateTest(unittest.TestCase):
    def test_initial_estimate_and_images(self):
        lane = Estimator()
        self.assertEqual(lane.estimate(100, 2), 4025)
        self.assertEqual(lane.estimate(1), 1)
        self.assertEqual(lane.estimate(0), 0)

    def test_latest_observation_and_lane_isolation(self):
        first, second = Estimator(), Estimator()
        first.observe(100, 50)
        self.assertEqual(first.estimate(200), 100)
        self.assertEqual(second.estimate(200), 50)
        first.observe(100, 10)
        self.assertEqual(first.estimate(200, 1), 2020)
        first.observe(0, 90)
        self.assertEqual(first.estimate(200), 20)
        first.observe(100, 0)
        self.assertEqual(first.estimate(200), 0)

    def test_invalid_counts_are_rejected(self):
        for method, args in ((Estimator().estimate, (-1,)),
                             (Estimator().estimate, (2, -1)),
                             (Estimator().observe, (2, -1))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                method(*args)
