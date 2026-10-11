"""The pool of kept-alive upstream connections: a connection is leased to one request at a time and goes back the moment
its response body is complete.

A response read to its end returns its connection (`PooledResponse`); one cut short spends it. The pool keeps at most
MAX_IDLE idle connections and a reaper closes those idle past IDLE_SECONDS, by time and not only at checkout, so open
sockets follow the requests in flight plus a bounded reserve. Connections are made in `model_gateway/upstream.py`.
"""
import http.client
import threading

from . import clock

# reqwest's default pool idle timeout. Past it a NAT or the edge may have dropped the connection without a
# FIN, and a request sent on it waits out the TCP retransmission timeout before failing.
IDLE_SECONDS = 90
MAX_IDLE = 16  # idle connections kept; the oldest beyond it are closed
REAP_SECONDS = 15  # how often the reaper looks for connections idle past IDLE_SECONDS


class PooledResponse(http.client.HTTPResponse):
    """A response that gives its connection back to the pool when its body is complete: a Content-Length body read to
    its last byte, a chunked body read through its terminator. http.client leaves the first open after `read1`
    returns nothing, so the next request on that connection would fail after it was sent and be sent again."""

    lease = None

    def complete(self):
        return self.fp is None and self.chunk_left is None if self.chunked else self.length == 0

    def settle(self):
        conn = self.lease
        if conn is None or not self.complete():
            return
        self.lease = None
        if self.fp is not None:
            self._close_conn()
        conn.give_back()

    def read(self, amt=None):
        data = super().read(amt)
        self.settle()
        return data

    def read1(self, n=-1):
        data = super().read1(n)
        self.settle()
        return data

    def readinto(self, buffer):
        count = super().readinto(buffer)
        self.settle()
        return count

    def close(self):
        done = self.complete()  # judged before closing: a chunked body closed unread looks complete after
        super().close()
        if done:
            self.settle()


class Pool:
    """The idle connections of one origin, oldest first, shared by every serving thread; a connection has one
    leaseholder (`holder`) or none, and only its leaseholder may close it."""

    def __init__(self, event):
        self.event = event
        self.lock = threading.Lock()
        self.idle = []
        self.reaper = None

    def _expired(self, now):
        stale = [c for c in self.idle if now - c.idle_since > IDLE_SECONDS]
        self.idle = [c for c in self.idle if now - c.idle_since <= IDLE_SECONDS]
        return stale

    def take(self):
        """The newest idle connection, leased to the calling thread, or None; connections idle too long are closed."""
        with self.lock:
            stale = self._expired(clock.monotonic())
            conn = self.idle.pop() if self.idle else None
            if conn is not None:
                conn.holder = conn.owner = threading.current_thread()
        self.close(stale)
        return conn

    def give(self, conn):
        """End `conn`'s lease: idle in the pool, or closed when it is spent or the pool is full."""
        with self.lock:
            if conn.holder is None:
                return
            conn.holder = None
            if conn.sock is None:
                return
            conn.idle_since = clock.monotonic()
            self.idle.append(conn)
            over, self.idle = self.idle[:-MAX_IDLE], self.idle[-MAX_IDLE:]
            if self.reaper is None:
                self.reaper = threading.Thread(target=self._reap, daemon=True)
                self.reaper.start()
        self.close(over)

    def revoke(self, conn, idle=True):
        """Whether the calling thread still has `conn` in its hands, its lease ended for the caller to close it: it holds
        the lease, or (`idle`) it was the last to and the connection waits in the pool untaken."""
        with self.lock:
            me = threading.current_thread()
            if conn.holder is me:
                conn.holder = None
                return True
            if idle and conn.holder is None and conn.owner is me and conn in self.idle:
                self.idle.remove(conn)
                return True
            return False

    def disown(self, conn):
        """Make the calling thread no longer `conn`'s last holder, so its own cleanup of that connection (`revoke` with
        `idle`) leaves it in the pool for any thread to take."""
        with self.lock:
            if conn.owner is threading.current_thread():
                conn.owner = None

    def clear(self):
        with self.lock:
            idle, self.idle = self.idle, []
        self.close(idle)

    def _reap(self):
        while True:
            clock.sleep(REAP_SECONDS)
            with self.lock:
                stale = self._expired(clock.monotonic())
                done = not self.idle
                if done:
                    self.reaper = None
            self.close(stale)
            if done:
                return

    def close(self, conns):
        for conn in conns:
            try:
                conn.close()
            except Exception as e:
                self.event("upstream_socket", what="close", error=repr(e))
