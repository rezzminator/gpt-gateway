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
CHATGPT = "MODEL_GATEWAY_CHATGPT"

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


CHATGPT_ORIGIN = "MODEL_GATEWAY_CHATGPT_ORIGIN"
CHATGPT_MODELS_PATH = "MODEL_GATEWAY_CHATGPT_MODELS_PATH"
CHATGPT_AUTH_ORIGIN = "MODEL_GATEWAY_CHATGPT_AUTH_ORIGIN"
CHATGPT_AUTH_STORE = "MODEL_GATEWAY_CHATGPT_AUTH_STORE"
CHATGPT_AUTH_PATH = "MODEL_GATEWAY_CHATGPT_AUTH_PATH"
CHATGPT_CODEX_AUTH = "MODEL_GATEWAY_CHATGPT_CODEX_AUTH"
CODEX_HOME = "CODEX_HOME"


def chatgpt_origin(environ, name, default):
    origin = value(environ, name) or default
    parts = urlsplit(origin)
    try:
        valid = (parts.scheme in ("http", "https") and parts.hostname and not parts.username
                 and not parts.password and parts.path in ("", "/") and not parts.query
                 and not parts.fragment and (parts.port is None or parts.port > 0))
    except ValueError:
        valid = False
    if not valid:
        raise EnvError("env %s: must be an http or https origin" % name)
    return origin.rstrip("/")


def chatgpt_path(environ, name, default, resolve=True):
    path = value(environ, name) or default
    if path.startswith("~/"):
        home = value(environ, "HOME")
        if not home or not os.path.isabs(home):
            raise EnvError("env HOME: an absolute path is required for ChatGPT storage")
        path = os.path.join(home, path[2:])
    if not os.path.isabs(path):
        raise EnvError("env %s: must be an absolute path" % name)
    return os.path.realpath(path) if resolve else os.path.abspath(path)


class ChatGPTEnv:
    """ChatGPT knobs; paths resolve against the supplied HOME mapping."""

    def __init__(self, auth_origin, auth_path, auth_store, origin="https://chatgpt.com", models_path=None,
                 codex_auth=None, home=None):
        self.auth_origin, self.auth_path, self.auth_store = auth_origin, auth_path, auth_store
        self.origin, self.models_path = origin, models_path
        self.codex_auth, self.home = codex_auth, home

    @classmethod
    def parse(cls, environ):
        auth_origin = chatgpt_origin(environ, CHATGPT_AUTH_ORIGIN, "https://auth.openai.com")
        origin = chatgpt_origin(environ, CHATGPT_ORIGIN, "https://chatgpt.com")
        kind = value(environ, CHATGPT_AUTH_STORE) or "auto"
        if kind not in ("auto", "file", "keychain"):
            raise EnvError("env %s: must be auto, file or keychain" % CHATGPT_AUTH_STORE)
        path = chatgpt_path(environ, CHATGPT_AUTH_PATH, "~/.local/state/model-gateway/chatgpt-auth.json")
        models = chatgpt_path(environ, CHATGPT_MODELS_PATH, "~/.local/state/model-gateway/chatgpt-models.json")
        codex = value(environ, CHATGPT_CODEX_AUTH)
        if codex != "off":
            default = "~/.codex/auth.json"
            if codex is None and value(environ, CODEX_HOME):  # a bad CODEX_HOME is named as itself
                default = os.path.join(chatgpt_path(environ, CODEX_HOME, None, resolve=False), "auth.json")
            codex = chatgpt_path(environ, CHATGPT_CODEX_AUTH, default, resolve=False)
        else:
            codex = None
        return cls(auth_origin, path, kind, origin, models, codex, value(environ, "HOME"))
