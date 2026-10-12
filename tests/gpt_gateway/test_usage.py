"""Tests for gpt_gateway/usage.py: usage and error codes read off SSE and JSON bodies at any chunking."""
import json
import time
import unittest

from gpt_gateway import usage


def sse(*events, sep=b"\n\n"):
    return b"".join(b"event: %s\ndata: %s%s" % (name.encode(), json.dumps(data).encode(), sep) for name, data in events)


START = ("message_start", {"type": "message_start", "message": {"id": "msg_1", "usage": {
    "input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 1,
    "cache_creation": {"ephemeral_5m_input_tokens": 100}, "service_tier": "standard"}}})
TEXT = ("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}})
DELTA_OUT = ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 50}})
DELTA_FULL = ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {
    "input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5000, "output_tokens": 437,
    "output_tokens_details": {"thinking_tokens": 194}, "iterations": [{"type": "message"}]}})
OVERLOADED = ("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
STOP = ("message_stop", {"type": "message_stop"})
TOKENS = {"input_tokens": 5110, "cache_read_tokens": 5000, "cache_write_tokens": 100}

CASES = [
    # (name, content type, body, fields, unread)
    ("stream with a cumulative final delta", "text/event-stream", sse(START, TEXT, DELTA_OUT, DELTA_FULL, STOP),
     dict(TOKENS, output_tokens=437, reasoning_tokens=194, hit=5000 / 5110), None),
    ("stream whose delta carries only output", "text/event-stream; charset=utf-8", sse(START, TEXT, DELTA_OUT, STOP),
     dict(TOKENS, output_tokens=50, reasoning_tokens=None, hit=5000 / 5110), None),
    ("CRLF separators", "text/event-stream", sse(START, DELTA_OUT, sep=b"\r\n\r\n"),
     dict(TOKENS, output_tokens=50, reasoning_tokens=None, hit=5000 / 5110), None),
    ("an error event mid-stream", "text/event-stream", sse(START, TEXT, OVERLOADED),
     dict(TOKENS, output_tokens=1, reasoning_tokens=None, hit=5000 / 5110, error_code="overloaded_error",
          error_head=json.dumps(OVERLOADED[1])), None),
    ("a zero-usage answer", "text/event-stream", sse(("message_start", {"message": {"usage": {
        "input_tokens": 0, "output_tokens": 0}}}), ("message_delta", {"usage": {"output_tokens": 4}})),
     {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 4,
      "reasoning_tokens": None, "hit": 0.0}, None),
    ("a JSON reply", "application/json", json.dumps({"type": "message", "content": [{"type": "text", "text": "x"}],
                                                     "usage": {"input_tokens": 3, "cache_read_input_tokens": 1,
                                                               "cache_creation_input_tokens": 0, "output_tokens": 2}}
                                                    ).encode(),
     {"input_tokens": 4, "cache_read_tokens": 1, "cache_write_tokens": 0, "output_tokens": 2,
      "reasoning_tokens": None, "hit": 0.25}, None),
    ("a JSON error reply", "application/json",
     json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}).encode(),
     {"error_code": "rate_limit_error"}, "no_usage"),
    ("a JSON body that is no JSON", "application/json", b"{not json", {}, "not_json"),
    ("a count-tokens reply", "application/json", b'{"input_tokens": 12}', {}, "no_usage"),
    ("a usage of the wrong shape", "application/json", b'{"usage": {"input_tokens": "12"}}', {}, "bad_usage"),
    ("plain text", "text/plain", b"event: message_start\n\n", {}, "no_usage"),
]


def metered(content_type, body, step):
    meter = usage.Meter(content_type)
    for i in range(0, len(body), step):
        meter.feed(body[i:i + step])
    meter.finish()
    return meter, meter.fields()


class MeterTest(unittest.TestCase):
    def test_cases_at_every_chunking(self):
        for name, content_type, body, fields, unread in CASES:
            for step in (1, 7, len(body) or 1):
                with self.subTest(name=name, step=step):
                    meter, got = metered(content_type, body, step)
                    self.assertEqual(got, fields)
                    self.assertEqual(meter.unread, unread)

    def test_an_oversize_event_is_dropped_never_held(self):
        big = ("content_block_delta", {"delta": {"text": "x" * (3 * usage.MAX_EVENT)}})
        body = sse(START, big, DELTA_FULL)
        meter = usage.Meter("text/event-stream")
        peak = 0
        for i in range(0, len(body), 65536):
            meter.feed(body[i:i + 65536])
            peak = max(peak, len(meter.pending))
        self.assertLessEqual(peak, usage.MAX_EVENT + 65536)
        self.assertEqual(meter.fields()["output_tokens"], 437)

    def test_a_nesting_too_deep_to_parse_is_unread_never_raised(self):
        deep = b"[" * 100000  # past the parser's recursion limit, inside MAX_EVENT
        for content_type, body in (("application/json", deep),
                                   ("text/event-stream", b"event: message_delta\ndata: " + deep + b"\n\n")):
            with self.subTest(content_type=content_type):
                try:
                    outcome = metered(content_type, body, len(body))[0].unread
                except RecursionError as e:  # the relay would end the client's stream on it
                    outcome = e
                self.assertEqual(outcome, "not_json")

    def test_a_chunk_of_many_small_events_is_split_in_linear_time(self):
        body = sse(START, *[TEXT] * 20000, DELTA_FULL)  # about 2.6 MB: one burst of deltas in one read
        began = time.monotonic()
        meter, got = metered("text/event-stream", body, len(body))
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual(got["output_tokens"], 437)

    def test_a_json_body_past_its_limit_is_not_read(self):
        meter = usage.Meter("application/json")
        meter.feed(b" " * usage.MAX_JSON)
        meter.feed(b'{"usage": {}}')
        meter.finish()
        self.assertEqual((meter.fields(), meter.unread, len(meter.pending)), ({}, "too_large", 0))


if __name__ == "__main__":
    unittest.main()
