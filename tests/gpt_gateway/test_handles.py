"""Tests for gpt_gateway.handles: the open-file limit raised at start, the open count, an out-of-handles failure named
as that and recorded once per window, and a jailed gateway under a lowered limit that reaps what it holds."""
import errno
import http.client
import os
import re
import resource
import socket
import subprocess
import sys
import unittest
from unittest import mock

from gpt_gateway import handles
from tests import fakes, jail

INFINITY = resource.RLIM_INFINITY
FDS = re.compile(rb" fds=(\d+)/(\d+) ")


class LimitTest(unittest.TestCase):
    def raise_from(self, soft, hard, refuse=None):
        events, lines = [], []
        with mock.patch.object(handles.resource, "getrlimit", return_value=(soft, hard)), \
                mock.patch.object(handles.resource, "setrlimit", side_effect=refuse) as setter:
            result = handles.raise_limit(lambda kind, **fields: events.append((kind, fields)), lines.append)
        return setter.call_args_list, events, lines, result

    def test_the_soft_limit_goes_to_the_lesser_of_the_target_and_the_hard_limit(self):
        for soft, hard, expected in ((256, INFINITY, (10240, INFINITY)), (256, 4096, (4096, 4096)),
                                     (256, 20000, (10240, 20000))):
            with self.subTest(soft=soft, hard=hard):
                calls, events, lines, result = self.raise_from(soft, hard)
                self.assertEqual(calls, [mock.call(resource.RLIMIT_NOFILE, expected)])
                self.assertEqual(result, expected[0])
                self.assertEqual(events, [])
                self.assertEqual([("%d" % expected[0] in line) for line in lines], [True])

    def test_a_soft_limit_at_or_above_the_target_is_left_alone(self):
        for soft, hard in ((256, 256), (10240, INFINITY), (20000, INFINITY), (4096, 4096)):
            with self.subTest(soft=soft, hard=hard):
                calls, events, lines, result = self.raise_from(soft, hard)
                self.assertEqual((calls, events, result), ([], [], soft))

    def test_a_refusal_is_an_event_and_a_start_line_never_fatal(self):
        calls, events, lines, result = self.raise_from(256, INFINITY, refuse=ValueError("not permitted"))
        self.assertEqual(result, 256)
        self.assertEqual([(kind, fields["soft"], fields["target"]) for kind, fields in events], [("fd_limit", 256, 10240)])
        self.assertIn("not permitted", events[0][1]["error"])
        self.assertEqual(len(lines), 1)
        self.assertIn("256", lines[0])
        self.assertIn("10240", lines[0])

    def test_the_open_count_follows_the_process_and_the_soft_limit_is_the_kernels(self):
        before = handles.open_count()
        self.assertIsNotNone(before)
        with open(os.devnull):
            self.assertEqual(handles.open_count(), before + 1)
        self.assertEqual(handles.soft_limit(), resource.getrlimit(resource.RLIMIT_NOFILE)[0])


class ShortageTest(unittest.TestCase):
    def setUp(self):
        handles.THROTTLE.clear()

    def counts(self, used, limit):
        for name, value in (("open_count", used), ("soft_limit", limit)):
            patcher = mock.patch.object(handles, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_out_of_descriptors_is_named_with_the_counts(self):
        self.counts(62, 64)
        for error in (OSError(errno.EMFILE, "Too many open files"), OSError(errno.ENFILE, "Too many open files in system"),
                      socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")):
            with self.subTest(error=error):
                shortage = handles.diagnose(error)
                self.assertIsNotNone(shortage)
                self.assertIn("out of file handles", shortage.message)
                self.assertIn("62 open of 64", shortage.message)
                self.assertEqual((shortage.open, shortage.limit), (62, 64))

    def test_a_name_that_does_not_resolve_with_handles_to_spare_is_a_dns_error(self):
        self.counts(10, 64)
        for error in (socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known"),
                      OSError(errno.ECONNREFUSED, "Connection refused"), ValueError("x")):
            with self.subTest(error=error):
                self.assertIsNone(handles.diagnose(error))

    def test_a_name_lookup_failed_with_every_handle_taken_is_named_as_that(self):
        # At the limit the count cannot be taken either: listing /dev/fd needs a handle of its own.
        script = ("import resource, socket, sys\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "from gpt_gateway import handles\n"
                  "resource.setrlimit(resource.RLIMIT_NOFILE, (64, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))\n"
                  "held = []\n"
                  "try:\n"
                  "    while True:\n"
                  "        held.append(open('/dev/null'))\n"
                  "except OSError:\n"
                  "    pass\n"
                  "found = handles.diagnose(socket.gaierror(socket.EAI_NONAME, 'nodename nor servname provided'))\n"
                  "print(found.message if found else 'none')\n")
        done = subprocess.run([sys.executable, "-I", "-B", "-c", script, jail.ROOT], env=jail.clean_env(jail.scratch(self)),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual((done.stdout.decode().strip(), done.stderr.decode()[-300:]), ("out of file handles: 64 open of 64 allowed", ""))

    def test_one_event_per_window_counts_the_ones_it_held_back(self):
        events, now = [], [100.0]
        with mock.patch.object(handles.clock, "monotonic", lambda: now[0]):
            for at in (100.0, 101.0, 102.0, 100.0 + handles.EVENT_SECONDS + 1):
                now[0] = at
                handles.announce(lambda kind, **fields: events.append((kind, fields)), "fd_exhausted", where="upstream")
        self.assertEqual([(kind, fields["where"], fields["suppressed"]) for kind, fields in events],
                         [("fd_exhausted", "upstream", 0), ("fd_exhausted", "upstream", 2)])


class ProcessCase(unittest.TestCase):
    """Gateways as processes, in a jailed home, against a fake upstream."""

    def setUp(self):
        self.tmp = jail.scratch(self)
        self.port = jail.free_port()
        self.fake = fakes.keeping()
        self.origin = fakes.start_upstream(self, self.fake)
        self.procs, self.conns = [], []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for conn in self.conns:
            conn.close()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def env(self):
        return jail.clean_env(self.tmp, GPT_GATEWAY_PORT=str(self.port), GPT_GATEWAY_UPSTREAM=self.origin,
                              GPT_GATEWAY_LOG=os.path.join(self.tmp, "gw.log"))

    def spawn(self, argv, **kw):
        p = subprocess.Popen(argv, env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
        self.procs.append(p)
        self.assertTrue(jail.wait_for(lambda: self.health() is not None), "the gateway never answered")
        return p

    def health(self):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            conn.request("GET", "/__gateway/health")
            data = conn.getresponse().read()
            conn.close()
            return data
        except (OSError, http.client.HTTPException):
            return None

    def fds(self):
        found = FDS.search(self.health() or b"")
        return tuple(int(n) for n in found.groups()) if found else None


class LimitProcessTest(ProcessCase):
    def test_a_started_gateway_raises_its_soft_limit_and_reports_the_handles(self):
        hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        target = 10240 if hard == INFINITY else min(10240, hard)
        if target <= 256:
            self.skipTest("this host's hard limit leaves nothing to raise to")

        def lowered():
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        app = jail.stage_install(os.path.join(self.tmp, "app"))
        self.spawn(["/usr/bin/python3", "-I", "-B", os.path.join(app, "gateway.py")], preexec_fn=lowered)
        found = self.fds()
        self.assertIsNotNone(found, "no fds= field in %r" % self.health())
        self.assertEqual(found[1], target)
        self.assertGreater(found[0], 0)
        self.assertLess(found[0], 100)


class ExhaustionTest(ProcessCase):
    LIMIT, CLIENTS, MAX_IDLE, CLIENT_IDLE = 96, 45, 2, 4

    def client(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.conns.append(conn)
        conn.request("POST", "/v1/json", body=b'{"model":"m"}')
        resp = conn.getresponse()
        return resp.status, resp.read()

    def closed(self, conn):
        conn.sock.setblocking(False)
        try:
            return conn.sock.recv(1) == b""
        except BlockingIOError:
            return False

    def test_idle_clients_do_not_exhaust_a_gateway_and_are_reaped(self):
        # 45 chats, each connected and idle after one request: with a connection pinned upstream per chat that is 90
        # handles plus the gateway's own, over a limit of 96; the pool keeps two, so the process stays far below it.
        self.spawn([sys.executable, "-I", "-B", os.path.join(jail.ROOT, "tests", "fd_gateway.py"),
                    str(self.LIMIT), str(self.CLIENT_IDLE), str(self.MAX_IDLE)])
        baseline = self.fds()
        failures = []
        for _ in range(self.CLIENTS):
            try:
                status, body = self.client()
                if status != 200:
                    failures.append((status, body[:200]))
            except (OSError, http.client.HTTPException) as error:
                failures.append(repr(error))
        self.assertEqual(failures[:3], [], "of %d requests %d failed" % (self.CLIENTS, len(failures)))
        held = self.fds()
        self.assertIsNotNone(held, "no fds= field in %r" % self.health())
        self.assertEqual(held[1], self.LIMIT)
        self.assertLessEqual(held[0], baseline[0] + self.CLIENTS + self.MAX_IDLE + 4, (baseline, held))
        self.assertTrue(jail.wait_for(lambda: all(self.closed(c) for c in self.conns), self.CLIENT_IDLE + 8),
                        "idle clients still connected: %d" % sum(not self.closed(c) for c in self.conns))
        self.assertLessEqual(self.fds()[0], baseline[0] + self.MAX_IDLE + 2, (baseline, self.fds()))
        self.assertEqual(self.client()[0], 200)


if __name__ == "__main__":
    unittest.main()
