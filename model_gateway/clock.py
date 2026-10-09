"""The clock: wall time, local-time stamps, a monotonic timer and sleeping, for every caller."""
import time


def now():
    """Seconds since the epoch."""
    return time.time()


def monotonic():
    """A timer for durations; never goes back."""
    return time.monotonic()


def stamp(fmt, when=None):
    """`when` (default: now) formatted in local time."""
    return time.strftime(fmt, time.localtime(when))


def sleep(seconds):
    time.sleep(seconds)
