"""Every error-table row, its after-output SSE, and bounded retry backoff."""
import dataclasses
import json
import unittest
from unittest.mock import patch

from model_gateway.chatgpt import errors
from model_gateway.events import EventSplitter


class ErrorsTest(unittest.TestCase):
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
            (503, {"code": "empty_completion"}, 503, "api_error", True, False, False),
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
        ]
        with patch("model_gateway.clock.now", return_value=1000):
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

    def test_limit_headers_and_body_forms(self):
        body = {"error": {"type": "usage_limit_reached", "resets_at": 1060}}
        with patch("model_gateway.clock.now", return_value=1000.25):
            for head in (body, json.dumps(body), json.dumps(body).encode()):
                with self.subTest(head=head):
                    d = errors.classify(429, head, "before-output")
                    self.assertEqual(d.client_headers, {
                        "retry-after": "60", "x-should-retry": "false",
                        "anthropic-ratelimit-unified-status": "rejected",
                        "anthropic-ratelimit-unified-reset": "1060"})
        with patch("model_gateway.clock.now", return_value=2000):
            d = errors.classify(429, body, "before-output")
            self.assertIsInstance(d.client_headers, dict)
            self.assertEqual(d.client_headers["retry-after"], "0")

    def test_empty_no_client_retry_and_frozen_decision(self):
        d = errors.classify(503, {"code": "empty_completion"}, "before-output")
        self.assertTrue(d.retry)
        self.assertEqual(d.client_status, 503)
        self.assertEqual(d.client_headers, {"x-should-retry": "false"})
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
