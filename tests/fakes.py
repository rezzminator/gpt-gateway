"""Fake upstreams, recording middleware, and an in-process gateway serving against them."""
import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from model_gateway import frames
from model_gateway.runtime import Runtime
from model_gateway.server import Server
from tests import jail

SSE_EVENTS = [
    b'event: message_start\ndata: {"type":"message_start","message":{"model":"m"}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]
SSE_BODY = b"".join(SSE_EVENTS)
JSON_BODY = b'{"id":"msg_1","type":"message","usage":{"input_tokens":3}}'
ERROR_BODY = b'{"type":"error","error":{"type":"rate_limit_error","message":"Error"}}'


class Upstream(BaseHTTPRequestHandler):
    """POST /v1/messages: SSE, one event per chunk (`?split`: the same bytes cut every 7 bytes);
    POST /v1/json: a JSON body; anything else: a 429 JSON error with rate-limit headers."""

    protocol_version = "HTTP/1.1"
    seen = []

    def log_message(self, *a):
        pass

    def chunked(self, status, ctype, chunks):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("request-id", "req_test")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for c in chunks:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"0\r\n\r\n")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        Upstream.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                              "items": list(self.headers.items()), "body": body})
        path = self.path.split("?", 1)[0]
        if path == "/v1/messages":
            split = "split" in self.path
            self.chunked(200, "text/event-stream",
                         [SSE_BODY[i:i + 7] for i in range(0, len(SSE_BODY), 7)] if split else SSE_EVENTS)
        elif path == "/v1/json":
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(JSON_BODY)))
            self.end_headers()
            self.wfile.write(JSON_BODY)
        else:
            self.send_response(429)
            self.send_header("content-type", "application/json")
            self.send_header("retry-after", "3")
            self.send_header("anthropic-ratelimit-requests-remaining", "0")
            self.send_header("content-length", str(len(ERROR_BODY)))
            self.end_headers()
            self.wfile.write(ERROR_BODY)

    do_GET = do_POST


class SlowUpstream(BaseHTTPRequestHandler):
    """Streams a messages response over about 1.5 seconds."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        try:
            for i in range(6):
                e = b"event: ping\ndata: {\"i\":%d}\n\n" % i
                self.wfile.write(b"%x\r\n%s\r\n" % (len(e), e))
                self.wfile.flush()
                time.sleep(0.25)
            e = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(e), e))
        except (BrokenPipeError, ConnectionResetError):
            pass  # the drain-limit test cuts its client on purpose


class LateUpstream(BaseHTTPRequestHandler):
    """Answers a JSON body after half a second, so a client can leave before the response headers arrive."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        time.sleep(0.5)
        try:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(JSON_BODY)))
            self.end_headers()
            self.wfile.write(JSON_BODY)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the gateway dropped this upstream connection once its client left


class ClosingUpstream(BaseHTTPRequestHandler):
    """Answers as if keeping the connection alive, then closes it: the next request on it meets a dead socket."""

    protocol_version = "HTTP/1.1"
    count = [0]

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        ClosingUpstream.count[0] += 1
        data = b'{"n":%d}' % ClosingUpstream.count[0]
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()
        self.close_connection = True


RECORDER = '''
NAME = "{name}"
CALLS = []

def on_request(ctx, req):
    CALLS.append(("on_request", dict(kind=ctx.kind, model=ctx.model, headers=dict(ctx.headers), route=ctx.route,
                  request_id=ctx.request_id, capture_dir=ctx.capture_dir, method=ctx.method, path=ctx.path,
                  settings=dict(ctx.settings), log_path=ctx.log_path)))
    return " {name}=seen"

def on_event(ctx, event):
    CALLS.append(("on_event", event))

def on_response_end(ctx, result):
    CALLS.append(("on_response_end", result))
'''


def calls(module):
    """The CALLS list of the loaded recording middleware `module`."""
    return sys.modules["model_gateway_mw_" + module].CALLS


class QuietServer(ThreadingHTTPServer):
    """A fake upstream's server; a client that resets its connection is expected, never a traceback."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            super().handle_error(request, client_address)


def start_upstream(test, handler=Upstream):
    server = QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return "http://127.0.0.1:%d" % server.server_address[1]


def start_gateway(test, upstream=None, env=None, settings=None, middleware=None):
    """(Runtime, port): a gateway loaded from `settings` (None: no settings.json) and `middleware`
    ({module: source} written to its middleware dir), serving in this process."""
    d = jail.scratch(test)
    path = os.path.join(d, "settings.json")
    if settings is not None:
        jail.write_json(path, settings)
    for module, source in (middleware or {}).items():
        jail.write_middleware(os.path.join(d, "middleware"), module, source)
    base = {"MODEL_GATEWAY_LOG": os.path.join(d, "gw.log"),
            "MODEL_GATEWAY_UPSTREAM": upstream or "http://127.0.0.1:%d" % jail.free_port()}
    rt = Runtime(dict(base, **(env or {})), path)
    rt.load()
    server = Server(("127.0.0.1", 0), rt)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return rt, server.server_address[1]


def request(port, method, path, body=None, headers=None, chunked=False):
    """(response, body bytes) of one request to the gateway on `port`."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    if chunked:
        conn.putrequest(method, path)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.putheader("transfer-encoding", "chunked")
        conn.endheaders()
        half = len(body) // 2
        for part in (body[:half], body[half:]):
            conn.send(b"%x\r\n%s\r\n" % (len(part), part))
        conn.send(b"0\r\n\r\n")
    else:
        conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def logged(rt, settle=0.1):
    time.sleep(settle)
    if not os.path.exists(rt.log.path):
        return ""
    with open(rt.log.path) as f:
        return f.read()


CHATGPT_MODELS = {"models": [{"slug": "gpt-test", "supported_reasoning_levels": [],
                             "use_responses_lite": True}]}


def responses_event(kind, **fields):
    return ("event: " + kind + "\ndata: " + json.dumps(dict(type=kind, **fields)) + "\n\n").encode()


class Cut:
    """A script payload: chunked headers, `sent` as one chunk when given, then EOF before the next chunk."""

    def __init__(self, sent=b""):
        self.sent = sent


CLOSE_AFTER_HEADERS = Cut()  # the wire shape http.client reports as IncompleteRead(0 bytes read)


def responses_turn(tool=False, reasoning=None):
    frames = [responses_event("response.created"),
              responses_event("response.output_item.done", output_index=0,
                              item=dict(type="reasoning", id="rs_invented", encrypted_content="opaque-invented"))]
    if tool:
        frames.append(responses_event("response.output_item.done", output_index=1,
                                      item=dict(type="function_call", call_id="call_invented", name="lookup",
                                                arguments='{"city":"Harbor"}')))
    else:
        frames.extend([responses_event("response.output_text.delta", output_index=1, delta="Hello"),
                       responses_event("response.output_item.done", output_index=1, item=dict(type="message"))])
    usage = {"input_tokens": 100, "output_tokens": 8, "input_tokens_details": {"cached_tokens": 60}}
    if reasoning is not None:
        usage["output_tokens_details"] = {"reasoning_tokens": reasoning}
    frames.append(responses_event("response.completed", response={"usage": usage}))
    return b"".join(frames)


RFC6455_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 § 1.3, independent of the code under test


def sse_data(stream):
    """The JSON texts of an SSE stream's data lines, in order."""
    return [line[6:] for block in stream.split(b"\n\n") for line in block.split(b"\n") if line.startswith(b"data: ")]


class ChatGPTUpstream(BaseHTTPRequestHandler):
    """Invented catalog, scripted Responses and OAuth; each test supplies an isolated subclass.

    A GET upgrading /backend-api/codex/responses to a WebSocket serves the same script, one response.create message
    per entry: a status other than 200 as a wrapped error event, a Cut as its events and then a hang-up, an issued
    turn-state token as a response.metadata event first. `seen` records each message with its upgrade's headers."""

    protocol_version = "HTTP/1.1"
    seen = []
    script = []
    models_status = 200
    models_body = CHATGPT_MODELS
    tokens = {}
    issued = []  # per Responses request, the x-codex-turn-state its response sends, or None
    upgrades = []  # per WebSocket upgrade, its request headers
    refuse = []  # per WebSocket upgrade, a status answered in place of 101
    vanish = []  # per WebSocket message, True: the socket closes without an answer
    hangup = []  # per WebSocket message answered, True: the socket closes after the answer
    stall = []  # per WebSocket message, seconds the answer waits

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        self.seen.append({"method": self.command, "path": self.path,
                          "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        extra = []
        if self.path.startswith("/backend-api/codex/models?"):
            status, payload = self.models_status, self.models_body
        elif self.path == "/oauth/token":
            status, payload = 200, self.tokens
        elif self.path == "/backend-api/codex/responses":  # a script entry may add a third item: its extra headers
            status, payload, *extra = self.script.pop(0) if self.script else (200, responses_turn())
        else:
            status, payload = 404, {"error": "unknown fake path"}
        if isinstance(payload, Cut):
            self.send_response(status)
            self.send_header("content-type", "text/event-stream")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            if payload.sent:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(payload.sent), payload.sent))
            self.wfile.flush()
            self.close_connection = True
            return
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        token = self.issued.pop(0) if self.path == "/backend-api/codex/responses" and self.issued else None
        if token:
            self.send_header("x-codex-turn-state", token)
        for name, value in (extra[0] if extra else {}).items():
            self.send_header(name, value)
        self.send_header("content-type", "text/event-stream" if isinstance(payload, bytes) else "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if (self.headers.get("upgrade") or "").lower() == "websocket":
            return self.websocket()
        return self.do_POST()

    def websocket(self):
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.upgrades.append(headers)
        self.close_connection = True
        if self.refuse:
            data = b'{"detail":"upgrade refused"}'
            self.send_response(self.refuse.pop(0))
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        accept = base64.b64encode(hashlib.sha1(headers["sec-websocket-key"].encode() + RFC6455_GUID).digest())
        self.send_response(101)
        for name, value in (("Upgrade", "websocket"), ("Connection", "Upgrade"), ("Sec-WebSocket-Accept", accept.decode())):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.flush()
        while True:
            data = self.ws_receive()
            if data is None:
                return
            self.seen.append({"method": "WS", "path": self.path, "headers": headers, "body": data})
            if self.stall:
                time.sleep(self.stall.pop(0))
            if self.vanish and self.vanish.pop(0):
                return self.connection.shutdown(socket.SHUT_RDWR)
            status, payload, *extra = self.script.pop(0) if self.script else (200, responses_turn())
            if isinstance(payload, Cut):
                for text in sse_data(payload.sent):
                    self.ws_send(text)
                return self.connection.shutdown(socket.SHUT_RDWR)
            if status != 200:
                event = dict(payload if isinstance(payload, dict) else {"detail": str(payload)}, type="error", status=status)
                if extra:
                    event["headers"] = extra[0]
                self.ws_send(json.dumps(event).encode())
                continue
            token = self.issued.pop(0) if self.issued else None
            if token:
                self.ws_send(json.dumps({"type": "response.metadata", "headers": {"x-codex-turn-state": token}}).encode())
            for text in sse_data(payload):
                self.ws_send(text)
            if self.hangup and self.hangup.pop(0):
                return self.connection.shutdown(socket.SHUT_RDWR)

    def ws_receive(self):
        """The next whole message a client sent masked, or None once it closed, hung up or sent an unmasked frame."""
        parts = []
        while True:
            head = self.rfile.read(2)
            if len(head) < 2 or not head[1] & 0x80:
                return None
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self.rfile.read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.rfile.read(8))[0]
            key = self.rfile.read(4)
            payload = frames.mask(self.rfile.read(length), key)
            opcode = head[0] & 0x0F
            if opcode == frames.CLOSE:
                return None
            if opcode in (frames.PING, frames.PONG):
                continue
            parts.append(payload)
            if head[0] & 0x80:
                return b"".join(parts)

    def ws_send(self, data, opcode=frames.TEXT):
        n = len(data)
        size = bytes([n]) if n < 126 else (bytes([126]) + struct.pack("!H", n) if n < 65536 else bytes([127]) + struct.pack("!Q", n))
        self.wfile.write(bytes([0x80 | opcode]) + size + data)
        self.wfile.flush()
