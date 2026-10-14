"""Per-lane estimates smooth measured input, including cached tokens, without attachment inflation."""
import unittest

from gpt_gateway.chatgpt.estimate import Estimator


class EstimateTest(unittest.TestCase):
    def test_initial_estimate_and_images(self):
        lane = Estimator()
        self.assertEqual(lane.estimate(100, 2), 4025)
        self.assertEqual(lane.estimate(1), 1)
        self.assertEqual(lane.estimate(0), 0)

    def test_smoothed_observations_and_lane_isolation(self):
        first, second = Estimator(), Estimator()
        first.observe(100, 50)
        self.assertEqual(first.estimate(200), 100)
        self.assertEqual(second.estimate(200), 50)
        first.observe(100, 10)
        self.assertEqual(first.estimate(200, 1), 2060)
        first.observe(0, 90)
        self.assertEqual(first.estimate(200), 60)
        first.observe(100, 0)
        self.assertEqual(first.estimate(200), 30)
        self.assertEqual(second.estimate(200), 50)

    def test_attachment_allowance_is_not_learned_as_text_ratio(self):
        lane = Estimator()
        self.assertEqual(lane.estimate(100, 2), 4025)
        lane.observe(100, 4050, 2)  # image and PDF allowances, plus 50 text tokens
        self.assertEqual(lane.estimate(200, 1), 2100)
        lane.observe(100, 2000, 1)  # attachment-only usage cannot push the ratio negative
        self.assertEqual(lane.estimate(200), 50)

    def test_invalid_counts_are_rejected(self):
        for method, args in ((Estimator().estimate, (-1,)),
                             (Estimator().estimate, (2, -1)),
                             (Estimator().observe, (2, -1)),
                             (Estimator().observe, (2, 1, -1))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                method(*args)
