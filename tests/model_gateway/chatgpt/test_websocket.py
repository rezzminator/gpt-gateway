"""The Responses WebSocket: the response.create message, the pool's pause, and real gateway exchanges over kept sockets."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from model_gateway.capture import SENT_BODY
from model_gateway.chatgpt import websocket
from model_gateway.chatgpt.auth import Credentials
from tests import fakes
from tests.model_gateway.chatgpt import test_errors as errors_tests
from tests.model_gateway.chatgpt import test_route as route_tests

CALL = dict(role="assistant", content=[dict(type="tool_use", id="call_invented", name="lookup", input={"city": "Harbor"})])
RESULT = dict(role="user", content=[dict(type="tool_result", tool_use_id="call_invented", content="sunny")])


class MessageTest(unittest.TestCase):
    def test_the_body_rides_verbatim_after_the_type_and_the_token_in_client_metadata(self):
        body = b'{"model":"m","input":[{"type":"message"}],"store":false}'
        self.assertEqual(websocket.message(body), b'{"type":"response.create","model":"m","input":[{"type":"message"}],"store":false}')
        framed = websocket.message(body, "ts-invented")
        self.assertTrue(framed.startswith(b'{"type":"response.create","model":"m",'))
        self.assertEqual(json.loads(framed)["client_metadata"], {"x-codex-turn-state": "ts-invented"})
        self.assertEqual(json.loads(websocket.message(b"{}", "t")), {"type": "response.create", "client_metadata": {"x-codex-turn-state": "t"}})
        self.assertEqual(websocket.message(b"{ }"), b'{"type":"response.create"}')
        for bad in (b"[]", b"{", b""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                websocket.message(bad)

    def test_a_socket_serves_one_lane_one_handshake_shape_and_one_login(self):
        one, other = Credentials("token-a", "account-a"), Credentials("token-b", "account-a")
        self.assertEqual(websocket.socket_key("lane", True, one), websocket.socket_key("lane", True, Credentials("token-a", "account-a")))
        for changed in (websocket.socket_key("other", True, one), websocket.socket_key("lane", False, one), websocket.socket_key("lane", True, other)):
            self.assertNotEqual(websocket.socket_key("lane", True, one), changed)
        self.assertNotIn("token-a", repr(websocket.socket_key("lane", True, one)))

    def test_unserved_statuses(self):
        self.assertEqual([s for s in (101, 200, 301, 400, 401, 403, 404, 405, 426, 429, 500) if websocket.unserved(s)],
                         [200, 301, 400, 404, 405, 426])


class PauseTest(unittest.TestCase):
    def test_five_transport_failures_in_a_row_pause_the_socket_for_http_and_a_success_resets_the_count(self):
        lines = []
        sockets = websocket.Sockets(None, lines.append)
        with patch("model_gateway.clock.monotonic", return_value=1000.0):
            for _ in range(4):
                sockets.failed("reset")
            sockets.succeeded()
            for _ in range(4):
                sockets.failed("reset")
            self.assertTrue(sockets.usable())
            sockets.failed("reset by peer")
            self.assertFalse(sockets.usable())
        self.assertEqual(lines, ["    gpt websocket paused seconds=600 reason=5 transport failures in a row, the last reset by peer; "
                                 "HTTP serves until then"])
        with patch("model_gateway.clock.monotonic", return_value=1000.0 + websocket.PAUSE_SECONDS):
            self.assertTrue(sockets.usable())


class WebSocketRouteTest(unittest.TestCase):
    jwt = route_tests.RouteTest.jwt
    gateway = route_tests.RouteTest.gateway
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses

    def setUp(self):
        route_tests.RouteTest.setUp(self)
        self.env["MODEL_GATEWAY_CHATGPT_TRANSPORT"] = "websocket"

    def lines(self, rt, prefix="    gpt lane="):
        return [line for line in fakes.logged(rt).splitlines() if prefix in line]

    def test_a_lane_rides_one_kept_socket_and_another_lane_its_own(self):
        rt, port = self.gateway()
        for _ in range(3):
            self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual(self.send(port, headers=dict(self.headers, **{"x-claude-code-agent-id": "agent-other"}))[0].status, 200)
        self.assertEqual(len(self.fake.upgrades), 2)
        self.assertEqual([x["method"] for x in self.responses()], ["WS"] * 4)
        endings = [line.split(" reused=", 1)[1] for line in self.lines(rt)]
        self.assertEqual(endings, ["0 stale=0 transport=ws", "1 stale=0 transport=ws", "1 stale=0 transport=ws",
                                   "0 stale=0 transport=ws"])
        for h in self.fake.upgrades:
            self.assertEqual(h["openai-beta"], "responses_websockets=2026-02-06")
            self.assertEqual({h["session-id"], h["thread-id"], h["x-client-request-id"]}, {h["session-id"]})
            self.assertEqual((h["x-openai-subagent"], h["x-openai-internal-codex-responses-lite"]), ("collab_spawn", "true"))
            self.assertEqual((h["authorization"], h["originator"]), ("Bearer " + self.token, "codex_cli_rs"))
            self.assertFalse({"x-api-key", "anthropic-version", "anthropic-beta"} & set(h))
        sent = sorted(Path(self.capture).rglob(SENT_BODY))
        self.assertEqual([b'{"type":"response.create",' + path.read_bytes()[1:] for path in sent],
                         [x["body"] for x in self.responses()])
        log = fakes.logged(rt)
        self.assertEqual(log.count("    gpt websocket open lane="), 2)
        self.assertNotIn("gpt websocket close", log)

    def test_a_turn_state_token_from_response_metadata_rides_the_turns_next_messages(self):
        self.fake.issued = ["ts-ws"]
        rt, port = self.gateway()
        self.send(port)
        self.send(port, dict(self.body, messages=self.body["messages"] + [CALL, RESULT]))
        first, second = (json.loads(x["body"]) for x in self.responses())
        self.assertNotIn("client_metadata", first)
        self.assertEqual(second["client_metadata"], {"x-codex-turn-state": "ts-ws"})
        self.assertNotIn("x-codex-turn-state", self.fake.upgrades[0])
        self.assertEqual([line.rsplit(" stale=0 ", 1)[1] for line in self.lines(rt)],
                         ["turn_state=issued transport=ws", "turn_state=sent transport=ws"])

    def test_a_kept_socket_that_dies_before_its_answer_or_while_idle_gives_way_silently(self):
        self.fake.vanish, self.fake.hangup = [False, True], [False, True]
        rt, port = self.gateway()
        self.assertEqual([self.send(port)[0].status for _ in range(3)], [200, 200, 200])
        self.assertEqual(len(self.fake.upgrades), 3)
        self.assertEqual([line.split(" attempts=", 1)[1][:1] + line.split(" reused=", 1)[1] for line in self.lines(rt)],
                         ["10 stale=0 transport=ws", "10 stale=1 transport=ws", "10 stale=0 transport=ws"])
        log = fakes.logged(rt)
        self.assertNotIn("gpt error", log)
        self.assertIn("reason=closed-while-idle", log)

    def test_the_connection_limit_reopens_and_a_wrapped_refusal_answers_as_its_status(self):
        limit = {"error": {"code": "websocket_connection_limit_reached", "message": "limit reached"}}
        self.fake.script = [(200, fakes.responses_turn()), (400, limit), (200, fakes.responses_turn()),
                            (429, {"detail": "Rate limit exceeded"}, {"Retry-After": "3"}), (200, fakes.responses_turn())]
        rt, port = self.gateway()
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual(self.send(port)[0].status, 200)
        refused = self.send(port)[0]
        self.assertEqual((refused.status, refused.getheader("retry-after")), (429, "3"))
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual(len(self.fake.upgrades), 3)
        self.assertEqual([line.split(" reused=", 1)[1] for line in self.lines(rt)],
                         ["0 stale=0 transport=ws", "0 stale=1 transport=ws", "0 stale=0 transport=ws"])
        log = fakes.logged(rt)
        self.assertIn("gpt error lane=", log)
        self.assertIn("status=429 attempt=1 action=final retry_after=3", log)
        self.assertIn("reason=abort", log)

    def test_an_upgrade_the_backend_does_not_serve_pauses_the_socket_and_http_answers(self):
        self.fake.refuse = [426]
        rt, port = self.gateway()
        self.assertEqual([self.send(port)[0].status for _ in range(2)], [200, 200])
        self.assertEqual(len(self.fake.upgrades), 1)
        self.assertEqual([x["method"] for x in self.responses()], ["POST", "POST"])
        self.assertEqual([line.rsplit(" ", 1)[1] for line in self.lines(rt)], ["transport=http"] * 2)
        self.assertIn("gpt websocket paused seconds=3600 reason=the upgrade answered 426", fakes.logged(rt))

    def test_a_refused_upgrade_with_a_status_of_its_own_answers_as_that_status(self):
        self.fake.refuse = [503] * 5
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            resp, data = self.send(port)
        self.assertEqual(resp.status, 503)
        self.assertEqual(len(self.fake.upgrades), 5)
        self.assertEqual(self.responses(), [])
        self.assertIn("status=503 attempt=5 action=final", fakes.logged(rt))

    def test_five_transport_failures_pause_the_socket_and_the_next_request_goes_over_http(self):
        self.fake.vanish = [True] * 5
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            failed = self.send(port)[0]
            served = self.send(port)[0]
        self.assertEqual((failed.status, served.status), (502, 200))
        self.assertEqual([x["method"] for x in self.responses()], ["WS"] * 5 + ["POST"])
        self.assertEqual([line.rsplit(" ", 1)[1] for line in self.lines(rt)], ["transport=http"])
        self.assertIn("gpt websocket paused seconds=600 reason=5 transport failures in a row", fakes.logged(rt))

    def test_a_hang_up_after_output_counts_one_failure_and_a_completed_response_resets_the_count(self):
        partial = fakes.responses_event("response.created") + fakes.responses_event(
            "response.output_text.delta", output_index=1, delta="partial")
        self.fake.script = [(200, fakes.Cut(partial))]
        rt, port = self.gateway()
        resp, data = self.send(port)
        self.assertEqual(resp.status, 200)
        self.assertIn(b"event: error", data)
        self.assertEqual(rt.chatgpt.client.sockets.failures, 1)
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual(rt.chatgpt.client.sockets.failures, 0)
        self.assertEqual(len(self.fake.upgrades), 2)
        self.assertIn("reason=ended-early", fakes.logged(rt))

    # The route's error, retry and refresh behaviour, over the WebSocket.
    test_refresh_once_and_oauth_identity = route_tests.RouteTest.test_refresh_once_and_oauth_identity
    test_usage_limit_and_precommit_context_error = route_tests.RouteTest.test_usage_limit_and_precommit_context_error
    test_nonstream_and_login_failure = route_tests.RouteTest.test_nonstream_and_login_failure
    test_committed_failure_streaming_and_nonstream = route_tests.RouteRetryTest.test_committed_failure_streaming_and_nonstream
    test_explicit_empty_completion_exhausts_before_output = route_tests.RouteRetryTest.test_explicit_empty_completion_exhausts_before_output
    test_server_retry_advice_paces_the_retry_and_reaches_claude_code = (
        errors_tests.ErrorsRouteTest.test_server_retry_advice_paces_the_retry_and_reaches_claude_code)


if __name__ == "__main__":
    unittest.main()
