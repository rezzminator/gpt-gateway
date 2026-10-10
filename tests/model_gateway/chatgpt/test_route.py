"""Real gateway exchanges against invented Anthropic, ChatGPT and OAuth servers."""
import base64
import json
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from model_gateway import clock
from model_gateway.capture import SENT_BODY
from model_gateway.chatgpt import stream
from tests import fakes, jail


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.home = jail.scratch(self)
        self.fake = type("FakeChatGPT", (fakes.ChatGPTUpstream,), {"seen": [], "script": [], "tokens": {}, "upgrades": [], "refuse": [], "vanish": [], "hangup": [], "stall": []})
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
        self.env = jail.clean_env(self.home, MODEL_GATEWAY_CHATGPT="1", MODEL_GATEWAY_CHATGPT_AUTH_STORE="file", MODEL_GATEWAY_CHATGPT_TRANSPORT="http",
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
            self.assertEqual(h["version"], "0.162.1")
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
        self.assertEqual([(r["input_tokens"], r["cache_read_tokens"], r["hit"], r["attempts"], r["kind"])
                          for r in fakes.gpt_rows(rt)], [(100, 60, 0.6, 1, "sub")] * 3)
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
        rows = re.compile(r" (db_rows|fds)=\S+")  # a row still queued may land between the two, a handle open for one
        self.assertEqual(rows.sub("", fakes.request(port, "GET", "/__gateway/health")[1].decode()),
                         rows.sub("", health_line(rt.settings.current(), rt)))

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
        self.assertEqual(refresh[0]["headers"]["version"], "0.162.1")
        self.assertEqual([e["fields"]["action"] for e in fakes.events(rt, "retry")], ["refresh"])

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
        self.assertGreaterEqual(len(fakes.events(rt, "catalog_refresh_failed")), 1)
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
        pdf = dict(type="document", source=dict(type="base64", media_type="application/pdf", data="A" * 40000))
        resp, data = self.send(port, dict(self.body, messages=[dict(role="user", content=[pdf])]), path="/v1/messages/count_tokens?beta=1")
        self.assertEqual(resp.status, 200)
        self.assertTrue(2000 <= json.loads(data)["input_tokens"] < 2500, "a PDF is one flat attachment, never its payload: %s" % data)
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
        self.assertEqual([(e["fields"], bool(e["lane"]), bool(e["request_id"])) for e in fakes.events(
            rt, "drop_reasoning")], [({"slug": "gpt-test"}, True, True)])
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
        self.assertEqual([e["fields"]["action"] for e in fakes.events(rt, "final_error")], ["final", "final"])

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
        first = rt.chatgpt.lane_state("oldest")
        for n in range(1024):
            rt.chatgpt.lane_state(str(n))
        self.assertEqual(len(rt.chatgpt.lanes), 1024)
        self.assertIsNot(rt.chatgpt.lane_state("oldest"), first)

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
        self.assertIn("run codex login, or gateway.py chatgpt-login", json.loads(data)["error"]["message"])
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
        self.assertEqual([(r["route"], r["client_disconnected"]) for r in fakes.recorded(rt)], [("chatgpt", 1)])

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
        self.assertEqual([(e["fields"]["status"], e["fields"]["attempt"]) for e in fakes.events(rt, "retry")],
                         [("transport", 1)])
        with patch.object(rt.chatgpt.store, "credentials", side_effect=AuthError("invented transient")):
            resp, data = self.send(port)
        self.assertEqual(resp.status, 503)
        self.assertEqual(json.loads(data)["error"]["message"], "chatgpt upstream: invented transient")
        with patch.object(rt.chatgpt.catalog, "models", side_effect=AssertionError("must not fetch")):
            self.assertEqual(self.send(port, dict(self.body, model="claude-test"))[1], fakes.SSE_BODY)
            resp, data = fakes.request(port, "POST", "/v1/json", json.dumps(self.body).encode(), self.headers)
            self.assertEqual(data, fakes.JSON_BODY)

    def test_an_empty_completion_ends_the_turn_with_no_content(self):
        # Claude Code answers an end_turn without text with one nudge; a retried empty only burns the plan.
        self.fake.script = [(200, fakes.responses_event("response.completed", response={"usage": {}}))]
        rt, port = self.gateway()
        resp, data = self.send(port)
        message = stream.accumulate(data)
        self.assertEqual((resp.status, len(self.responses()), message["content"]), (200, 1, []))
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertEqual([r["empty"] for r in fakes.gpt_rows(rt)], [1])


class CodexRouteTest(unittest.TestCase):
    setUp = RouteTest.setUp
    jwt = RouteTest.jwt
    gateway = RouteTest.gateway
    send = RouteTest.send
    responses = RouteTest.responses

    def test_k15_codex_headers_reach_fake_without_creating_own_store(self):
        path = os.path.join(self.home, ".codex", "auth.json")
        os.makedirs(os.path.dirname(path))
        access = self.jwt("codex")
        jail.write_json(path, {"auth_mode": "chatgpt", "tokens": {
            "access_token": access, "refresh_token": "invented-codex-refresh", "account_id": "acct-test-0001"}})
        os.unlink(self.auth_path)
        rt, port = self.gateway()
        response, data = self.send(port)
        self.assertEqual(response.status, 200)
        self.assertIn(b"message_stop", data)
        self.assertGreater(len(self.responses()), 0)
        for request in self.fake.seen:
            headers = request["headers"]
            self.assertEqual(headers["authorization"], "Bearer " + access)
            self.assertEqual(headers["chatgpt-account-id"], "acct-test-0001")
            self.assertNotIn("x-api-key", headers)
            self.assertFalse(any(name.startswith("anthropic-") for name in headers))
            self.assertNotIn("claude-secret", str(headers))
        self.assertFalse(os.path.exists(self.auth_path))
        from model_gateway.chatgpt.auth import LoginRequired
        with patch.object(rt.chatgpt.store, "credentials", side_effect=LoginRequired(
                "Codex login expired; run `codex login`")):
            response, data = self.send(port)
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(data)["error"]["message"],
                         "chatgpt upstream: Codex login expired; run `codex login`")
        os.unlink(path)
        response, data = self.send(port)
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(data)["error"]["message"],
                         "chatgpt upstream: ChatGPT login required: run codex login, or gateway.py chatgpt-login")

    def test_k17_reuses_clean_connection_drops_errors_and_recovers_stale(self):
        import http.client
        import threading
        from model_gateway.chatgpt.response import Reader
        release = threading.Event()
        self.addCleanup(release.set)
        # held: response.completed arrives but the stream's last chunk does not; never reuse that connection.
        for mode, expected in (("clean", 1), ("error", 2), ("stale", 2), ("will_close", 2), ("held", 2)):
            with self.subTest(mode=mode):
                accepted, held = [], []
                original_setup = fakes.ChatGPTUpstream.setup
                original_post = fakes.ChatGPTUpstream.do_POST
                original_header = fakes.ChatGPTUpstream.send_header
                def setup(handler):
                    accepted.append(handler.client_address)
                    original_setup(handler)
                def post(handler):
                    if mode == "held" and handler.path == "/backend-api/codex/responses" and not held:
                        held.append(handler.rfile.read(int(handler.headers["content-length"])))
                        data = fakes.responses_turn()
                        handler.send_response(200)
                        original_header(handler, "content-type", "text/event-stream")
                        original_header(handler, "transfer-encoding", "chunked")
                        handler.end_headers()
                        handler.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                        handler.wfile.flush()
                        release.wait(10)
                        return
                    original_post(handler)
                    if mode in ("stale", "will_close"):
                        handler.close_connection = True
                def header(handler, name, value):
                    original_header(handler, name, value)
                    if name == "content-length" and mode == "will_close":
                        original_header(handler, "Connection", "close")
                self.fake.setup, self.fake.do_POST, self.fake.send_header = setup, post, header
                error = (fakes.responses_event("response.output_text.delta", delta="partial") +
                         fakes.responses_event("response.failed", response={"error": {
                             "code": "invalid_prompt", "message": "invented failure"}}))
                self.fake.script = [(200, error if mode == "error" else fakes.responses_turn()),
                                    (200, fakes.responses_turn())]
                jail.write_json(self.models_path, fakes.CHATGPT_MODELS)
                rt, port = self.gateway()
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                self.addCleanup(client.close)
                readers = []
                watch = rt.chatgpt.client.watch
                def watched(reader):
                    readers.append(reader.thread)
                    watch(reader)
                before = set(threading.enumerate())
                connections = []
                connection = rt.chatgpt.client.upstream.connection
                def connected(**kwargs):
                    conn = connection(**kwargs)
                    connections.append(conn)
                    return conn
                with patch.object(rt.chatgpt.client, "watch", side_effect=watched), \
                        patch.object(rt.chatgpt.client.upstream, "connection", side_effect=connected):
                    for index in range(2):
                        client.request("POST", "/v1/messages", json.dumps(self.body), self.headers)
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        data = response.read()
                        self.assertIn(b"event: error" if mode == "error" and index == 0 else b"message_stop", data)
                        self.assertTrue(jail.wait_for(lambda: all(not t.is_alive() for t in readers), timeout=3))
                self.assertEqual(len(accepted), expected)
                if mode in ("clean", "stale"):
                    self.assertIs(connections[0], connections[1])
                    self.assertEqual(len(connections), 3 if mode == "stale" else 2)
                    if mode == "stale":
                        self.assertIsNot(connections[1], connections[2])
                else:
                    self.assertIsNot(connections[0], connections[1])
                self.assertTrue(all(t not in threading.enumerate() for t in readers))
                self.assertFalse(any(isinstance(getattr(getattr(t, "_target", None), "__self__", None), Reader)
                                     for t in set(threading.enumerate()) - before))
                rows = fakes.gpt_rows(rt)
                if mode == "stale":  # Upstream.send's own fresh retry marks the answer stale
                    self.assertEqual([(r["reused"], r["stale"], r["transport"]) for r in rows], [(0, 0, "http"), (0, 1, "http")])
                if mode == "held":  # the client had message_stop before the reader's 2 s join gave up
                    self.assertEqual([r["total_ms"] < 2000 for r in rows[:1]], [True], rows)
                client.close()

    def keepalive(self, port, turns):
        """The (status, body) of `turns` requests sent one after another on one client connection."""
        import http.client
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(client.close)
        answers = []
        for _ in range(turns):
            client.request("POST", "/v1/messages", json.dumps(self.body), self.headers)
            response = client.getresponse()
            answers.append((response.status, response.read()))
        return answers

    def test_reused_connection_that_dies_before_the_first_body_byte_retries_once_silently(self):
        self.fake.script = [(200, fakes.responses_turn()), (200, fakes.CLOSE_AFTER_HEADERS),
                            (200, fakes.responses_turn())]
        jail.write_json(self.models_path, fakes.CHATGPT_MODELS)
        rt, port = self.gateway()
        with patch("model_gateway.chatgpt.errors.backoff", return_value=0) as backoff:
            answers = self.keepalive(port, 2)
        self.assertEqual([status for status, _ in answers], [200, 200])
        self.assertIn(b"message_stop", answers[1][1])
        self.assertEqual(len(self.responses()), 3)
        backoff.assert_not_called()
        self.assertEqual(fakes.events(rt, "retry") + fakes.events(rt, "final_error"), [])
        self.assertEqual([(r["attempts"], r["reused"], r["stale"], r["transport"]) for r in fakes.gpt_rows(rt)],
                         [(1, 0, 0, "http"), (1, 0, 1, "http")])

    def test_a_fresh_failure_a_body_byte_or_a_second_failure_is_an_ordinary_retry(self):
        turn, cut, byte = ((200, fakes.responses_turn()), (200, fakes.CLOSE_AFTER_HEADERS),
                           (200, fakes.Cut(fakes.responses_event("response.created"))))
        for name, script, turns, stale in (("fresh connection", [cut, turn], 1, 0),
                                           ("body byte first", [turn, byte, turn], 2, 0),
                                           ("after the silent retry", [turn, cut, cut, turn], 2, 1)):
            with self.subTest(name):
                self.fake.script = script
                jail.write_json(self.models_path, fakes.CHATGPT_MODELS)
                rt, port = self.gateway()
                with patch("model_gateway.chatgpt.errors.backoff", return_value=0) as backoff:
                    self.assertEqual({status for status, _ in self.keepalive(port, turns)}, {200})
                backoff.assert_called_once_with(1)
                self.assertEqual([{k: e["fields"][k] for k in ("status", "attempt", "action", "error_head")}
                                  for e in fakes.events(rt, "retry")], [{"status": "transport", "attempt": 1,
                                  "action": "retry", "error_head": "IncompleteRead(0 bytes read)"}])
                self.assertEqual(fakes.events(rt, "final_error"), [])
                row = fakes.gpt_rows(rt)[-1]
                self.assertEqual((row["attempts"], row["reused"], row["stale"], row["transport"]), (2, 0, stale, "http"))

    def test_gpt_row_holds_the_measurement_fields(self):
        self.fake.script = [(200, fakes.responses_turn(reasoning=3)), (200, fakes.responses_turn())]
        jail.write_json(self.models_path, fakes.CHATGPT_MODELS)
        rt, port = self.gateway()
        self.keepalive(port, 2)
        names = ("input_tokens", "cache_read_tokens", "hit", "attempts", "output_tokens", "reasoning_tokens",
                 "reused", "stale", "transport", "status")
        rows = fakes.gpt_rows(rt)
        self.assertEqual([tuple(r[n] for n in names) for r in rows], [
            (100, 60, 0.6, 1, 8, 3, 0, 0, "http", 200), (100, 60, 0.6, 1, 8, None, 1, 0, "http", 200)])
        for row in rows:
            self.assertLessEqual(row["ttfb_ms"], row["total_ms"])
            self.assertLessEqual(row["upstream_ms"], row["total_ms"])

    def test_terminal_measures_from_receipt_send_and_terminal_event(self):
        from types import SimpleNamespace
        from model_gateway.chatgpt.route import Measure, Route
        events, row = [], {}
        route = Route.__new__(Route)
        route.event, route.lock = lambda kind, **fields: events.append(kind), __import__("threading").Lock()
        translator = stream.Translator("gpt-test", {}, 10)
        translator.terminal, translator.usage = "response.completed", {
            "output_tokens": 8, "cache_read_input_tokens": 60, "cache_creation_input_tokens": 0}
        translator.input_tokens, translator.cache_ratio, translator.reasoning_tokens = 100, 0.6, 5
        reader = SimpleNamespace(first_byte_at=101.25, terminal_at=103.0, transport_error=None, error_head=b"")
        measure = Measure(started=100.0, sent_at=100.5, reused=True, stale=True)
        estimator = SimpleNamespace(observe=lambda *args: None)
        with patch.object(clock, "monotonic", return_value=104.5):
            route._terminal("lane", SimpleNamespace(slug="gpt-test"), SimpleNamespace(estimate_chars=1, notes=("a \"b\"",)),
                            translator, estimator, 1, 200, reader, None, measure, row)
        self.assertEqual((events, row), ([], {
            "attempts": 1, "input_tokens": 100, "cache_read_tokens": 60, "cache_write_tokens": 0, "output_tokens": 8,
            "reasoning_tokens": 5, "hit": 0.6, "ttfb_ms": 1250, "upstream_ms": 2500, "total_ms": 4500, "reused": True,
            "stale": True, "turn_state": None, "transport": "http", "delta": None,
            "translation_notes": '["a \\"b\\""]', "empty": False}))
        route._terminal("lane", SimpleNamespace(slug="gpt-test"), SimpleNamespace(estimate_chars=1, notes=("damaged",) * 200),
                        translator, estimator, 1, 200, reader, None, measure, row)  # notes: whole JSON within 600, then a count
        self.assertEqual(row["translation_notes"], json.dumps(["damaged"] * 53 + ["147 more"]))

    def test_ttfb_counts_from_the_gateway_receipt_of_the_request(self):
        jail.write_json(self.models_path, fakes.CHATGPT_MODELS)
        rt, port = fakes.start_gateway(self, self.anthropic, env=self.env, settings={"middleware": [{"module": "slow"}]},
                                       middleware={"slow": 'NAME = "slow"\nfrom model_gateway import clock\n'
                                                           'def on_request(ctx, req):\n    clock.sleep(0.3)\n'})
        self.assertEqual(self.send(port)[0].status, 200)
        rows = fakes.gpt_rows(rt)
        self.assertEqual([r["ttfb_ms"] >= 300 for r in rows], [True], rows)
