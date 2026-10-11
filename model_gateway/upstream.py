"""The upstream API: a shared pool of kept-alive connections, each leased to one request at a time, with one fresh
retry; and WebSocket upgrades.

A connection goes back to the pool the moment its response body is complete, never earlier; a response cut short
spends its connection. The pool keeps at most MAX_IDLE idle connections and a reaper closes those idle past
IDLE_SECONDS, so open sockets follow the requests in flight plus a bounded reserve, never the number of clients. A
pooled connection the server closed fails on its next use; the request is retried once on a new connection;
`reuse()` reports whether the last send rode a kept-alive connection and whether that retry fired. Any other
connection error, and a response http.client cannot parse, is final, and the caller answers 502; one that is the
process running out of file handles says so (`model_gateway/handles.py`). Request headers go as an ordered list of
pairs, so a header sent twice goes twice. Every socket probes its path with TCP keepalive, so a path that died
without a FIN or RST fails the read in seconds. `websocket()` upgrades a fresh connection (RFC 6455) and hands the
caller the socket to keep; its frames are `model_gateway/frames.py`'s.
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

from . import handles
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
RETRYABLE = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
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
    """The upstream request failed; `message` is the client-facing text."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


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
    response_class = PooledResponse
    pool = holder = owner = None  # the pool it returns to; the thread whose request it serves now; the last such thread
    idle_since = 0.0

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
        raise UpstreamError("upstream protocol error: status line %r" % lines[0][:100])
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

    def unreachable(self, error, where):
        """The UpstreamError for a connection that could not be made, naming a shortage of file handles as one."""
        shortage = handles.diagnose(error)
        if shortage is None:
            return UpstreamError("upstream unreachable: %r" % error)
        handles.announce(self.event, "fd_exhausted", where=where, error=repr(error), open=shortage.open,
                       limit=shortage.limit)
        return UpstreamError(shortage.message + " (%r)" % error)

    def send(self, method, path, body, headers, timeout=None):
        """The upstream response, or UpstreamError after the one fresh retry.

        headers: ((name, value), ...) in the client's order; Host comes from the origin.
        timeout: seconds this request reads and writes with, TIMEOUT_SECONDS when None.
        """
        self.local.reused = self.local.stale = False
        for attempt in (1, 2):
            try:
                conn = self.connection(fresh=attempt == 2)
                self.local.reused = conn.sock is not None
                conn.timeout = TIMEOUT_SECONDS if timeout is None else timeout  # a pooled connection keeps no earlier request's
                if conn.sock is not None:
                    conn.sock.settimeout(conn.timeout)
                conn.putrequest(method, path, skip_accept_encoding=True)
                for name, value in headers:
                    conn.putheader(name, value)
                conn.endheaders(body)
                return conn.getresponse()
            except RETRYABLE as e:
                self.local.stale = self.local.stale or self.local.reused
                self.drop()
                if attempt == 2:
                    raise UpstreamError("upstream connection failed: %r" % e)
            except (OSError, socket.timeout) as e:
                self.drop()
                raise self.unreachable(e, "upstream")
            except http.client.HTTPException as e:
                self.drop()
                raise UpstreamError("upstream protocol error: %r" % e)

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
                    raise UpstreamError("upstream protocol error: upgrade answer headers exceed 64 KiB")
                got = sock.recv(65536)
                if not got:
                    raise UpstreamError("upstream connection failed: closed before the upgrade answer")
                data += got
            head, _, rest = bytes(data).partition(b"\r\n\r\n")
            status, answer = _answer_head(head)
            if status != 101:
                return Upgrade(status, answer, _answer_body(sock, rest, answer))
            accept = base64.b64encode(hashlib.sha1(key.encode() + ACCEPT_GUID).digest()).decode()
            if header(answer, "sec-websocket-accept") != accept:
                raise UpstreamError("upstream protocol error: the upgrade answer's Sec-WebSocket-Accept is wrong")
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
