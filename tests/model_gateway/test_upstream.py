"""Tests for model_gateway.upstream: a pooled connection the server closed gets one fresh retry."""
import json
import socket
import threading
import unittest

from model_gateway.upstream import Upstream, UpstreamError
from tests import fakes, jail
from tests.fakes import ClosingUpstream

HEADERS = (("content-length", "2"),)


class UpstreamTest(unittest.TestCase):
    def test_a_stale_pooled_connection_is_retried_once_on_a_fresh_one(self):
        ClosingUpstream.count[0] = 0
        up = Upstream(fakes.start_upstream(self, ClosingUpstream))
        self.assertEqual(json.loads(up.send("POST", "/x", b"{}", HEADERS).read()), {"n": 1})
        first = up.local.conn
        self.assertEqual(json.loads(up.send("POST", "/x", b"{}", HEADERS).read()), {"n": 2})
        self.assertIsNot(up.local.conn, first)

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

    def test_is_stale_names_the_errors_of_a_connection_closed_before_any_body(self):
        import http.client
        from model_gateway.upstream import is_stale
        for error, expected in ((http.client.IncompleteRead(b""), True), (http.client.RemoteDisconnected("x"), True),
                                (ConnectionResetError(), True), (BrokenPipeError(), True),
                                (http.client.IncompleteRead(b"ab", 3), False), (OSError("other"), False),
                                (None, False)):
            self.assertEqual(is_stale(error), expected, repr(error))

    def test_a_connection_that_fails_twice_is_final(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)

        def slam():
            for _ in range(2):
                conn, _ = listener.accept()
                conn.recv(65536)
                conn.close()
        threading.Thread(target=slam, daemon=True).start()
        up = Upstream("http://127.0.0.1:%d" % listener.getsockname()[1])
        with self.assertRaises(UpstreamError) as cm:
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertTrue(cm.exception.message.startswith("upstream connection failed: "), cm.exception.message)

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
        up = Upstream("http://127.0.0.1:%d" % listener.getsockname()[1])
        with self.assertRaises(UpstreamError) as cm:
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertTrue(cm.exception.message.startswith("upstream protocol error: BadStatusLine"), cm.exception.message)
        self.assertIsNone(up.local.conn)

    def test_a_close_that_fails_is_logged(self):
        class Broken:
            def close(self):
                raise OSError(9, "Bad file descriptor")
        lines = []
        up = Upstream("http://127.0.0.1:9")
        up.log = lines.append
        up.local.conn = Broken()
        up.drop()
        self.assertIsNone(up.local.conn)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("upstream close failed: OSError(9, 'Bad file descriptor')", lines[0])

    def test_unreachable(self):
        up = Upstream("http://127.0.0.1:%d" % jail.free_port())
        with self.assertRaises(UpstreamError) as cm:
            up.send("POST", "/x", b"{}", HEADERS)
        self.assertTrue(cm.exception.message.startswith("upstream unreachable: ConnectionRefusedError"))
        self.assertIsNone(up.local.conn)

    def test_https_gets_a_tls_context(self):
        self.assertIsNotNone(Upstream("https://example.invalid").tls)
        self.assertIsNone(Upstream("http://example.invalid").tls)


if __name__ == "__main__":
    unittest.main()
