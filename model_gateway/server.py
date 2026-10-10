"""The listening server: SO_REUSEPORT for a graceful restart, in-flight tracking, the SIGTERM drain.

A restarted gateway binds the port while its predecessor still drains; new connections go to the
newest. On SIGTERM the gateway stops accepting, closes idle keep-alive connections at once, and lets
in-flight requests finish for up to the drain limit.
"""
import errno
import signal
import socket
import sys
import threading
from http.server import ThreadingHTTPServer

from . import clock
from .proxy import Handler


class Drain:
    """Counts in-flight requests and remembers idle client connections, so a stop can wait for the first and close the second."""

    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = 0
        self.idle = set()  # client connections between requests
        self.stopping = threading.Event()

    @property
    def draining(self):
        return self.stopping.is_set()

    def connected(self, conn):
        with self.lock:
            self.idle.add(conn)

    def disconnected(self, conn):
        with self.lock:
            self.idle.discard(conn)

    def begin(self, conn):
        with self.lock:
            self.inflight += 1
            self.idle.discard(conn)

    def end(self, conn):
        with self.lock:
            self.inflight -= 1
            self.idle.add(conn)

    def wait(self, limit, log):
        """Wait until no request is in flight, or `limit` seconds pass; log the outcome; return the count left."""
        deadline = clock.now() + limit
        with self.lock:
            busy = self.inflight
            idle = list(self.idle)
        for conn in idle:  # an idle keep-alive client sees the close and reconnects to the successor
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                if e.errno != errno.ENOTCONN:  # ENOTCONN: the client closed it already
                    log("stop: shutdown of an idle connection failed: %r" % (e,))
        log("stop: draining %d in-flight request(s), up to %ss" % (busy, limit))
        while busy and clock.now() < deadline:
            clock.sleep(0.2)
            with self.lock:
                busy = self.inflight
        if busy:
            log("stop: %d request(s) still in flight after %ss; exiting anyway" % (busy, limit))
        else:
            log("stop: drained, exiting")
        return busy


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, runtime, handler=Handler):
        self.runtime = runtime
        super().__init__(address, handler)

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # Claude Code drops idle keep-alive connections; that is not an error worth a traceback.
        err = sys.exc_info()[1]
        if isinstance(err, (ConnectionResetError, BrokenPipeError, socket.timeout)):
            return
        self.runtime.db.event("client_error", port=client_address[1], error=repr(err))


def bind(runtime):
    """The server listening on 127.0.0.1:{port}; OSError when the port cannot be bound."""
    return Server(("127.0.0.1", runtime.env.port), runtime)


def serve(runtime, server):
    """Serve until SIGTERM or SIGINT, then drain and let the database writer finish; the exit status."""
    def on_term(signum, frame):
        if runtime.drain.draining:
            return
        runtime.drain.stopping.set()
        # shutdown() waits for serve_forever, which runs on this thread: stop it from another one.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    server.serve_forever()
    server.server_close()  # no new connections from here on; the successor takes them
    left = runtime.drain.wait(runtime.env.drain_seconds, runtime.log.write)
    runtime.db.event("stop", left=left, drain_seconds=runtime.env.drain_seconds)
    if not runtime.db.close(5.0):
        runtime.log.write("stop: the database writer did not finish within 5s; the rows still queued are lost")
    return 0
