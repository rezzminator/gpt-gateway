"""Reachability as data: the class of a failed upstream exchange, runs of failures in a row, and host pauses.

`error_class` names one class per failure, the `error_class` column of a request row. `Runs` turns consecutive failed
upstream exchanges into one `upstream_unreachable_start` event at the RUN_START-th and one
`upstream_unreachable_end` once an answer comes QUIET_SECONDS or more after the last failure, so an outage of any
length is two rows, never one per failure. `Pause` sees the time the host slept grow (`clock.slept`, which no step
of the wall clock moves) and records it as one `host_resumed` event.
"""
import errno
import http.client
import socket
import ssl
import threading

from . import clock, handles

RUN_START = 3  # consecutive failures that open a run
QUIET_SECONDS = 30  # an answer this long after a run's last failure ends it; one sooner leaves it open
PAUSE_SECONDS = 30  # host sleep since the last check that is a pause; less is a doze no connection noticed
UNROUTED = (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN, errno.EADDRNOTAVAIL)


def error_class(error):
    """One of fd, dns, tls, timeout, connect, reset, protocol, other for a failed upstream exchange's `error`; an
    UpstreamError keeps the class it was raised with."""
    if getattr(error, "error_class", None) is not None:
        return error.error_class
    if handles.diagnose(error) is not None:
        return "fd"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(error, ConnectionRefusedError) or (isinstance(error, OSError) and error.errno in UNROUTED):
        return "connect"
    if isinstance(error, (ConnectionError, http.client.IncompleteRead, http.client.ImproperConnectionState)):
        return "reset"  # the connection dropped, or cannot carry the request
    if isinstance(error, http.client.HTTPException):
        return "protocol"
    return "other"


class Runs:
    """Failed exchanges with one origin in a row, recorded as a start and an end event per run."""

    def __init__(self, event, origin):
        self.event, self.origin = event, origin
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        """Forget the streak: its failures by class, the wall time of its first and last, the timer at its last."""
        self.classes, self.first_at, self.last_at, self.last_mono = {}, None, None, None
        self.open, self.answers = False, 0

    def failed(self, cls, error):
        """A failed exchange of class `cls`; `error` names it in a start event."""
        with self.lock:
            self.classes[cls] = self.classes.get(cls, 0) + 1
            self.last_at, self.last_mono = clock.now(), clock.monotonic()
            self.first_at = self.last_at if self.first_at is None else self.first_at
            failures = sum(self.classes.values())
            if self.open or failures < RUN_START:
                return
            self.open, first_at = True, self.first_at
        self.event("upstream_unreachable_start", origin=self.origin, first_at=first_at, failures=failures,
                   error_class=cls, error=error)

    def answered(self):
        """An exchange the origin answered: a streak below RUN_START is forgotten; an open run ends once its last
        failure is QUIET_SECONDS old, else the answer is counted in it."""
        with self.lock:
            if not self.open:
                self.reset()
                return
            if clock.monotonic() - self.last_mono < QUIET_SECONDS:
                self.answers += 1
                return
            end = dict(origin=self.origin, failures=sum(self.classes.values()), answered=self.answers,
                       classes=self.classes, first_at=self.first_at, last_at=self.last_at,
                       seconds=self.last_at - self.first_at)
            self.reset()
        self.event("upstream_unreachable_end", **end)


class Pause:
    """Watches the time the host slept: PAUSE_SECONDS or more of it since the last check is a pause."""

    def __init__(self, event):
        self.event = event
        self.lock = threading.Lock()
        self.seen, self.slept = clock.now(), clock.slept()

    def check(self):
        """Whether the host paused since the last check, recorded as one `host_resumed` event (gap_s, last_seen)."""
        with self.lock:
            now, slept = clock.now(), clock.slept()
            gap, last_seen = slept - self.slept, self.seen
            self.slept, self.seen = slept, now
        if gap < PAUSE_SECONDS:
            return False
        self.event("host_resumed", gap_s=round(gap, 1), last_seen=last_seen)
        return True
