"""Tests for gpt_gateway.response: the in-memory response surface the relay reads."""
from http import HTTPStatus
import unittest

from gpt_gateway.response import Response


class ResponseTest(unittest.TestCase):
    def test_json_case_insensitive_headers_and_small_reads(self):
        response = Response.json(413, {"type": "error"}, {"Retry-After": "4"})
        self.assertEqual(response.getheader("retry-after"), "4")
        self.assertEqual(response.getheader("missing", "default"), "default")
        self.assertEqual(response.reason, HTTPStatus(413).phrase)
        self.assertEqual(response.read1(2) + response.read(), b'{"type":"error"}')
        self.assertEqual(response.read1(20), b"")

    def test_bytes_with_their_own_content_type(self):
        response = Response(200, b"event: message_stop\n\n", {"content-type": "text/event-stream"})
        self.assertEqual((response.reason, response.getheaders()), ("OK", [("content-type", "text/event-stream")]))
        self.assertEqual(response.read(), b"event: message_stop\n\n")
        self.assertEqual(Response(599, b"").reason, "Error")


if __name__ == "__main__":
    unittest.main()
