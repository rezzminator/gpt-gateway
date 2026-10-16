"""The listening server: SO_REUSEPORT for a graceful restart, in-flight tracking, the SIGTERM drain.

The Linux supervisor and its gateways share one inherited listener and accept queue. A handover
keeps queued connections on that socket and drains accepted connections with request data as well as streams.
Independently bound SO_REUSEPORT listeners can lose queued connections when one closes on Linux.
On SIGTERM silent accepted clients and idle keep-alives close; received requests finish up to the drain limit.
"""
import errno
import select
import signal
import socket
import sys
import threading
from http.server import ThreadingHTTPServer

from . import clock, handles
from .log import record
from .proxy import Handler

REQUEST_QUEUE_SIZE = 256  # the listen backlog; the stdlib's 5 reset a burst of simultaneous connects (the kernel caps it)
ACCEPT_BACKOFF = 0.1  # seconds to wait after a first failed accept, so a listener that cannot accept does not spin
ACCEPT_BACKOFF_MAX = 2.0  # the wait doubles with each failure in a row, up to this


class Drain:
    """Counts in-flight requests and remembers idle client connections, so a stop can wait for the first and close the second."""

    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = 0
        self.idle = set()  # client connections between requests
        self.pending = {}  # first connections: False until request data is ready to read
        self.preserve_pending = sys.platform == 'linux'
        self.stopping = threading.Event()

    @property
    def draining(self):
        return self.stopping.is_set()

    def connected(self, conn):
        with self.lock:
            if self.preserve_pending:
                self.pending.setdefault(conn, False)
            else:
                self.idle.add(conn)

    def disconnected(self, conn):
        with self.lock:
            self.idle.discard(conn)
            self.pending.pop(conn, None)

    def receiving(self, conn):
        with self.lock:
            if conn in self.pending:
                self.pending[conn] = True

    @staticmethod
    def readable(conn, seconds=0):
        poll = select.poll()
        poll.register(conn, select.POLLIN)
        return bool(poll.poll(int(seconds * 1000)))

    def begin(self, conn):
        with self.lock:
            self.inflight += 1
            self.idle.discard(conn)
            self.pending.pop(conn, None)

    def end(self, conn):
        with self.lock:
            self.inflight -= 1
            self.idle.add(conn)

    def wait(self, limit, log):
        """Wait until no request is in flight, or `limit` seconds pass; log the outcome; return the count left."""
        deadline = clock.now() + limit
        with self.lock:
            for conn, receiving in list(self.pending.items()):
                if receiving:
                    continue
                try:
                    ready = self.readable(conn)
                except (OSError, ValueError):
                    ready = []
                if not ready:  # a TCP handshake without request data is an idle client
                    self.pending.pop(conn, None)
                    self.idle.add(conn)
            busy = self.inflight + len(self.pending)
            idle = list(self.idle)
        for conn in idle:  # an idle keep-alive client sees the close and reconnects to the successor
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                if e.errno != errno.ENOTCONN:  # ENOTCONN: the client closed it already
                    record(log, "warning", "idle-shutdown-failed", "stop: shutdown of an idle connection failed: %r" % (e,), "gpt_gateway/server.py#Drain.wait")
        record(log, "info", "draining", "stop: draining %d in-flight request(s), up to %ss" % (busy, limit), "gpt_gateway/server.py#Drain.wait")
        while busy and clock.now() < deadline:
            clock.sleep(min(0.2, max(0.0, deadline - clock.now())))
            with self.lock:
                busy = self.inflight + len(self.pending)
        if busy:
            record(log, "warning", "in-flight-exit", "stop: %d request(s) still in flight after %ss; exiting anyway" % (busy, limit), "gpt_gateway/server.py#Drain.wait")
        else:
            record(log, "info", "drained", "stop: drained, exiting", "gpt_gateway/server.py#Drain.wait")
        return busy


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = REQUEST_QUEUE_SIZE
    accept_failures = 0  # accepts that failed in a row

    def __init__(self, address, runtime, handler=Handler):
        self.runtime = runtime
        descriptor = getattr(runtime.env, 'listener_fd', None)
        if descriptor is None:
            super().__init__(address, handler)
        else:
            super().__init__(address, handler, bind_and_activate=False)
            self.socket.close()
            self.socket = socket.socket(fileno=descriptor)
            self.socket.setblocking(False)
            self.server_address = self.socket.getsockname()
            if self.server_address != address or not self.listening(self.socket):
                self.socket.close()
                raise OSError('inherited gateway descriptor is not the configured listener')
            self.server_name = socket.getfqdn(address[0])
            self.server_port = address[1]

    @staticmethod
    def listening(sock):
        """Whether `sock` is a listening socket; macOS cannot report it (ENOPROTOOPT), so listen() proves it there."""
        try:
            return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        except OSError as error:
            if error.errno != errno.ENOPROTOOPT:
                raise
        try:
            sock.listen(REQUEST_QUEUE_SIZE)  # idempotent on a listening socket; EINVAL on a connected one
        except OSError:
            return False
        return True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        super().server_bind()

    def get_request(self):
        # socketserver swallows an accept failure; a process out of descriptors is the one that matters.
        try:
            accepted = super().get_request()
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise  # another gateway accepted the readiness event's connection first
            shortage = handles.diagnose(error)
            if shortage is None:
                handles.announce(self.runtime.db.event, "accept_failed", error=repr(error))
            else:
                handles.announce(self.runtime.db.event, "fd_exhausted", where="accept", error=repr(error),
                                 open=shortage.open, limit=shortage.limit)
            self.accept_failures += 1
            doublings = min(self.accept_failures - 1, 16)  # 2 ** 1024 overflows a float; 16 is past the bound already
            clock.sleep(min(ACCEPT_BACKOFF * 2 ** doublings, ACCEPT_BACKOFF_MAX))
            raise
        self.accept_failures = 0
        self.runtime.drain.connected(accepted[0])
        return accepted

    def close_request(self, request):
        self.runtime.drain.disconnected(request)
        super().close_request(request)

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
    server.serve_forever(poll_interval=0.1 if sys.platform == 'linux' else 0.5)
    server.server_close()  # the shared Linux listener stays open in the supervisor and successor
    left = runtime.drain.wait(runtime.env.drain_seconds, runtime.log.write)
    if runtime.chatgpt is not None:
        runtime.chatgpt.client.close()
    runtime.db.event("stop", left=left, drain_seconds=runtime.env.drain_seconds)
    if not runtime.db.close(5.0):
        runtime.log.record("error", "writer-unfinished-rows-lost", "stop: the database writer did not finish within 5s; the rows still queued are lost", "gpt_gateway/server.py#serve")
    return 0
