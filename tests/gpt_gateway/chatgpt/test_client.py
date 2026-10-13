"""Client headers are constructed locally, and the thread's reader is cancelled on drop."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gpt_gateway.chatgpt.auth import Credentials
from gpt_gateway.chatgpt.client import Client
from gpt_gateway.chatgpt.request import Translation
from gpt_gateway.upstream import UpstreamError
from tests import fakes


class ClientTest(unittest.TestCase):
    def test_responses_and_models_use_codex_headers(self):
        fake = type("ClientFake", (fakes.ChatGPTUpstream,), {"seen": [], "script": []})
        client = Client(SimpleNamespace(origin=fakes.start_upstream(self, fake), transport="http"), lambda line: None)
        self.addCleanup(client.drop)
        creds = Credentials("invented-bearer", "invented-account")
        for lite in (True, False):
            response = client.responses(Translation(b"{}", lite, {}, (), 2), "invented-lane", creds)
            response.read()
            headers = fake.seen[-1]["headers"]
            self.assertEqual(headers["authorization"], "Bearer invented-bearer")
            self.assertEqual(headers["chatgpt-account-id"], "invented-account")
            self.assertEqual(headers["session-id"], "invented-lane")
            self.assertEqual(headers["content-length"], "2")
            self.assertEqual(headers.get("x-openai-internal-codex-responses-lite"), "true" if lite else None)
        self.assertEqual(client.models(creds), fakes.CHATGPT_MODELS)
        self.assertEqual(fake.seen[-1]["path"], "/backend-api/codex/models?client_version=0.162.1")

    def test_responses_name_the_request_to_the_upstream_for_its_retry_event(self):
        client = Client(SimpleNamespace(origin="http://127.0.0.1:1", transport="http"), lambda line: None)
        client.upstream = Mock()
        client.responses(Translation(b"{}", False, {}, (), 2), "invented-lane",
                         Credentials("invented-bearer", "invented-account"), request_id=7)
        self.assertEqual(client.upstream.send.call_args.kwargs.get("request_id"), 7)

    def test_a_websocket_that_fails_to_start_carries_its_cause_class(self):
        client = Client(SimpleNamespace(origin="http://127.0.0.1:1", transport="http"), lambda line: None)
        client.sockets = Mock()
        client.sockets.checkout.return_value = SimpleNamespace(exchanges=0, baseline=None)  # a fresh socket
        for cause, expected in ((ConnectionResetError(104, "Connection reset by peer"), "reset"),
                                (UpstreamError("upstream unreachable: timeout('timed out')", "timeout"), "timeout")):
            with self.subTest(cause=repr(cause)), patch("gpt_gateway.chatgpt.websocket.Answer") as answer:
                answer.return_value.start.side_effect = cause
                with self.assertRaises(UpstreamError) as caught:
                    client._over_websocket(Translation(b"{}", False, {}, (), 2), "invented-lane",
                                           Credentials("invented-bearer", "invented-account"), None, None)
                self.assertEqual(caught.exception.error_class, expected)

    def test_drop_cancels_the_reader_before_closing_connection(self):
        client = Client(SimpleNamespace(origin="http://127.0.0.1:1", transport="http"), lambda line: None)
        reader = Mock()
        client.watch(reader)
        client.drop()
        reader.stop.set.assert_called_once_with()

    def test_release_lets_a_finished_response_settle_where_drop_cancels_it(self):
        client = Client(SimpleNamespace(origin="http://127.0.0.1:1", transport="http"), lambda line: None)
        reader, answer, client.upstream = Mock(), Mock(), Mock()
        client.watch(reader)
        client.local.answer = answer
        client.release()
        reader.stop.set.assert_called_once_with()
        answer.abort.assert_not_called()
        client.upstream.release.assert_called_once_with()
        client.upstream.abort.assert_not_called()
        self.assertEqual((client.local.reader, client.local.answer), (None, None))
