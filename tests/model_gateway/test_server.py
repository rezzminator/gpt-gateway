"""Tests for model_gateway.server: real gateway processes; SIGTERM lets in-flight streams finish while a
successor takes the port, and the drain gives up at its limit."""
import errno
import http.client
import os
import signal
import subprocess
import threading
import time
import unittest

from model_gateway.server import Drain
from tests import fakes, jail


class DrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = jail.scratch(self)
        self.app = jail.stage_install(os.path.join(self.tmp, "app"))
        self.port = jail.free_port()
        self.upstream = fakes.start_upstream(self, fakes.SlowUpstream)
        self.log = os.path.join(self.tmp, "gw.log")
        self.procs = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def gateway(self, drain="10"):
        env = jail.clean_env(self.tmp, MODEL_GATEWAY_PORT=str(self.port), MODEL_GATEWAY_UPSTREAM=self.upstream,
                             MODEL_GATEWAY_LOG=self.log, MODEL_GATEWAY_DRAIN_SECONDS=drain)
        p = subprocess.Popen(["/usr/bin/python3", "-I", "-B", os.path.join(self.app, "gateway.py")], env=env)
        self.procs.append(p)
        return p

    def health_pid(self):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            conn.request("GET", "/__gateway/health")
            data = conn.getresponse().read()
            conn.close()
            return int(data.split(b"pid=")[1].split()[0])
        except (OSError, IndexError, ValueError):
            return None

    def stream(self, out):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("POST", "/v1/messages", body=b'{"model":"m","messages":[]}')
        out.append(conn.getresponse().read())
        conn.close()

    def logged(self):
        with open(self.log) as f:
            return f.read()

    def test_a_stream_survives_a_restart_and_the_successor_takes_new_requests(self):
        old = self.gateway()
        self.assertTrue(jail.wait_for(lambda: self.health_pid() == old.pid))
        got = []
        t = threading.Thread(target=self.stream, args=(got,))
        t.start()
        time.sleep(0.4)  # the stream is now mid-flight on the old gateway
        new = self.gateway()
        time.sleep(0.5)  # the successor binds beside it
        old.send_signal(signal.SIGTERM)
        self.assertTrue(jail.wait_for(lambda: self.health_pid() == new.pid), "the successor never answered")
        t.join(20)
        self.assertEqual(len(got), 1)
        self.assertIn(b"message_stop", got[0])
        self.assertEqual(old.wait(timeout=10), 0)
        after = []
        self.stream(after)
        self.assertIn(b"message_stop", after[0])
        self.assertIn("stop: drained, exiting", self.logged())

    def test_drain_gives_up_at_its_limit(self):
        old = self.gateway(drain="0.3")
        self.assertTrue(jail.wait_for(lambda: self.health_pid() == old.pid))
        t = threading.Thread(target=lambda: self.assertRaises(Exception, self.stream, []))
        t.start()
        time.sleep(0.3)
        start = time.time()
        old.send_signal(signal.SIGTERM)
        self.assertEqual(old.wait(timeout=10), 0)
        self.assertLess(time.time() - start, 1.2)
        t.join(20)
        self.assertIn("still in flight", self.logged())


class DrainStateTest(unittest.TestCase):
    def test_an_idle_gateway_drains_at_once(self):
        lines = []
        d = Drain()
        d.connected("conn")
        d.begin("conn")
        self.assertEqual(d.idle, set())
        d.end("conn")
        self.assertEqual((d.inflight, d.idle), (0, {"conn"}))
        d.disconnected("conn")
        self.assertEqual(d.wait(5, lines.append), 0)
        self.assertEqual(lines, ["stop: draining 0 in-flight request(s), up to 5s", "stop: drained, exiting"])

    def test_a_shutdown_error_other_than_not_connected_is_logged(self):
        class Conn:
            def __init__(self, code):
                self.code = code

            def shutdown(self, how):
                raise OSError(self.code, os.strerror(self.code))
        lines = []
        d = Drain()
        d.connected(Conn(errno.ENOTCONN))
        d.connected(Conn(errno.EBADF))
        self.assertEqual(d.wait(0, lines.append), 0)
        failed = [line for line in lines if "shutdown" in line]
        self.assertEqual(len(failed), 1, lines)
        self.assertIn("OSError(%d, " % errno.EBADF, failed[0])


if __name__ == "__main__":
    unittest.main()
