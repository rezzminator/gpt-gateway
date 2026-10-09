"""Every environment read of model-gateway, and the core knobs parsed from it.

`snapshot()` is the only reader of the process environment; every other module receives that
mapping (or a test's own) and reads it through `value()`.
"""
import os
from urllib.parse import urlsplit

PORT = "MODEL_GATEWAY_PORT"
UPSTREAM = "MODEL_GATEWAY_UPSTREAM"
LOG = "MODEL_GATEWAY_LOG"
CAPTURE_DIR = "MODEL_GATEWAY_CAPTURE_DIR"
DRAIN_SECONDS = "MODEL_GATEWAY_DRAIN_SECONDS"
DEBUG = "MODEL_GATEWAY_DEBUG"
MIDDLEWARE_DIR = "MODEL_GATEWAY_MIDDLEWARE_DIR"

DEFAULT_PORT = 18787
DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_LOG = "~/Library/Logs/model-gateway.log"
DEFAULT_DRAIN_SECONDS = 600.0


class EnvError(ValueError):
    """An environment variable holds a value the gateway cannot use; the message names it."""


def snapshot():
    """A copy of the process environment, taken once."""
    return dict(os.environ)


def value(env, name):
    """The variable's value with surrounding whitespace removed, or None when it is unset or empty."""
    raw = env.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


class CoreEnv:
    """The knobs the environment alone sets: port, upstream, log, drain limit, debug."""

    __slots__ = ("port", "upstream", "log_path", "drain_seconds", "debug")

    def __init__(self, port, upstream, log_path, drain_seconds, debug):
        self.port = port
        self.upstream = upstream
        self.log_path = log_path
        self.drain_seconds = drain_seconds
        self.debug = debug

    @classmethod
    def parse(cls, env):
        """CoreEnv from a mapping; EnvError names the first variable it cannot use."""
        raw = value(env, PORT)
        port = DEFAULT_PORT
        if raw is not None:
            if not (raw.isascii() and raw.isdigit()) or not 0 < int(raw) < 65536:
                raise EnvError("env %s: must be a port number 1-65535, got %r" % (PORT, raw))
            port = int(raw)
        upstream = value(env, UPSTREAM) or DEFAULT_UPSTREAM
        parts = urlsplit(upstream)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise EnvError("env %s: must be an http or https origin, got %r" % (UPSTREAM, upstream))
        try:
            parts.port
        except ValueError as e:
            raise EnvError("env %s: bad port in %r: %s" % (UPSTREAM, upstream, e))
        log_path = os.path.expanduser(value(env, LOG) or DEFAULT_LOG)
        raw = value(env, DRAIN_SECONDS)
        drain = DEFAULT_DRAIN_SECONDS
        if raw is not None:
            try:
                drain = float(raw)
            except ValueError:
                drain = -1.0
            if not drain >= 0:
                raise EnvError("env %s: must be a number of seconds >= 0, got %r" % (DRAIN_SECONDS, raw))
        return cls(port, upstream, log_path, drain, value(env, DEBUG) is not None)
