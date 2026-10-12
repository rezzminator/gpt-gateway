"""Loads middleware: one file per module, `{middleware_dir}/{module}.py`, checked against the contract.

A module needs only `NAME`; `SETTINGS`, `check` and the hooks are optional. Its code loads once, at
start (a restart reloads it), under the name `gpt_gateway_mw_{module}`; it never imports the gateway.
"""
import importlib.util
import os
import re
import sys
import threading

LOADER_PREFIX = "gpt_gateway_mw_"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*\Z")
HOOKS = ("on_request", "on_event", "on_response_end", "health", "cli")


class MiddlewareError(Exception):
    """A module that cannot load: its file, its import, or an attribute the contract rejects."""


class Module:
    """One loaded middleware: its file stem `module`, its `NAME`, its settings spec, hooks and error count."""

    __slots__ = ("module", "path", "NAME", "spec", "check", "_errors", "_lock") + HOOKS

    def __init__(self, module, path, name, spec, check, hooks):
        self.module = module
        self.path = path
        self.NAME = name
        self.spec = spec
        self.check = check
        for hook in HOOKS:
            setattr(self, hook, hooks.get(hook))
        self._errors = 0
        self._lock = threading.Lock()

    def failed(self):
        with self._lock:
            self._errors += 1

    @property
    def errors(self):
        return self._errors


def check_spec(module, spec):
    """The module's SETTINGS as {key: (default, validator, env_var_or_None)}, or MiddlewareError."""
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise MiddlewareError("middleware %s: SETTINGS must be a dict, got %s" % (module, type(spec).__name__))
    out = {}
    for key, entry in spec.items():
        if not isinstance(key, str) or not key:
            raise MiddlewareError("middleware %s: SETTINGS key %r must be a non-empty string" % (module, key))
        if not isinstance(entry, (tuple, list)) or len(entry) != 3:
            raise MiddlewareError("middleware %s: SETTINGS[%r] must be (default, validator, env_var_or_None)"
                                  % (module, key))
        default, validator, var = entry
        if not callable(validator):
            raise MiddlewareError("middleware %s: SETTINGS[%r] validator is not callable" % (module, key))
        if var is not None and (not isinstance(var, str) or not var):
            raise MiddlewareError("middleware %s: SETTINGS[%r] env variable must be a non-empty string or None"
                                  % (module, key))
        out[key] = (default, validator, var)
    return out


def load_module(module, directory):
    """Module for `{directory}/{module}.py`, or MiddlewareError naming the module and the cause."""
    path = os.path.join(directory, module + ".py")
    if not os.path.isfile(path):
        raise MiddlewareError("middleware %s: no module file %s" % (module, path))
    loader_name = LOADER_PREFIX + module
    try:
        spec = importlib.util.spec_from_file_location(loader_name, path)
        code = importlib.util.module_from_spec(spec)
        sys.modules[loader_name] = code
        spec.loader.exec_module(code)
    except (Exception, SystemExit) as e:
        sys.modules.pop(loader_name, None)
        raise MiddlewareError("middleware %s: import of %s failed: %r" % (module, path, e))
    name = getattr(code, "NAME", None)
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise MiddlewareError("middleware %s: NAME must be a string matching %s, got %r"
                              % (module, NAME_PATTERN.pattern, name))
    hooks = {}
    for attr in HOOKS + ("check",):
        fn = getattr(code, attr, None)
        if fn is not None and not callable(fn):
            raise MiddlewareError("middleware %s: %s must be callable, got %s" % (module, attr, type(fn).__name__))
        hooks[attr] = fn
    return Module(module, path, name, check_spec(module, getattr(code, "SETTINGS", None)),
                  hooks.pop("check"), hooks)


def load_modules(names, directory):
    """The modules of `names` from `directory`, in order; a duplicate NAME is a MiddlewareError."""
    modules, seen = [], {}
    for module in names:
        m = load_module(module, directory)
        if m.NAME in seen:
            raise MiddlewareError("middleware %s and %s: duplicate NAME %r" % (seen[m.NAME], module, m.NAME))
        seen[m.NAME] = module
        modules.append(m)
    return modules
