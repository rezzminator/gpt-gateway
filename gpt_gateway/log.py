"""The gateway log: unchanged rotating text, plus bounded append-only rows split by level.

Failures name the destination on stderr once per failing streak and never interrupt gateway work.
Level rows contain diagnostics, not request bodies; each is one O_APPEND write of at most 4096 bytes.
"""
from datetime import datetime, timezone
import json
import os
import sys
import threading

from . import clock, env

MAX_BYTES = 5 * 1024 * 1024
PLUGIN_LOG_CAP_BYTES = 20971520
ROW_MAX_BYTES = 4096
LEVEL_FILES = {"error": "errors.jsonl", "warning": "warnings.jsonl", "info": "info.jsonl"}


def record(writer, level, event, msg, where, detail=None):
    """Classify an existing line callback without changing its single-message contract."""
    owner = getattr(writer, "__self__", None)
    if isinstance(owner, Log):
        owner.record(level, event, msg, where, detail)
    else:
        writer(msg)


def level_row(level, event, msg, where, detail=None):
    """One UTC JSON row; oversized detail and then message are truncated, never split writes."""
    row = {"ts": datetime.fromtimestamp(clock.now(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
           "plugin": "gpt-gateway", "level": level, "event": event, "msg": msg, "where": where}
    if detail is not None:
        row["detail"] = detail

    def encoded():
        return (json.dumps(row, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

    data = encoded()
    if len(data) > ROW_MAX_BYTES:
        row["detail"] = {"truncated": True, "bytes": len(data)}
        data = encoded()
        if len(data) > ROW_MAX_BYTES:
            low, high = 0, len(msg)
            while low < high:
                middle = (low + high + 1) // 2
                row["msg"] = msg[:middle]
                if len(encoded()) <= ROW_MAX_BYTES:
                    low = middle
                else:
                    high = middle - 1
            row["msg"] = msg[:low]
            data = encoded()
        if len(data) > ROW_MAX_BYTES:
            raise ValueError("level row metadata exceeds 4096 bytes")
    return data


def _append(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError("short level log write: %d of %d bytes" % (written, len(data)))
    finally:
        os.close(fd)


def _cap(path):
    if os.path.exists(path) and os.path.getsize(path) > PLUGIN_LOG_CAP_BYTES:
        os.replace(path, path + ".1")
        return True
    return False


class Log:
    """`write` keeps the old text; `record` writes that text and its classified level row."""

    def __init__(self, path, max_bytes=MAX_BYTES, environ=None):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.failing = False
        self.level_failing = False
        environ = env.snapshot() if environ is None else environ
        self.level_error = None
        try:
            self.level_dir = env.plugin_log_dir(environ)
        except env.EnvError as error:
            self.level_dir = env.value(environ, "PLUGIN_LOG_DIR") or env.value(environ, "HOME")
            self.level_error = error

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

    def record(self, level, event, msg, where, detail=None):
        """Keep the text line and append the classified row, even when either destination fails."""
        self.write(msg)
        self._level(level, event, msg, where, detail)

    def _level(self, level, event, msg, where, detail=None):
        if self.level_dir is None:
            return
        with self.lock:
            try:
                if self.level_error is not None:
                    raise self.level_error
                data = level_row(level, event, msg, where, detail)
                path = os.path.join(self.level_dir, LEVEL_FILES[level])
                os.makedirs(self.level_dir, mode=0o700, exist_ok=True)
                if _cap(path):
                    warnings = os.path.join(self.level_dir, LEVEL_FILES["warning"])
                    capped = [path]
                    if warnings != path and _cap(warnings):
                        capped.insert(0, warnings)
                    for old in capped:
                        _append(warnings, level_row("warning", "log-capped", "level log moved aside after its safety cap",
                                                    "gpt_gateway/log.py#_level", {"file": os.path.basename(old)}))
                _append(path, data)
                self.level_failing = False
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
                if not self.level_failing:
                    print("gpt-gateway: cannot write the level log directory %s: %r" % (self.level_dir, error), file=sys.stderr)
                self.level_failing = True
