"""Tests for model_gateway.proxy: the passthrough end to end against a fake upstream, hooks included."""
import contextlib
import http.client
import io
import json
import socket
import struct
import time
import unittest

from tests import fakes, jail
from tests.fakes import ERROR_BODY, JSON_BODY, RECORDER, SSE_BODY, Upstream, calls

ODD = b'{"model":  "claude-x", "stream": true,\n "messages": [{"role": "user", "content": "hi"}]}'
EDIT = 'NAME = "edit"\ndef on_request(ctx, req):\n    req.body["added"] = 1\n    req.mark_changed()\n'
FAIL = 'NAME = "fail"\ndef on_request(ctx, req):\n    req.body["messages"] = []\n    req.mark_changed()\n    raise RuntimeError("x")\n'
NOISY = 'NAME = "noisy"\ndef on_event(ctx, e):\n    raise RuntimeError("every event")\n'


def chain(*modules):
    return {"middleware": [{"module": m} for m in modules]}


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
        logged = fakes.logged(rt)
        self.assertIn("POST /v1/messages?beta=true -> 200 kind=main\n", logged)
        self.assertNotIn("secret-token", logged)

    def test_chunked_request_bodies_are_read_whole(self):
        rt, port = fakes.start_gateway(self, self.up)
        resp, _ = fakes.request(port, "POST", "/v1/messages", ODD, {"content-type": "application/json"}, chunked=True)
        self.assertEqual((resp.status, Upstream.seen[-1]["body"]), (200, ODD))

    def test_upstream_errors_pass_through_with_their_status_and_are_logged(self):
        rt, port = fakes.start_gateway(self, self.up)
        resp, data = fakes.request(port, "GET", "/v1/models")
        self.assertEqual((resp.status, data), (429, ERROR_BODY))
        self.assertIn("GET /v1/models -> 429 kind=main ratelimit(retry-after=3 requests-remaining=0) body=" +
                      ERROR_BODY.decode(), fakes.logged(rt))

    def test_answers_502_when_upstream_is_unreachable(self):
        rt, port = fakes.start_gateway(self)
        resp, data = fakes.request(port, "POST", "/v1/messages", b"{}")
        body = json.loads(data)
        self.assertEqual((resp.status, body["type"]), (502, "error"))
        self.assertTrue(body["error"]["message"].startswith("model-gateway: upstream unreachable"))
        self.assertIn("POST /v1/messages -> 502 upstream unreachable", fakes.logged(rt))

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
            self.assertIn(b"model-gateway: unreadable request body", data)
        self.assertEqual(Upstream.seen, [])
        self.assertIn("POST /v1/messages -> 400 unreadable request body", fakes.logged(rt))

    def test_kind_from_the_agent_header(self):
        rt, port = fakes.start_gateway(self, self.up)
        fakes.request(port, "POST", "/v1/messages", ODD, {"x-claude-code-agent-id": "a1"})
        self.assertIn("-> 200 kind=sub\n", fakes.logged(rt))

    def test_debug_logs_headers_without_secrets(self):
        rt, port = fakes.start_gateway(self, self.up, env={"MODEL_GATEWAY_DEBUG": "1"})
        fakes.request(port, "POST", "/v1/messages", b'{"model":"m","metadata":{"user_id":"u"}}',
                      {"authorization": "Bearer secret-token", "x-claude-code-agent-id": "a1"})
        logged = fakes.logged(rt)
        self.assertIn('debug headers={"x-claude-code-agent-id": "a1"', logged)
        self.assertIn('metadata={"user_id": "u"}', logged)
        self.assertNotIn("secret-token", logged)

    def test_a_client_reset_writes_no_traceback(self):
        rt, port = fakes.start_gateway(self, self.up)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = socket.create_connection(("127.0.0.1", port))
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
        self.assertIn("-> 200 kind=sub rec=seen\n", fakes.logged(rt))

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
        logged = fakes.logged(rt)
        self.assertIn("middleware fail on_request failed: RuntimeError('x')", logged)
        self.assertIn("-> 200 kind=main\n", logged)

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
        self.assertEqual(fakes.logged(rt).count("middleware noisy on_event failed"), 1)

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
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"POST /v1/messages HTTP/1.1\r\nhost: x\r\ncontent-length: 2\r\n\r\n{}")
        s.recv(100)
        s.close()
        self.assertTrue(jail.wait_for(lambda: any(c[0] == "on_response_end" for c in calls("rec")), 5))
        (end,) = [c[1] for c in calls("rec") if c[0] == "on_response_end"]
        self.assertEqual((end.completed, end.client_disconnected), (False, True))
        self.assertIn("POST /v1/messages client disconnected mid-stream kind=main rec=seen", fakes.logged(rt))

    def test_a_client_that_leaves_before_the_response_headers_is_reported(self):
        up = fakes.start_upstream(self, fakes.LateUpstream)
        rt, port = fakes.start_gateway(self, up, settings=chain("rec"), middleware={"rec": RECORDER.format(name="rec")})
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"POST /v1/json HTTP/1.1\r\nhost: x\r\ncontent-length: 2\r\n\r\n{}")
        time.sleep(0.1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        self.assertTrue(jail.wait_for(lambda: any(c[0] == "on_response_end" for c in calls("rec")), 5),
                        fakes.logged(rt))
        (end,) = [c[1] for c in calls("rec") if c[0] == "on_response_end"]
        self.assertEqual((end.status, end.completed, end.client_disconnected), (200, False, True))
        self.assertIn("POST /v1/json client disconnected mid-stream kind=main rec=seen", fakes.logged(rt))


if __name__ == "__main__":
    unittest.main()
