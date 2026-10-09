"""One exchange: read the request (chunked included), run the hooks, forward it, relay the response.

The response streams back as it arrives: each upstream chunk is written and flushed before the next
is read, and the event hooks see it only after the client has. A request line is logged for
POST /v1/messages and for every status >= 400; on_response_end runs after it.
"""
import json
import copy
from http.server import BaseHTTPRequestHandler

from . import clock
from .capture import Capture
from .headers import HOP, Headers, debug_headers
from .health import HEALTH_PATH, health_line
from .hooks import Context, Request, Result, parse_object
from .upstream import UpstreamError

MESSAGES_PATH = "/v1/messages"
ERROR_HEAD = 600
CHUNK = 65536


def assemble_note(kind, notes):
    """The request's log note: its kind, then each on_request note verbatim, in list order."""
    return " kind=%s%s" % (kind, "".join(notes))


def request_meta(body):
    """The request's shape for an error line: max_tokens, message and tool counts, stream."""
    if body is None:
        return ""
    msgs, tools = body.get("messages") or [], body.get("tools") or []
    return " req(max_tokens=%s msgs=%d tools=%d stream=%s)" % (
        body.get("max_tokens"), len(msgs) if isinstance(msgs, list) else -1,
        len(tools) if isinstance(tools, list) else -1, body.get("stream"))


def logs_line(method, path, status):
    return (method == "POST" and path.split("?", 1)[0] == MESSAGES_PATH) or status >= 400


def request_line(method, path, status, note, meta, headers, error_head):
    """`METHOD path -> status note`, plus the request shape, rate limits and body head on an error."""
    line = "%s %s -> %d%s" % (method, path, status, note)
    if status >= 400:
        line += meta
        limits = " ".join("%s=%s" % (k.lower().replace("anthropic-ratelimit-", ""), v) for k, v in headers
                          if k.lower().startswith("anthropic-ratelimit-") or k.lower() == "retry-after")
        if limits:
            line += " ratelimit(" + limits + ")"
        line += " body=" + error_head[:300].decode("utf-8", "replace").replace("\n", " ")
    return line


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "model-gateway"

    def setup(self):
        super().setup()
        self.server.runtime.drain.connected(self.connection)

    def finish(self):
        # One thread serves one client connection; its upstream connection ends with it.
        rt = self.server.runtime
        rt.drain.disconnected(self.connection)
        try:
            super().finish()
        finally:
            rt.upstream.drop()
            if rt.chatgpt is not None:
                rt.chatgpt.client.drop()

    def log_message(self, *args):
        pass  # the gateway log records each exchange itself

    def read_body(self):
        """The request body; ValueError when its content-length or a chunk size is not a number >= 0."""
        if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
            parts = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size < 0:
                    raise ValueError("negative chunk size %d" % size)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    break
                parts.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(parts)
        n = int(self.headers.get("content-length") or 0)
        if n < 0:
            raise ValueError("negative content-length %d" % n)
        return self.rfile.read(n) if n else b""

    def reply(self, status, content_type, data):
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        rt = self.server.runtime
        snap = rt.settings.current()  # one settings snapshot per request
        if self.path == HEALTH_PATH:
            return self.reply(200, "text/plain", health_line(snap, rt.chain, rt.log, rt.chatgpt).encode())
        rt.drain.begin(self.connection)
        try:
            self.proxy(rt, snap)
        finally:
            rt.drain.end(self.connection)
            if rt.drain.draining:
                self.close_connection = True  # a draining gateway serves no further request on this connection

    def proxy(self, rt, snap):
        started = clock.monotonic()
        try:
            raw = self.read_body()
        except ValueError as e:
            rt.log.write("%s %s -> 400 unreadable request body: %s" % (self.command, self.path, e))
            self.close_connection = True  # the body's end is unknown, so nothing after it can be read
            return self.reply(400, "application/json", json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "model-gateway: unreadable request body: %s" % e}}).encode())
        body = parse_object(raw)
        selected = ("anthropic", None, None)
        if rt.chatgpt is not None:
            selected = rt.chatgpt.decide(self.command, self.path, body, self.headers)
        kind = "sub" if self.headers.get("x-claude-code-agent-id") else "main"
        seq = rt.sequence.next()
        capture_kind = "gpt" if selected[0] == "chatgpt" else kind
        cap = Capture(snap.capture_dir, seq, self.command, self.path, self.headers, capture_kind, rt.log.write)
        try:
            self.exchange(rt, snap, raw, body, selected, kind, seq, cap, started)
        finally:
            cap.close()

    def exchange(self, rt, snap, raw, body, selected, kind, seq, cap, started):
        cap.request_body(raw)
        meta = request_meta(body)
        ctx = Context(log=rt.log.write, log_path=rt.log.path, settings=snap.module_settings, kind=kind,
                      method=self.command, path=self.path, headers=Headers(self.headers.items()),
                      model=body.get("model") if body is not None else None, request_id=seq,
                      capture_dir=cap.dir, route=selected[0])
        if rt.env.debug and logs_line(self.command, self.path, 0):
            metadata = body.get("metadata") if body is not None else None
            rt.log.write("    debug headers=%s metadata=%s" % (json.dumps(debug_headers(self.headers.items())),
                                                              json.dumps(metadata)[:300]))
        req = Request(raw, copy.deepcopy(body) if selected[0] == "chatgpt" else body)
        sent, notes = rt.chain.run_request(ctx, req)
        if sent is not raw and selected[0] != "chatgpt":  # the chatgpt route captures the bytes it sends
            cap.sent_body(sent)
        note = assemble_note(kind, notes)
        if selected[0] == "chatgpt":
            resp = selected[2] or rt.chatgpt.exchange(req.body if sent is not raw else body,
                                                     selected[1], self.headers, self.path, cap, started)
            return self.relay(rt, ctx, resp, note, meta, cap, started, rt.chatgpt.client)
        headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP]  # a repeated header goes as sent
        headers.append(("accept-encoding", "identity"))
        if sent or self.command in ("POST", "PUT", "PATCH"):
            headers.append(("content-length", str(len(sent))))
        try:
            resp = rt.upstream.send(self.command, self.path, sent or None, headers)
        except UpstreamError as e:
            rt.log.write("%s %s -> 502 %s%s" % (self.command, self.path, e.message, note))
            cap.gateway_error(502, e.message)
            data = json.dumps({"type": "error", "error": {"type": "api_error",
                                                          "message": "model-gateway: " + e.message}}).encode()
            self.reply(502, "application/json", data)
            rt.chain.run_response_end(ctx, Result(502, "Bad Gateway", (), data[:ERROR_HEAD], True, False,
                                                  clock.monotonic() - started, note))
            return
        self.relay(rt, ctx, resp, note, meta, cap, started)

    def relay(self, rt, ctx, resp, note, meta, cap, started, upstream=None):
        upstream = rt.upstream if upstream is None else upstream
        resp_headers = tuple(resp.getheaders())
        cap.response(resp.status, resp.reason, resp_headers)
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        error_head, completed, disconnected, sink = b"", False, False, None
        try:  # the client may be gone before the headers reach it, so they are inside the try too
            self.send_response(resp.status)
            for k, v in resp_headers:
                if k.lower() not in HOP:
                    self.send_header(k, v)
            if no_body:
                self.send_header("content-length", "0")
                self.end_headers()
                resp.read()
            else:
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                sink = rt.chain.event_sink(ctx, resp.getheader("content-type"))
                while True:
                    chunk = resp.read1(CHUNK)
                    if not chunk:
                        break
                    cap.chunk(chunk)
                    if resp.status >= 400 and len(error_head) < ERROR_HEAD:
                        error_head += chunk[:ERROR_HEAD - len(error_head)]
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    if sink is not None:
                        sink.feed(chunk)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            completed = True
        except (BrokenPipeError, ConnectionResetError):
            upstream.drop()  # the client left mid-stream; this upstream connection is spent
            disconnected = True
            rt.log.write("%s %s client disconnected mid-stream%s" % (self.command, self.path, note))
            self.close_connection = True
        except Exception as e:
            upstream.drop()
            rt.log.write("%s %s stream error %r%s" % (self.command, self.path, e, note))
            self.close_connection = True
        if sink is not None:
            sink.close(completed)
        if completed and logs_line(self.command, self.path, resp.status):
            rt.log.write(request_line(self.command, self.path, resp.status, note, meta, resp_headers, error_head))
        rt.chain.run_response_end(ctx, Result(resp.status, resp.reason, resp_headers,
                                              error_head if resp.status >= 400 else b"", completed, disconnected,
                                              clock.monotonic() - started, note))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any
