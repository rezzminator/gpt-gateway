"""The clock: wall time, local-time stamps, a monotonic timer, the time the host slept and sleeping, for every caller."""
import time


def now():
    """Seconds since the epoch."""
    return time.time()


def monotonic():
    """A timer for durations; never goes back."""
    return time.monotonic()


# Two clocks whose difference grows only while the host sleeps: on macOS the raw monotonic clock runs on through a
# sleep where the uptime clock stops; on Linux the boot clock runs on through a suspend where the monotonic one stops.
if hasattr(time, "CLOCK_UPTIME_RAW"):
    _ASLEEP = (time.CLOCK_MONOTONIC_RAW, time.CLOCK_UPTIME_RAW)
elif hasattr(time, "CLOCK_BOOTTIME"):
    _ASLEEP = (time.CLOCK_BOOTTIME, time.CLOCK_MONOTONIC)
else:
    _ASLEEP = None


def slept():
    """Seconds the host has slept since it booted, which no step of the wall clock moves; on a platform without such
    clocks, the wall clock's gain on the monotonic timer, which a step of the wall clock moves too."""
    if _ASLEEP is None:
        return now() - monotonic()
    running, awake = _ASLEEP
    return time.clock_gettime(running) - time.clock_gettime(awake)


def milliseconds(seconds):
    """A duration in whole milliseconds."""
    return int(round(seconds * 1000))


def stamp(fmt, when=None):
    """`when` (default: now) formatted in local time."""
    return time.strftime(fmt, time.localtime(when))


def sleep(seconds):
    time.sleep(seconds)
