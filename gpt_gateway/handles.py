"""File handles: the soft open-file limit raised at start, the open count, and an out-of-handles failure named as that.

A process out of descriptors fails in ways that read as other faults: macOS answers a name lookup with EAI_NONAME when
the resolver cannot open its socket, so `diagnose` reads a failure against the open count before anyone calls it DNS.
`announce` records such a failure as an event at most once per EVENT_SECONDS, the ones held back counted in the next.
"""
import errno
import os
import resource
import socket
import threading

from . import clock
from .log import record

TARGET = 10240  # the soft limit a start asks for, or the hard limit when that is lower
NEAR = 16  # a name lookup that fails within this many handles of the limit failed for want of one
EVENT_SECONDS = 30  # one fd_exhausted event per this many seconds, per kind
DESCRIPTORS = "/dev/fd"  # one entry per open descriptor, on macOS and Linux


class Shortage:
    """A failure that is the process running out of handles: the client-facing `message` and the counts."""

    def __init__(self, open_, limit):
        self.open, self.limit = open_, limit
        self.message = "out of file handles: %s open of %s allowed" % (
            "?" if open_ is None else open_, "?" if limit is None else limit)


class Throttle:
    """At most one report per EVENT_SECONDS per kind; `allow` says how many were held back since the last one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last, self.held = {}, {}

    def allow(self, kind):
        """The count held back since the kind's last report when this one may go, else None."""
        now = clock.monotonic()
        with self.lock:
            if kind in self.last and now - self.last[kind] < EVENT_SECONDS:
                self.held[kind] = self.held.get(kind, 0) + 1
                return None
            self.last[kind] = now
            return self.held.pop(kind, 0)

    def clear(self):
        with self.lock:
            self.last.clear()
            self.held.clear()


THROTTLE = Throttle()


def soft_limit():
    """The soft open-file limit, None when unlimited."""
    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    return None if soft == resource.RLIM_INFINITY else soft


def open_count():
    """How many descriptors this process has open, None when they cannot be listed; the soft limit when the listing
    itself finds none left, since it needs one of its own."""
    try:
        return len(os.listdir(DESCRIPTORS)) - 1  # the listing's own descriptor
    except OSError as error:
        return soft_limit() if error.errno == errno.EMFILE else None


def raise_limit(event, log):
    """Raise the soft limit to min(TARGET, hard limit) unless it is there already; the soft limit after. A refusal
    is an `fd_limit` event and a log line, never fatal."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = TARGET if hard == resource.RLIM_INFINITY else min(TARGET, hard)
    if soft == resource.RLIM_INFINITY or soft >= target:
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError) as error:
        event("fd_limit", soft=soft, hard=None if hard == resource.RLIM_INFINITY else hard, target=target,
              error=repr(error))
        record(log, "warning", "fd-limit-unraised", "start: cannot raise the open-file limit from %d to %d: %r" % (soft, target, error), "gpt_gateway/handles.py#raise_limit")
        return soft
    record(log, "info", "fd-limit-raised", "start: open-file limit raised from %d to %d" % (soft, target), "gpt_gateway/handles.py#raise_limit")
    return target


def diagnose(error):
    """A Shortage when `error` is the process running out of handles: EMFILE or ENFILE, or a name lookup that
    failed with the open count at the limit; else None."""
    if isinstance(error, OSError) and error.errno in (errno.EMFILE, errno.ENFILE):
        return Shortage(open_count(), soft_limit())
    if isinstance(error, socket.gaierror) and error.errno == socket.EAI_NONAME:
        used, limit = open_count(), soft_limit()
        if used is not None and limit is not None and used >= limit - NEAR:
            return Shortage(used, limit)
    return None


def announce(event, kind, **fields):
    """Record `kind` as an event unless one went within EVENT_SECONDS; `suppressed` counts those held back."""
    held = THROTTLE.allow(kind)
    if held is not None:
        event(kind, suppressed=held, **fields)
