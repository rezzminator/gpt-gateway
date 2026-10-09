"""The one-relay response surface preserves commit, heartbeat and true EOF."""
import http.client
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from model_gateway.chatgpt.response import EOF, Reader, Response
from model_gateway.chatgpt.stream import Translator
from tests import fakes


class ResponseTest(unittest.TestCase):
    def test_json_case_insensitive_headers_and_small_reads(self):
        response = Response.json(413, {"type": "error"}, {"Retry-After": "4"})
        self.assertEqual(response.getheader("retry-after"), "4")
        self.assertEqual(response.getheader("missing", "default"), "default")
        self.assertEqual(response.reason, "Request Entity Too Large")
        self.assertEqual(response.read1(2) + response.read(), b'{"type":"error"}')
        self.assertEqual(response.read1(20), b"")

    def test_reader_skips_empty_translations_and_finishes_once(self):
        upstream = Mock()
        upstream.read1.side_effect = [fakes.responses_event("response.created"), fakes.responses_turn(), b""]
        translator = Translator("gpt-test", {}, 10)
        client = Mock()
        reader = Reader(upstream, translator, client)
        done = Mock()
        reader.on_end = done
        response = Response(200, reader=reader)
        data = response.read()
        self.assertIn(b"signature_delta", data)
        self.assertIn(b"message_stop", data)
        self.assertEqual(response.read(), b"")
        done.assert_called_once_with()
        self.assertTrue(reader.thread.daemon)
        reader.thread.join(1)
        self.assertFalse(reader.thread.is_alive())

    def test_queue_timeout_drives_idle_without_socket_timeout(self):
        upstream = Mock()
        translator = Translator("gpt-test", {}, 10)
        with patch("model_gateway.chatgpt.response.threading.Thread"):
            reader = Reader(upstream, translator, Mock())
        translator.feed(fakes.responses_event("response.output_text.delta", delta="x"))
        with patch("model_gateway.clock.monotonic", return_value=translator._last_upstream + 16):
            with patch.object(reader.queue, "get", side_effect=queue.Empty):
                self.assertIn(b"event: ping", reader.next())
        reader.queue.put(EOF)
        self.assertIn(b"event: error", reader.next())
        self.assertEqual(reader.next(), b"")
        upstream.read1.assert_not_called()

    def test_precommit_read_failure_remains_retryable(self):
        upstream = Mock()
        upstream.read1.side_effect = OSError("invented disconnect")
        reader = Reader(upstream, Translator("gpt-test", {}, 10), Mock())
        self.assertEqual(reader.next(), b"")
        self.assertTrue(reader.translator.error.retry)
        self.assertEqual(reader.transport_error, "invented disconnect")

    def test_reader_marks_the_first_body_byte_the_terminal_event_and_the_exception(self):
        upstream = Mock()
        upstream.read1.side_effect = [fakes.responses_turn(), b""]
        main = threading.main_thread()
        with patch("model_gateway.clock.monotonic",
                   side_effect=lambda: 9.0 if threading.current_thread() is main else 7.5):  # 7.5: the reader thread
            reader = Reader(upstream, Translator("gpt-test", {}, 10), Mock())
            self.assertIn(b"message_stop", Response(200, reader=reader).read())
        self.assertEqual((reader.first_byte_at, reader.terminal_at), (7.5, 9.0))
        failing = Mock()
        failing.read1.side_effect = http.client.IncompleteRead(b"")
        reader = Reader(failing, Translator("gpt-test", {}, 10), Mock())
        self.assertEqual(reader.next(), b"")
        self.assertIsNone(reader.first_byte_at)
        self.assertIsInstance(reader.transport_exception, http.client.IncompleteRead)
        self.assertEqual(reader.transport_error, "IncompleteRead(0 bytes read)")
