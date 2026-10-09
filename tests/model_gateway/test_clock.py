"""Tests for model_gateway.clock."""
import unittest

from model_gateway import clock


class ClockTest(unittest.TestCase):
    def test_stamp_formats_local_time(self):
        self.assertRegex(clock.stamp("%Y-%m-%d %H:%M:%S"), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(clock.stamp("%Y", 0)[:3], "197")

    def test_monotonic_never_goes_back(self):
        a = clock.monotonic()
        clock.sleep(0.01)
        self.assertGreater(clock.monotonic(), a)
        self.assertGreater(clock.now(), 1.7e9)


if __name__ == "__main__":
    unittest.main()
