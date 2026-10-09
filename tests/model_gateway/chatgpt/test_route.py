"""Real gateway exchanges against invented Anthropic, ChatGPT and OAuth servers."""
import base64
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from model_gateway import clock
from model_gateway.capture import SENT_BODY
from model_gateway.chatgpt import stream
from tests import fakes, jail


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.home = jail.scratch(self)
        self.fake = type("FakeChatGPT", (fakes.ChatGPTUpstream,), {"seen": [], "script": [], "tokens": {}})
        self.origin = fakes.start_upstream(self, self.fake)
        self.anthropic = fakes.start_upstream(self)
        fakes.Upstream.seen.clear()
        self.auth_path = os.path.join(self.home, "auth.json")
        self.models_path = os.path.join(self.home, "models.json")
        self.capture = os.path.join(self.home, "capture")
        self.token = self.jwt("first")
        self.fake.tokens = dict(access_token=self.jwt("refreshed"), refresh_token="refresh-invented-2",
                                id_token="id-invented", account_id="account-invented")
        jail.write_json(self.auth_path, dict(access_token=self.token, refresh_token="refresh-invented",
                        id_token="id-invented", account_id="account-invented", last_refresh=clock.now()))
        self.env = dict(HOME=self.home, MODEL_GATEWAY_CHATGPT="1", MODEL_GATEWAY_CHATGPT_AUTH_STORE="file",
                        MODEL_GATEWAY_CHATGPT_AUTH_PATH=self.auth_path, MODEL_GATEWAY_CHATGPT_ORIGIN=self.origin,
                        MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN=self.origin, MODEL_GATEWAY_CHATGPT_MODELS_PATH=self.models_path)
        self.headers = {"x-claude-code-session-id": "session-invented", "x-claude-code-agent-id": "agent-invented",
                        "authorization": "Bearer claude-secret", "x-api-key": "claude-key",
                        "anthropic-version": "invented", "anthropic-beta": "invented"}
        self.body = dict(model="gpt-test", messages=[dict(role="user", content="Look up Harbor")], stream=True,
                         tools=[dict(name="lookup", input_schema={"type": "object"})])

    def jwt(self, label):
        data = json.dumps({"exp": clock.now() + 3600, "label": label}).encode()
        return "fixture." + base64.urlsafe_b64encode(data).decode().rstrip("=") + ".signature"

    def gateway(self, **env):
        return fakes.start_gateway(self, self.anthropic, env=dict(self.env, **env),
               settings={"capture_dir": self.capture, "middleware": [{"module": "rec"}]},
               middleware={"rec": fakes.RECORDER.format(name="rec")})

    def send(self, port, body=None, headers=None, path="/v1/messages"):
        return fakes.request(port, "POST", path, json.dumps(self.body if body is None else body).encode(),
                             self.headers if headers is None else headers)

    def responses(self):
        return [x for x in self.fake.seen if x["path"] == "/backend-api/codex/responses"]

    def test_three_turn_prefix_capture_headers_and_hooks(self):
        self.fake.script = [(200, fakes.responses_turn(True)), (200, fakes.responses_turn()),
                            (200, fakes.responses_turn())]
        rt, port = self.gateway()
        for turn in range(3):
            resp, data = self.send(port)
            self.assertEqual(resp.status, 200)
            self.assertIn(b"signature_delta", data)
            msg = stream.accumulate(data)
            self.body["messages"].append(dict(role="assistant", content=msg["content"]))
            for block in msg["content"]:
                if block["type"] == "thinking":
                    block["thinking"] = ""
            reply = ([dict(type="tool_result", tool_use_id="call_invented", content="Harbor is sunny")]
                     if turn == 0 else "Continue")
            self.body["messages"].append(dict(role="user", content=reply))
        seen = self.responses()
        second, third = (json.loads(x["body"])["input"] for x in seen[1:])
        self.assertEqual(json.dumps(second, separators=(",", ":")),
                         json.dumps(third[:len(second)], separators=(",", ":")))
        for req in self.fake.seen:
            h = req["headers"]
            self.assertNotIn("x-api-key", h)
            self.assertFalse(any(k.startswith("anthropic-") for k in h))
            self.assertEqual(h["authorization"], "Bearer " + self.token)
            self.assertEqual(h["originator"], "codex_cli_rs")
            self.assertEqual(h["version"], "0.155.1")
            self.assertTrue(h["user-agent"].endswith(" unknown"))
        for req in seen:
            h = req["headers"]
            self.assertEqual(h["session-id"], h["x-client-request-id"])
            self.assertEqual(h["x-openai-subagent"], "collab_spawn")
            self.assertEqual(h["x-openai-internal-codex-responses-lite"], "true")
        folders = sorted(Path(self.capture).rglob(SENT_BODY))
        self.assertEqual(len(folders), 3)
        for path, req in zip(folders, seen):
            self.assertIn("-gpt-", str(path))
            self.assertEqual(path.read_bytes(), req["body"])
        log = fakes.logged(rt)
        self.assertEqual(log.count("    gpt lane="), 3)
        self.assertIn("input=100 cached=60 ratio=0.600 attempts=1", log)
        self.assertIn("kind=sub", log)
        calls = fakes.calls("rec")
        self.assertEqual([v["route"] for k, v in calls if k == "on_request"], ["chatgpt"] * 3)
        self.assertEqual([v.status for k, v in calls if k == "on_response_end"], [200] * 3)
        self.assertTrue(any(k == "on_event" and v["event"] == "message_stop" for k, v in calls))

    def test_off_forwards_gpt_untouched(self):
        from model_gateway.health import health_line
        rt, port = self.gateway(MODEL_GATEWAY_CHATGPT="false", MODEL_GATEWAY_CHATGPT_ORIGIN="bad")
        resp, data = self.send(port)
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, fakes.SSE_BODY)
        gpt = fakes.Upstream.seen[-1]
        self.assertEqual(gpt["body"], json.dumps(self.body).encode())
        self.send(port, dict(self.body, model="claude-test"))  # a GPT id goes exactly as a Claude id does
        claude = fakes.Upstream.seen[-1]
        self.assertEqual([x for x in gpt["items"] if x[0] != "content-length"],
                         [x for x in claude["items"] if x[0] != "content-length"])
        self.assertEqual(gpt["headers"]["content-length"], str(len(gpt["body"])))
        self.assertEqual((gpt["headers"]["authorization"], gpt["headers"]["x-api-key"]), ("Bearer claude-secret", "claude-key"))
        self.assertEqual(self.fake.seen, [])
        self.assertEqual(fakes.request(port, "GET", "/__gateway/health")[1],
                         health_line(rt.settings.current(), rt.chain, rt.log).encode())

    def test_guard_unknown_and_suffixes(self):
        rt, port = self.gateway()
        for model, headers, text in [("gpt-test", {}, "GPT models run as sub-agents only"),
                ("unknown", self.headers, 'unknown model "unknown"; the ChatGPT catalog has: gpt-test')]:
            with self.subTest(model=model):
                resp, data = self.send(port, dict(self.body, model=model), headers)
                self.assertEqual(resp.status, 400)
                self.assertEqual(json.loads(data)["error"]["message"], "chatgpt upstream: " + text)
        self.assertEqual(self.send(port, dict(self.body, model="gpt-test-fast[1m]"))[0].status, 200)
        self.assertEqual(json.loads(self.responses()[-1]["body"])["service_tier"], "priority")

    def test_refresh_once_and_oauth_identity(self):
        self.fake.script = [(401, {"error": {"message": "expired"}}), (200, fakes.responses_turn())]
        rt, port = self.gateway()
        resp, data = self.send(port)
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(self.responses()), 2)
        self.assertEqual(self.responses()[1]["headers"]["authorization"], "Bearer " + self.fake.tokens["access_token"])
        refresh = [x for x in self.fake.seen if x["path"] == "/oauth/token"]
        self.assertEqual(len(refresh), 1)
        self.assertEqual(refresh[0]["headers"]["version"], "0.155.1")
        self.assertIn("action=refresh", fakes.logged(rt))

    def test_usage_limit_and_precommit_context_error(self):
        self.fake.script = [(429, {"error": {"type": "usage_limit_reached", "resets_at": clock.now() + 60}}),
                            (200, fakes.responses_event("response.failed", response={
                                "error": {"code": "context_length_exceeded", "message": "too big"}}))]
        rt, port = self.gateway()
        for status, kind in [(429, "rate_limit_error"), (413, "request_too_large")]:
            resp, data = self.send(port)
            self.assertEqual(resp.status, status)
            self.assertEqual(json.loads(data)["error"]["type"], kind)
            if status == 429:
                self.assertIsNotNone(resp.getheader("retry-after"))
        self.assertEqual(len(self.responses()), 2)

    def test_catalog_fallback_and_unavailable_leave_claude_alone(self):
        jail.write_json(self.models_path, fakes.CHATGPT_MODELS)
        os.utime(self.models_path, (0, 0))
        self.fake.models_status = 503
        rt, port = self.gateway()
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertIn("catalog refresh failed", fakes.logged(rt))
        os.unlink(self.models_path)
        rt2, port2 = self.gateway()
        resp, data = self.send(port2)
        self.assertEqual(resp.status, 503)
        self.assertIn("no model catalog", json.loads(data)["error"]["message"])
        calls = len(self.fake.seen)
        self.assertEqual(self.send(port2, dict(self.body, model="claude-test"))[1], fakes.SSE_BODY)
        self.assertEqual(len(self.fake.seen), calls)

    def test_count_tokens_without_credentials_and_health(self):
        jail.write_json(self.models_path, fakes.CHATGPT_MODELS)
        os.unlink(self.auth_path)
        rt, port = self.gateway()
        resp, data = self.send(port, path="/v1/messages/count_tokens?beta=1")
        self.assertEqual(resp.status, 200)
        self.assertGreater(json.loads(data)["input_tokens"], 0)
        self.assertEqual(self.fake.seen, [])
        line = fakes.request(port, "GET", "/__gateway/health")[1]
        self.assertIn(b" chatgpt=on catalog=1 models middleware=", line)

    def test_nonstream_and_login_failure(self):
        rt, port = self.gateway()
        resp, data = self.send(port, dict(self.body, stream=False))
        self.assertEqual(resp.getheader("content-type"), "application/json")
        self.assertEqual(json.loads(data)["content"][-1]["text"], "Hello")
        os.unlink(self.auth_path)
        resp, data = self.send(port)
        self.assertEqual(resp.status, 403)
        self.assertEqual(json.loads(data)["error"]["type"], "permission_error")
        self.assertIn("gateway.py chatgpt-login", json.loads(data)["error"]["message"])
        self.assertEqual(resp.getheader("x-should-retry"), "false")


class RouteRetryTest(unittest.TestCase):
    setUp = RouteTest.setUp
    jwt = RouteTest.jwt
    gateway = RouteTest.gateway
    send = RouteTest.send
    responses = RouteTest.responses
    def test_retry_budget_drop_reasoning_and_final_auth(self):
        from model_gateway.chatgpt import signature
        self.body["messages"].append(dict(role="assistant", content=[dict(type="thinking", thinking="",
            signature=signature.encode("gpt-test", "rs_old", "old-invented"))]))
        self.fake.script = [(400, {"error": {"code": "invalid_encrypted_content"}}), (200, fakes.responses_turn())]
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0) as backoff:
            resp, data = self.send(port)
        self.assertEqual(resp.status, 200)
        backoff.assert_called_once_with(1)
        before, after = [json.loads(x["body"])["input"] for x in self.responses()]
        self.assertTrue(any(item.get("type") == "reasoning" for item in before))
        self.assertFalse(any(item.get("type") == "reasoning" for item in after))
        self.assertIn("gpt drop-reasoning lane=", fakes.logged(rt))
        captured = next(Path(self.capture).rglob(SENT_BODY)).read_bytes()
        self.assertEqual(captured, self.responses()[-1]["body"])
        self.fake.script = [(503, {"error": {"message": "unavailable"}})] * 5
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0) as backoff:
            resp, data = self.send(port)
        self.assertEqual(resp.status, 503)
        self.assertEqual(backoff.call_args_list, [unittest.mock.call(n) for n in range(1, 5)])
        self.assertEqual(len(self.responses()), 7)
        self.fake.script = [(401, {"error": {"message": "expired"}})] * 2
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            resp, data = self.send(port)
        self.assertEqual(resp.status, 403)
        self.assertEqual(resp.getheader("x-should-retry"), "false")
        self.assertEqual(len([x for x in self.fake.seen if x["path"] == "/oauth/token"]), 1)
        for req in self.fake.seen:  # G7 on every door: retries, the refresh and the catalog fetch
            with self.subTest(path=req["path"]):
                self.assertFalse({"x-api-key", "anthropic-version", "anthropic-beta"} & set(req["headers"]))
                self.assertNotIn("claude-secret", req["headers"].get("authorization", ""))

    def test_committed_failure_streaming_and_nonstream(self):
        failed = (fakes.responses_event("response.output_text.delta", delta="visible") +
                  fakes.responses_event("response.failed", response={"error": {"message": "failed"}}))
        self.fake.script = [(200, failed), (200, failed)]
        rt, port = self.gateway()
        resp, data = self.send(port)
        self.assertEqual(resp.status, 200)
        self.assertIn(b"event: error", data)
        resp, data = self.send(port, dict(self.body, stream=False))
        self.assertEqual(resp.status, 502)
        self.assertEqual(json.loads(data)["type"], "error")
        self.assertEqual(len(self.responses()), 2)
        self.assertIn("action=final", fakes.logged(rt))

    def test_calibration_images_and_lane_bound(self):
        from model_gateway.chatgpt import lane, request
        rt, port = self.gateway()
        self.send(port)
        translation = request.translate(self.body, rt.chatgpt.catalog.models()["gpt-test"],
                                        lane.key("session-invented", "agent-invented"))
        self.body["messages"].append(dict(role="user", content=[dict(type="tool_result", tool_use_id="missing",
            content=[dict(type="image", source=dict(type="base64", media_type="image/png", data="invented"))])]))
        resp, data = self.send(port, path="/v1/messages/count_tokens")
        self.assertEqual(resp.status, 200)
        self.assertGreaterEqual(json.loads(data)["input_tokens"], 2100)
        self.assertEqual(len(self.responses()), 1)
        first = rt.chatgpt.estimator("oldest")
        for n in range(1024):
            rt.chatgpt.estimator(str(n))
        self.assertEqual(len(rt.chatgpt.lanes), 1024)
        self.assertIsNot(rt.chatgpt.estimator("oldest"), first)

    def test_failed_and_unmarked_hooks_preserve_original_body(self):
        for source in ['req.body["model"] = "claude-other"; req.mark_changed()',
                       'req.body["system"] = "unmarked"',
                       'req.body["system"] = "discard"; req.mark_changed(); raise ValueError("invented")']:
            rt, port = fakes.start_gateway(self, self.anthropic, env=self.env,
                settings={"middleware": [{"module": "change"}]}, middleware={"change":
                    'NAME = "change"\ndef on_request(ctx, req):\n    ' + source + '\n'})
            resp, data = self.send(port)
            self.assertEqual(resp.status, 200)
            sent = json.loads(self.responses()[-1]["body"])
            self.assertEqual(sent["model"], "gpt-test")
            self.assertFalse(any(x.get("role") == "developer" for x in sent["input"] if x["type"] == "message"))


    def test_no_login_without_catalog_and_modelless_bodies(self):
        os.unlink(self.auth_path)
        rt, port = self.gateway()
        resp, data = self.send(port)
        self.assertEqual((resp.status, json.loads(data)["error"]["type"]), (403, "permission_error"))
        self.assertIn("run gateway.py chatgpt-login", json.loads(data)["error"]["message"])
        self.assertEqual(resp.getheader("x-should-retry"), "false")
        for raw in (b"not json", json.dumps(dict(messages=[])).encode()):  # Anthropic validates these, as before
            with self.subTest(raw=raw):
                self.assertEqual(fakes.request(port, "POST", "/v1/messages", raw, self.headers)[1], fakes.SSE_BODY)
                self.assertEqual(fakes.Upstream.seen[-1]["body"], raw)

    def test_client_leaving_mid_stream_ends_the_reader_on_a_silent_upstream(self):
        import http.client
        import threading
        import time
        from model_gateway.chatgpt import response, route
        left, release, readers = threading.Event(), threading.Event(), []
        base = self.fake

        class Silent(base):
            def do_POST(inner):
                if inner.path != "/backend-api/codex/responses":
                    return base.do_POST(inner)
                inner.rfile.read(int(inner.headers["content-length"]))
                inner.send_response(200)
                inner.send_header("content-type", "text/event-stream")
                inner.send_header("transfer-encoding", "chunked")
                inner.end_headers()
                for n in range(3):  # the first commits; the rest reach a client that has gone
                    data = fakes.responses_event("response.output_text.delta", output_index=0, delta="x%d" % n)
                    inner.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    inner.wfile.flush()
                    if n == 0:
                        left.wait(5)
                    time.sleep(0.1)
                release.wait(10)  # then silence: a long reasoning pause, the connection kept open

        origin = fakes.start_upstream(self, Silent)
        self.addCleanup(release.set)
        rt, port = self.gateway(MODEL_GATEWAY_CHATGPT_ORIGIN=origin)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        with patch.object(route, "Reader", side_effect=lambda *a: readers.append(response.Reader(*a)) or readers[-1]):
            conn.request("POST", "/v1/messages", json.dumps(self.body).encode(), self.headers)
            resp = conn.getresponse()
            self.assertIn(b"x0", resp.read1(65536))
        conn.sock.close()
        conn.close()
        left.set()
        readers[0].thread.join(3)
        self.assertFalse(readers[0].thread.is_alive())
        self.assertIn("client disconnected mid-stream", fakes.logged(rt))

    def test_edited_body_capture_holds_only_what_was_sent(self):
        rt, port = fakes.start_gateway(self, self.anthropic, env=self.env,
            settings={"capture_dir": self.capture, "middleware": [{"module": "edit"}]}, middleware={"edit":
                'NAME = "edit"\ndef on_request(ctx, req):\n    req.body["metadata"] = {}\n    req.mark_changed()\n'})
        for body, status in [(dict(self.body, model="unknown"), 400),
                             (dict(self.body, messages=[dict(content="no role")]), 400), (self.body, 200)]:
            self.assertEqual(self.send(port, body)[0].status, status)
        sent = sorted(Path(self.capture).rglob(SENT_BODY))
        self.assertEqual([path.read_bytes() for path in sent], [self.responses()[-1]["body"]])

    def test_transport_retry_auth_error_and_claude_bypass(self):
        from model_gateway.chatgpt.auth import AuthError
        from model_gateway.upstream import UpstreamError
        rt, port = self.gateway()
        original = rt.chatgpt.client.responses
        attempts = []
        def transient(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise UpstreamError("invented transport")
            return original(*args)
        with patch.object(rt.chatgpt.client, "responses", side_effect=transient), \
                patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual(len(attempts), 2)
        self.assertIn("status=transport attempt=1 action=retry", fakes.logged(rt))
        with patch.object(rt.chatgpt.store, "credentials", side_effect=AuthError("invented transient")):
            resp, data = self.send(port)
        self.assertEqual(resp.status, 503)
        self.assertEqual(json.loads(data)["error"]["message"], "chatgpt upstream: invented transient")
        with patch.object(rt.chatgpt.catalog, "models", side_effect=AssertionError("must not fetch")):
            self.assertEqual(self.send(port, dict(self.body, model="claude-test"))[1], fakes.SSE_BODY)
            resp, data = fakes.request(port, "POST", "/v1/json", json.dumps(self.body).encode(), self.headers)
            self.assertEqual(data, fakes.JSON_BODY)

    def test_explicit_empty_completion_exhausts_before_output(self):
        self.fake.script = [(200, fakes.responses_event("response.completed", response={"usage": {}}))] * 5
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0):
            resp, data = self.send(port)
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.getheader("x-should-retry"), "false")
        self.assertEqual(len(self.responses()), 5)
        self.assertEqual(json.loads(data)["error"]["message"], "chatgpt upstream: empty completion")
