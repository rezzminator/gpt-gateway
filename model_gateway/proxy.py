"""One exchange: read the request (chunked included), run the hooks, forward it, relay the response.

The response streams back as it arrives: each upstream chunk is written and flushed before the next
is read, and the event hooks and the usage meter see it only after the client has. A request a hook
answered goes to no upstream: its answer takes the same relay. Every request but the health check
becomes one `requests` row in the database, its usage read off the relay; on_response_end runs after it.
"""
import json
import copy
from http.server import BaseHTTPRequestHandler

from . import clock
from .capture import Capture
from .headers import HOP, Headers, debug_headers
from .health import HEALTH_PATH, health_line
from .hooks import Context, Request, Result, parse_object
from .response import Response
from .upstream import UpstreamError
from .usage import Meter

MESSAGES_PATH = "/v1/messages"
ERROR_HEAD = 600
CHUNK = 65536


def assemble_note(kind, notes):
    """The request's note as Result.note carries it: its kind, then each on_request note verbatim, in list order."""
    return " kind=%s%s" % (kind, "".join(notes))


def session_id(body):
    """The session id in the body's `metadata.user_id`: a JSON object's `session_id`, or the end of a
    `user_{hash}_account_{uuid}_session_{uuid}` string; None for any other form."""
    metadata = body.get("metadata") if isinstance(body, dict) else None
    user = metadata.get("user_id") if isinstance(metadata, dict) else None
    if not isinstance(user, str):
        return None
    if user.startswith("{"):
        try:
            user = json.loads(user)
        except ValueError:
            return None
        found = user.get("session_id") if isinstance(user, dict) else None
        return found if isinstance(found, str) and found else None
    _, sep, tail = user.rpartition("_session_")
    return tail if sep and tail else None


def request_row(method, path, body, headers, kind, route, request_id):
    """The request's share of its row: what it is and who sent it, never its body."""
    model = body.get("model") if body is not None else None
    return {"request_id": request_id, "route": route, "kind": kind, "method": method, "path": path,
            "model": model if isinstance(model, str) else None, "session_id": session_id(body),
            "agent_id": headers.get("x-claude-code-agent-id"),
            "stream": body.get("stream") is True if body is not None else None}


def note_row(row, notes, answered):
    """The notes' share of the row: the middleware notes, and the NAME of the module that answered."""
    row["note"] = "".join(notes).strip() or None
    if answered:
        row["answered_by"] = notes[-1][len(" answered="):]  # run_request closes the notes with it


def relay_row(row, status, completed, disconnected, failure, error_head, meter):
    """The relay's share of the row: the status and how the relay ended, then the meter's error code and, on the
    Anthropic route, its usage; the ChatGPT route's usage is the backend's, which the route wrote."""
    row.update(status=status, completed=completed, client_disconnected=disconnected)
    if status >= 400:
        row["error_head"] = error_head
    if failure is not None:
        row.update(error_code="stream_error", error_head=failure)
    for name, value in meter.fields().items():
        if row.get("route") != "chatgpt" or name in ("error_code", "error_head"):
            row.setdefault(name, value)
    return row


def metered(method, path, status):
    """Whether a response's usage and error code are read: a Messages call's, or an error reply's."""
    return status >= 400 or (method == "POST" and path.split("?", 1)[0] == MESSAGES_PATH)


def error_detail(body, headers):
    """An error reply's context, never its body: the request's shape and the rate-limit headers."""
    detail = {}
    if body is not None:
        msgs, tools = body.get("messages") or [], body.get("tools") or []
        detail.update(max_tokens=body.get("max_tokens"), messages=len(msgs) if isinstance(msgs, list) else -1,
                      tools=len(tools) if isinstance(tools, list) else -1)
    limits = {k.lower().replace("anthropic-ratelimit-", ""): v for k, v in headers
              if k.lower().startswith("anthropic-ratelimit-") or k.lower() == "retry-after"}
    if limits:
        detail["ratelimit"] = limits
    return detail


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
        pass  # the database records each exchange itself

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
            return self.reply(200, "text/plain", health_line(snap, rt).encode())
        rt.drain.begin(self.connection)
        try:
            self.proxy(rt, snap)
        finally:
            rt.drain.end(self.connection)
            if rt.drain.draining:
                self.close_connection = True  # a draining gateway serves no further request on this connection

    def record(self, rt, row, started, **fields):
        """Queue the request's row, the relay's end its end."""
        row.update(fields, ended=clock.now())
        row.setdefault("total_ms", clock.milliseconds(clock.monotonic() - started))
        rt.db.request(row)

    def proxy(self, rt, snap):
        started, row = clock.monotonic(), {"started": clock.now(), "method": self.command, "path": self.path}
        try:
            raw = self.read_body()
        except ValueError as e:
            self.close_connection = True  # the body's end is unknown, so nothing after it can be read
            data = json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "model-gateway: unreadable request body: %s" % e}}).encode()
            self.reply(400, "application/json", data)
            return self.record(rt, dict(row, route="anthropic"), started, status=400, completed=True,
                               error_code="invalid_request_error", error_head=data)
        body = parse_object(raw)
        selected = ("anthropic", None, None)
        if rt.chatgpt is not None:
            selected = rt.chatgpt.decide(self.command, self.path, body, self.headers)
        kind = "sub" if self.headers.get("x-claude-code-agent-id") else "main"
        seq = rt.sequence.next()
        row.update(request_row(self.command, self.path, body, self.headers, kind, selected[0], seq))
        capture_kind = "gpt" if selected[0] == "chatgpt" else kind
        cap = Capture(snap.capture_dir, seq, self.command, self.path, self.headers, capture_kind, rt.db.event)
        try:
            self.exchange(rt, snap, raw, body, selected, kind, seq, cap, started, row)
        except Exception as e:
            if "ended" not in row:  # the exchange broke before its row was queued: the request still gets one
                self.record(rt, row, started, error_code="gateway_error", error_head=repr(e))
            raise
        finally:
            cap.close()

    def exchange(self, rt, snap, raw, body, selected, kind, seq, cap, started, row):
        cap.request_body(raw)
        ctx = Context(log=lambda line: rt.db.event("middleware_log", request_id=seq, line=line), log_path=rt.log.path,
                      settings=snap.module_settings, kind=kind, method=self.command, path=self.path,
                      headers=Headers(self.headers.items()), model=body.get("model") if body is not None else None,
                      request_id=seq, capture_dir=cap.dir, route=selected[0])
        if rt.env.debug and self.command == "POST" and self.path.split("?", 1)[0] == MESSAGES_PATH:
            rt.db.event("debug", request_id=seq, headers=debug_headers(self.headers.items()),
                        metadata=body.get("metadata") if body is not None else None)
        req = Request(raw, copy.deepcopy(body) if selected[0] == "chatgpt" else body)
        sent, notes = rt.chain.run_request(ctx, req)
        if sent is not raw and selected[0] != "chatgpt":  # the chatgpt route captures the bytes it sends
            cap.sent_body(sent)
        note = assemble_note(kind, notes)
        note_row(row, notes, req.answered is not None)
        if req.answered is not None:  # no upstream on either route, so no upstream connection to drop
            answer = req.answered
            return self.relay(rt, ctx, Response(answer.status, answer.data, {"content-type": answer.content_type}),
                              note, body, cap, started, None, row)
        if selected[0] == "chatgpt":
            resp = selected[2] or rt.chatgpt.exchange(req.body if sent is not raw else body,
                                                     selected[1], self.headers, self.path, cap, started, row)
            return self.relay(rt, ctx, resp, note, body, cap, started, rt.chatgpt.client, row)
        headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP]  # a repeated header goes as sent
        headers.append(("accept-encoding", "identity"))
        if sent or self.command in ("POST", "PUT", "PATCH"):
            headers.append(("content-length", str(len(sent))))
        try:
            resp = rt.upstream.send(self.command, self.path, sent or None, headers)
        except UpstreamError as e:
            cap.gateway_error(502, e.message)
            data = json.dumps({"type": "error", "error": {"type": "api_error",
                                                          "message": "model-gateway: " + e.message}}).encode()
            self.reply(502, "application/json", data)
            self.record(rt, row, started, status=502, completed=True, error_code="api_error", error_head=data)
            rt.chain.run_response_end(ctx, Result(502, "Bad Gateway", (), data[:ERROR_HEAD], True, False,
                                                  clock.monotonic() - started, note))
            return
        self.relay(rt, ctx, resp, note, body, cap, started, rt.upstream, row)

    def relay(self, rt, ctx, resp, note, body, cap, started, upstream, row):
        """Relay `resp` to the client and record its row; `upstream` is the connection a broken relay spends,
        None for a hook's answer."""
        resp_headers = tuple(resp.getheaders())
        cap.response(resp.status, resp.reason, resp_headers)
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        error_head, completed, disconnected, sink, failure = b"", False, False, None, None
        meter = Meter(resp.getheader("content-type") if metered(self.command, self.path, resp.status) else None)
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
                    meter.feed(chunk)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            completed = True
        except (BrokenPipeError, ConnectionResetError):
            if upstream is not None:
                upstream.drop()  # the client left mid-stream; this upstream connection is spent
            disconnected = True
            self.close_connection = True
        except Exception as e:
            if upstream is not None:
                upstream.drop()
            failure = repr(e)
            self.close_connection = True
        if sink is not None:
            sink.close(completed)
        meter.finish()
        self.finish_row(rt, row, resp, completed, disconnected, failure, error_head, meter, body, resp_headers)
        self.record(rt, row, started)
        rt.chain.run_response_end(ctx, Result(resp.status, resp.reason, resp_headers,
                                              error_head if resp.status >= 400 else b"", completed, disconnected,
                                              clock.monotonic() - started, note))

    def finish_row(self, rt, row, resp, completed, disconnected, failure, error_head, meter, body, resp_headers):
        """The relay's share of the row; an error reply's context and an unreadable usage as events."""
        relay_row(row, resp.status, completed, disconnected, failure, error_head, meter)
        if resp.status >= 400:
            rt.db.event("error_detail", request_id=row["request_id"], **error_detail(body, resp_headers))
        if (completed and resp.status == 200 and meter.unread and row["route"] == "anthropic" and self.command == "POST"
                and self.path.split("?", 1)[0] == MESSAGES_PATH):
            rt.db.event("usage_unread", request_id=row["request_id"], reason=meter.unread)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any
