"""Tests for gpt_gateway.reach: a failure's class, runs of upstream failures, and host pauses as events."""
import errno
import http.client
import socket
import ssl
import unittest
from unittest import mock

from gpt_gateway import reach
from gpt_gateway.upstream import UpstreamError

ALERT = ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac (_ssl.c:2633)")


class Clock:
    """Wall time, the monotonic timer and the time the host slept, each moved by hand."""

    def __init__(self, wall=1000.0, mono=50.0, asleep=7.0):
        self.wall, self.mono, self.asleep = wall, mono, asleep

    def advance(self, seconds, paused=0.0):
        """`seconds` pass awake, then the host sleeps `paused` more, which the monotonic timer never counts."""
        self.mono += seconds
        self.wall += seconds + paused
        self.asleep += paused

    def patched(self, test):
        for name, read in (("now", lambda: self.wall), ("monotonic", lambda: self.mono),
                           ("slept", lambda: self.asleep)):
            patcher = mock.patch.object(reach.clock, name, read)
            patcher.start()
            test.addCleanup(patcher.stop)
        return self


class ErrorClassTest(unittest.TestCase):
    def test_each_failure_has_one_class(self):
        cases = [
            (OSError(errno.EMFILE, "Too many open files"), "fd"),
            (socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided"), "dns"),
            (ALERT, "tls"),
            (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), "tls"),
            (socket.timeout("timed out"), "timeout"),
            (TimeoutError(errno.ETIMEDOUT, "Operation timed out"), "timeout"),
            (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), "connect"),
            (OSError(errno.ENETUNREACH, "Network is unreachable"), "connect"),
            (OSError(errno.EHOSTUNREACH, "No route to host"), "connect"),
            (OSError(errno.EADDRNOTAVAIL, "Can't assign requested address"), "connect"),
            (ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"), "reset"),
            (BrokenPipeError(errno.EPIPE, "Broken pipe"), "reset"),
            (http.client.RemoteDisconnected("Remote end closed connection without response"), "reset"),
            (http.client.IncompleteRead(b"ab", 3), "reset"),
            (http.client.CannotSendRequest("Request-sent"), "reset"),  # a kept-alive connection that cannot carry it
            (http.client.BadStatusLine("garbage"), "protocol"),
            (http.client.LineTooLong("header line"), "protocol"),
            (OSError(errno.EIO, "Input/output error"), "other"),
            (ValueError("invented"), "other"),
            (UpstreamError("upstream unreachable: invented", "dns"), "dns"),  # classed where it was raised
        ]
        for error, expected in cases:
            with self.subTest(error=repr(error)):
                self.assertEqual(reach.error_class(error), expected)


class RunsTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock().patched(self)
        self.events = []
        self.runs = reach.Runs(lambda kind, **fields: self.events.append((kind, fields)), "api.invented.test")

    def miss(self, cls="tls", error="SSLError(1)"):
        self.runs.failed(cls, error)
        self.clock.advance(1)

    def test_a_run_opens_at_the_third_failure_and_ends_at_an_answer_after_the_quiet_window(self):
        first = self.clock.wall
        self.miss("reset", "ConnectionResetError()")
        self.miss("tls")
        self.assertEqual(self.events, [])
        self.miss("tls", "SSLError(1, 'bad record mac')")
        self.assertEqual(self.events, [("upstream_unreachable_start", {
            "origin": "api.invented.test", "first_at": first, "failures": 3, "error_class": "tls",
            "error": "SSLError(1, 'bad record mac')"})])
        self.runs.answered()  # inside the quiet window: the run goes on
        self.miss("connect")
        last = self.clock.wall - 1
        self.clock.advance(reach.QUIET_SECONDS - 2)  # one second short of the window since that failure
        self.runs.answered()
        self.assertEqual(len(self.events), 1)
        self.clock.advance(1)
        self.runs.answered()
        self.runs.answered()
        self.assertEqual(self.events[1:], [("upstream_unreachable_end", {
            "origin": "api.invented.test", "failures": 4, "answered": 2,
            "classes": {"reset": 1, "tls": 2, "connect": 1}, "first_at": first, "last_at": last,
            "seconds": last - first})])

    def test_failures_an_answer_breaks_before_the_third_record_nothing(self):
        for _ in range(5):
            self.miss()
            self.miss()
            self.runs.answered()
        self.assertEqual(self.events, [])

    def test_a_run_after_an_ended_one_opens_again(self):
        for _ in range(2):
            for _ in range(reach.RUN_START):
                self.miss()
            self.clock.advance(reach.QUIET_SECONDS)
            self.runs.answered()
        self.assertEqual([kind for kind, _ in self.events], ["upstream_unreachable_start", "upstream_unreachable_end"] * 2)


class PauseTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock().patched(self)
        self.events = []
        self.pause = reach.Pause(lambda kind, **fields: self.events.append((kind, fields)))

    def test_a_sleep_of_the_host_past_the_threshold_is_one_host_resumed_event(self):
        self.clock.advance(100)
        self.assertFalse(self.pause.check())
        seen = self.clock.wall
        self.clock.advance(5, paused=reach.PAUSE_SECONDS - 1)
        self.assertFalse(self.pause.check())  # below the threshold: too short a sleep to count
        seen = self.clock.wall
        self.clock.advance(5, paused=3600)
        self.assertTrue(self.pause.check())
        self.assertFalse(self.pause.check())  # one event per jump
        self.assertEqual(self.events, [("host_resumed", {"gap_s": 3600.0, "last_seen": seen})])

    def test_a_step_of_the_wall_clock_is_no_pause(self):
        self.clock.wall += 3600  # a manual or network time correction while the host stayed awake
        self.assertFalse(self.pause.check())
        self.clock.wall -= 7200
        self.assertFalse(self.pause.check())
        self.assertEqual(self.events, [])

    def test_a_pause_before_the_first_check_counts_from_the_start(self):
        started = self.clock.wall
        self.clock.advance(1, paused=90)
        self.assertTrue(self.pause.check())
        self.assertEqual(self.events, [("host_resumed", {"gap_s": 90.0, "last_seen": started})])


if __name__ == "__main__":
    unittest.main()
