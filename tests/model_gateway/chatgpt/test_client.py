"""Client headers are constructed locally, and the thread's reader is cancelled on drop."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from model_gateway.chatgpt.auth import Credentials
from model_gateway.chatgpt.client import Client
from model_gateway.chatgpt.request import Translation
from tests import fakes


class ClientTest(unittest.TestCase):
    def test_responses_and_models_use_codex_headers(self):
        fake = type("ClientFake", (fakes.ChatGPTUpstream,), {"seen": [], "script": []})
        client = Client(SimpleNamespace(origin=fakes.start_upstream(self, fake)), lambda line: None)
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
        self.assertEqual(fake.seen[-1]["path"], "/backend-api/codex/models?client_version=0.155.1")

    def test_drop_cancels_the_reader_before_closing_connection(self):
        client = Client(SimpleNamespace(origin="http://127.0.0.1:1"), lambda line: None)
        reader = Mock()
        client.watch(reader)
        client.drop()
        reader.stop.set.assert_called_once_with()
