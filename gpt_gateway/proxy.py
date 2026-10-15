"""One exchange: read the request (chunked included), run the hooks, forward it, relay the response.

The response streams back as it arrives: each upstream chunk is written and flushed before the next
is read, and the event hooks and the usage meter see it only after the client has. A request a hook
answered goes to no upstream: its answer takes the same relay. Every request but the health check
becomes one `requests` row in the database, its usage read off the relay; on_response_end runs after it. An Anthropic
row carries its transport (reused, stale, attempts) and a failure's class; a failed read of the upstream's body is the
upstream's failure, a failed write to the client the client leaving. Every request, the health check included, first
looks for a host pause (`gpt_gateway/reach.py`): one closes the idle upstream connections, which did not survive it.
"""
import json
import copy
import hmac
from http.server import BaseHTTPRequestHandler

from . import clock
from .capture import Capture
from .headers import HOP, Headers, debug_headers
from .health import HEALTH_PATH, health_line
from .hooks import Context, Request, Result, parse_object
from .reach import error_class
from .response import Response
from .upstream import UpstreamError
from .usage import Meter

MESSAGES_PATH = "/v1/messages"
# A client connection that sends nothing for this long is closed. Twice the 5-minute prompt-cache TTL: a chat that
# is still inside its cache window, and a pause as long again, keep their connection; a closed one is reopened by the
# client, and a request that crosses the close is a connection failure Claude Code's SDK retries.
CLIENT_IDLE_SECONDS = 600
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
    """An error reply's context, never its body: the request's shape, the upstream's request id and the rate-limit
    headers; empty when there is none of them."""
    detail = {}
    request_id = next((v for k, v in headers if k.lower() == "request-id"), None)
    if request_id:
        detail["upstream_request_id"] = request_id
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
    server_version = "gpt-gateway"

    def setup(self):
        super().setup()
        self.server.runtime.drain.connected(self.connection)

    def finish(self):
        # One thread serves one client connection; an upstream connection it still leases, a response never finished, ends with it.
        rt = self.server.runtime
        rt.drain.disconnected(self.connection)
        try:
            super().finish()
        finally:
            rt.upstream.drop()
            if rt.chatgpt is not None:
                rt.chatgpt.client.drop()

    def handle_one_request(self):
        # Waiting for the next request is the only wait this bounds; stdlib answers the timeout by closing the connection.
        self.connection.settimeout(CLIENT_IDLE_SECONDS)
        drain = self.server.runtime.drain
        if drain.preserve_pending and self.connection in drain.pending:
            if not drain.readable(self.connection, CLIENT_IDLE_SECONDS):
                self.close_connection = True
                return
            drain.receiving(self.connection)  # remember data before readline can consume a partial line
        super().handle_one_request()

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
        self.connection.settimeout(None)  # the request is in; the idle limit does not cut its body, hooks or stream
        # Claude Code appends its API path to the credential-bearing base URL. The prefix is removed before anything
        # else sees the path, so Anthropic still gets its original path and body bytes and no row holds the credential.
        authorized = False
        if rt.route_secret:
            # The first segment ends at "/", "?" or the end, so "/<secret>" and "/<secret>?q" are stripped too.
            end = min([i for i in (self.path.find("/", 1), self.path.find("?", 1)) if i != -1] or [len(self.path)])
            authorized = self.path.startswith("/") and hmac.compare_digest(  # latin-1 request text; compare as bytes
                self.path[1:end].encode("latin-1"), rt.route_secret.encode("ascii"))
            if authorized:
                rest = self.path[end:]
                self.path = rest if rest.startswith("/") else "/" + rest
        snap = rt.settings.current()  # one settings snapshot per request
        if rt.pause.check():
            rt.upstream.pool.clear()  # idle connections from before the pause are dead, or die on their next use
        if self.path == HEALTH_PATH:
            if not rt.drain.preserve_pending:
                return self.reply(200, "text/plain", health_line(snap, rt).encode())
            rt.drain.begin(self.connection)
            try:
                return self.reply(200, "text/plain", health_line(snap, rt).encode())
            finally:
                rt.drain.end(self.connection)
                if rt.drain.draining:
                    self.close_connection = True
        rt.drain.begin(self.connection)
        try:
            self.proxy(rt, snap, authorized)
        finally:
            rt.drain.end(self.connection)
            if rt.drain.draining:
                self.close_connection = True  # a draining gateway serves no further request on this connection

    def record(self, rt, row, started, **fields):
        """Queue the request's row, the relay's end its end."""
        row.update(fields, ended=clock.now())
        row.setdefault("total_ms", clock.milliseconds(clock.monotonic() - started))
        rt.db.request(row)

    def proxy(self, rt, snap, authorized=False):
        started, row = clock.monotonic(), {"started": clock.now(), "method": self.command, "path": self.path}
        try:
            raw = self.read_body()
        except ValueError as e:
            self.close_connection = True  # the body's end is unknown, so nothing after it can be read
            data = json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "gpt-gateway: unreadable request body: %s" % e}}).encode()
            self.reply(400, "application/json", data)
            return self.record(rt, dict(row, route="anthropic"), started, status=400, completed=True,
                               error_code="invalid_request_error", error_head=data)
        row["request_bytes"] = len(raw)
        body = parse_object(raw)
        selected = ("anthropic", None, None)
        if rt.chatgpt is not None:
            selected = rt.chatgpt.decide(self.command, self.path, body, self.headers, authorized)
        kind = "sub" if self.headers.get("x-claude-code-agent-id") else "main"
        seq = rt.sequence.next()
        row.update(request_row(self.command, self.path, body, self.headers, kind, selected[0], seq))
        capture_kind = "gpt" if selected[0] == "chatgpt" else kind
        cap = Capture(snap.capture_dir, seq, self.command, self.path, self.headers, capture_kind, rt.db.event)
        try:
            self.exchange(rt, snap, raw, body, selected, kind, seq, cap, started, row)
        except Exception as e:
            rt.log.record("error", "gateway-error", "gateway request failed: %r" % e,
                          "gpt_gateway/proxy.py#proxy", {"error": repr(e)})
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
                                                     selected[1], self.headers, self.path, cap, started, row,
                                                     snap)
            return self.relay(rt, ctx, resp, note, body, cap, started, rt.chatgpt.client, row)
        headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP]  # a repeated header goes as sent
        headers.append(("accept-encoding", "identity"))
        if sent or self.command in ("POST", "PUT", "PATCH"):
            headers.append(("content-length", str(len(sent))))
        sent_at = clock.monotonic()
        try:
            resp = rt.upstream.send(self.command, self.path, sent or None, headers, request_id=seq)
        except UpstreamError as e:
            rt.log.record("error", "upstream-failed", "upstream request failed: %s" % e.message,
                          "gpt_gateway/proxy.py#exchange", {"error": e.message, "error_class": e.error_class})
            row.update(zip(("reused", "stale"), rt.upstream.reuse()), attempts=rt.upstream.attempts())
            cap.gateway_error(502, e.message)
            data = json.dumps({"type": "error", "error": {"type": "api_error",
                                                          "message": "gpt-gateway: " + e.message}}).encode()
            self.reply(502, "application/json", data)
            self.record(rt, row, started, status=502, completed=True, error_code="api_error", error_head=data,
                        error_class=e.error_class)
            rt.chain.run_response_end(ctx, Result(502, "Bad Gateway", (), data[:ERROR_HEAD], True, False,
                                                  clock.monotonic() - started, note))
            return
        row.update(zip(("reused", "stale"), rt.upstream.reuse()), attempts=rt.upstream.attempts())
        self.relay(rt, ctx, resp, note, body, cap, started, rt.upstream, row, sent_at)

    def spend(self, upstream, resp):
        """The relay broke: a response read to its end leaves its connection in the pool, any other spends it, and the
        reader of a streamed ChatGPT body records what the upstream had reported. `upstream`: None for a hook's answer."""
        if upstream is not None:
            (upstream.release if resp.complete() else upstream.drop)()
        if hasattr(resp, "abandon"):
            resp.abandon()

    def relay(self, rt, ctx, resp, note, body, cap, started, upstream, row, sent_at=None):
        """Relay `resp` to the client and record its row; `upstream` is the connection a broken relay spends,
        None for a hook's answer; `sent_at` is when the Anthropic route sent the request, for the row's timings."""
        first = last = None
        resp_headers = tuple(resp.getheaders())
        cap.response(resp.status, resp.reason, resp_headers)
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        error_head, completed, disconnected, sink, failure, reading = b"", False, False, None, None, False
        meter = Meter(resp.getheader("content-type") if metered(self.command, self.path, resp.status) else None)
        try:  # the client may be gone before the headers reach it, so they are inside the try too
            self.send_response(resp.status)
            for k, v in resp_headers:
                if k.lower() not in HOP:
                    self.send_header(k, v)
            if no_body:
                self.send_header("content-length", "0")
                self.end_headers()
                reading = True
                resp.read()
                reading = False
                first = last = clock.monotonic()
            else:
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                sink = rt.chain.event_sink(ctx, resp.getheader("content-type"))
                while True:
                    reading = True
                    chunk = resp.read1(CHUNK)
                    reading = False
                    if not chunk:
                        last = clock.monotonic()
                        first = last if first is None else first
                        break
                    if first is None:
                        first = clock.monotonic()
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
        except Exception as e:
            self.spend(upstream, resp)
            self.close_connection = True
            if not reading and isinstance(e, (BrokenPipeError, ConnectionResetError)):
                disconnected = True  # the client left mid-stream
            else:
                failure = repr(e)
                if reading:  # the upstream's body broke off
                    row["error_class"] = getattr(e, "error_class", None) or error_class(e)
        if sink is not None:
            sink.close(completed)
        meter.finish()
        if sent_at is not None and first is not None:  # the upstream's own answer, by clock reads beside the relay
            row.update(ttfb_ms=clock.milliseconds(first - started))
            if last is not None:
                row.update(upstream_ms=clock.milliseconds(last - sent_at))
        self.finish_row(rt, row, resp, completed, disconnected, failure, error_head, meter, body, resp_headers)
        self.record(rt, row, started)
        rt.chain.run_response_end(ctx, Result(resp.status, resp.reason, resp_headers,
                                              error_head if resp.status >= 400 else b"", completed, disconnected,
                                              clock.monotonic() - started, note))

    def finish_row(self, rt, row, resp, completed, disconnected, failure, error_head, meter, body, resp_headers):
        """The relay's share of the row; an error reply's context and an unreadable usage as events."""
        relay_row(row, resp.status, completed, disconnected, failure, error_head, meter)
        detail = error_detail(body, resp_headers) if resp.status >= 400 else None
        if detail:
            rt.db.event("error_detail", request_id=row["request_id"], **detail)
        if (completed and resp.status == 200 and meter.unread and row["route"] == "anthropic" and self.command == "POST"
                and self.path.split("?", 1)[0] == MESSAGES_PATH):
            rt.db.event("usage_unread", request_id=row["request_id"], reason=meter.unread)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any
