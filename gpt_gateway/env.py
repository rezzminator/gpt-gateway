"""Every environment read of gpt-gateway, and the core knobs parsed from it.

`snapshot()` is the only reader of the process environment; every other module receives that
mapping (or a test's own) and reads it through `value()`.
"""
import os
import sys
from urllib.parse import urlsplit

PORT = "GPT_GATEWAY_PORT"
UPSTREAM = "GPT_GATEWAY_UPSTREAM"
LOG = "GPT_GATEWAY_LOG"
DB = "GPT_GATEWAY_DB"
CAPTURE_DIR = "GPT_GATEWAY_CAPTURE_DIR"
DRAIN_SECONDS = "GPT_GATEWAY_DRAIN_SECONDS"
DEBUG = "GPT_GATEWAY_DEBUG"
LISTENER_FD = "GPT_GATEWAY_LISTENER_FD"
MIDDLEWARE_DIR = "GPT_GATEWAY_MIDDLEWARE_DIR"
CHATGPT = "GPT_GATEWAY_CHATGPT"

DEFAULT_PORT = 18787
DEFAULT_UPSTREAM = "https://api.anthropic.com"
DEFAULT_LOG = "~/Library/Logs/gpt-gateway.log"
DEFAULT_DB = "~/.local/state/gpt-gateway/gateway.db"
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


def under_home(environ, path, needed_for):
    """`path` with a leading `~/` resolved against the mapping's HOME, never the process's own."""
    if path.startswith("~/"):
        home = value(environ, "HOME")
        if not home or not os.path.isabs(home):
            raise EnvError("env HOME: an absolute path is required for %s" % needed_for)
        path = os.path.join(home, path[2:])
    return path


class CoreEnv:
    """The knobs the environment alone sets: port, upstream, log, database, drain limit, debug."""

    __slots__ = ("port", "upstream", "log_path", "db_path", "drain_seconds", "debug", "listener_fd")

    def __init__(self, port, upstream, log_path, db_path, drain_seconds, debug, listener_fd=None):
        self.port = port
        self.upstream = upstream
        self.log_path = log_path
        self.db_path = db_path
        self.drain_seconds = drain_seconds
        self.debug = debug
        self.listener_fd = listener_fd

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
        default_log = '~/.local/state/gpt-gateway/gpt-gateway.log' if sys.platform == 'linux' else DEFAULT_LOG
        log_path = under_home(env, value(env, LOG) or default_log, "the log, or set %s" % LOG)
        if not os.path.isabs(log_path):
            raise EnvError("env %s: must be an absolute path, got %r" % (LOG, log_path))
        raw = value(env, DRAIN_SECONDS)
        drain = DEFAULT_DRAIN_SECONDS
        if raw is not None:
            try:
                drain = float(raw)
            except ValueError:
                drain = -1.0
            if not drain >= 0:
                raise EnvError("env %s: must be a number of seconds >= 0, got %r" % (DRAIN_SECONDS, raw))
        db_path = under_home(env, value(env, DB) or DEFAULT_DB, "the database, or set %s" % DB)
        if not os.path.isabs(db_path):
            raise EnvError("env %s: must be an absolute path, got %r" % (DB, db_path))
        raw = value(env, LISTENER_FD)
        listener_fd = None
        if raw is not None:
            if not raw.isascii() or not raw.isdigit() or int(raw) < 3:
                raise EnvError("env %s: must be a listening descriptor >= 3" % LISTENER_FD)
            listener_fd = int(raw)
        return cls(port, upstream, log_path, db_path, drain, value(env, DEBUG) is not None, listener_fd)


CHATGPT_ORIGIN = "GPT_GATEWAY_CHATGPT_ORIGIN"
CHATGPT_MODELS_PATH = "GPT_GATEWAY_CHATGPT_MODELS_PATH"
CHATGPT_AUTH_ORIGIN = "GPT_GATEWAY_CHATGPT_AUTH_ORIGIN"
CHATGPT_AUTH_STORE = "GPT_GATEWAY_CHATGPT_AUTH_STORE"
CHATGPT_AUTH_PATH = "GPT_GATEWAY_CHATGPT_AUTH_PATH"
CHATGPT_CODEX_AUTH = "GPT_GATEWAY_CHATGPT_CODEX_AUTH"
CHATGPT_TRANSPORT = "GPT_GATEWAY_CHATGPT_TRANSPORT"
CHATGPT_AGENT_CONTRACT = "GPT_GATEWAY_CHATGPT_AGENT_CONTRACT"
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
    path = under_home(environ, value(environ, name) or default, "ChatGPT storage")
    if not os.path.isabs(path):
        raise EnvError("env %s: must be an absolute path" % name)
    return os.path.realpath(path) if resolve else os.path.abspath(path)


class ChatGPTEnv:
    """ChatGPT knobs; paths resolve against the supplied HOME mapping."""

    def __init__(self, auth_origin, auth_path, auth_store, origin="https://chatgpt.com", models_path=None,
                 codex_auth=None, home=None, transport="websocket", agent_contract=True):
        self.auth_origin, self.auth_path, self.auth_store = auth_origin, auth_path, auth_store
        self.origin, self.models_path = origin, models_path
        self.codex_auth, self.home = codex_auth, home
        self.transport, self.agent_contract = transport, agent_contract

    @classmethod
    def parse(cls, environ):
        auth_origin = chatgpt_origin(environ, CHATGPT_AUTH_ORIGIN, "https://auth.openai.com")
        origin = chatgpt_origin(environ, CHATGPT_ORIGIN, "https://chatgpt.com")
        kind = value(environ, CHATGPT_AUTH_STORE) or "auto"
        if kind not in ("auto", "file", "keychain"):
            raise EnvError("env %s: must be auto, file or keychain" % CHATGPT_AUTH_STORE)
        path = chatgpt_path(environ, CHATGPT_AUTH_PATH, "~/.local/state/gpt-gateway/chatgpt-auth.json")
        models = chatgpt_path(environ, CHATGPT_MODELS_PATH, "~/.local/state/gpt-gateway/chatgpt-models.json")
        codex = value(environ, CHATGPT_CODEX_AUTH)
        if codex != "off":
            default = "~/.codex/auth.json"
            if codex is None and value(environ, CODEX_HOME):  # a bad CODEX_HOME is named as itself
                default = os.path.join(chatgpt_path(environ, CODEX_HOME, None, resolve=False), "auth.json")
            codex = chatgpt_path(environ, CHATGPT_CODEX_AUTH, default, resolve=False)
        else:
            codex = None
        transport = value(environ, CHATGPT_TRANSPORT) or "websocket"
        if transport not in ("websocket", "http"):
            raise EnvError("env %s: must be websocket or http" % CHATGPT_TRANSPORT)
        contract = value(environ, CHATGPT_AGENT_CONTRACT) or "on"
        if contract not in ("on", "off"):
            raise EnvError("env %s: must be on or off" % CHATGPT_AGENT_CONTRACT)
        return cls(auth_origin, path, kind, origin, models, codex, value(environ, "HOME"), transport, contract == "on")
