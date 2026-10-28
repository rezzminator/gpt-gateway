"""The gateway log: the rotating text log `gpt-gateway.log` and the level rows of the vendored plugin-log library, both
in the plugin log directory.

Failures never interrupt gateway work. A service has no screen: stderr gets only a log's one line per failing streak
(the text log's, the library's, an unusable PLUGIN_LOG_DIR or HOME); every row, errors included, goes to its file.
"""
import os
import sys
import threading

from . import clock, env
from .plugin_log import PluginLog

PLUGIN = "gpt-gateway"
TEXT_NAME = "gpt-gateway.log"
MAX_BYTES = 5 * 1024 * 1024


def text_path(environ):
    """`gpt-gateway.log` in the plugin log directory; None without one, or when PLUGIN_LOG_DIR or HOME is unusable
    (a Log built from the same mapping says so)."""
    try:
        directory = env.plugin_log_dir(environ)
    except env.EnvError:
        return None
    return None if directory is None else os.path.join(directory, TEXT_NAME)


def record(writer, level, event, msg, where, detail=None):
    """Classify an existing line callback without changing its single-message contract."""
    owner = getattr(writer, "__self__", None)
    if isinstance(owner, Log):
        owner.record(level, event, msg, where, detail)
    else:
        writer(msg)


class Log:
    """`write` keeps the old text; `record` writes that text and its classified level row."""

    def __init__(self, path, max_bytes=MAX_BYTES, environ=None):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.failing = False
        environ = env.snapshot() if environ is None else environ
        try:
            directory, self.unusable = env.plugin_log_dir(environ), None
        except env.EnvError as error:
            directory, self.unusable = None, error
        self.said_unusable = False
        self.levels = PluginLog(PLUGIN, directory, now_ms=lambda: round(clock.now() * 1000))

    def write(self, line):
        if self.path is None:
            return
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", errors="backslashreplace",
                          opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
                    f.write(clock.stamp("%Y-%m-%d %H:%M:%S ") + line + "\n")
                self.failing = False
            except OSError as e:
                if not self.failing:
                    msg = "cannot write the log %s: %r" % (self.path, e)
                    print("%s: %s" % (PLUGIN, msg), file=sys.stderr)
                    self._level("error", "text-log-failed", msg, "gpt_gateway/log.py#Log.write")
                self.failing = True

    def record(self, level, event, msg, where, detail=None):
        """Keep the text line and append the classified row, even when either destination fails."""
        self.write(msg)
        self._level(level, event, msg, where, detail)

    def _level(self, level, event, msg, where, detail=None):
        if self.unusable is None:
            self.levels.record(level, event, msg, where, detail=detail)
        elif not self.said_unusable:
            # The library would say "no PLUGIN_LOG_DIR and no HOME"; a variable is set but unusable: said once.
            self.said_unusable = True
            print("%s: plugin log off: %s" % (PLUGIN, self.unusable), file=sys.stderr)
