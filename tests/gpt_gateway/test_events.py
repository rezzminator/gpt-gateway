"""Tests for gpt_gateway.events: SSE events come out whole whatever the chunk boundaries."""
import unittest

from gpt_gateway.events import EventSplitter, json_event, parse_event
from tests.fakes import SSE_BODY


def split_all(data, size):
    s = EventSplitter()
    out = [e for i in range(0, len(data), size) for e in s.feed(data[i:i + size])]
    return out + s.finish()


class EventSplitterTest(unittest.TestCase):
    def test_every_chunk_boundary_yields_the_same_events(self):
        whole = split_all(SSE_BODY, len(SSE_BODY))
        self.assertEqual([e["event"] for e in whole], ["message_start", "content_block_delta", "message_stop"])
        self.assertEqual(whole[0]["data"], {"type": "message_start", "message": {"model": "m"}})
        self.assertEqual(whole[2]["raw"], b'event: message_stop\ndata: {"type":"message_stop"}')
        for size in range(1, len(SSE_BODY)):
            self.assertEqual(split_all(SSE_BODY, size), whole, size)

    def test_an_event_completes_only_at_its_blank_line(self):
        s = EventSplitter()
        self.assertEqual(s.feed(b'event: a\ndata: {"x":'), [])
        self.assertEqual(s.feed(b'1}\n'), [])
        self.assertEqual(s.feed(b'\n'), [{"event": "a", "data": {"x": 1}, "raw": b'event: a\ndata: {"x":1}'}])

    def test_crlf_comments_multiline_data_and_non_json(self):
        s = EventSplitter()
        events = s.feed(b": ping\r\n\r\nevent: a\r\ndata: [1,\r\ndata: 2]\r\n\r\ndata: not json\n\n")
        self.assertEqual([(e["event"], e["data"]) for e in events], [("a", [1, 2]), (None, None)])

    def test_finish_returns_an_unterminated_last_event(self):
        s = EventSplitter()
        s.feed(b"event: last\ndata: {}")
        self.assertEqual(s.finish(), [{"event": "last", "data": {}, "raw": b"event: last\ndata: {}"}])
        self.assertEqual(s.finish(), [])

    def test_an_event_past_the_limit_is_dropped_and_counted(self):
        s = EventSplitter(max_pending=8)
        self.assertEqual(s.feed(b"data: 0123456789"), [])
        self.assertEqual(s.overflowed, 1)
        self.assertEqual(s.feed(b"data: 1\n\n"), [{"event": None, "data": 1, "raw": b"data: 1"}])

    def test_json_event(self):
        self.assertEqual(json_event(b'{"a":1}'), {"event": None, "data": {"a": 1}, "raw": b'{"a":1}'})
        self.assertEqual(json_event(b"<html>")["data"], None)
        self.assertIsNone(parse_event(b": only a comment"))


if __name__ == "__main__":
    unittest.main()
