"""The upstream API: one connection per serving thread, kept alive, with one fresh retry.

A pooled connection the server closed fails on its next use; the request is retried once on a new
connection. Any other connection error, and a response http.client cannot parse, is final, and the
caller answers 502. Request headers go as an ordered list of pairs, so a header sent twice goes twice.
"""
import http.client
import socket
import ssl
import threading
from urllib.parse import urlsplit

TIMEOUT_SECONDS = 900
RETRYABLE = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
             http.client.CannotSendRequest, http.client.ResponseNotReady)


class UpstreamError(Exception):
    """The upstream request failed; `message` is the client-facing text."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class Upstream:
    def __init__(self, origin, log=None):
        self.origin = urlsplit(origin)
        self.log = log if log is not None else (lambda line: None)
        self.local = threading.local()
        self.tls = ssl.create_default_context() if self.origin.scheme == "https" else None

    def connection(self):
        conn = getattr(self.local, "conn", None)
        if conn is None:
            host, port = self.origin.hostname, self.origin.port
            if self.tls is not None:
                conn = http.client.HTTPSConnection(host, port or 443, timeout=TIMEOUT_SECONDS, context=self.tls)
            else:
                conn = http.client.HTTPConnection(host, port or 80, timeout=TIMEOUT_SECONDS)
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

    def send(self, method, path, body, headers):
        """The upstream response, or UpstreamError after the one fresh retry.

        headers: ((name, value), ...) in the client's order; Host comes from the origin.
        """
        for attempt in (1, 2):
            try:
                conn = self.connection()
                conn.putrequest(method, path, skip_accept_encoding=True)
                for name, value in headers:
                    conn.putheader(name, value)
                conn.endheaders(body)
                return conn.getresponse()
            except RETRYABLE as e:
                self.drop()
                if attempt == 2:
                    raise UpstreamError("upstream connection failed: %r" % e)
            except (OSError, socket.timeout) as e:
                self.drop()
                raise UpstreamError("upstream unreachable: %r" % e)
            except http.client.HTTPException as e:
                self.drop()
                raise UpstreamError("upstream protocol error: %r" % e)
