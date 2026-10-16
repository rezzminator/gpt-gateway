"""Tests for gpt_gateway.upstream: a pooled connection the server closed gets one fresh retry; every socket probes
its path with TCP keepalive; a WebSocket upgrade follows RFC 6455."""
import errno
import json
import socket
import ssl
import threading
import unittest
import unittest.mock
from urllib.parse import urlsplit

from gpt_gateway import frames, handles, pool, reach, upstream
from gpt_gateway.upstream import Upstream, UpstreamError
from tests import fakes, jail
from tests.fakes import ClosingUpstream

HEADERS = (("content-length", "2"),)
ALERT = ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac (_ssl.c:2633)")


class Failing:
    """A kept-alive connection's socket whose peer fails the next exchange as `error`: before any answer, or after
    the `status` line when one is given; while the request is written when `writing`."""

    def __init__(self, real, error, status=None, writing=False):
        self.real, self.error, self.lines, self.writing = real, error, [status] if status else [], writing

    def sendall(self, data):
        if self.writing:
            raise self.error

    def settimeout(self, value):
        pass

    def makefile(self, mode):
        return self

    def flush(self):
        pass

    def readline(self, limit=-1):
        if self.lines:
            return self.lines.pop(0)
        raise self.error

    def close(self):
        self.real.close()


class Keeping(ClosingUpstream):
    def do_POST(self):
        super().do_POST()
        self.close_connection = False


class UpstreamTest(unittest.TestCase):
    def test_a_stale_pooled_connection_is_retried_once_on_a_fresh_one(self):
        ClosingUpstream.count[0] = 0
        up = Upstream(fakes.start_upstream(self, ClosingUpstream))
        self.assertEqual(json.loads(up.send("POST", "/x", b"{}", HEADERS).read()), {"n": 1})
        first = up.local.conn
        self.assertEqual(json.loads(up.send("POST", "/x", b"{}", HEADERS).read()), {"n": 2})
        self.assertIsNot(up.local.conn, first)

    def test_a_released_connection_stays_pooled_where_a_dropped_one_is_closed(self):
        up = Upstream(fakes.start_upstream(self))
        for release in (True, False):
            up.send("POST", "/v1/json", b"{}", HEADERS).read()
            conn = up.local.conn
            if release:
                up.release()
            up.drop()
            self.assertEqual((up.pool.idle == [conn], conn.sock is not None), (release, release))
            up.pool.clear()

    def test_reuse_reports_a_kept_alive_connection_and_the_stale_retry(self):
        ClosingUpstream.count[0] = 0
        up = Upstream(fakes.start_upstream(self, ClosingUpstream))
        self.assertEqual(up.reuse(), (False, False))
        up.send("POST", "/x", b"{}", HEADERS).read()
        self.assertEqual(up.reuse(), (False, False))  # a fresh connection
        up.send("POST", "/x", b"{}", HEADERS).read()
        self.assertEqual(up.reuse(), (False, True))  # the kept-alive one was dead; the answer came on a fresh one

    def test_reuse_is_true_when_the_server_keeps_the_connection(self):
        class Keeping(ClosingUpstream):
            def do_POST(self):
                super().do_POST()
                self.close_connection = False
        up = Upstream(fakes.start_upstream(self, Keeping))
        for expected in (False, True, True):
            up.send("POST", "/x", b"{}", HEADERS).read()
            self.assertEqual(up.reuse(), (expected, False))

    def test_a_kept_alive_connection_idle_past_the_limit_is_replaced_before_use(self):
        from unittest import mock

        class Keeping(ClosingUpstream):
            def do_POST(self):
                super().do_POST()
                self.close_connection = False
        up = Upstream(fakes.start_upstream(self, Keeping))
        t = [1000.0]
        with mock.patch.object(pool.clock, "monotonic", lambda: t[0]):
            up.send("POST", "/x", b"{}", HEADERS).read()
            first = up.local.conn
            t[0] += pool.IDLE_SECONDS
            up.send("POST", "/x", b"{}", HEADERS).read()
            self.assertEqual(up.reuse(), (True, False))  # idle exactly the limit: still reused
            self.assertIs(up.local.conn, first)
            t[0] += pool.IDLE_SECONDS + 1
            up.send("POST", "/x", b"{}", HEADERS).read()
            self.assertEqual(up.reuse(), (False, False))  # idle past it: a fresh connection, no stale retry
            self.assertIsNot(up.local.conn, first)
            self.assertIsNone(first.sock)  # the idle one was closed, not leaked

    def test_is_stale_names_the_errors_of_a_connection_closed_before_any_body(self):
        import http.client
        from gpt_gateway.upstream import is_stale
        for error, expected in ((http.client.IncompleteRead(b""), True), (http.client.RemoteDisconnected("x"), True),
                                (ConnectionResetError(), True), (BrokenPipeError(), True),
                                (http.client.IncompleteRead(b"ab", 3), False), (OSError("other"), False),
                                (None, False)):
            self.assertEqual(is_stale(error), expected, repr(error))

    def kept(self):
        """(Upstream, its events, the kept-alive connection its first answer left in the pool)."""
        events = []
        up = Upstream(fakes.start_upstream(self, Keeping), lambda kind, **fields: events.append((kind, fields)))
        up.send("POST", "/x", b"{}", HEADERS).read()
        self.assertEqual(len(up.pool.idle), 1)
        return up, events, up.pool.idle[-1]

    def test_a_reused_connection_that_fails_before_any_answer_goes_again_once_on_a_fresh_one(self):
        # The alert the gateway logged 96 times overnight: the request is sent again, unchanged, on a new connection.
        up, events, conn = self.kept()
        conn.sock = Failing(conn.sock, ALERT)
        try:
            resp = up.send("POST", "/x", b"{}", HEADERS)
        except UpstreamError as e:
            self.fail("a reused connection's failure before any answer was final: %s" % e.message)
        self.assertEqual(json.loads(resp.read())["n"] > 1, True)
        self.assertIsNot(up.local.conn, conn)
        self.assertEqual((up.reuse(), up.attempts()), ((False, True), 2))
        (kind, fields), = events
        self.assertEqual((kind, fields["request_id"], fields["error_class"], fields["error"]),
                         ("upstream_retry", None, "tls", repr(ALERT)))
        self.assertIsInstance(fields["idle_ms"], int)
        self.assertIsInstance(fields.get("wait_ms"), int)  # how long after the whole request was written

    def test_a_failure_long_after_the_whole_request_was_written_is_never_sent_again(self):
        # The origin may have been working on it; a failure while it was still being written leaves it unread.
        # A host that slept between the write and the failure held the request that long too.
        for writing, window, slept, retried in ((False, 0, 0.0, False), (True, 0, 0.0, True),
                                                (False, upstream.REUSE_WINDOW_SECONDS, 3600.0, False)):
            with self.subTest(writing=writing, slept=slept), \
                    unittest.mock.patch.object(upstream, "REUSE_WINDOW_SECONDS", window):
                up, events, conn = self.kept()
                conn.sock = Failing(conn.sock, ALERT, writing=writing)
                readings = iter((0.0, slept))  # clock.slept() at the write, then at the failure
                with unittest.mock.patch.object(upstream.clock, "slept", lambda: next(readings, slept)):
                    try:
                        resp = up.send("POST", "/x", b"{}", HEADERS)
                    except UpstreamError as e:
                        resp = e
                if isinstance(resp, UpstreamError):
                    self.assertEqual((retried, resp.error_class, up.reuse(), up.attempts(), events),
                                     (False, "tls", (True, False), 1, []))
                else:
                    resp.read()
                    self.assertTrue(retried, "a failure past the window was sent again")
                    (kind, fields), = events
                    self.assertEqual((kind, fields["wait_ms"], up.attempts()), ("upstream_retry", None, 2))

    def test_a_reused_connection_whose_request_may_have_run_is_never_sent_again(self):
        # After the status line the model ran; a timeout may be a slow answer, never only an idle connection's death.
        for error, status, message, cls in (
                (ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"), b"HTTP/1.1 200 OK\r\n",
                 "upstream connection failed: ConnectionResetError", "reset"),
                (socket.timeout("timed out"), None, "upstream unreachable: %s" % socket.timeout.__name__, "timeout"),  # TimeoutError since 3.10
                (TimeoutError(errno.ETIMEDOUT, "Operation timed out"), None, "upstream unreachable: TimeoutError",
                 "timeout")):
            with self.subTest(error=repr(error)):
                up, events, conn = self.kept()
                conn.sock = Failing(conn.sock, error, status)
                with self.assertRaises(UpstreamError) as cm:
                    up.send("POST", "/x", b"{}", HEADERS)
                self.assertTrue(cm.exception.message.startswith(message), cm.exception.message)
                self.assertEqual((cm.exception.error_class, up.reuse(), up.attempts(), events),
                                 (cls, (True, False), 1, []))

    def test_a_retry_whose_fresh_connection_fails_too_is_final_and_one_failure_of_the_origin(self):
        up, events, conn = self.kept()
        conn.sock = Failing(conn.sock, ALERT)
        up.origin = urlsplit("http://127.0.0.1:%d" % jail.free_port())  # the origin went away with the connection
        with self.assertRaises(UpstreamError) as cm:
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertTrue(cm.exception.message.startswith("upstream unreachable: ConnectionRefusedError"),
                        cm.exception.message)
        self.assertEqual((cm.exception.error_class, up.reuse(), up.attempts()), ("connect", (False, True), 2))
        with self.assertRaises(UpstreamError):
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertEqual([kind for kind, _ in events], ["upstream_retry"])  # two failures: no run yet, the alert retried

    def test_a_fresh_connection_that_fails_is_final_at_once(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(5)
        self.addCleanup(listener.close)
        served = []

        def slam():
            for _ in range(2):
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.recv(65536)
                served.append(1)
                conn.close()
        threading.Thread(target=slam, daemon=True).start()
        up = Upstream("http://127.0.0.1:%d" % listener.getsockname()[1])
        with self.assertRaises(UpstreamError) as cm:
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertEqual(len(served), 1)  # never sent again
        self.assertTrue(cm.exception.message.startswith("upstream connection failed: "), cm.exception.message)
        self.assertEqual((cm.exception.error_class, up.reuse(), up.attempts()), ("reset", (False, False), 1))

    def test_failed_sends_open_an_unreachable_run_and_an_answer_ends_it(self):
        events = []
        up = Upstream("http://127.0.0.1:%d" % jail.free_port(), lambda kind, **fields: events.append((kind, fields)))
        for _ in range(reach.RUN_START):
            with self.assertRaises(UpstreamError):
                up.send("POST", "/x", b"{}", HEADERS)
        up.origin = urlsplit(fakes.start_upstream(self, ClosingUpstream))
        with unittest.mock.patch.object(reach, "QUIET_SECONDS", 0):
            up.send("POST", "/x", b"{}", HEADERS).read()
        self.assertEqual([(kind, f.get("error_class"), f["failures"]) for kind, f in events],
                         [("upstream_unreachable_start", "connect", 3), ("upstream_unreachable_end", None, 3)])

    def test_a_malformed_response_is_final_and_drops_the_connection(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)

        def garble():
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(b"garbage\r\n\r\n")
            conn.close()
        threading.Thread(target=garble, daemon=True).start()
        events = []
        up = Upstream("http://127.0.0.1:%d" % listener.getsockname()[1], lambda kind, **fields: events.append(kind))
        for _ in range(reach.RUN_START - 1):
            up.runs.failed("connect", "ConnectionRefusedError()")  # the failures in a row before it
        with self.assertRaises(UpstreamError) as cm:
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertTrue(cm.exception.message.startswith("upstream protocol error: BadStatusLine"), cm.exception.message)
        self.assertEqual(cm.exception.error_class, "protocol")
        self.assertIsNone(up.local.conn)
        up.runs.failed("connect", "ConnectionRefusedError()")
        self.assertEqual(events, [])  # the origin answered between: no run of it being unreachable

    def test_a_close_that_fails_is_recorded(self):
        class Broken:
            holder = threading.current_thread()

            def close(self):
                raise OSError(9, "Bad file descriptor")
        events = []
        up = Upstream("http://127.0.0.1:9")
        up.event = lambda kind, **fields: events.append((kind, fields))
        up.local.conn = Broken()
        up.drop()
        self.assertIsNone(up.local.conn)
        self.assertEqual(events, [("upstream_socket", {"what": "close", "error": "OSError(9, 'Bad file descriptor')"})])

    def test_unreachable(self):
        up = Upstream("http://127.0.0.1:%d" % jail.free_port())
        with self.assertRaises(UpstreamError) as cm:
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertTrue(cm.exception.message.startswith("upstream unreachable: ConnectionRefusedError"))
        self.assertEqual(cm.exception.error_class, "connect")
        self.assertIsNone(up.local.conn)

    def test_every_connection_probes_its_path_with_tcp_keepalive(self):
        # A path that dies without a FIN or RST (a Wi-Fi roam, a dropped NAT mapping) leaves a read waiting on silence:
        # three upstream streams sat 300 s on one at 07:41:57. The kernel's probes fail such a socket after
        # KEEPALIVE_IDLE + KEEPALIVE_INTERVAL x KEEPALIVE_PROBES seconds, while a live peer that is only slow
        # (a model reasoning before its first token) still answers them.
        class Keeping(ClosingUpstream):
            def do_POST(self):
                super().do_POST()
                self.close_connection = False
        events = []
        up = Upstream(fakes.start_upstream(self, Keeping), lambda kind, **fields: events.append(kind))
        up.send("POST", "/x", b"{}", HEADERS).read()
        sock = up.local.conn.sock
        self.assertNotEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 0)
        options = dict(upstream.KEEPALIVE_OPTIONS)
        self.assertEqual(sorted(options.values()), sorted((upstream.KEEPALIVE_IDLE, upstream.KEEPALIVE_INTERVAL,
                                                           upstream.KEEPALIVE_PROBES)))
        for option, value in options.items():
            with self.subTest(option=option):
                self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, option), value)
        self.assertEqual(events, [])
        self.assertIn(upstream.KeepaliveHTTPSConnection, type(Upstream("https://example.invalid").connection()).__mro__)

    def test_a_keepalive_option_the_kernel_refuses_is_recorded_and_the_request_goes_on(self):
        events = []
        up = Upstream(fakes.start_upstream(self, ClosingUpstream), lambda kind, **fields: events.append((kind, fields)))
        with unittest.mock.patch.object(upstream, "KEEPALIVE_OPTIONS", ((0x7FFF, 1),)):
            self.assertEqual(json.loads(up.send("POST", "/x", b"{}", HEADERS).read())["n"] > 0, True)
        self.assertEqual([(kind, f["what"], f["error"].startswith("OSError")) for kind, f in events],
                         [("upstream_socket", "keepalive", True)])

    def test_https_gets_a_tls_context(self):
        self.assertIsNotNone(Upstream("https://example.invalid").tls)
        self.assertIsNone(Upstream("http://example.invalid").tls)



class OutOfHandlesTest(unittest.TestCase):
    def test_an_upstream_that_cannot_open_a_socket_says_why_and_records_it_once(self):
        handles.THROTTLE.clear()
        self.addCleanup(handles.THROTTLE.clear)
        events = []
        up = Upstream("http://127.0.0.1:9", lambda kind, **fields: events.append((kind, fields)))
        refuse = OSError(errno.EMFILE, "Too many open files")
        with unittest.mock.patch.object(handles, "open_count", return_value=62), \
                unittest.mock.patch.object(handles, "soft_limit", return_value=64), \
                unittest.mock.patch("gpt_gateway.upstream.KeepaliveHTTPConnection.connect", side_effect=refuse):
            for _ in range(3):
                with self.assertRaises(UpstreamError) as caught:
                    up.send("POST", "/x", b"{}", HEADERS)
        self.assertIn("out of file handles", caught.exception.message)
        self.assertIn("62 open of 64", caught.exception.message)
        self.assertNotIn("unreachable", caught.exception.message)
        self.assertEqual(caught.exception.error_class, "fd")
        self.assertEqual([(kind, fields["where"], fields["open"], fields["limit"]) for kind, fields in events],
                         [("fd_exhausted", "upstream", 62, 64)])



class UpgradeTest(unittest.TestCase):
    def test_upgrade_checks_the_accept_key_and_returns_a_refusal_with_its_body(self):
        fake = type("UpgradeFake", (fakes.ChatGPTUpstream,), {"seen": [], "script": [], "upgrades": [], "refuse": [426],
                                                              "vanish": [], "hangup": []})
        origin = fakes.start_upstream(self, fake)
        up = Upstream(origin)
        refused = up.websocket("/backend-api/codex/responses", (("x-invented", "1"),))
        self.assertEqual((refused.status, refused.body, refused.socket), (426, b'{"detail":"upgrade refused"}', None))
        upgraded = up.websocket("/backend-api/codex/responses", (("x-invented", "2"),))
        self.addCleanup(upgraded.socket.close)
        self.assertEqual(upgraded.status, 101)
        self.assertEqual(upstream.header(upgraded.headers, "upgrade"), "websocket")
        self.assertEqual([(h["x-invented"], h["upgrade"], h["sec-websocket-version"]) for h in fake.upgrades],
                         [("1", "websocket", "13"), ("2", "websocket", "13")])
        ws = frames.WebSocket(upgraded.socket, upgraded.pending)
        ws.send(b'{"type":"response.create"}')
        self.assertEqual(json.loads(ws.receive())["type"], "response.created")
        with unittest.mock.patch("gpt_gateway.upstream.ACCEPT_GUID", b"wrong-guid"):
            with self.assertRaisesRegex(UpstreamError, "Sec-WebSocket-Accept is wrong"):
                up.websocket("/backend-api/codex/responses", ())
        with self.assertRaisesRegex(UpstreamError, "unreachable") as cm:
            Upstream("http://127.0.0.1:%d" % jail.free_port()).websocket("/", ())
        self.assertEqual(cm.exception.error_class, "connect")


if __name__ == "__main__":
    unittest.main()
