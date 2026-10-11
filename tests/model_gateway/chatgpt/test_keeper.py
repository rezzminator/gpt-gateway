"""The keeper: idle pooled sockets are answered and pinged, a socket it finds dead leaves the pool before the next request,
and it never reads a socket a request holds. Real gateway exchanges against a fake backend that ends idle sockets two ways."""
import threading
import time
import unittest
from unittest.mock import patch

from model_gateway import handles
from model_gateway.chatgpt import keeper, websocket
from tests import fakes, jail
from tests.model_gateway.chatgpt import test_route as route_tests
from tests.model_gateway.chatgpt import test_websocket as ws_tests

WAIT = 5.0  # bound on every wait for the keeper's tick


class FakeSocket:
    """A WebSocket stand-in: counts how many `alive` calls overlap, and can hold one until released."""

    def __init__(self, answer=True, hold=None):
        self.answer, self.hold = answer, hold
        self.calls, self.running, self.overlap, self.pinged = 0, 0, 0, []
        self.entered = threading.Event()
        self.guard = threading.Lock()
        self.closed = []

    def alive(self, ping=False):
        with self.guard:
            self.calls += 1
            self.running += 1
            self.overlap = max(self.overlap, self.running)
            self.pinged.append(ping)
        self.entered.set()
        if self.hold is not None:
            self.hold.wait(WAIT)
        with self.guard:
            self.running -= 1
        return self.answer

    def close(self):
        with self.guard:
            self.closed.append("close" if not self.running else "close-while-read")

    abort = close


class KeeperUnitTest(unittest.TestCase):
    def setUp(self):
        handles.THROTTLE.clear()
        self.events = []
        self.sockets = websocket.Sockets(None, lambda kind, **fields: self.events.append((kind, fields)))
        self.addCleanup(self.sockets.stop)

    def pooled(self, ws, key=("lane", True, "login")):
        kept = websocket.Kept(ws, key, [])
        kept.out = True
        self.sockets.busy[key] += 1
        self.sockets.checkin(kept)
        return kept

    def closes(self):
        return [f["reason"] for kind, f in self.events if kind == "websocket_close"]

    def test_a_checked_out_socket_is_never_read(self):
        ws = FakeSocket()
        key = ("lane", True, "login")
        kept = self.pooled(ws)
        self.assertIs(self.sockets.checkout(key), kept)
        before = ws.calls
        self.sockets.keeper.tend(kept)
        self.assertEqual((ws.calls, kept.out, self.closes()), (before, True, []))

    def test_a_checkout_waits_for_the_keeper_reading_the_socket_and_never_reads_it_alongside(self):
        hold = threading.Event()
        ws = FakeSocket(hold=hold)
        key = ("lane", True, "login")
        kept = self.pooled(ws)
        tending = threading.Thread(target=self.sockets.keeper.tend, args=(kept,))
        tending.start()
        self.assertTrue(ws.entered.wait(WAIT), "the keeper never read the socket")
        got = []
        taking = threading.Thread(target=lambda: got.append(self.sockets.checkout(key)))
        taking.start()
        taking.join(0.3)
        self.assertTrue(taking.is_alive(), "the checkout read the socket the keeper was reading")
        hold.set()
        tending.join(WAIT)
        taking.join(WAIT)
        self.assertEqual((ws.overlap, got == [kept], ws.calls), (1, True, 2))

    def test_a_socket_the_keeper_finds_dead_leaves_the_pool_once_with_its_finding(self):
        ws = FakeSocket(answer=False)
        kept = self.pooled(ws)
        self.sockets.keeper.tend(kept)
        self.sockets.keeper.tend(kept)
        self.assertEqual((self.closes(), list(self.sockets.idle), ws.closed), (["keeper-closed"], [], ["close"]))

    def test_a_socket_closed_out_of_the_pool_waits_for_the_keeper_reading_it(self):
        hold = threading.Event()
        ws = FakeSocket(hold=hold)
        kept = self.pooled(ws)
        tending = threading.Thread(target=self.sockets.keeper.tend, args=(kept,))
        tending.start()
        self.assertTrue(ws.entered.wait(WAIT), "the keeper never read the socket")
        with self.sockets.lock:  # as pause, a lane's spare or the pool-full trim take it
            del self.sockets.idle[kept]
        closing = threading.Thread(target=self.sockets.close, args=(kept, "paused"))
        closing.start()
        closing.join(0.3)
        hold.set()
        tending.join(WAIT)
        closing.join(WAIT)
        self.assertEqual((ws.closed, self.closes()), (["close"], ["paused"]))

    def test_a_keeper_thread_that_cannot_start_is_one_event_and_the_next_checkin_tries_again(self):
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
            self.pooled(FakeSocket(), key=("one", True, "login"))
        self.assertEqual([kind for kind, _ in self.events], ["keeper_failed"])
        self.assertIn("can't start new thread", self.events[0][1]["error"])
        self.assertIsNone(self.sockets.keeper.thread)
        self.pooled(FakeSocket(), key=("two", True, "login"))
        self.assertTrue(self.sockets.keeper.thread.is_alive())

    def test_a_socket_at_max_age_is_closed_as_keeper_age_without_being_read(self):
        ws = FakeSocket()
        kept = self.pooled(ws)
        with patch.object(websocket, "MAX_AGE", 0):
            self.sockets.keeper.tend(kept)
        self.assertEqual((self.closes(), ws.calls), (["keeper-age"], 0))

    def test_it_pings_a_socket_only_after_it_went_the_interval_without_a_frame(self):
        ws = FakeSocket()
        kept = self.pooled(ws)
        self.sockets.keeper.tend(kept)
        kept.touched -= keeper.PING_SECONDS
        self.sockets.keeper.tend(kept)
        self.sockets.keeper.tend(kept)
        self.assertEqual(ws.pinged, [False, True, False])

    def test_a_failure_in_the_keeper_is_one_event_and_the_thread_goes_on(self):
        with patch.object(keeper, "TICK_SECONDS", 0.02), patch.object(keeper.Keeper, "tend", side_effect=RuntimeError("boom")):
            self.pooled(FakeSocket())
            self.assertTrue(jail.wait_for(lambda: any(kind == "keeper_failed" for kind, _ in self.events), WAIT))
            time.sleep(0.1)
        failed = [f for kind, f in self.events if kind == "keeper_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("boom", failed[0]["error"])
        self.assertTrue(self.sockets.keeper.thread.is_alive())


class KeeperRouteTest(unittest.TestCase):
    """The fake backend ends an idle socket (a) when its pings go unanswered or (b) on a hard timer, pings or none."""
    jwt = route_tests.RouteTest.jwt
    gateway = route_tests.RouteTest.gateway
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses
    endings = ws_tests.WebSocketRouteTest.endings
    close_reasons = ws_tests.WebSocketRouteTest.close_reasons

    def setUp(self):
        route_tests.RouteTest.setUp(self)
        handles.THROTTLE.clear()
        self.env["MODEL_GATEWAY_CHATGPT_TRANSPORT"] = "websocket"
        for name, value in (("TICK_SECONDS", 0.05), ("PING_SECONDS", 0.1)):
            patcher = patch.object(keeper, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def closes_before_the_next_request(self, rt):
        jail.wait_for(lambda: fakes.events(rt, "websocket_close", settle=0), WAIT)
        return self.close_reasons(rt)

    def test_a_server_that_ends_unanswered_sockets_does_not_end_the_idle_one(self):
        self.fake.idle = [dict(ping=0.15, miss=0.6)]
        rt, port = self.gateway()
        self.assertEqual(self.send(port)[0].status, 200)
        time.sleep(1.5)  # two and a half times the silence the server tolerates
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual(len(self.fake.upgrades), 1)
        self.assertEqual(self.close_reasons(rt), [])
        self.assertEqual(self.endings(rt), [(0, 0, "ws", ws_tests.FULL), (1, 0, "ws", ws_tests.FULL)])

    def test_a_socket_the_server_ended_on_a_timer_is_found_and_replaced_before_the_next_request(self):
        self.fake.idle = [dict(hard=0.4)]
        rt, port = self.gateway()
        self.assertEqual(self.send(port)[0].status, 200)
        reasons = self.closes_before_the_next_request(rt)
        self.assertEqual(reasons, ["keeper-closed"])
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual(len(self.fake.upgrades), 2)
        self.assertEqual(self.close_reasons(rt), ["keeper-closed"])
        self.assertEqual(self.endings(rt), [(0, 0, "ws", ws_tests.FULL), (0, 0, "ws", ws_tests.FULL)])

    def test_a_failing_keeper_leaves_the_requests_alone(self):
        rt, port = self.gateway()
        with patch.object(keeper.Keeper, "tend", side_effect=RuntimeError("boom")):
            self.assertEqual(self.send(port)[0].status, 200)
            self.assertTrue(jail.wait_for(lambda: fakes.events(rt, "keeper_failed", settle=0), WAIT))
            self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual((len(self.fake.upgrades), self.close_reasons(rt)), (1, []))
        self.assertEqual([r["reused"] for r in fakes.gpt_rows(rt)], [0, 1])


if __name__ == "__main__":
    unittest.main()
