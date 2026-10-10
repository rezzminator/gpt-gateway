"""The upstream API: one connection per serving thread, kept alive, with one fresh retry; and WebSocket upgrades.

A pooled connection idle past IDLE_SECONDS is closed before use, never sent on. A pooled connection
the server closed fails on its next use; the request is retried once on a new
connection; `reuse()` reports whether the last send rode a kept-alive connection and whether that retry
fired. Any other connection error, and a response http.client cannot parse, is final, and the caller
answers 502. Request headers go as an ordered list of pairs, so a header sent twice goes twice. Every socket
probes its path with TCP keepalive, so a path that died without a FIN or RST fails the read in seconds.
`websocket()` upgrades a fresh connection (RFC 6455) and hands the caller the socket to keep; its frames are
`model_gateway/frames.py`'s.
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

from . import clock

TIMEOUT_SECONDS = 900
# reqwest's default pool idle timeout. Past it a NAT or the edge may have dropped the connection without a
# FIN, and a request sent on it waits out the TCP retransmission timeout before failing.
IDLE_SECONDS = 90
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


def keepalive(sock, log):
    """Turn TCP keepalive on for `sock`; a refused option is logged, never fatal."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option, value in KEEPALIVE_OPTIONS:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError as e:
        log("    upstream keepalive failed: %r" % (e,))


class _Keepalive:
    """Turns TCP keepalive on for the connection's socket as it connects."""

    log = staticmethod(lambda line: None)

    def connect(self):
        super().connect()
        keepalive(self.sock, self.log)


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
    def __init__(self, origin, log=None):
        self.origin = urlsplit(origin)
        self.log = log if log is not None else (lambda line: None)
        self.local = threading.local()
        self.tls = ssl.create_default_context() if self.origin.scheme == "https" else None

    def connection(self):
        conn = getattr(self.local, "conn", None)
        # Idle runs from the last response's headers, so a long stream counts toward it: reconnecting early
        # costs one handshake, reusing a dead connection costs the TCP timeout.
        if conn is not None and conn.sock is not None and clock.monotonic() - getattr(self.local, "answered", float("-inf")) > IDLE_SECONDS:
            self.drop()
            conn = None
        if conn is None:
            host, port = self.origin.hostname, self.origin.port
            if self.tls is not None:
                conn = KeepaliveHTTPSConnection(host, port or 443, timeout=TIMEOUT_SECONDS, context=self.tls)
            else:
                conn = KeepaliveHTTPConnection(host, port or 80, timeout=TIMEOUT_SECONDS)
            conn.log = self.log
            self.local.conn = conn
        return conn

    def drop(self):
        """Close this thread's connection; the next request opens a fresh one."""
        conn = getattr(self.local, "conn", None)
        self.local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception as e:
                self.log("    upstream close failed: %r" % (e,))

    def reuse(self):
        """(reused, stale) of this thread's last send: the answering attempt rode a kept-alive connection;
        a kept-alive connection failed and the request went again on a fresh one."""
        return getattr(self.local, "reused", False), getattr(self.local, "stale", False)

    def abort(self):
        """Shut this thread's socket down, so a reader blocked on its response returns, then drop it."""
        conn = getattr(self.local, "conn", None)
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                self.log("    upstream shutdown failed: %r" % (e,))
        self.drop()

    def send(self, method, path, body, headers, timeout=None):
        """The upstream response, or UpstreamError after the one fresh retry.

        headers: ((name, value), ...) in the client's order; Host comes from the origin.
        timeout: seconds for this thread's connection from now on, when given.
        """
        self.local.reused = self.local.stale = False
        for attempt in (1, 2):
            try:
                conn = self.connection()
                self.local.reused = conn.sock is not None
                if timeout is not None:
                    conn.timeout = timeout
                    if conn.sock is not None:
                        conn.sock.settimeout(timeout)
                conn.putrequest(method, path, skip_accept_encoding=True)
                for name, value in headers:
                    conn.putheader(name, value)
                conn.endheaders(body)
                resp = conn.getresponse()
                self.local.answered = clock.monotonic()
                return resp
            except RETRYABLE as e:
                self.local.stale = self.local.stale or self.local.reused
                self.drop()
                if attempt == 2:
                    raise UpstreamError("upstream connection failed: %r" % e)
            except (OSError, socket.timeout) as e:
                self.drop()
                raise UpstreamError("upstream unreachable: %r" % e)
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
            keepalive(sock, self.log)
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
            raise UpstreamError("upstream unreachable: %r" % e)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError as e:
                    self.log("    upstream close failed: %r" % (e,))
