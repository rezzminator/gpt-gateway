"""Tests for gpt_gateway.headers: the hooks' read-only view and the debug selection drop every secret."""
import unittest

from gpt_gateway.headers import Headers, debug_headers

SENT = [("Authorization", "Bearer x"), ("x-api-key", "k"), ("Cookie", "c"), ("Proxy-Authorization", "p"),
        ("X-Claude-Code-Agent-Id", "a1"), ("anthropic-beta", "b"), ("User-Agent", "u"), ("Content-Type", "j")]


class HeadersTest(unittest.TestCase):
    def test_secrets_are_absent(self):
        h = Headers(SENT)
        self.assertEqual(set(h), {"x-claude-code-agent-id", "anthropic-beta", "user-agent", "content-type"})
        for name in ("authorization", "Authorization", "x-api-key", "cookie", "proxy-authorization"):
            self.assertNotIn(name, h)
            self.assertIsNone(h.get(name))

    def test_lookup_ignores_case_and_keeps_the_first_value(self):
        h = Headers(SENT + [("x-claude-code-agent-id", "second")])
        self.assertEqual(h["x-claude-code-agent-id"], "a1")
        self.assertEqual(h.get("X-CLAUDE-CODE-AGENT-ID"), "a1")
        self.assertIsNone(h.get(1))

    def test_read_only(self):
        h = Headers(SENT)
        with self.assertRaises(TypeError):
            h["x"] = "y"
        with self.assertRaises(AttributeError):
            h._items = {}

    def test_debug_headers_keep_no_secret(self):
        kept = debug_headers(SENT)
        self.assertEqual(set(k.lower() for k in kept), {"x-claude-code-agent-id", "anthropic-beta", "user-agent"})


if __name__ == "__main__":
    unittest.main()
