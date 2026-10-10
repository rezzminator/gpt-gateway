"""The upstream API: one connection per serving thread, kept alive, with one fresh retry.

A pooled connection idle past IDLE_SECONDS is closed before use, never sent on. A pooled connection
the server closed fails on its next use; the request is retried once on a new
connection; `reuse()` reports whether the last send rode a kept-alive connection and whether that retry
fired. Any other connection error, and a response http.client cannot parse, is final, and the caller
answers 502. Request headers go as an ordered list of pairs, so a header sent twice goes twice. Every socket
probes its path with TCP keepalive, so a path that died without a FIN or RST fails the read in seconds.
"""
import http.client
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


class _Keepalive:
    """Turns TCP keepalive on for the connection's socket as it connects; a refused option is logged, never fatal."""

    log = staticmethod(lambda line: None)

    def connect(self):
        super().connect()
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for option, value in KEEPALIVE_OPTIONS:
                self.sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError as e:
            self.log("    upstream keepalive failed: %r" % (e,))


class KeepaliveHTTPConnection(_Keepalive, http.client.HTTPConnection):
    pass


class KeepaliveHTTPSConnection(_Keepalive, http.client.HTTPSConnection):
    pass


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
