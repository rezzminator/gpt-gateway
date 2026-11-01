"""Main-session routing and the shared local-client authentication boundary, through a jailed gateway."""
import json
import unittest
from unittest.mock import patch

from gpt_gateway.chatgpt import signature
from gpt_gateway.chatgpt.route import without_reasoning
from tests import fakes
from tests.gpt_gateway.chatgpt import test_route as route_tests


class MainRouteTest(unittest.TestCase):
    setUp = route_tests.RouteTest.setUp
    jwt = route_tests.RouteTest.jwt
    gateway = route_tests.RouteTest.gateway
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses

    def test_authorized_main_routes_its_model_and_keeps_catalog_rules(self):
        rt, port = self.gateway()
        headers = {k: v for k, v in self.headers.items() if k != "x-claude-code-agent-id"}
        for model in ("gpt-test", "gpt-test-fast[1m]"):
            with self.subTest(model=model):
                response, _ = self.send(port, dict(self.body, model=model), headers)
                self.assertEqual(response.status, 200)
                self.assertNotIn("x-openai-subagent", self.responses()[-1]["headers"])
                sent = json.loads(self.responses()[-1]["body"])
                self.assertEqual(sent["model"], "gpt-test")
                self.assertIsNone(sent.get("service_tier"))  # This catalog advertises no priority tier.
        response, data = self.send(port, dict(self.body, model="unknown"), headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(data)["error"]["message"],
                         'chatgpt upstream: unknown model "unknown"; the ChatGPT catalog has: gpt-test')
        before = len(self.responses())
        response, data = self.send(port, headers=headers, path="/v1/messages/count_tokens?beta=1")
        self.assertEqual(response.status, 200)
        self.assertGreater(json.loads(data)["input_tokens"], 0)
        self.assertEqual(len(self.responses()), before)
        self.assertEqual([row["kind"] for row in fakes.gpt_rows(rt)], ["main"] * 2)

    def test_main_and_sub_keep_separate_websockets_even_when_ids_contain_colons(self):
        rt, port = self.gateway(GPT_GATEWAY_CHATGPT_TRANSPORT="websocket")
        main = {"x-claude-code-session-id": "session:agent"}
        sub = {"x-claude-code-session-id": "session", "x-claude-code-agent-id": "agent:"}
        for headers in (main, sub, main, sub):
            self.assertEqual(self.send(port, headers=headers)[0].status, 200)
        self.assertEqual(len(self.fake.upgrades), 2)
        first, second = self.fake.upgrades
        self.assertNotEqual(first["session-id"], second["session-id"])
        self.assertNotIn("x-openai-subagent", first)
        self.assertEqual(second["x-openai-subagent"], "collab_spawn")
        rows = fakes.gpt_rows(rt)
        self.assertEqual([row["kind"] for row in rows], ["main", "sub", "main", "sub"])
        self.assertEqual([row["reused"] for row in rows], [0, 0, 1, 1])

    def test_main_and_sub_without_install_secret_are_refused_before_catalog(self):
        rt, port = self.gateway()
        for headers in ({}, self.headers):
            for path in ("/v1/messages", "/v1/messages/count_tokens?beta=1"):
                for body in (self.body, dict(self.body, model="unknown", thread={"type": "create"})):
                    with self.subTest(headers=headers, path=path, model=body["model"]):
                        with patch.object(rt.chatgpt.catalog, "models", side_effect=AssertionError("catalog reached")):
                            response, data = fakes.request(port, "POST", path, json.dumps(body).encode(), headers)
                        self.assertEqual(response.status, 403)
                        self.assertEqual(json.loads(data)["error"], {
                            "type": "permission_error", "message": "chatgpt upstream: local client authentication required"})
                        self.assertEqual(response.getheader("x-should-retry"), "false")
        self.assertEqual(self.fake.seen, [])
        self.assertEqual(self.send(port)[0].status, 200)


class WithoutReasoningTest(unittest.TestCase):
    def test_reasoning_drop_retry_survives_a_damaged_signature(self):
        body = {"messages": [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "t", "signature": signature.PREFIX + "broken"},
            {"type": "thinking", "thinking": "f", "signature": "foreign"}, {"type": "text", "text": "kept"}]}]}
        self.assertEqual(without_reasoning(body)["messages"][0]["content"], [
            {"type": "thinking", "thinking": "f", "signature": "foreign"}, {"type": "text", "text": "kept"}])
