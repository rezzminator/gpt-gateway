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
        response = client.responses(Translation(b"{}", {}, (), 2), "invented-lane", creds)
        response.read()
        headers = fake.seen[-1]["headers"]
        self.assertEqual(headers["authorization"], "Bearer invented-bearer")
        self.assertEqual(headers["chatgpt-account-id"], "invented-account")
        self.assertEqual(headers["session-id"], "invented-lane")
        self.assertEqual(headers.get("thread-id"), "invented-lane")
        self.assertEqual(headers["content-length"], "2")
        self.assertEqual(client.models(creds), fakes.CHATGPT_MODELS)
        self.assertEqual(fake.seen[-1]["path"], "/backend-api/codex/models?client_version=0.162.1")

    def test_both_transports_use_only_owned_compatibility_metadata(self):
        client = Client(SimpleNamespace(origin="http://127.0.0.1:1", transport="http"), lambda line: None)
        self.addCleanup(client.close)
        client.upstream = Mock()
        metadata = {"session_id":"session", "thread_id":"child", "x-codex-window-id":"window",
                    "x-codex-parent-thread-id":"parent", "x-codex-turn-metadata":'{"request_kind":"turn"}',
                    "Authorization":"must-not-cross", "x-oai-attestation":"must-not-cross"}
        body = json.dumps({"client_metadata": metadata}).encode()
        client.responses(Translation(body, {}, (), len(body)), "lane", Credentials("token", "account"), subagent=True)
        headers = dict(client.upstream.send.call_args.args[3])
        self.assertEqual(headers.get("thread-id"), "child")
        self.assertEqual(headers.get("session-id"), "session")
        self.assertEqual(headers.get("x-codex-parent-thread-id"), "parent")
        self.assertEqual(headers.get("x-codex-window-id"), "window")
        self.assertEqual(headers.get("Authorization"), "Bearer token")
        self.assertNotIn("x-oai-attestation", headers)

    def test_main_lane_omits_subagent_identity_on_both_transports(self):
        for transport in ('http', 'websocket'):
            with self.subTest(transport=transport):
                fake = type('MainClientFake', (fakes.ChatGPTUpstream,), {'seen': [], 'script': [], 'upgrades': []})
                client = Client(SimpleNamespace(origin=fakes.start_upstream(self, fake), transport=transport), lambda kind, **fields: None)
                self.addCleanup(client.close)
                response = client.responses(Translation(b'{"model":"gpt-test","input":[]}', {}, (), 2),
                                            'main-lane', Credentials('invented-bearer', 'invented-account'))
                self.assertEqual(response.status, 200)
                response.read()
                response.close()
                headers = fake.upgrades[-1] if transport == 'websocket' else fake.seen[-1]['headers']
                self.assertNotIn('x-openai-subagent', headers)
                self.assertEqual(headers.get('x-codex-routing-hint'), 'model=gpt-test')

    def test_routing_hint_uses_only_bounded_model_and_tier(self):
        client = Client(SimpleNamespace(origin='http://127.0.0.1:1', transport='http'), lambda kind, **fields: None)
        self.addCleanup(client.close)
        for model, tier, expected in [('gpt-test', 'priority', 'model=gpt-test;tier=priority'),
                                      ('gpt-test;other=x', None, None), ('gpt-test', 'bad\r\nheader', None),
                                      ('g' * 1025, None, None)]:
            body = json.dumps({'model':model, 'service_tier':tier,
                               'client_metadata':{'x-codex-routing-hint':'forged'}}).encode()
            headers = client._response_headers(Translation(body, {}, (), len(body)), 'lane', Credentials('token','account'), False)
            self.assertEqual(headers.get('x-codex-routing-hint'), expected)

    def test_responses_name_the_request_to_the_upstream_for_its_retry_event(self):
        client = Client(SimpleNamespace(origin="http://127.0.0.1:1", transport="http"), lambda line: None)
        client.upstream = Mock()
        client.responses(Translation(b"{}", {}, (), 2), "invented-lane",
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
                    client._over_websocket(Translation(b"{}", {}, (), 2), "invented-lane",
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
