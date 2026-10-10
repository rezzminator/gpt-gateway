"""settings.json in the install dir: the core keys, the middleware list and each module's settings.

Every key is optional and an unknown key is refused. Each value comes from an environment variable
that is set and not empty, else the file, else the default. `Settings.load()` is the strict read at
start; `Settings.current()` re-reads the file whenever its (mtime, size, inode) changes, and a file
it refuses then is logged once while the last good snapshot stays. The middleware list and the
middleware directory change only at a restart.
"""
import dataclasses
import json
import os
import re
import threading
from types import MappingProxyType

from . import env as envmod
from . import middleware

FILE_NAME = "settings.json"
KEYS = ("capture_dir", "middleware", "middleware_dir", "chatgpt", "db_retention_days")
ENTRY_KEYS = ("module", "settings")
MODULE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\Z")
CAPTURE_OFF = ("off", "0")
DEFAULT_RETENTION_DAYS = 30
EMPTY = MappingProxyType({})


class SettingsError(ValueError):
    """A settings value the gateway refuses; the message names the key and where it came from."""


def default_path(install_dir):
    return os.path.join(install_dir, FILE_NAME)


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """One consistent version of the settings; a request uses one snapshot from start to end."""

    capture_dir: object
    middleware_dir: str
    modules: tuple            # module names, in list order
    module_settings: object   # {module: read-only settings}
    sources: object           # {key: "env" | "file" | "default"}
    state: str                # "ok" | "missing" | "unread" | "error(...)"
    chatgpt: bool = False
    db_retention_days: int = DEFAULT_RETENTION_DAYS


def absolute_dir(value, where, key):
    """`value` as an absolute directory path (`~` expanded), or SettingsError."""
    path = os.path.expanduser(value.strip()) if isinstance(value, str) else ""
    if not path or not os.path.isabs(path):
        raise SettingsError("%s: %s must be an absolute path or null, got %s" % (where, key, json.dumps(value)))
    return path


def middleware_entries(value, where):
    """((module, settings), ...) from the `middleware` list, or SettingsError."""
    if not isinstance(value, list):
        raise SettingsError('%s: middleware must be a list of {"module": ..., "settings": {...}}, got %s'
                            % (where, json.dumps(value)))
    out, seen = [], set()
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise SettingsError("%s: middleware[%d] must be an object, got %s" % (where, i, json.dumps(entry)))
        unknown = sorted(set(entry) - set(ENTRY_KEYS))
        if unknown:
            raise SettingsError("%s: middleware[%d]: unknown key(s) %s; known: %s"
                                % (where, i, ", ".join(unknown), ", ".join(ENTRY_KEYS)))
        module = entry.get("module")
        if not isinstance(module, str) or not MODULE_PATTERN.match(module):
            raise SettingsError("%s: middleware[%d]: module must match %s, got %s"
                                % (where, i, MODULE_PATTERN.pattern, json.dumps(module)))
        if module in seen:
            raise SettingsError("%s: middleware[%d]: module %s appears twice" % (where, i, module))
        seen.add(module)
        values = entry.get("settings", {})
        if not isinstance(values, dict):
            raise SettingsError("%s: middleware[%d] (%s): settings must be an object, got %s"
                                % (where, i, module, json.dumps(values)))
        out.append((module, values))
    return tuple(out)


def read_file(path):
    """The file's validated values {key: value}; None when the file does not exist."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise SettingsError("%s: cannot read: %r" % (path, e))
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise SettingsError("%s: not valid JSON: %s" % (path, e))
    if not isinstance(data, dict):
        raise SettingsError("%s: must be a JSON object, got %s" % (path, type(data).__name__))
    unknown = sorted(set(data) - set(KEYS))
    if unknown:
        raise SettingsError("%s: unknown key(s) %s; known: %s" % (path, ", ".join(unknown), ", ".join(KEYS)))
    out = {}
    if "chatgpt" in data:
        if data["chatgpt"] is not None and type(data["chatgpt"]) is not bool:
            raise SettingsError("%s: chatgpt must be a boolean or null" % path)
        out["chatgpt"] = data["chatgpt"]
    if "db_retention_days" in data:
        days = data["db_retention_days"]
        if type(days) is not int or days < 1:
            raise SettingsError("%s: db_retention_days must be a whole number of days >= 1, got %s" % (path, json.dumps(days)))
        out["db_retention_days"] = days
    for key in ("capture_dir", "middleware_dir"):
        if key in data:
            out[key] = None if data[key] is None else absolute_dir(data[key], path, key)
    if "middleware" in data:
        out["middleware"] = middleware_entries(data["middleware"], path)
    return out


def module_settings(module, values, env, where):
    """A module's validated settings, read-only: env over the file's `settings` over the default.

    Every value goes through the module's validator; a ValueError (or any other error) from it, or
    from the module's check(), becomes a SettingsError naming the module, the key and the source.
    """
    unknown = sorted(set(values) - set(module.spec))
    if unknown:
        raise SettingsError("%s: middleware %s: unknown setting(s) %s; known: %s" % (
            where, module.module, ", ".join(unknown), ", ".join(sorted(module.spec)) or "none"))
    out = {}
    for key, (default, validator, var) in module.spec.items():
        raw = envmod.value(env, var) if var else None
        if raw is not None:
            value, source = raw, "env " + var
        elif key in values:
            value, source = values[key], "file " + where
        else:
            value, source = default, "default"
        try:
            out[key] = validator(value)
        except Exception as e:
            raise SettingsError("middleware %s: %s from %s: %s" % (
                module.module, key, source, e if isinstance(e, ValueError) else repr(e)))
    frozen = MappingProxyType(out)
    if module.check is not None:
        try:
            module.check(frozen)
        except Exception as e:
            raise SettingsError("middleware %s: check failed: %s" % (
                module.module, e if isinstance(e, ValueError) else repr(e)))
    return frozen


def describe(snap):
    """capture_dir=-(default) middleware_dir=/dir(default) middleware=[a,b](file): values and sources."""
    s = snap.sources
    return "capture_dir=%s(%s) middleware_dir=%s(%s) middleware=[%s](%s)" % (
        snap.capture_dir or "-", s["capture_dir"], snap.middleware_dir, s["middleware_dir"],
        ",".join(snap.modules), s["middleware"])


class Settings:
    """The effective settings and the loaded middleware they belong to."""

    def __init__(self, path, env, log, event=None):
        self.path = path
        self.env = dict(env)  # read once; validated with the file at load() or a reload
        self.log = log  # the text log: a refused reload
        self.event = event if event is not None else (lambda kind, **fields: None)  # the database
        self.lock = threading.Lock()
        self.key = None    # the file's (mtime_ns, size, inode) at the last read, None when missing
        self.read = False
        self.modules = ()  # the loaded middleware, in list order, fixed until a restart
        self.snapshot = Snapshot(None, self.default_middleware_dir(), (), EMPTY,
                                 MappingProxyType({k: "default" for k in KEYS}), "unread")

    def default_middleware_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), "middleware")

    def core(self, file_values):
        """(capture_dir, middleware_dir, entries, sources, chatgpt, db_retention_days) from env, file and defaults."""
        sources = {}
        raw = envmod.value(self.env, envmod.CAPTURE_DIR)
        if raw is not None:
            capture = None if raw.lower() in CAPTURE_OFF else absolute_dir(raw, "env " + envmod.CAPTURE_DIR,
                                                                            "capture_dir")
            sources["capture_dir"] = "env"
        else:
            capture = file_values.get("capture_dir")
            sources["capture_dir"] = "file" if "capture_dir" in file_values else "default"
        raw = envmod.value(self.env, envmod.MIDDLEWARE_DIR)
        if raw is not None:
            mdir = absolute_dir(raw, "env " + envmod.MIDDLEWARE_DIR, "middleware_dir")
            sources["middleware_dir"] = "env"
        elif file_values.get("middleware_dir") is not None:
            mdir, sources["middleware_dir"] = file_values["middleware_dir"], "file"
        else:
            mdir, sources["middleware_dir"] = self.default_middleware_dir(), "default"
        entries = file_values.get("middleware", ())
        sources["middleware"] = "file" if "middleware" in file_values else "default"
        raw = envmod.value(self.env, envmod.CHATGPT)
        if raw is not None:
            if raw.lower() not in ("1", "true", "on", "0", "false", "off"):
                raise SettingsError("env %s: must be 1|true|on or 0|false|off" % envmod.CHATGPT)
            chatgpt, sources["chatgpt"] = raw.lower() in ("1", "true", "on"), "env"
        else:
            chatgpt = file_values.get("chatgpt") or False
            sources["chatgpt"] = "file" if file_values.get("chatgpt") is not None else "default"
        retention = file_values.get("db_retention_days", DEFAULT_RETENTION_DAYS)
        sources["db_retention_days"] = "file" if "db_retention_days" in file_values else "default"
        return capture, mdir, entries, sources, chatgpt, retention

    def build(self, core, modules, state):
        capture, mdir, entries, sources, chatgpt, retention = core
        per_module = {m.module: module_settings(m, values, self.env, self.path)
                      for m, (_, values) in zip(modules, entries)}
        return Snapshot(capture, mdir, tuple(name for name, _ in entries), MappingProxyType(per_module),
                        MappingProxyType(sources), state, chatgpt, retention)

    def stat_key(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def load(self):
        """Strict read: SettingsError for a bad file or value, MiddlewareError for a module that cannot load."""
        with self.lock:
            try:
                key = self.stat_key()
            except OSError as e:
                raise SettingsError("%s: cannot stat: %r" % (self.path, e))
            file_values = read_file(self.path)
            core = self.core(file_values or {})
            modules = middleware.load_modules([name for name, _ in core[2]], core[1])
            self.snapshot = self.build(core, modules, "missing" if file_values is None else "ok")
            self.modules = tuple(modules)
            self.key, self.read = key, True

    def current(self):
        """The snapshot as of now, re-reading the file first if it changed."""
        try:
            key = self.stat_key()
        except OSError as e:
            key = ("stat-error", repr(e))
        if not self.read or key != self.key:
            with self.lock:
                if not self.read or key != self.key:
                    self.reload(key)
        return self.snapshot

    def reload(self, key):
        self.key, self.read = key, True
        try:
            if isinstance(key, tuple) and key[:1] == ("stat-error",):
                raise SettingsError("%s: cannot stat: %s" % (self.path, key[1]))
            file_values = read_file(self.path)
            core = self.core(file_values or {})
            running = tuple(m.module for m in self.modules)
            listed = tuple(name for name, _ in core[2])
            if listed != running:
                raise SettingsError("%s: middleware list changed; restart to apply (running [%s], file [%s])"
                                    % (self.path, ", ".join(running), ", ".join(listed)))
            if core[1] != self.snapshot.middleware_dir:
                raise SettingsError("%s: middleware_dir changed; restart to apply (running %s, file %s)"
                                    % (self.path, self.snapshot.middleware_dir, core[1]))
            if core[4] != self.snapshot.chatgpt:
                raise SettingsError("%s: chatgpt changed; restart to apply" % self.path)
            snap = self.build(core, self.modules, "missing" if file_values is None else "ok")
        except SettingsError as e:
            self.snapshot = dataclasses.replace(self.snapshot, state="error(%s)" % str(e).replace("\n", " "))
            self.log("settings error, keeping the last good settings (%s): %s" % (describe(self.snapshot), e))
            self.event("settings_error", path=self.path, error=str(e))
            return
        self.snapshot = snap
        self.event("settings_reload", path=self.path, state=snap.state, settings=describe(snap),
                   db_retention_days=snap.db_retention_days)
