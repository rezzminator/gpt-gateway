"""Golden streams, every chunk boundary, error termination and reasoning replay."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gpt_gateway.chatgpt import signature, stream
from gpt_gateway.events import MAX_PENDING, EventSplitter

FIXTURES = Path(__file__).with_name("fixtures")


def upstream(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps(dict(type=kind, **fields),
            ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()


class StreamTest(unittest.TestCase):
    def setUp(self):
        timer = patch("gpt_gateway.clock.monotonic", return_value=0)
        identity = patch("uuid.uuid4", return_value=SimpleNamespace(hex="fixture"))
        self.time = timer.start()
        identity.start()
        self.addCleanup(timer.stop)
        self.addCleanup(identity.stop)

    def translator(self):
        return stream.Translator("gpt-test", {"lookup_short": "lookup_original"}, 25)

    def test_goldens_and_all_chunk_boundaries(self):
        for name in ("recorded_text", "recorded_tool", "reasoning", "incomplete", "failed"):
            original = (FIXTURES / (name + ".responses.sse")).read_bytes()
            expected = (FIXTURES / (name + ".anthropic.sse")).read_bytes()
            whole = self.translator()
            self.assertEqual(whole.feed(original) + whole.finish(), expected)
            for boundary in range(len(original) + 1):
                with self.subTest(name=name, boundary=boundary):
                    t = self.translator()
                    result = t.feed(original[:boundary]) + t.feed(original[boundary:]) + t.finish()
                    self.assertEqual(result, expected)
                    self.assertTrue(t.emitted_any)
                    self.assertIsNotNone(t.terminal)
                    self.assertEqual(t.feed(original), b"")
                    self.assertEqual(t.finish(), b"")

    def test_held_prelude_is_incremental_and_final_usage_is_exact(self):
        t = self.translator()
        first = t.feed(b"e")
        self.assertEqual(first, b"")
        self.assertFalse(t.emitted_any)
        body = (FIXTURES / "recorded_text.responses.sse").read_bytes()
        remainder = b"".join(t.feed(bytes([b])) for b in body[1:]) + t.finish()
        self.assertEqual(first + remainder, (FIXTURES / "recorded_text.anthropic.sse").read_bytes())
        self.assertEqual(EventSplitter().feed(remainder)[0]["data"]["message"]["usage"],
                         {"input_tokens": 25, "output_tokens": 0})
        self.assertEqual(t.usage, {"input_tokens": 60, "output_tokens": 12,
                                 "cache_read_input_tokens": 30, "cache_creation_input_tokens": 10})
        self.assertEqual(t.input_tokens, 100)
        self.assertEqual(t.cache_ratio, 0.3)

    def test_reasoning_tokens_come_from_the_completed_usage_or_stay_absent(self):
        for details, expected in (({"output_tokens_details": {"reasoning_tokens": 7}}, 7),
                                  ({"output_tokens_details": {"reasoning_tokens": 0}}, 0),
                                  ({"output_tokens_details": {}}, None), ({}, None)):
            with self.subTest(details=details):
                t = self.translator()
                t.feed(upstream("response.output_text.delta", output_index=0, delta="x"))
                t.feed(upstream("response.completed", response={"usage": dict(input_tokens=10, output_tokens=4,
                                                                              **details)}))
                self.assertEqual((t.terminal, t.reasoning_tokens), ("response.completed", expected))

    def test_accumulate_and_emptied_reasoning_round_trip(self):
        for name, reason in (("recorded_text", "end_turn"), ("recorded_tool", "tool_use"),
                             ("reasoning", "end_turn"), ("incomplete", "max_tokens")):
            with self.subTest(name=name):
                message = stream.accumulate((FIXTURES / (name + ".anthropic.sse")).read_bytes())
                self.assertEqual(message.get("stop_reason"), reason)
                self.assertEqual(message["usage"]["input_tokens"], 60)
                self.assertEqual(message["id"], "msg_fixture")
                for item in message["content"]:
                    if item["type"] == "thinking":
                        item["thinking"] = ""
                        self.assertEqual(signature.decode(item["signature"]),
                                         ("gpt-test", "rs_fixture", "encrypted-fixture"))
                    elif item["type"] == "tool_use":
                        self.assertEqual(item["input"], {"city": "Harbor"})
                        self.assertEqual(item["name"], "lookup_original")
        message = stream.accumulate((FIXTURES / "reasoning.anthropic.sse").read_bytes())
        self.assertIn("content", message)
        self.assertEqual(message["content"][0]["thinking"], "\n\nPlan α\n\nCheck β")
        message = stream.accumulate((FIXTURES / "recorded_text.anthropic.sse").read_bytes())
        self.assertEqual(message["content"][1], {"type": "text", "text": "Hello!"})

    def test_accumulate_surfaces_errors_and_truncation(self):
        bad = (FIXTURES / "failed.anthropic.sse").read_bytes()
        self.assertEqual(stream.accumulate(bad).get("type"), "error")
        for data in (b"", (FIXTURES / "recorded_text.anthropic.sse").read_bytes()[:-60]):
            with self.subTest(data=data[:20]):
                error = stream.accumulate(data)
                self.assertEqual(error.get("type"), "error")
                self.assertIn("chatgpt upstream", error["error"]["message"])

    def test_control_events_and_silence_heartbeat(self):
        for kind in ("response.created", "response.in_progress", "codex.rate_limits", "keepalive"):
            with self.subTest(kind=kind):
                t = self.translator()
                self.assertEqual(t.feed(upstream(kind)), b"")
                self.assertFalse(t.emitted_any)
                committed = t.feed(upstream("response.output_text.delta", output_index=0, delta="x"))
                self.assertIn(b"event: ping", committed)
        t = self.translator()
        t.feed(upstream("response.output_text.delta", output_index=0, delta="x"))
        self.time.return_value = 14.99
        self.assertEqual(t.idle(), b"")
        self.time.return_value = 15
        self.assertEqual(t.idle(), b'event: ping\ndata: {"type":"ping"}\n\n')
        self.assertEqual(t.idle(), b"")
        self.time.return_value = 29
        t.feed(b": alive\n\n")
        self.time.return_value = 30
        self.assertEqual(t.idle(), b"")
        self.time.return_value = 44
        self.assertIn(b"event: ping", t.idle())

    def test_idle_timeout_is_an_error(self):
        t = self.translator()
        t.feed(upstream("response.output_text.delta", output_index=0, delta="x"))
        self.time.return_value = 300
        self.assertIn(b"event: error", t.idle())
        self.assertFalse(t.error.retry)
        self.assertEqual(t.idle(), b"")

    def test_a_bad_event_costs_only_itself_and_is_noted(self):
        text = [{"type": "text", "text": "Hello"}]
        unsigned = [{"type": "thinking", "thinking": "", "signature": ""}]
        for name, bad, content, note in (
                ("invalid JSON", b"data: invalid\n\n", text,
                 "skipped Responses event unnamed: ValueError data is not a JSON object"),
                ("not an object", b"event: odd\ndata: []\n\n", text,
                 "skipped Responses event odd: ValueError data is not a JSON object"),
                ("text without delta", upstream("response.output_text.delta", output_index=1), text,
                 "skipped Responses event response.output_text.delta: KeyError 'delta'"),
                ("summary delta not text", upstream("response.reasoning_summary_text.delta", output_index=1, delta=7), text,
                 "skipped Responses event response.reasoning_summary_text.delta: TypeError delta is int, not str"),
                ("call without name", upstream("response.output_item.done", output_index=1, item={
                    "type": "function_call", "call_id": "call_1", "arguments": "{}"}), text,
                 "skipped Responses event response.output_item.done: KeyError 'name'"),
                ("call arguments not text", upstream("response.output_item.done", output_index=1, item={
                    "type": "function_call", "call_id": "call_1", "name": "lookup_short", "arguments": {}}), text,
                 "skipped Responses event response.output_item.done: TypeError function_call name, call_id and "
                 "arguments must be strings"),
                ("reasoning without id", upstream("response.output_item.done", output_index=1, item={
                    "type": "reasoning", "encrypted_content": "opaque"}), text + unsigned,
                 "reasoning sent unsigned: signature item_id must be a non-empty string, got None"),
                ("late delta", upstream("response.output_text.delta", output_index=0, delta="late"), text,
                 "skipped Responses event response.output_text.delta: output 0 already closed"),
                ("oversized", b"data: " + b"x" * MAX_PENDING, text, "skipped an SSE event over the size limit")):
            with self.subTest(name=name):
                t = self.translator()
                out = t.feed(upstream("response.output_text.delta", output_index=0, delta="Hello"))
                out += t.feed(upstream("response.output_item.done", output_index=0, item={"type": "message"}))
                out += t.feed(bad) + t.feed(upstream("response.completed", response={"usage": {"output_tokens": 3}}))
                message = stream.accumulate(out)
                self.assertEqual((t.terminal, t.error, message.get("content"), message.get("stop_reason"), t.notes),
                                 ("response.completed", None, content, "end_turn", [note]))

    def test_a_usage_count_that_does_not_read_is_unknown_and_the_response_completes(self):
        t = self.translator()
        out = t.feed(upstream("response.output_text.delta", output_index=0, delta="Hello"))
        out += t.feed(upstream("response.completed", response={"usage": {
            "input_tokens": "many", "output_tokens": 4, "input_tokens_details": {"cached_tokens": 2}}}))
        message = stream.accumulate(out)
        self.assertEqual((t.terminal, t.error, message.get("content")), ("response.completed", None, [{"type": "text", "text": "Hello"}]))
        self.assertEqual(message.get("usage"), {"input_tokens": 0, "output_tokens": 4, "cache_creation_input_tokens": 0,
                                            "cache_read_input_tokens": 2})
        self.assertEqual((t.usage, t.input_tokens, t.cache_ratio, t.notes), (
            {"input_tokens": None, "output_tokens": 4, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 2},
            None, None, ["unreadable usage input_tokens: 'many'"]))

    def test_unterminated_frame_and_missing_terminal(self):
        t = self.translator()
        self.assertEqual(t.finish(), b"")
        self.assertIsNotNone(t.error)
        self.assertTrue(t.error.retry)
        self.assertFalse(t.emitted_any)
        t = self.translator()
        data = t.feed(upstream("response.output_text.delta", output_index=0, delta="x")) + t.finish()
        self.assertIn(b"content_block_stop", data)
        self.assertIn(b"event: error", data)
        self.assertFalse(t.error.retry)
        self.assertIn("closed before the terminal event", t.error.client_body["error"]["message"])
        t = self.translator()
        raw = (FIXTURES / "incomplete.responses.sse").read_bytes().rstrip()
        output = t.feed(raw) + t.finish()
        self.assertEqual(output, (FIXTURES / "incomplete.anthropic.sse").read_bytes())

    def test_incomplete_failed_and_error(self):
        cases = [("response.incomplete", {"response": {"incomplete_details": {"reason": "content_filter"}}}),
                 ("response.failed", {"response": {"error": {"code": "usage_not_included"}}}),
                 ("error", {"code": "context_length_exceeded"})]
        for kind, fields in cases:
            with self.subTest(kind=kind):
                t = self.translator()
                result = t.feed(upstream(kind, **fields))
                self.assertEqual(result, b"")
                self.assertFalse(t.emitted_any)
                self.assertIsNotNone(t.error)
                self.assertFalse(t.error.retry)
                self.assertIn("chatgpt upstream", t.error.client_body["error"]["message"])

    def test_an_empty_completion_is_a_turn_with_no_content(self):
        for items in ([], [("response.output_item.added", {"item": {"type": "reasoning"}}),
                           ("response.output_item.done", {"item": {"type": "reasoning", "id": "rs_1"}})]):
            with self.subTest(items=len(items)):
                t = self.translator()
                out = b"".join(t.feed(upstream(kind, **fields)) for kind, fields in items)
                out += t.feed(upstream("response.completed", response={"usage": {"output_tokens": 9}}))
                message = stream.accumulate(out)
                self.assertEqual((t.error, t.empty, t.emitted_any, message["stop_reason"]), (None, True, True, "end_turn"))
                self.assertEqual([b for b in message["content"] if b["type"] != "thinking"], [])
                self.assertEqual(t.usage["output_tokens"], 9)

    def test_tools_are_whole_at_done_and_parallel_indexes_are_unique(self):
        t = self.translator()
        pre = t.feed(upstream("response.output_item.added", output_index=4,
                              item={"type": "function_call", "name": "lookup_short"}))
        pre += t.feed(upstream("response.function_call_arguments.delta", output_index=4, delta="{"))
        self.assertEqual(pre, b"")
        self.assertFalse(t.emitted_any)
        out = pre
        for i in (4, 2):
            out += t.feed(upstream("response.output_item.done", output_index=i,
                                  item={"type": "function_call", "call_id": "call_%d" % i,
                                        "name": "lookup_short", "arguments": '{"x":1}'}))
        out += t.feed(upstream("response.completed", response={"usage": {"input_tokens": 0}}))
        message = stream.accumulate(out)
        self.assertEqual([b["id"] for b in message["content"]], ["call_4", "call_2"])
        self.assertEqual(message["stop_reason"], "tool_use")
        self.assertEqual(t.cache_ratio, 0.0)

    def test_hold_until_first_content_and_preserve_frame_order(self):
        t = self.translator()
        self.assertEqual(t.feed(upstream("response.created")), b"")
        self.assertFalse(t.emitted_any)
        self.time.return_value = 20
        self.assertEqual(t.idle(), b"")
        self.assertFalse(t.emitted_any)
        self.assertEqual(t.feed(upstream("response.output_item.added", output_index=0,
                                        item={"type": "message"})), b"")
        self.assertFalse(t.emitted_any)
        committed = t.feed(upstream("response.output_text.delta", output_index=0, delta="Hello"))
        self.assertTrue(t.emitted_any)
        frames = EventSplitter().feed(committed)
        self.assertEqual([f["event"] for f in frames],
                         ["message_start", "ping", "content_block_start", "content_block_delta"])
        self.assertEqual(frames[-1]["data"]["delta"], {"type": "text_delta", "text": "Hello"})

    def test_hold_all_precontent_failures_for_http_status_or_retry(self):
        cases = [
            ("context", upstream("response.failed", response={"error": {"code": "context_length_exceeded"}}), 413, False),
            ("failed", upstream("response.failed", response={"error": None}), 502, True),
            ("error", upstream("error", code="usage_not_included"), 403, False),
            ("incomplete", upstream("response.incomplete", response={"incomplete_details": {"reason": "content_filter"}}), 502, False),
            ("closed", None, 502, True),
            ("idle", None, 502, True),
        ]
        for name, failure, status, retry in cases:
            with self.subTest(name=name):
                self.time.return_value = 0
                t = self.translator()
                self.assertEqual(t.feed(upstream("response.created")), b"")
                self.assertFalse(t.emitted_any)
                if name == "idle":
                    self.time.return_value = 300
                    result = t.idle()
                else:
                    result = t.finish() if failure is None else t.feed(failure)
                self.assertEqual(result, b"")
                self.assertFalse(t.emitted_any)
                self.assertEqual(t.error.client_status, status)
                self.assertEqual(t.error.retry, retry)
                self.assertEqual(t.finish(), b"")
                self.assertEqual(t.idle(), b"")

    def test_hold_empty_max_tokens_still_commits(self):
        t = self.translator()
        self.assertEqual(t.feed(upstream("response.created")), b"")
        result = t.feed(upstream("response.incomplete", response={
            "incomplete_details": {"reason": "max_output_tokens"}, "usage": {}}))
        self.assertTrue(t.emitted_any)
        self.assertIsNone(t.error)
        self.assertEqual(stream.accumulate(result)["stop_reason"], "max_tokens")
