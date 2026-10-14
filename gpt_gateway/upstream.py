"""The upstream API: a shared pool of kept-alive connections, each leased to one request at a time, with one fresh
retry; and WebSocket upgrades.

A connection goes back to the pool the moment its response body is complete, never earlier; a response cut short
spends its connection. The pool keeps at most MAX_IDLE idle connections and a reaper closes those idle past
IDLE_SECONDS, so open sockets follow the requests in flight plus a bounded reserve, never the number of clients. A
kept-alive connection that fails before its response's status line arrives (a TLS alert, a reset, a broken pipe, a
close) is retried once, the same bytes on a new connection, and recorded as an `upstream_retry` event; `reuse()` and
`attempts()` report whether the last send rode a kept-alive connection, whether that retry fired and how many
attempts it took. A new connection's failure, and any failure after the status line, is final, and the caller
answers 502 with the failure's class (`gpt_gateway/reach.py`); one that is the process running out of file handles
says so (`gpt_gateway/handles.py`). Failures in a row are recorded as a run of the origin being unreachable. Request headers go as an ordered list of
pairs, so a header sent twice goes twice. Every socket probes its path with TCP keepalive, so a path that died
without a FIN or RST fails the read in seconds. `websocket()` upgrades a fresh connection (RFC 6455) and hands the
caller the socket to keep; its frames are `gpt_gateway/frames.py`'s.
"""
import base64
import hashlib
import http.client
import os
import socket
import ssl
import sys
import threading
from urllib.parse import urlsplit

from . import clock, handles, reach
from .pool import Pool, PooledResponse

TIMEOUT_SECONDS = 900
# Past KEEPALIVE_IDLE seconds without traffic the kernel probes the peer every KEEPALIVE_INTERVAL seconds and fails
# the socket after KEEPALIVE_PROBES unanswered probes. A peer that is only slow (a model reasoning before its first
# token) still answers them, so only a dead path (a Wi-Fi roam, a dropped NAT mapping) ends the read: in about 35 s,
# never the stream's 300 s idle timeout or TIMEOUT_SECONDS. The system default waits two hours before a first probe.
KEEPALIVE_IDLE, KEEPALIVE_INTERVAL, KEEPALIVE_PROBES = 15, 5, 4
# TCP_KEEPIDLE on Linux; TCP_KEEPALIVE (0x10 in Darwin's netinet/tcp.h) on macOS, which Python 3.9 does not export.
_IDLE = (getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
         or (0x10 if sys.platform == "darwin" else None))
KEEPALIVE_OPTIONS = tuple((option, value) for option, value in (
    (_IDLE, KEEPALIVE_IDLE), (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_INTERVAL),
    (getattr(socket, "TCP_KEEPCNT", None), KEEPALIVE_PROBES)) if option is not None)
# The failures of a kept-alive connection the server or the path dropped while it sat idle: sent again on a new one
# when nothing of the answer arrived, and the request was still being written or was written less than
# REUSE_WINDOW_SECONDS before: a dead connection fails within a round trip, a later failure may cut off the origin's work.
REUSE_FAILURES = (ssl.SSLError, ConnectionError, http.client.CannotSendRequest, http.client.ResponseNotReady)
REUSE_WINDOW_SECONDS = 1.0
# The failures named "upstream connection failed"; any other OSError is "upstream unreachable".
CONNECTION_FAILED = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
                     http.client.CannotSendRequest, http.client.ResponseNotReady)
ACCEPT_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 § 1.3
CONNECT_SECONDS = 15  # codex-rs/model-provider-info/src/lib.rs:71, DEFAULT_WEBSOCKET_CONNECT_TIMEOUT_MS
HEAD_LIMIT = 65536  # an upgrade answer's headers, and the part of a refusal's body kept


def is_stale(error):
    """Whether `error` is the signature of a kept-alive connection the server closed before sending any body."""
    if isinstance(error, http.client.IncompleteRead):
        return not error.partial
    return isinstance(error, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError))


class UpstreamError(Exception):
    """The upstream request failed; `message` is the client-facing text, `error_class` the failure's class
    (`gpt_gateway/reach.py`), `protocol` for a WebSocket frame the gateway refused."""

    def __init__(self, message, error_class=None):
        super().__init__(message)
        self.message, self.error_class = message, error_class


def keepalive(sock, event):
    """Turn TCP keepalive on for `sock`; a refused option is an `upstream_socket` event, never fatal."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option, value in KEEPALIVE_OPTIONS:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError as e:
        event("upstream_socket", what="keepalive", error=repr(e))


class _Keepalive:
    """Turns TCP keepalive on for the connection's socket as it connects, and leases it to a pool."""

    event = staticmethod(lambda kind, **fields: None)
    pool = holder = owner = None  # the pool it returns to; the thread whose request it serves now; the last such thread
    idle_since = 0.0
    answer = None  # the response of the request in flight, kept from the moment http.client starts to read it

    def response_class(self, sock, *args, **kwargs):
        self.answer = PooledResponse(sock, *args, **kwargs)
        return self.answer

    def heard(self):
        """Whether the status line of the answer in flight arrived; http.client sets `status` to a number on it."""
        return self.answer is not None and isinstance(self.answer.status, int)

    def connect(self):
        super().connect()
        keepalive(self.sock, self.event)

    def getresponse(self):
        resp = super().getresponse()
        resp.lease = self
        resp.settle()  # a response with no body is complete already
        return resp

    def give_back(self):
        self.pool.give(self)


class KeepaliveHTTPConnection(_Keepalive, http.client.HTTPConnection):
    pass


class KeepaliveHTTPSConnection(_Keepalive, http.client.HTTPSConnection):
    pass


class Upgrade:
    """A WebSocket upgrade's answer: on 101 the upgraded `socket` and the bytes read past the answer (`pending`);
    otherwise its status, headers and up to HEAD_LIMIT body bytes."""

    def __init__(self, status, headers, body=b"", socket=None, pending=b""):
        self.status, self.headers, self.body, self.socket, self.pending = status, headers, body, socket, pending


def header(headers, name):
    """The first value of `name` in (name, value) pairs, case-insensitively, or None."""
    return next((v for k, v in headers if k.lower() == name), None)


def _answer_head(head):
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise UpstreamError("upstream protocol error: status line %r" % lines[0][:100], "protocol")
    pairs = [line.partition(":") for line in lines[1:] if line]
    return int(parts[1]), [(name.strip(), value.strip()) for name, _, value in pairs]


def _answer_body(sock, data, headers):
    data = bytearray(data)

    def more():
        try:
            got = sock.recv(65536)
        except (OSError, socket.timeout):
            return False  # the status and headers already arrived; a body cut short is still their answer
        data.extend(got)
        return bool(got)

    length = header(headers, "content-length")
    if length is not None and length.isdigit():
        while len(data) < min(int(length), HEAD_LIMIT) and more():
            pass
        return bytes(data[:min(int(length), HEAD_LIMIT)])
    if "chunked" not in (header(headers, "transfer-encoding") or "").lower():
        while len(data) < HEAD_LIMIT and more():
            pass
        return bytes(data[:HEAD_LIMIT])
    body, at = bytearray(), 0
    while len(body) < HEAD_LIMIT:
        end = data.find(b"\r\n", at)
        if end < 0:
            if more():
                continue
            break
        try:
            size = int(bytes(data[at:end]).split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        while len(data) < end + 2 + size and more():
            pass
        body += data[end + 2:end + 2 + size]
        at = end + 4 + size
    return bytes(body[:HEAD_LIMIT])


class Upstream:
    def __init__(self, origin, event=None):
        self.origin = urlsplit(origin)
        self.event = event if event is not None else (lambda kind, **fields: None)
        self.pool = Pool(lambda kind, **fields: self.event(kind, **fields))
        self.local = threading.local()  # `conn`: the connection this thread leased last, its if the pool says so
        self.runs = reach.Runs(lambda kind, **fields: self.event(kind, **fields), self.origin.hostname)
        self.tls = ssl.create_default_context() if self.origin.scheme == "https" else None

    def connection(self, fresh=False):
        """A connection leased to this thread: the newest idle one (never when `fresh`), else a new one. A lease this
        thread still holds, a response it never finished, is spent."""
        self.forget(idle=False)
        conn = None if fresh else self.pool.take()
        if conn is None:
            host, port = self.origin.hostname, self.origin.port
            if self.tls is not None:
                conn = KeepaliveHTTPSConnection(host, port or 443, timeout=TIMEOUT_SECONDS, context=self.tls)
            else:
                conn = KeepaliveHTTPConnection(host, port or 80, timeout=TIMEOUT_SECONDS)
            me = threading.current_thread()
            conn.event, conn.pool, conn.holder, conn.owner = self.event, self.pool, me, me
        self.local.conn = conn
        return conn

    def forget(self, idle):
        """Close this thread's connection if it still has it in its hands: it holds the lease, or (`idle`) it finished
        the last response and no other thread has taken the connection from the pool since."""
        conn = getattr(self.local, "conn", None)
        self.local.conn = None
        if conn is not None and self.pool.revoke(conn, idle):
            self.pool.close([conn])

    def drop(self):
        """Spend this thread's connection, a failed or abandoned exchange's, unless another thread has it now."""
        self.forget(idle=True)

    def release(self):
        """Let go of this thread's connection after a response read to its end: it stays in the pool, whatever
        becomes of the client, for any thread to take. Never for a response cut short: that is `drop`."""
        conn = getattr(self.local, "conn", None)
        self.local.conn = None
        if conn is not None:
            self.pool.disown(conn)

    def close(self):
        """Close this thread's connection and every idle one."""
        self.drop()
        self.pool.clear()

    def reuse(self):
        """(reused, stale) of this thread's last send: the answering attempt rode a kept-alive connection;
        a kept-alive connection failed and the request went again on a fresh one."""
        return getattr(self.local, "reused", False), getattr(self.local, "stale", False)

    def attempts(self):
        """The number of connections this thread's last send tried: 2 when the reuse retry fired."""
        return getattr(self.local, "attempts", 0)

    def abort(self):
        """Shut this thread's socket down, so a reader blocked on its response returns, then drop it. A connection
        whose lease ended, its response read to the end, is no longer this thread's to shut. The lease ends before the
        shutdown, so a reader finishing the body meanwhile cannot hand the connection to another request."""
        conn = getattr(self.local, "conn", None)
        if conn is not None and self.pool.revoke(conn, idle=False):
            self.local.conn = None
            if conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError as e:
                    self.event("upstream_socket", what="shutdown", error=repr(e))
            self.pool.close([conn])
        self.drop()

    def missed(self, failed):
        """`failed`, told to the unreachable runs."""
        self.runs.failed(failed.error_class, failed.message)
        return failed

    def unreachable(self, error, where):
        """The UpstreamError for a connection that could not be made, naming a shortage of file handles as one: this
        process's own, never the origin's, so it stays out of the unreachable runs."""
        shortage = handles.diagnose(error)
        if shortage is None:
            return self.missed(UpstreamError("upstream unreachable: %r" % error, reach.error_class(error)))
        handles.announce(self.event, "fd_exhausted", where=where, error=repr(error), open=shortage.open,
                       limit=shortage.limit)
        return UpstreamError(shortage.message + " (%r)" % error, "fd")

    def failure(self, error):
        """The final UpstreamError for a send's `error`; an answer http.client cannot parse is no unreachable origin."""
        if isinstance(error, CONNECTION_FAILED):
            return self.missed(UpstreamError("upstream connection failed: %r" % error, reach.error_class(error)))
        if isinstance(error, http.client.HTTPException):
            self.runs.answered()  # the origin spoke, however badly: no failure to reach it
            return UpstreamError("upstream protocol error: %r" % error, "protocol")
        return self.unreachable(error, "upstream")

    def send(self, method, path, body, headers, timeout=None, request_id=None):
        """The upstream response, or UpstreamError; a kept-alive connection that failed before its answer's status
        line, while the request was written or within REUSE_WINDOW_SECONDS after, is retried once on a new one, `body`
        unchanged.

        headers: ((name, value), ...) in the client's order; Host comes from the origin.
        timeout: seconds this request reads and writes with, TIMEOUT_SECONDS when None.
        request_id: the gateway's number for the request, named in its `upstream_retry` event.
        """
        self.local.reused = self.local.stale = False
        for attempt in (1, 2):
            self.local.attempts, conn, idle, written = attempt, None, None, None
            try:
                conn = self.connection(fresh=attempt == 2)
                conn.answer = None
                self.local.reused = conn.sock is not None
                if self.local.reused:
                    idle = clock.monotonic() - conn.idle_since
                conn.timeout = TIMEOUT_SECONDS if timeout is None else timeout  # a pooled connection keeps no earlier request's
                if conn.sock is not None:
                    conn.sock.settimeout(conn.timeout)
                conn.putrequest(method, path, skip_accept_encoding=True)
                for name, value in headers:
                    conn.putheader(name, value)
                conn.endheaders(body)
                written = clock.monotonic() + clock.slept()  # the timer stops while the host sleeps; the origin's does not
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                self.drop()
                waited = None if written is None else clock.monotonic() + clock.slept() - written
                if (attempt == 2 or not self.local.reused or not isinstance(e, REUSE_FAILURES) or conn.heard()
                        or waited is not None and waited >= REUSE_WINDOW_SECONDS):
                    raise self.failure(e) from e
                self.local.stale = True
                self.event("upstream_retry", request_id=request_id, error_class=reach.error_class(e), error=repr(e),
                           idle_ms=clock.milliseconds(idle), wait_ms=None if waited is None else clock.milliseconds(waited))
                continue
            self.runs.answered()
            return resp

    def websocket(self, path, headers):
        """An Upgrade for `path` on a fresh connection, or UpstreamError when none answered.

        headers: ((name, value), ...) after the upgrade's own; the socket, once upgraded, reads with TIMEOUT_SECONDS.
        """
        host = self.origin.hostname
        port = self.origin.port or (443 if self.tls is not None else 80)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = ["GET %s HTTP/1.1" % path, "Host: " + (host if self.origin.port is None else "%s:%d" % (host, port)),
                 "Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
        lines += ["%s: %s" % pair for pair in headers]
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=CONNECT_SECONDS)
            keepalive(sock, self.event)
            if self.tls is not None:
                sock = self.tls.wrap_socket(sock, server_hostname=host)
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            data = bytearray()
            while b"\r\n\r\n" not in data:
                if len(data) > HEAD_LIMIT:
                    raise UpstreamError("upstream protocol error: upgrade answer headers exceed 64 KiB", "protocol")
                got = sock.recv(65536)
                if not got:
                    raise self.missed(UpstreamError("upstream connection failed: closed before the upgrade answer",
                                                    "reset"))
                data += got
            head, _, rest = bytes(data).partition(b"\r\n\r\n")
            status, answer = _answer_head(head)
            self.runs.answered()
            if status != 101:
                return Upgrade(status, answer, _answer_body(sock, rest, answer))
            accept = base64.b64encode(hashlib.sha1(key.encode() + ACCEPT_GUID).digest()).decode()
            if header(answer, "sec-websocket-accept") != accept:
                raise UpstreamError("upstream protocol error: the upgrade answer's Sec-WebSocket-Accept is wrong",
                                    "protocol")
            sock.settimeout(TIMEOUT_SECONDS)
            upgraded, sock = sock, None
            return Upgrade(101, answer, socket=upgraded, pending=rest)
        except (OSError, socket.timeout) as e:
            raise self.unreachable(e, "websocket")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError as e:
                    self.event("upstream_socket", what="close", error=repr(e))
