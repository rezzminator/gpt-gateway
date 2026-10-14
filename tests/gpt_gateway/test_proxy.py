"""Tests for gpt_gateway.proxy: the passthrough end to end against a fake upstream, hooks included."""
import contextlib
import http.client
import io
import json
import os
import socket
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from gpt_gateway import clock
from tests import fakes, jail
from tests.gpt_gateway.chatgpt import test_route as route_tests
from tests.fakes import ERROR_BODY, JSON_BODY, RECORDER, SSE_BODY, SSE_EVENTS, Upstream, calls

ODD = b'{"model":  "claude-x", "stream": true,\n "messages": [{"role": "user", "content": "hi"}]}'
EDIT = 'NAME = "edit"\ndef on_request(ctx, req):\n    req.body["added"] = 1\n    req.mark_changed()\n'
FAIL = 'NAME = "fail"\ndef on_request(ctx, req):\n    req.body["messages"] = []\n    req.mark_changed()\n    raise RuntimeError("x")\n'
NOISY = 'NAME = "noisy"\ndef on_event(ctx, e):\n    raise RuntimeError("every event")\n'


def chain(*modules):
    return {"middleware": [{"module": m} for m in modules]}


ROW = ("method", "path", "status", "kind", "route", "model", "stream", "completed", "client_disconnected", "note",
       "answered_by", "error_code")


def rows(rt, *names):
    """The request rows, each cut to `names` (default ROW)."""
    return [{k: r[k] for k in names or ROW} for r in fakes.recorded(rt)]


class Bare(BaseHTTPRequestHandler):
    """Answers 404 with no body, no request id and no rate-limit header."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.send_header("content-length", "0")
        self.end_headers()


class Resetting(BaseHTTPRequestHandler):
    """Streams the first event, then resets the connection (RST, no FIN) once the client has it (`relayed`)."""

    protocol_version = "HTTP/1.1"
    relayed = threading.Event()

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"%x\r\n%s\r\n" % (len(SSE_EVENTS[0]), SSE_EVENTS[0]))
        self.wfile.flush()
        Resetting.relayed.wait(10)  # the gateway read the event and waits on the next
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        self.connection.close()
        self.close_connection = True


def ended(module, n):
    """The recorded calls once `n` responses have ended: on_response_end runs after the client has the response."""
    jail.wait_for(lambda: sum(c[0] == "on_response_end" for c in calls(module)) >= n, 5)
    return calls(module)


class PassthroughTest(unittest.TestCase):
    def setUp(self):
        Upstream.seen.clear()
        self.up = fakes.start_upstream(self)

    def test_streams_the_response_and_forwards_the_request_byte_for_byte(self):
        rt, port = fakes.start_gateway(self, self.up)
        resp, data = fakes.request(port, "POST", "/v1/messages?beta=true", ODD,
                                   {"authorization": "Bearer secret-token", "anthropic-beta": "b1"})
        self.assertEqual((resp.status, data, resp.getheader("request-id")), (200, SSE_BODY, "req_test"))
        sent = Upstream.seen[-1]
        self.assertEqual((sent["path"], sent["body"]), ("/v1/messages?beta=true", ODD))
        self.assertEqual(sent["headers"].get("authorization") or sent["headers"].get("Authorization"), "Bearer secret-token")
        self.assertEqual(rows(rt), [{"method": "POST", "path": "/v1/messages?beta=true", "status": 200, "kind": "main",
                                     "route": "anthropic", "model": "claude-x", "stream": 1, "completed": 1,
                                     "client_disconnected": 0, "note": None, "answered_by": None, "error_code": None}])
        self.assertNotIn(b"secret-token", fakes.db_bytes(rt))

    def test_chunked_request_bodies_are_read_whole(self):
        rt, port = fakes.start_gateway(self, self.up)
        resp, _ = fakes.request(port, "POST", "/v1/messages", ODD, {"content-type": "application/json"}, chunked=True)
        self.assertEqual((resp.status, Upstream.seen[-1]["body"]), (200, ODD))

    def test_upstream_errors_pass_through_with_their_status_and_are_recorded(self):
        rt, port = fakes.start_gateway(self, self.up)
        resp, data = fakes.request(port, "GET", "/v1/models")
        self.assertEqual((resp.status, data), (429, ERROR_BODY))
        self.assertEqual(rows(rt, "method", "path", "status", "kind", "error_code", "error_head"), [
            {"method": "GET", "path": "/v1/models", "status": 429, "kind": "main", "error_code": "rate_limit_error",
             "error_head": ERROR_BODY.decode()}])
        self.assertEqual([e["fields"] for e in fakes.events(rt, "error_detail")],
                         [{"upstream_request_id": "req_limited",
                           "ratelimit": {"retry-after": "3", "requests-remaining": "0"}}])

    def test_an_error_reply_with_no_context_records_no_empty_detail(self):
        rt, port = fakes.start_gateway(self, fakes.start_upstream(self, Bare))
        self.assertEqual(fakes.request(port, "GET", "/")[0].status, 404)
        self.assertEqual(rows(rt, "path", "status"), [{"path": "/", "status": 404}])
        self.assertEqual(fakes.events(rt, "error_detail"), [])

    def kept(self, port):
        """A client connection Claude Code's way: kept alive, so its upstream connection stays with it."""
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.addCleanup(conn.close)
        return conn

    def ask(self, conn, method, path, body=None):
        conn.request(method, path, body=body)
        resp = conn.getresponse()
        resp.read()
        return resp.status

    def test_an_anthropic_row_carries_its_transport(self):
        rt, port = fakes.start_gateway(self, self.up)
        client = self.kept(port)
        for _ in range(2):
            self.assertEqual(self.ask(client, "POST", "/v1/json", b'{"model":"m"}'), 200)
        recorded = fakes.recorded(rt)
        self.assertEqual([(r["reused"], r["stale"], r["attempts"]) for r in recorded], [(0, 0, 1), (1, 0, 1)])
        self.assertEqual([(r.get("request_bytes"), r.get("error_class")) for r in recorded], [(13, None)] * 2)

    def test_an_upstream_that_resets_mid_stream_is_its_failure_never_the_client_leaving(self):
        Resetting.relayed.clear()
        rt, port = fakes.start_gateway(self, fakes.start_upstream(self, Resetting))
        client = self.kept(port)
        client.request("POST", "/v1/messages", body=b'{"model":"m","stream":true}')
        resp, got = client.getresponse(), b""
        while len(got) < len(SSE_EVENTS[0]):
            got += resp.read1(65536) or self.fail("the stream ended before its first event: %r" % got)
        Resetting.relayed.set()
        with self.assertRaises((http.client.HTTPException, OSError)):
            resp.read()
        (row,) = fakes.recorded(rt)
        self.assertEqual((row["status"], row["completed"], row["client_disconnected"], row["error_code"]),
                         (200, 0, 0, "stream_error"))
        self.assertEqual((row.get("error_class"), row["error_head"].startswith("ConnectionResetError")), ("reset", True))

    def test_a_host_pause_is_recorded_and_closes_the_idle_upstream_connections(self):
        rt, port = fakes.start_gateway(self, self.up)
        client = self.kept(port)
        self.assertEqual(self.ask(client, "POST", "/v1/json", b"{}"), 200)
        self.assertEqual(len(rt.upstream.pool.idle), 1)
        slept = clock.slept
        with mock.patch.object(clock, "slept", lambda: slept() + 3600):  # the host slept an hour
            self.assertEqual(self.ask(client, "GET", "/__gateway/health"), 200)
        resumed = [e["fields"]["gap_s"] for e in fakes.events(rt, "host_resumed")]
        self.assertEqual([gap >= 3599 for gap in resumed], [True])
        self.assertEqual(rt.upstream.pool.idle, [])

    def test_a_request_whose_exchange_breaks_still_gets_its_row(self):
        from unittest import mock
        rt, port = fakes.start_gateway(self, self.up)
        rt.upstream.send = mock.Mock(side_effect=RuntimeError("invented"))
        with self.assertRaises((http.client.HTTPException, OSError)):
            fakes.request(port, "POST", "/v1/messages", ODD)
        self.assertEqual(rows(rt, "path", "status", "error_code", "error_head"), [
            {"path": "/v1/messages", "status": None, "error_code": "gateway_error", "error_head": "RuntimeError('invented')"}])

    def test_usage_is_read_off_a_messages_call_only(self):
        rt, port = fakes.start_gateway(self, self.up)
        fakes.request(port, "POST", "/v1/json", b"{}")  # a JSON reply that carries a usage, on another path
        self.assertEqual(rows(rt, "path", "status", "input_tokens"), [{"path": "/v1/json", "status": 200, "input_tokens": None}])

    def test_answers_502_when_upstream_is_unreachable(self):
        rt, port = fakes.start_gateway(self)
        resp, data = fakes.request(port, "POST", "/v1/messages", b"{}")
        body = json.loads(data)
        self.assertEqual((resp.status, body["type"]), (502, "error"))
        self.assertTrue(body["error"]["message"].startswith("gpt-gateway: upstream unreachable"))
        self.assertEqual([(r["method"], r["path"], r["status"], r["error_code"], "upstream unreachable" in r["error_head"],
                           r.get("error_class"), r["reused"], r["stale"], r["attempts"]) for r in fakes.recorded(rt)],
                         [("POST", "/v1/messages", 502, "api_error", True, "connect", 0, 0, 1)])

    def test_a_header_sent_twice_goes_upstream_twice_in_order(self):
        rt, port = fakes.start_gateway(self, self.up)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"POST /v1/json HTTP/1.1\r\nhost: x\r\nanthropic-beta: b1\r\nanthropic-beta: b2\r\n"
                  b"content-length: 2\r\nconnection: close\r\n\r\n{}")
        resp = http.client.HTTPResponse(s)
        resp.begin()
        self.assertEqual((resp.status, resp.read()), (200, JSON_BODY))
        s.close()
        self.assertEqual([v for k, v in Upstream.seen[-1]["items"] if k.lower() == "anthropic-beta"], ["b1", "b2"])

    def test_an_unreadable_body_length_is_answered_400_and_never_forwarded(self):
        rt, port = fakes.start_gateway(self, self.up)
        for length in (b"abc", b"-5"):
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-length: " + length + b"\r\n\r\n{}")
            resp = http.client.HTTPResponse(s)
            try:
                resp.begin()
                status, data = resp.status, resp.read()
            except OSError as e:
                status, data = None, repr(e).encode()
            s.close()
            self.assertEqual(status, 400, (length, data))
            self.assertIn(b"gpt-gateway: unreadable request body", data)
        self.assertEqual(Upstream.seen, [])
        self.assertEqual([(r["status"], r["request_id"], r["route"], r["error_code"], "unreadable request body" in r[
            "error_head"]) for r in fakes.recorded(rt)], [(400, None, "anthropic", "invalid_request_error", True)] * 2)

    def test_kind_from_the_agent_header(self):
        rt, port = fakes.start_gateway(self, self.up)
        fakes.request(port, "POST", "/v1/messages", ODD, {"x-claude-code-agent-id": "a1"})
        self.assertEqual([(r["status"], r["kind"], r["agent_id"]) for r in fakes.recorded(rt)], [(200, "sub", "a1")])

    def test_debug_records_headers_without_secrets(self):
        rt, port = fakes.start_gateway(self, self.up, env={"GPT_GATEWAY_DEBUG": "1"})
        fakes.request(port, "POST", "/v1/messages", b'{"model":"m","metadata":{"user_id":"u"}}',
                      {"authorization": "Bearer secret-token", "x-claude-code-agent-id": "a1"})
        self.assertEqual([(e["fields"]["headers"].get("x-claude-code-agent-id"), e["fields"]["metadata"])
                          for e in fakes.events(rt, "debug")], [("a1", {"user_id": "u"})])
        self.assertNotIn(b"secret-token", fakes.db_bytes(rt))

    def test_a_client_reset_writes_no_traceback(self):
        rt, port = fakes.start_gateway(self, self.up)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = socket.create_connection(("127.0.0.1", port), timeout=30)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            time.sleep(0.2)
            s.close()
            time.sleep(0.3)
        self.assertEqual(err.getvalue(), "")


class HookExchangeTest(unittest.TestCase):
    def setUp(self):
        Upstream.seen.clear()
        self.up = fakes.start_upstream(self)

    def gateway(self, *modules, **sources):
        return fakes.start_gateway(self, self.up, settings=chain(*modules), middleware=sources)

    def test_the_context_a_hook_sees(self):
        rt, port = self.gateway("rec", rec=RECORDER.format(name="rec"))
        fakes.request(port, "POST", "/v1/messages?beta=true", ODD,
                      {"authorization": "Bearer t", "x-api-key": "k", "cookie": "c", "x-claude-code-agent-id": "a1"})
        (_, seen), = [c for c in calls("rec") if c[0] == "on_request"]
        self.assertEqual({k: seen[k] for k in ("kind", "model", "route", "request_id", "capture_dir", "method", "path")},
                         {"kind": "sub", "model": "claude-x", "route": "anthropic", "request_id": 1,
                          "capture_dir": None, "method": "POST", "path": "/v1/messages?beta=true"})
        self.assertEqual(seen["log_path"], rt.log.path)
        self.assertEqual(seen["headers"]["x-claude-code-agent-id"], "a1")
        for secret in ("authorization", "x-api-key", "cookie"):
            self.assertNotIn(secret, seen["headers"])
        self.assertEqual(rows(rt, "kind", "note"), [{"kind": "sub", "note": "rec=seen"}])

    def test_a_marked_edit_goes_upstream_compact(self):
        rt, port = self.gateway("edit", edit=EDIT)
        fakes.request(port, "POST", "/v1/messages", ODD)
        expected = dict(json.loads(ODD), added=1)
        self.assertEqual(Upstream.seen[-1]["body"], json.dumps(expected, separators=(",", ":")).encode())

    def test_an_on_request_failure_forwards_the_original_bytes_and_the_stream_whole(self):
        rt, port = self.gateway("edit", "fail", "rec", edit=EDIT, fail=FAIL, rec=RECORDER.format(name="rec"))
        resp, data = fakes.request(port, "POST", "/v1/messages", ODD)
        self.assertEqual((resp.status, data, Upstream.seen[-1]["body"]), (200, SSE_BODY, ODD))
        self.assertEqual([c[0] for c in ended("rec", 1)], ["on_event"] * 3 + ["on_response_end"])
        self.assertEqual([(e["fields"]["module"], e["fields"]["hook"], e["fields"]["error"])
                          for e in fakes.events(rt, "middleware_failed")], [("fail", "on_request", "RuntimeError('x')")])
        self.assertEqual(rows(rt, "status", "kind", "note"), [{"status": 200, "kind": "main", "note": None}])

    def test_on_event_sees_whole_events_across_split_chunks(self):
        rt, port = self.gateway("rec", rec=RECORDER.format(name="rec"))
        resp, data = fakes.request(port, "POST", "/v1/messages?split=1", ODD)
        self.assertEqual(data, SSE_BODY)
        events = [c[1] for c in ended("rec", 1) if c[0] == "on_event"]
        self.assertEqual([(e["event"], e["data"]["type"]) for e in events],
                         [("message_start", "message_start"), ("content_block_delta", "content_block_delta"),
                          ("message_stop", "message_stop")])
        self.assertEqual(b"".join(e["raw"] + b"\n\n" for e in events), SSE_BODY)

    def test_on_event_gets_a_non_stream_json_body_once_and_error_bodies_too(self):
        rt, port = self.gateway("rec", rec=RECORDER.format(name="rec"))
        fakes.request(port, "POST", "/v1/json", b"{}")
        fakes.request(port, "GET", "/v1/models")
        events = [c[1] for c in ended("rec", 2) if c[0] == "on_event"]
        self.assertEqual(events, [{"event": None, "data": json.loads(JSON_BODY), "raw": JSON_BODY},
                                  {"event": None, "data": json.loads(ERROR_BODY), "raw": ERROR_BODY}])

    def test_a_failing_on_event_never_cuts_the_stream(self):
        rt, port = self.gateway("noisy", "rec", noisy=NOISY, rec=RECORDER.format(name="rec"))
        resp, data = fakes.request(port, "POST", "/v1/messages?split=1", ODD)
        self.assertEqual((resp.status, data), (200, SSE_BODY))
        self.assertEqual(len([c for c in ended("rec", 1) if c[0] == "on_event"]), 3)
        self.assertEqual([(e["fields"]["module"], e["fields"]["hook"]) for e in fakes.events(rt, "middleware_failed")],
                         [("noisy", "on_event")])

    def test_on_response_end_fields(self):
        rt, port = self.gateway("rec", rec=RECORDER.format(name="rec"))
        fakes.request(port, "POST", "/v1/messages", ODD)
        fakes.request(port, "GET", "/v1/models")
        ok, err = [c[1] for c in ended("rec", 2) if c[0] == "on_response_end"]
        self.assertEqual((ok.status, ok.reason, ok.error_head, ok.completed, ok.client_disconnected, ok.note),
                         (200, "OK", b"", True, False, " kind=main rec=seen"))
        self.assertIn(("request-id", "req_test"), ok.headers)
        self.assertGreater(ok.duration_s, 0)
        self.assertEqual((err.status, err.error_head, err.completed), (429, ERROR_BODY, True))

    def test_a_client_that_leaves_mid_stream_is_reported(self):
        up = fakes.start_upstream(self, fakes.SlowUpstream)
        rt, port = fakes.start_gateway(self, up, settings=chain("rec"), middleware={"rec": RECORDER.format(name="rec")})
        s = socket.create_connection(("127.0.0.1", port), timeout=30)
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-length: 2\r\n\r\n{}")
        s.recv(100)
        s.close()
        self.assertTrue(jail.wait_for(lambda: any(c[0] == "on_response_end" for c in calls("rec")), 5))
        (end,) = [c[1] for c in calls("rec") if c[0] == "on_response_end"]
        self.assertEqual((end.completed, end.client_disconnected), (False, True))
        self.assertEqual(rows(rt, "method", "path", "completed", "client_disconnected", "kind", "note"), [
            {"method": "POST", "path": "/v1/messages", "completed": 0, "client_disconnected": 1, "kind": "main",
             "note": "rec=seen"}])

    def test_a_client_that_leaves_before_the_response_headers_is_reported(self):
        up = fakes.start_upstream(self, fakes.LateUpstream)
        rt, port = fakes.start_gateway(self, up, settings=chain("rec"), middleware={"rec": RECORDER.format(name="rec")})
        s = socket.create_connection(("127.0.0.1", port), timeout=30)
        s.sendall(b"POST /v1/json HTTP/1.1\r\nhost: x\r\ncontent-length: 2\r\n\r\n{}")
        time.sleep(0.1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        self.assertTrue(jail.wait_for(lambda: any(c[0] == "on_response_end" for c in calls("rec")), 5))
        (end,) = [c[1] for c in calls("rec") if c[0] == "on_response_end"]
        self.assertEqual((end.status, end.completed, end.client_disconnected), (200, False, True))
        self.assertEqual(rows(rt, "method", "path", "status", "completed", "client_disconnected"), [
            {"method": "POST", "path": "/v1/json", "status": 200, "completed": 0, "client_disconnected": 1}])


ANSWER = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
ANSWERS = ('NAME = "answers"\ndef on_request(ctx, req):\n    req.answer(200, "text/event-stream", %r)\n' % ANSWER)


class AnswerExchangeTest(unittest.TestCase):
    """A hook's answer goes back through the one relay, and no request leaves the gateway on either route."""

    def setUp(self):
        Upstream.seen.clear()
        self.up = fakes.start_upstream(self)

    def test_an_answered_request_never_goes_upstream_and_relays_like_one(self):
        from gpt_gateway.capture import RESPONSE_BODY, SENT_BODY
        settings = dict(chain("rec", "answers"), capture_dir=jail.scratch(self))
        rt, port = fakes.start_gateway(self, self.up, settings=settings,
                                       middleware={"rec": RECORDER.format(name="rec"), "answers": ANSWERS})
        resp, data = fakes.request(port, "POST", "/v1/messages?beta=true", ODD, {"x-claude-code-agent-id": "a1"})
        self.assertEqual((resp.status, resp.getheader("content-type"), data), (200, "text/event-stream", ANSWER))
        self.assertEqual(Upstream.seen, [])
        recorded = ended("rec", 1)
        self.assertEqual([c[0] for c in recorded], ["on_request", "on_event", "on_response_end"])
        end = recorded[-1][1]
        self.assertEqual((end.status, end.completed, end.note), (200, True, " kind=sub rec=seen answered=answers"))
        self.assertEqual(rows(rt, "path", "status", "kind", "note", "answered_by"), [
            {"path": "/v1/messages?beta=true", "status": 200, "kind": "sub", "note": "rec=seen answered=answers",
             "answered_by": "answers"}])
        folder = recorded[0][1]["capture_dir"]
        with open(os.path.join(folder, RESPONSE_BODY), "rb") as f:
            self.assertEqual(f.read(), ANSWER)
        self.assertFalse(os.path.exists(os.path.join(folder, SENT_BODY)))

    def test_an_answer_on_the_chatgpt_route_never_reaches_the_route(self):
        import base64
        from gpt_gateway import clock
        home = jail.scratch(self)
        fake = type("FakeChatGPT", (fakes.ChatGPTUpstream,), {"seen": [], "script": [], "tokens": {}, "upgrades": [],
                                                              "refuse": [], "vanish": [], "hangup": [], "stall": []})
        origin = fakes.start_upstream(self, fake)
        claims = base64.urlsafe_b64encode(json.dumps({"exp": clock.now() + 3600}).encode()).decode().rstrip("=")
        auth = os.path.join(home, "auth.json")
        jail.write_json(auth, dict(access_token="fixture.%s.signature" % claims, refresh_token="refresh-invented",
                                   id_token="id-invented", account_id="account-invented", last_refresh=clock.now()))
        secret = jail.write_secret(os.path.join(home, "route-secret"), "a" * 64)
        env = jail.clean_env(home, GPT_GATEWAY_CHATGPT="1", GPT_GATEWAY_CHATGPT_AUTH_STORE="file",
                             GPT_GATEWAY_ROUTE_SECRET_PATH=secret,
                             GPT_GATEWAY_CHATGPT_TRANSPORT="http", GPT_GATEWAY_CHATGPT_AUTH_PATH=auth,
                             GPT_GATEWAY_CHATGPT_ORIGIN=origin, GPT_GATEWAY_CHATGPT_AUTH_ORIGIN=origin,
                             GPT_GATEWAY_CHATGPT_MODELS_PATH=os.path.join(home, "models.json"))
        rt, port = fakes.start_gateway(self, self.up, env=env, settings=chain("rec", "answers"),
                                       middleware={"rec": RECORDER.format(name="rec"), "answers": ANSWERS})
        body = json.dumps({"model": "gpt-test", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        resp, data = fakes.request(port, "POST", "/v1/messages", body.encode(),
                                   {"x-claude-code-agent-id": "agent-invented", "x-claude-code-session-id": "session-invented"})
        self.assertEqual((resp.status, data), (200, ANSWER))
        self.assertEqual(ended("rec", 1)[0][1]["route"], "chatgpt")
        self.assertEqual([x["path"] for x in fake.seen if "responses" in x["path"]], [])
        self.assertEqual(Upstream.seen, [])
        self.assertEqual(rows(rt, "route", "status", "kind", "note", "answered_by", "lane", "input_tokens"), [
            {"route": "chatgpt", "status": 200, "kind": "sub", "note": "rec=seen answered=answers",
             "answered_by": "answers", "lane": None, "input_tokens": None}])  # the route never ran


class RelayRouteTest(unittest.TestCase):
    def test_disconnect_drops_only_selected_upstream(self):
        from unittest.mock import Mock
        from gpt_gateway import clock
        from gpt_gateway.proxy import Handler
        from gpt_gateway.response import Response
        for failure in (BrokenPipeError(), RuntimeError("invented write failure")):
            for answered in (False, True):  # a hook's answer spends no connection: the relay gets None
                rt, client, cap, ctx = Mock(), Mock(), Mock(), Mock()
                rt.chain.event_sink.return_value = None
                handler = Handler.__new__(Handler)
                handler.command, handler.path = "POST", "/v1/messages"
                handler.send_response = Mock(side_effect=failure)
                handler.close_connection = False
                handler.relay(rt, ctx, Response.json(200, {"ok": True}), " kind=sub", "", cap,
                              clock.monotonic(), None if answered else client, {"request_id": 1, "route": "anthropic"})
                self.assertEqual(client.drop.call_count, 0 if answered else 1)
                rt.upstream.drop.assert_not_called()
                self.assertTrue(handler.close_connection)
                result = rt.chain.run_response_end.call_args.args[1]
                self.assertFalse(result.completed)
                self.assertEqual(result.client_disconnected, isinstance(failure, BrokenPipeError))

    def broken_relay(self, up, path):
        """Relay `up`'s answer to `path` to a client whose socket breaks on the first body write, then end the client's
        connection as Handler.finish does. Returns the upstream connection the answer rode and the handler."""
        from unittest.mock import Mock
        from gpt_gateway import clock
        from gpt_gateway.proxy import Handler
        rt = Mock()
        rt.chain.event_sink.return_value = None
        handler = Handler.__new__(Handler)
        handler.command, handler.path, handler.close_connection = "POST", path, False
        handler.send_response = handler.send_header = handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        resp = up.send("POST", path, b"{}", (("content-length", "2"),))
        conn = up.local.conn
        handler.relay(rt, Mock(), resp, "", "", Mock(), clock.monotonic(), up, {"request_id": 1, "route": "anthropic"})
        up.drop()
        return conn, handler

    def test_a_connection_whose_body_was_read_whole_stays_pooled_when_the_client_leaves(self):
        from gpt_gateway.upstream import Upstream as Pooled
        up = Pooled(fakes.start_upstream(self))
        conn, handler = self.broken_relay(up, "/v1/json")
        self.assertTrue(handler.close_connection)
        self.assertEqual(up.pool.idle, [conn])
        self.assertIsNotNone(conn.sock)
        up.send("POST", "/v1/json", b"{}", (("content-length", "2"),)).read()
        self.assertEqual((up.reuse(), up.local.conn is conn), ((True, False), True))

    def test_a_connection_whose_body_was_cut_short_is_spent_when_the_client_leaves(self):
        from gpt_gateway.upstream import Upstream as Pooled
        up = Pooled(fakes.start_upstream(self, fakes.SlowUpstream))
        conn, handler = self.broken_relay(up, "/v1/messages")
        self.assertTrue(handler.close_connection)
        self.assertEqual((up.pool.idle, conn.sock), ([], None))

    def test_a_streamed_reply_whose_client_left_keeps_what_the_upstream_already_reported(self):
        from unittest.mock import Mock
        from gpt_gateway import clock
        from gpt_gateway.chatgpt.response import Reader
        from gpt_gateway.chatgpt.stream import Translator
        from gpt_gateway.proxy import Handler
        from gpt_gateway.response import Response
        for sent, reported in (([fakes.responses_turn(), b""], True),  # the terminal event arrived before the client left
                               ([fakes.responses_event("response.output_text.delta", delta="x"), b""], False)):
            with self.subTest(reported=reported):
                upstream, client, rt, row = Mock(), Mock(), Mock(), {"request_id": 1, "route": "chatgpt"}
                upstream.will_close = False
                upstream.read1.side_effect = sent
                rt.chain.event_sink.return_value = None
                translator = Translator("gpt-test", {}, 10)
                reader = Reader(upstream, translator, client)
                reader.thread.join(5)  # everything the upstream said is queued before the client leaves
                reader.on_end = lambda: row.update(input_tokens=translator.input_tokens)
                handler = Handler.__new__(Handler)
                handler.command, handler.path, handler.close_connection = "POST", "/v1/messages", False
                handler.send_response = handler.send_header = handler.end_headers = Mock()
                handler.wfile = Mock()
                handler.wfile.write.side_effect = BrokenPipeError()
                handler.relay(rt, Mock(), Response(200, reader=reader), "", "", Mock(), clock.monotonic(), client, row)
                self.assertEqual(row.get("input_tokens", "unreported"), 100 if reported else "unreported")
                self.assertTrue(row["client_disconnected"])
                # a reply read to its end gives its connection back; any other is cancelled
                self.assertEqual((client.release.call_count, client.drop.call_count), (1, 0) if reported else (0, 1))


class ClaudeRouteTimingTest(unittest.TestCase):
    def test_a_streamed_reply_carries_ttfb_ms_and_upstream_ms(self):
        up = fakes.start_upstream(self, fakes.SlowUpstream)  # first event at once, the last about 1.5 s later
        rt, port = fakes.start_gateway(self, up)
        resp, data = fakes.request(port, "POST", "/v1/messages", b'{"model":"m","stream":true}')
        self.assertEqual(resp.status, 200)
        (row,) = fakes.recorded(rt)
        self.assertIsNotNone(row["ttfb_ms"])
        self.assertIsNotNone(row["upstream_ms"])
        self.assertLess(row["ttfb_ms"], 700)
        self.assertGreaterEqual(row["upstream_ms"], 1400)
        self.assertLessEqual(row["upstream_ms"], row["total_ms"])

    def test_a_json_reply_and_an_error_reply_carry_them_too(self):
        rt, port = fakes.start_gateway(self, fakes.start_upstream(self))
        fakes.request(port, "POST", "/v1/json", b'{"model":"m"}')
        fakes.request(port, "POST", "/v1/other", b'{"model":"m"}')  # the fake answers 429
        ok, limited = fakes.recorded(rt)
        self.assertEqual((ok["status"], limited["status"]), (200, 429))
        for row in (ok, limited):
            self.assertIsNotNone(row["ttfb_ms"], row["path"])
            self.assertIsNotNone(row["upstream_ms"], row["path"])
            self.assertLessEqual(row["ttfb_ms"], row["total_ms"])
            self.assertLessEqual(row["upstream_ms"], row["total_ms"])

    def test_a_request_that_reached_no_upstream_has_none(self):
        rt, port = fakes.start_gateway(self, "http://127.0.0.1:%d" % jail.free_port())
        self.assertEqual(fakes.request(port, "POST", "/v1/json", b'{"model":"m"}')[0].status, 502)
        (row,) = fakes.recorded(rt)
        self.assertEqual((row["ttfb_ms"], row["upstream_ms"]), (None, None))


class RouteGateTest(unittest.TestCase):
    """The proxy's gate on the ChatGPT route, through a served gateway with the route's fixtures."""
    setUp = route_tests.RouteTest.setUp
    jwt = route_tests.RouteTest.jwt
    gateway = route_tests.RouteTest.gateway
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses

    def test_gpt_route_requires_install_secret(self):
        _, port = self.gateway()
        unsigned = fakes.request(port, "POST", "/v1/messages", json.dumps(self.body).encode(), self.headers)
        self.assertEqual((unsigned[0].status, json.loads(unsigned[1])["error"]["message"],
                          unsigned[0].getheader("x-should-retry")),
                         (403, "chatgpt upstream: local client authentication required", "false"))
        anonymous = fakes.request(port, "POST", "/v1/messages", json.dumps(self.body).encode(), {})
        self.assertEqual(anonymous[0].status, 400)  # the sub-agent rule answers first
        self.assertEqual((len(self.responses()), self.send(port)[0].status), (0, 200))

    def test_an_image_turn_teaches_the_text_ratio_without_the_attachment_allowance(self):
        rt, port = self.gateway()
        image = dict(type="image", source=dict(type="base64", media_type="image/png", data="invented"))
        self.body["messages"].append(dict(role="user", content=[image]))
        self.assertEqual(self.send(port)[0].status, 200)
        self.assertEqual([r["input_tokens"] for r in fakes.gpt_rows(rt)], [100])  # the upstream's total, image included
        resp, data = self.send(port, path="/v1/messages/count_tokens")
        self.assertEqual((resp.status, json.loads(data)["input_tokens"]), (200, 2000))  # 100 total is under the allowance: no text ratio


if __name__ == "__main__":
    unittest.main()
