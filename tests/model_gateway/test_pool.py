"""Tests for model_gateway.pool: a connection goes back to the shared pool when its response body is complete and not
before, from any thread; the pool keeps a bounded reserve and closes what sits idle past its limit."""
import socket
import threading
import unittest
import unittest.mock

from model_gateway import pool, upstream
from model_gateway.upstream import Upstream
from tests import fakes, jail

HEADERS = (("content-length", "2"),)

def drain(resp):
    """Read a response the way the relay does: read1 until it returns nothing."""
    body = b""
    while True:
        chunk = resp.read1(65536)
        if not chunk:
            return body
        body += chunk


def in_thread(fn):
    """Run `fn` on a new thread to its end; its result."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    t.join(20)
    return out[0]


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.fake = fakes.keeping()
        self.up = Upstream(fakes.start_upstream(self, self.fake))
        self.addCleanup(self.fake.release.set)

    def get(self, path="/x"):
        return self.up.send("POST", path, b"{}", HEADERS)

    def test_each_request_reaches_the_upstream_once_on_a_kept_connection(self):
        # http.client leaves a Content-Length response open after a read1 loop, so the next send on the kept connection
        # failed with ResponseNotReady after its request went out, and the retry sent it again: a billed duplicate.
        for _ in range(3):
            self.assertEqual(drain(self.get()), fakes.JSON_BODY)
        self.assertEqual(len(self.fake.ports), 3)
        self.assertEqual(len(set(self.fake.ports)), 1)

    def test_a_finished_connection_serves_the_next_request_from_any_thread(self):
        for _ in range(3):
            in_thread(lambda: drain(self.get()))
        self.assertEqual(len(self.fake.ports), 3)
        self.assertEqual(len(set(self.fake.ports)), 1, "each thread opened its own connection")

    def test_a_lease_that_ended_cannot_abort_the_next_leaseholder(self):
        # The ChatGPT reader thread finishes a response off the serving thread; the serving thread's later abort()
        # must not shut down the connection another thread has leased since.
        done, leased, aborted = threading.Event(), threading.Event(), threading.Event()

        def first():
            drain(self.get())
            done.set()
            leased.wait(10)
            self.up.abort()
            aborted.set()
        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(done.wait(10))
        resp = self.get()
        leased.set()
        self.assertTrue(aborted.wait(10))
        t.join(10)
        self.assertEqual(drain(resp), fakes.JSON_BODY)
        self.assertEqual(len(set(self.fake.ports)), 1, "the second request never rode the first's connection")

    def test_a_response_cut_mid_body_never_returns_to_the_pool(self):
        in_thread(lambda: drain(self.get()))

        def cut():
            resp = self.get("/big")
            resp.read1(10)
            resp.close()
        in_thread(cut)
        self.assertEqual(self.fake.ports[0], self.fake.ports[1], "the finished connection was not reused")
        self.assertEqual(in_thread(lambda: drain(self.get())), fakes.JSON_BODY)
        self.assertNotEqual(self.fake.ports[2], self.fake.ports[1])

    def test_a_stream_closed_before_its_first_read_never_returns_to_the_pool(self):
        # A ChatGPT reader stopped as it starts, or a response collected unread, closes a chunked body that still sits
        # whole on the socket; pooled, the next request on that connection would read the stream as its answer.
        up = Upstream(fakes.start_upstream(self))

        def json_reply():
            try:
                return drain(up.send("POST", "/v1/json", b"{}", HEADERS))
            except upstream.UpstreamError as error:
                return error.message
        in_thread(lambda: up.send("POST", "/v1/messages", b"{}", HEADERS).close())
        self.assertEqual(in_thread(json_reply), fakes.JSON_BODY)

    def test_an_abort_racing_the_end_of_its_lease_spares_the_next_leaseholder(self):
        # The ChatGPT reader thread finishes the body just as the serving thread aborts: the connection must not be
        # pooled, taken by another request and then shut down under it, which sends that request a second time.
        resp = self.get()
        late, real = [], socket.socket.shutdown

        def shutdown(sock, how):
            if not late:  # the moment between the abort's decision and its shutdown
                late.append(in_thread(lambda: drain(resp)))
                threading.Thread(target=lambda: late.append(drain(self.get("/hold"))), daemon=True).start()
                jail.wait_for(lambda: len(self.fake.ports) == 2, 3)
            return real(sock, how)
        with unittest.mock.patch.object(socket.socket, "shutdown", shutdown):
            self.up.abort()
        self.fake.release.set()
        self.assertTrue(jail.wait_for(lambda: len(late) == 2, 10), late)
        self.assertEqual(late[1], fakes.JSON_BODY)
        self.assertEqual(len(self.fake.ports), 2, "the request on the next lease went upstream twice")

    def test_a_shortened_timeout_does_not_follow_the_connection_to_its_next_request(self):
        # The model catalog fetch reads with 10 seconds; a stream that rides its connection next waits on a model.
        in_thread(lambda: drain(self.up.send("POST", "/x", b"{}", HEADERS, timeout=1)))
        self.assertEqual(in_thread(lambda: (drain(self.get()), self.up.local.conn.sock.gettimeout())[1]),
                         upstream.TIMEOUT_SECONDS)
        self.assertEqual(len(set(self.fake.ports)), 1)

    def test_idle_connections_are_capped(self):
        with unittest.mock.patch.object(pool, "MAX_IDLE", 2, create=True):
            gate, finish = threading.Barrier(5), threading.Event()
            self.addCleanup(finish.set)

            def one():
                resp = self.get()
                gate.wait(10)  # five responses in flight at once, on five connections
                drain(resp)
                finish.wait(10)  # the thread outlives its response, as a client connection's thread does
            threads = [threading.Thread(target=one) for _ in range(5)]
            for t in threads:
                t.start()
            self.assertTrue(jail.wait_for(lambda: self.fake.live[0] == 2), "open upstream connections: %d" % self.fake.live[0])
            finish.set()
            for t in threads:
                t.join(10)

    def test_idle_connections_are_closed_by_time_not_only_at_checkout(self):
        with unittest.mock.patch.object(pool, "IDLE_SECONDS", 0.3), \
                unittest.mock.patch.object(pool, "REAP_SECONDS", 0.1, create=True):
            finish = threading.Event()
            self.addCleanup(finish.set)
            threading.Thread(target=lambda: (drain(self.get()), finish.wait(10)), daemon=True).start()
            self.assertTrue(jail.wait_for(lambda: self.fake.live[0] == 1))
            self.assertTrue(jail.wait_for(lambda: self.fake.live[0] == 0, 3), "the idle connection stayed open")


if __name__ == "__main__":
    unittest.main()
