"""The keeper: idle pooled WebSockets are read, answered and pinged until they are used or MAX_AGE ends them.

Nothing reads a pooled socket between two exchanges, so a server ping goes unanswered and a server that ends an
idle socket, for missed pongs or on a timer of its own, is found only when the next request checks the socket out
and re-sends its whole conversation on a new one, losing the cache the old socket held. One daemon thread ticks every
TICK_SECONDS while any socket idles: it answers whatever the server sent, sends a ping of its own to a socket that
has gone PING_SECONDS without a frame from us, and takes a socket it finds dead out of the pool, closing it as
`keeper-closed`, or as `keeper-age` at MAX_AGE. It never touches a socket that is checked out. A socket it works
on holds its `guard`, which the checkout waits on, so the two never read one socket together.
"""
import threading

from .. import clock, handles

TICK_SECONDS = 5  # answers a server ping within this
# Measured on the live route: sockets nobody serviced died at 101 s idle at the earliest, and idle gaps up to 30 s
# (p99) survived unserviced. A ping every 20 s is a fifth of the shortest death and still sparse.
PING_SECONDS = 20


class Keeper:
    """The tending thread of one Sockets pool; it exists only while the pool holds idle sockets."""

    def __init__(self, sockets):
        self.sockets = sockets
        self.thread = None  # under sockets.lock
        self.stopping = threading.Event()

    def ensure(self):
        """Under sockets.lock: have a thread tending, unless stopped. A thread that cannot start is a `keeper_failed`
        event, and the next check-in tries again."""
        if self.thread is None and not self.stopping.is_set():
            thread = threading.Thread(target=self.run, name="websocket-keeper", daemon=True)
            try:
                thread.start()
            except RuntimeError as error:  # out of threads or memory: the pool goes on untended
                handles.announce(self.sockets.event, "keeper_failed", error=repr(error))
                return
            self.thread = thread

    def stop(self):
        """End the tending and wait for the thread."""
        self.stopping.set()
        with self.sockets.lock:
            thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(TICK_SECONDS + 5)

    def run(self):
        while not self.stopping.wait(TICK_SECONDS):
            with self.sockets.lock:
                batch = list(self.sockets.idle)
                if not batch:
                    self.thread = None
                    return
            for kept in batch:
                try:
                    self.tend(kept)
                except Exception as error:  # a keeper failure is an event, never a failed request or a dead keeper
                    handles.announce(self.sockets.event, "keeper_failed", error=repr(error))

    def tend(self, kept):
        """One idle socket: answered, pinged when due, and out of the pool if dead or too old."""
        if not kept.guard.acquire(False):  # a checkout is testing it
            return
        try:
            if not self.pooled(kept):
                return
            now = clock.monotonic()
            if now - kept.opened >= self.sockets.max_age:
                finding = "keeper-age"
            else:
                due = now - kept.touched >= PING_SECONDS
                if due:
                    kept.touched = now
                finding = None if kept.ws.alive(due) else "keeper-closed"
            if finding is None:
                return
            with self.sockets.lock:
                if kept.out or kept not in self.sockets.idle:
                    return
                del self.sockets.idle[kept]
        finally:
            kept.guard.release()
        self.sockets.close(kept, finding)

    def pooled(self, kept):
        with self.sockets.lock:
            return not kept.out and kept in self.sockets.idle
