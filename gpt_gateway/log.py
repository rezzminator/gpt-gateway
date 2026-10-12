"""The gateway log: one locked writer, a timestamp per line, rotation to `.1` past 5 MiB.

A write that fails is reported on stderr (launchd's stderr log) when the failing starts, never
dropped in silence; the request it belonged to goes on. The file is UTF-8; a character UTF-8 cannot
hold (a lone surrogate from a JSON escape) is written backslash-escaped.
"""
import os
import sys
import threading

from . import clock

MAX_BYTES = 5 * 1024 * 1024


class Log:
    """`write(line)` appends `YYYY-mm-dd HH:MM:SS line`; a path of None discards every line."""

    def __init__(self, path, max_bytes=MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.failing = False

    def write(self, line):
        if self.path is None:
            return
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8", errors="backslashreplace") as f:
                    f.write(clock.stamp("%Y-%m-%d %H:%M:%S ") + line + "\n")
                self.failing = False
            except OSError as e:
                if not self.failing:
                    print("gpt-gateway: cannot write the log %s: %r" % (self.path, e), file=sys.stderr)
                self.failing = True
