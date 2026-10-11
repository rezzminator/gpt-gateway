"""The 24-hour cache retention setting through real gateway exchanges, over both transports: sent on every Responses
request when set, never touching the cache key, and a backend that refuses it costs one retry, one event and no more sends."""
import json
import unittest

from tests import fakes, jail
from tests.model_gateway.chatgpt import test_route as route_tests

PARAM = "prompt_cache_retention"


class RetentionTest(unittest.TestCase):
    jwt = route_tests.RouteTest.jwt
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses

    def setUp(self):
        route_tests.RouteTest.setUp(self)

    def gateway(self, transport, retention=None):
        self.env["MODEL_GATEWAY_CHATGPT_TRANSPORT"] = transport
        settings = {} if retention is None else {"chatgpt_cache_retention": retention}
        return fakes.start_gateway(self, self.anthropic, env=self.env, settings=settings)

    def sent(self):
        """The parameters of each Responses message the backend saw, decoded."""
        return [json.loads(x["body"]) for x in self.responses()]

    def health(self, port):
        return fakes.request(port, "GET", "/__gateway/health")[1].decode()

    def test_off_by_default_the_parameter_is_never_sent(self):
        for transport in ("http", "websocket"):
            with self.subTest(transport=transport):
                self.fake.seen.clear()
                rt, port = self.gateway(transport)
                self.assertEqual(self.send(port)[0].status, 200)
                self.assertTrue(self.sent())
                self.assertTrue(all(PARAM not in message for message in self.sent()))
                self.assertIn(" cache_retention=off middleware=", self.health(port))

    def test_set_it_rides_every_request_and_leaves_the_cache_key_alone(self):
        for transport in ("http", "websocket"):
            with self.subTest(transport=transport):
                self.fake.seen.clear()
                rt, port = self.gateway(transport, "24h")
                for _ in range(2):
                    self.assertEqual(self.send(port)[0].status, 200)
                messages = self.sent()
                self.assertEqual([m[PARAM] for m in messages], ["24h", "24h"])
                self.assertEqual({m["prompt_cache_key"] for m in messages},
                                 {x["headers"]["session-id"] for x in self.responses() if "session-id" in x["headers"]})
                self.assertEqual(fakes.events(rt, "retention_rejected"), [])
                self.assertIn(" cache_retention=24h middleware=", self.health(port))

    def test_a_backend_that_refuses_it_costs_one_retry_and_is_not_asked_again(self):
        for transport in ("http", "websocket"):
            with self.subTest(transport=transport):
                self.fake.seen.clear()
                self.fake.unsupported = (PARAM,)
                rt, port = self.gateway(transport, "24h")
                for _ in range(2):
                    self.assertEqual(self.send(port)[0].status, 200)
                self.assertEqual([PARAM in m for m in self.sent()], [True, False, False])
                found = fakes.events(rt, "retention_rejected")
                self.assertEqual(len(found), 1)
                self.assertEqual((found[0]["fields"]["retention"], found[0]["request_id"]), ("24h", 1))
                self.assertEqual([e["fields"]["action"] for e in fakes.events(rt, "retry")], ["drop_retention"])
                self.assertEqual([r["attempts"] for r in fakes.gpt_rows(rt)], [2, 1])
                self.assertIn(" cache_retention=24h(rejected) middleware=", self.health(port))

    def test_changing_the_setting_asks_again(self):
        self.fake.unsupported = (PARAM,)
        rt, port = self.gateway("http", "24h")
        self.send(port)
        jail.write_json(rt.settings.path, {"chatgpt_cache_retention": "off"}, bump=1)
        self.send(port)
        jail.write_json(rt.settings.path, {"chatgpt_cache_retention": "24h"}, bump=2)
        self.send(port)
        self.assertEqual([PARAM in m for m in self.sent()], [True, False, False, True, False])
        self.assertEqual(len(fakes.events(rt, "retention_rejected")), 2)
        # off and back to 24h with no ChatGPT request between: a settings change all the same
        jail.write_json(rt.settings.path, {"chatgpt_cache_retention": "off"}, bump=3)
        self.assertIn(" cache_retention=off middleware=", self.health(port))
        jail.write_json(rt.settings.path, {"chatgpt_cache_retention": "24h"}, bump=4)
        self.assertIn(" cache_retention=24h middleware=", self.health(port))
        self.send(port)
        self.assertEqual([PARAM in m for m in self.sent()][5:], [True, False])
        self.assertEqual(len(fakes.events(rt, "retention_rejected")), 3)

    def test_a_stream_failure_naming_it_when_it_was_not_sent_is_retried_as_any_other(self):
        self.fake.script = [(200, fakes.responses_event("response.failed", response={"error": {
            "code": "server_error", "message": "upstream hiccup near prompt_cache_retention"}}))]
        rt, port = self.gateway("http")
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual([e["fields"]["action"] for e in fakes.events(rt, "retry")], ["retry"])
        self.assertEqual(fakes.events(rt, "retention_rejected"), [])


if __name__ == "__main__":
    unittest.main()
