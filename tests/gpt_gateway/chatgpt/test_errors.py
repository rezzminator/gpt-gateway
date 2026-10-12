"""Every error-table row, its after-output SSE, bounded retry backoff, and server retry advice through a real gateway."""
import dataclasses
import json
import unittest
from unittest.mock import patch

from gpt_gateway import clock
from gpt_gateway.chatgpt import errors
from gpt_gateway.events import EventSplitter
from tests import fakes
from tests.gpt_gateway.chatgpt import test_route as route_tests


class ErrorsTest(unittest.TestCase):
    def test_a_refused_cache_retention_is_a_retry_that_drops_the_parameter(self):
        refused = [(400, {"error": {"message": "Unsupported parameter: 'prompt_cache_retention'",
                                    "type": "invalid_request_error", "code": "unsupported_parameter"}}),
                   (422, "prompt_cache_retention: extra fields not permitted"),
                   (None, {"type": "error", "status": 400, "error": {"message": "Unknown parameter: prompt_cache_retention",
                                                                      "code": "unknown_parameter"}})]
        for status, body in refused:
            with self.subTest(status=status):
                result = errors.classify(status, body, "before-output")
                self.assertEqual((result.retry, result.refresh, result.drop_reasoning, result.drop_retention),
                                 (True, False, False, True))
                self.assertEqual((result.client_status, result.client_body["error"]["type"]), (400, "invalid_request_error"))
        for status, body in ((400, {"error": {"message": "Unsupported parameter: 'seed'"}}), (500, "down"),
                             (400, {"error": {"code": "invalid_encrypted_content"}})):
            self.assertFalse(errors.classify(status, body, "before-output").drop_retention)

    def test_table(self):
        cases = [
            (None, "connection reset", 502, "api_error", True, False, False),
            (500, "down", 500, "api_error", True, False, False),
            (200, {"type": "response.failed", "response": {"error": None}},
             502, "api_error", True, False, False),
            (401, "expired", 401, "authentication_error", True, True, False),
            (429, {"error": {"type": "usage_limit_reached", "resets_at": 1060}},
             429, "rate_limit_error", False, False, False),
            (400, {"error": {"code": "context_length_exceeded"}},
             413, "request_too_large", False, False, False),
            (400, "Exceeds the context window", 413, "request_too_large", False, False, False),
            (400, {"error": {"code": "server_is_overloaded"}},
             529, "overloaded_error", False, False, False),
            (400, {"error": {"code": "slow_down"}}, 529, "overloaded_error", False, False, False),
            (400, {"error": {"code": "usage_not_included"}},
             403, "permission_error", False, False, False),
            (400, {"error": {"code": "invalid_encrypted_content"}},
             400, "invalid_request_error", True, False, True),
            (None, {"code": "stream_closed"}, 502, "api_error", True, False, False),
            (200, {"type": "response.incomplete", "response": {
                "incomplete_details": {"reason": "content_filter"}}},
             502, "api_error", False, False, False),
            (403, "denied", 403, "permission_error", False, False, False),
            (429, "too many", 429, "rate_limit_error", False, False, False),
            (400, "bad request", 400, "invalid_request_error", False, False, False),
            # Codex codex-api/src/sse/responses_error.rs:47-84: these response.failed codes are fatal.
            (None, {"type": "response.failed", "response": {"error": {"code": "invalid_prompt", "message": "bad"}}},
             400, "invalid_request_error", False, False, False),
            (None, {"type": "response.failed", "response": {"error": {"code": "cyber_policy"}}},
             400, "invalid_request_error", False, False, False),
            (None, {"type": "response.failed", "response": {"error": {"code": "insufficient_quota"}}},
             429, "rate_limit_error", False, False, False),
            (None, {"type": "response.failed", "response": {"error": {"code": "unrecognised_invented"}}},
             502, "api_error", True, False, False),
            # codex-api/src/sse/responses_error.rs:90-96: a stream's rate_limit_exceeded or slow_down is a rate
            # limit Codex always retries; the same code on an HTTP error answer keeps its status's row.
            (None, {"type": "response.failed", "response": {"error": {"code": "rate_limit_exceeded"}}},
             429, "rate_limit_error", True, False, False),
            (None, {"type": "response.failed", "response": {"error": {"code": "slow_down"}}},
             429, "rate_limit_error", True, False, False),
            (429, {"error": {"code": "rate_limit_exceeded"}}, 429, "rate_limit_error", False, False, False),
        ]
        with patch("gpt_gateway.clock.now", return_value=1000):
            for status, body, code, kind, retry, refresh, drop in cases:
                with self.subTest(status=status, body=body):
                    result = errors.classify(status, body, "before-output")
                    self.assertEqual(result.client_status, code)
                    self.assertEqual((result.retry, result.refresh, result.drop_reasoning),
                                     (retry, refresh, drop))
                    self.assertEqual(result.client_body["error"]["type"], kind)
                    self.assertIn("chatgpt upstream", result.client_body["error"]["message"])
                    after = errors.classify(status, body, "after-output")
                    events = EventSplitter().feed(after)
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["event"], "error")
                    self.assertEqual(events[0]["data"]["error"]["type"],
                                     "api_error" if status == 401 else kind)

    def test_a_context_overflow_names_the_context_window_so_claude_code_compacts(self):
        # Claude Code (2.1.296) turns a 413 into its reactive compaction only when the message names the context
        # window; any other 413 is a final "request too large".
        for body in ({"error": {"code": "context_length_exceeded", "message": "too big"}},
                     {"error": {"code": "context_length_exceeded"}},
                     {"type": "response.failed", "response": {"error": {"code": "context_length_exceeded"}}}):
            with self.subTest(body=body):
                result = errors.classify(400, body, "before-output")
                self.assertEqual(result.client_status, 413)
                self.assertIn("context window", result.client_body["error"]["message"].lower())

    def test_limit_headers_and_body_forms(self):
        body = {"error": {"type": "usage_limit_reached", "resets_at": 1060}}
        with patch("gpt_gateway.clock.now", return_value=1000.25):
            for head in (body, json.dumps(body), json.dumps(body).encode()):
                with self.subTest(head=head):
                    d = errors.classify(429, head, "before-output")
                    self.assertEqual(d.client_headers, {
                        "retry-after": "60", "x-should-retry": "false",
                        "anthropic-ratelimit-unified-status": "rejected",
                        "anthropic-ratelimit-unified-reset": "1060"})
        with patch("gpt_gateway.clock.now", return_value=2000):
            d = errors.classify(429, body, "before-output")
            self.assertIsInstance(d.client_headers, dict)
            self.assertEqual(d.client_headers["retry-after"], "0")

    def test_server_retry_advice_sets_the_delay_and_reaches_the_client(self):
        # Codex sleeps on the server's advice (codex-client/src/retry.rs; protocol/src/error.rs:395-457): the
        # Retry-After header first, then, for a stream's rate limit, "try again in Xs" in its message
        # (codex-api/src/sse/responses_error.rs:90-96, 138-143). retry-after-ms leads, as the OpenAI SDKs read it.
        failed = lambda code, message: {"type": "response.failed", "response": {"error": {"code": code, "message": message}}}
        cases = [  # status, body, upstream headers, delay, the advice headers the client receives
            (503, "down", [("Retry-After", "2")], 2.0, {"retry-after": "2", "retry-after-ms": "2000"}),
            (503, "down", {"retry-after": "1.25"}, 1.25, {"retry-after": "2", "retry-after-ms": "1250"}),
            (503, "down", [("retry-after-ms", "1500"), ("retry-after", "9")], 1.5,
             {"retry-after": "2", "retry-after-ms": "1500"}),
            (503, "down", [("Retry-After", "Sat, 10 Oct 2026 07:00:10 GMT")], 5.0,
             {"retry-after": "5", "retry-after-ms": "5000"}),
            (503, "down", [("Retry-After", "Sat, 10 Oct 2026 06:00:00 GMT")], 0.0, {"retry-after": "0", "retry-after-ms": "0"}),
            (503, "down", [("Retry-After", "-3")], 0.0, {"retry-after": "0", "retry-after-ms": "0"}),
            (503, "down", [("Retry-After", "soon")], None, {}),
            (503, "down", [("Retry-After", "nan")], None, {}),
            (503, "down", None, None, {}),
            (429, {"detail": "Rate limit exceeded"}, [("Retry-After", "7")], 7.0, {"retry-after": "7", "retry-after-ms": "7000"}),
            (None, failed("rate_limit_exceeded", "Rate limit reached. Please try again in 1.898s."), None, 1.898,
             {"retry-after": "2", "retry-after-ms": "1898"}),
            (None, failed("slow_down", "Please TRY AGAIN IN 250ms"), None, 0.25, {"retry-after": "1", "retry-after-ms": "250"}),
            (None, failed("rate_limit_exceeded", "try again in 3 seconds"), None, 3.0, {"retry-after": "3", "retry-after-ms": "3000"}),
            (None, failed("rate_limit_exceeded", "slow down"), None, None, {}),
            (None, failed("server_error", "try again in 4s"), None, None, {}),
        ]
        stamp = 1791615605.0  # Sat, 10 Oct 2026 07:00:05 GMT
        with patch("gpt_gateway.clock.now", return_value=stamp):
            for status, body, headers, delay, client in cases:
                with self.subTest(status=status, headers=headers, body=body):
                    d = errors.classify(status, body, "before-output", headers)
                    if delay is None:
                        self.assertIsNone(d.delay)
                    else:
                        self.assertIsNotNone(d.delay)
                        self.assertAlmostEqual(d.delay, delay)
                    self.assertEqual({k: v for k, v in d.client_headers.items() if k.startswith("retry-after")}, client)
            # usage_limit_reached keeps its own reset over a header's advice
            d = errors.classify(429, {"error": {"type": "usage_limit_reached", "resets_at": stamp + 60}},
                                "before-output", [("Retry-After", "7")])
            self.assertEqual(d.client_headers["retry-after"], "60")
            self.assertIsNotNone(d.delay)
            self.assertAlmostEqual(d.delay, 60)
            self.assertFalse(d.retry)

    def test_frozen_decision(self):
        d = errors.classify(None, {"code": "stream_closed"}, "before-output")
        self.assertTrue(d.retry)
        self.assertEqual(d.client_status, 502)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d.retry = True
        with self.assertRaises(TypeError):
            d.client_headers["x-should-retry"] = "true"
        with self.assertRaises(TypeError):
            d.client_body["error"]["message"] = "changed"
        self.assertIn("chatgpt upstream", json.dumps(d.client_body))

    def test_backoff_in_seconds_and_retry_cap(self):
        for n in range(1, 5):
            with self.subTest(n=n):
                self.assertAlmostEqual(errors.backoff(n, lambda a, b: 0.9), 0.2 * 2**(n-1) * 0.9)
                self.assertAlmostEqual(errors.backoff(n, lambda a, b: 1.1), 0.2 * 2**(n-1) * 1.1)
        for n in (0, 5, -1):
            with self.subTest(n=n), self.assertRaises(ValueError):
                errors.backoff(n)


class ErrorsRouteTest(unittest.TestCase):
    setUp = route_tests.RouteTest.setUp
    jwt = route_tests.RouteTest.jwt
    gateway = route_tests.RouteTest.gateway
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses

    def test_server_retry_advice_paces_the_retry_and_reaches_claude_code(self):
        # Codex sleeps until a 5xx's Retry-After before its retry and carries the advice of the answer it gives
        # up on; a 429 goes to Claude Code with the advice, and advice past MAX_ADVISED_SLEEP is never slept here.
        unavailable = {"error": {"message": "unavailable"}}
        self.fake.script = [(503, unavailable, {"Retry-After": "0.05"}), (200, fakes.responses_turn()),
                            (429, {"detail": "Rate limit exceeded"}, {"Retry-After": "3"}),
                            (503, unavailable, {"Retry-After": str(errors.MAX_ADVISED_SLEEP + 1)}),
                            (200, fakes.responses_event("response.failed", response={"instructions": "x" * 400, "status": "failed", "error": {
                                "code": "rate_limit_exceeded", "message": "Please try again in 0.04s."}})),
                            (200, fakes.responses_turn())]
        rt, port = self.gateway()
        slept, real = [], clock.sleep
        with patch("gpt_gateway.clock.sleep", side_effect=lambda s: (slept.append(s), real(min(s, 0.01)))), \
                patch("gpt_gateway.chatgpt.errors.backoff", return_value=0) as backoff:
            answers = [self.send(port)[0] for _ in range(4)]
        self.assertEqual([x.status for x in answers], [200, 429, 503, 200])
        backoff.assert_not_called()
        self.assertIn(0.05, slept)
        self.assertIn(0.04, slept)
        self.assertNotIn(errors.MAX_ADVISED_SLEEP + 1, slept)
        self.assertEqual([(x.getheader("retry-after"), x.getheader("retry-after-ms")) for x in answers[1:]],
                         [("3", "3000"), ("%d" % (errors.MAX_ADVISED_SLEEP + 1), "%d" % ((errors.MAX_ADVISED_SLEEP + 1) * 1000)),
                          (None, None)])
        self.assertEqual(len(self.responses()), 6)
        failed = [(e["fields"]["status"], e["fields"]["attempt"], e["fields"]["action"], e["fields"]["retry_after"],
                   e["fields"]["error_head"]) for e in fakes.events(rt) if e["kind"] in ("retry", "final_error")]
        for attempt in ((503, 1, "retry", 0.05), (429, 1, "final", 3), (503, 1, "final", errors.MAX_ADVISED_SLEEP + 1),
                        (200, 1, "retry", 0.04)):
            self.assertIn(attempt, [x[:4] for x in failed])
        # a stream failure records its failing event, never the stream's opening bytes or the response it carries
        self.assertIn('{"type": "response.failed", "status": "failed", "error": {"code": "rate_limit_exceeded", '
                      '"message": "Please try again in 0.04s."}}', [x[4] for x in failed])
