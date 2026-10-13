"""Tests for gpt_gateway.clock."""
import unittest
from unittest import mock

from gpt_gateway import clock


class ClockTest(unittest.TestCase):
    def test_stamp_formats_local_time(self):
        self.assertRegex(clock.stamp("%Y-%m-%d %H:%M:%S"), r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(clock.stamp("%Y", 0)[:3], "197")

    def test_monotonic_never_goes_back(self):
        a = clock.monotonic()
        clock.sleep(0.01)
        self.assertGreater(clock.monotonic(), a)
        self.assertGreater(clock.now(), 1.7e9)

    def test_slept_holds_still_while_the_host_is_awake_whatever_the_wall_clock_does(self):
        before, wall = clock.slept(), clock.time.time
        with mock.patch.object(clock.time, "time", lambda: wall() + 3600):  # the wall clock stepped an hour ahead
            stepped = clock.slept()
        clock.sleep(0.05)
        self.assertGreaterEqual(before, 0)
        self.assertAlmostEqual(stepped, before, delta=1)
        self.assertAlmostEqual(clock.slept(), before, delta=1)


if __name__ == "__main__":
    unittest.main()
