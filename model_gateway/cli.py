"""The gateway's commands — serve (the default), check [FILE], mw NAME [ARGS...] — and the usage built from them."""
import os
import re
import sys

from . import env as envmod
from . import server as servermod
from .hooks import Chain, Context
from .log import Log
from .middleware import MiddlewareError
from .runtime import Runtime
from .settings import FILE_NAME, Settings, SettingsError, default_path, describe


def start_error(log, kind, error):
    if log is not None:
        log.write("start failed: %s: %s" % (kind, error))
    print("model-gateway: %s: %s" % (kind, error), file=sys.stderr)
    return 1


def cmd_serve(args, install_dir, environ):
    if args:
        return usage_error()
    try:
        rt = Runtime(environ, default_path(install_dir))
    except envmod.EnvError as e:
        return start_error(None, "env error", e)
    try:
        rt.load()
    except (SettingsError, envmod.EnvError) as e:
        return start_error(rt.log, "settings error", e)
    except MiddlewareError as e:
        return start_error(rt.log, "middleware error", e)
    try:
        server = servermod.bind(rt)
    except OSError as e:
        return start_error(rt.log, "cannot listen on 127.0.0.1:%d" % rt.env.port, repr(e))
    snap = rt.settings.current()
    rt.log.write("start port=%d %s settings=%s file=%s upstream=%s middleware=[%s] python=%s" % (
        rt.env.port, describe(snap), snap.state, rt.settings.path, rt.env.upstream,
        ", ".join(m.NAME for m in rt.settings.modules), sys.version.split()[0]))
    return servermod.serve(rt, server)


def cmd_check(args, install_dir, environ):
    if len(args) > 1:
        return usage_error()
    path = args[0] if args else default_path(install_dir)
    settings = Settings(path, environ, Log(None).write)
    try:
        settings.load()
    except (SettingsError, MiddlewareError) as e:
        print("invalid: %s" % e, file=sys.stderr)
        return 1
    print("ok: %s" % path if settings.snapshot.state == "ok" else "ok: %s missing, defaults apply" % path)
    return 0


def invoke(module, argv, ctx, log):
    """`module.cli(argv, ctx)` as `gateway.py mw` runs it; its int return value (None: 0) is the exit status."""
    if module.cli is None:
        print("model-gateway: middleware %s has no cli" % module.NAME, file=sys.stderr)
        return 2
    ctx._module = module.module
    try:
        code = module.cli(argv, ctx)
    except SystemExit as e:
        code = e.code
        if isinstance(code, str):
            print(code, file=sys.stderr)
            code = 1
    except Exception as e:
        Chain((module,), lambda line: (log.write(line), print(line, file=sys.stderr))).fail(module, "cli", e)
        return 1
    finally:
        ctx._module = None
    if code is None:
        return 0
    if isinstance(code, int):
        return int(code)
    print("model-gateway: middleware %s cli returned %r, not an int" % (module.NAME, code), file=sys.stderr)
    return 1


def cmd_mw(args, install_dir, environ):
    if not args:
        return usage_error()
    try:
        core = envmod.CoreEnv.parse(environ)
    except envmod.EnvError as e:
        print("model-gateway: env error: %s" % e, file=sys.stderr)
        return 1
    log = Log(core.log_path)
    settings = Settings(default_path(install_dir), environ, log.write)
    try:
        settings.load()
    except (SettingsError, MiddlewareError) as e:
        print("model-gateway: settings error: %s" % e, file=sys.stderr)
        return 1
    module = next((m for m in settings.modules if m.NAME == args[0]), None)
    if module is None:
        print("model-gateway: unknown middleware %r; loaded: %s" % (
            args[0], ", ".join(m.NAME for m in settings.modules) or "none"), file=sys.stderr)
        return 2
    ctx = Context(log=log.write, log_path=log.path, settings=settings.snapshot.module_settings)
    return invoke(module, list(args[1:]), ctx, log)


def cmd_chatgpt_login(args, install_dir, environ):
    if args not in ([], ["--device"]):
        return usage_error()
    from .chatgpt import auth, login
    try:
        store = auth.Store(envmod.ChatGPTEnv.parse(environ))
        (login.device_login if args else login.browser_login)(store)
    except (envmod.EnvError, auth.AuthError, OSError) as error:
        return start_error(None, "ChatGPT login failed", error)
    print("ChatGPT: logged in")
    return 0


def cmd_chatgpt_status(args, install_dir, environ):
    if args:
        return usage_error()
    from .chatgpt import auth
    try:
        print(auth.Store(envmod.ChatGPTEnv.parse(environ)).status())
    except (envmod.EnvError, auth.AuthError, OSError) as error:
        return start_error(None, "ChatGPT status failed", error)
    return 0


def cmd_chatgpt_stats(args, install_dir, environ):
    name = None
    try:
        path = envmod.CoreEnv.parse(environ).log_path
        # The default rotation exists only once the log has passed its size limit; a named file is required.
        files = args or [path] + ([path + ".1"] if os.path.exists(path + ".1") else [])
        lanes = {}
        pattern = re.compile(r"    gpt lane=(\S+) slug=\S+ status=\d+ input=(\d+) cached=(\d+) ratio=\S+ attempts=\d+")
        for name in files:
            with open(name, encoding="utf-8") as source:
                for line in source:
                    match = pattern.search(line)
                    if match:
                        key, tokens, cached = match.groups()
                        row = lanes.setdefault(key, [0, 0, 0])
                        row[0] += 1
                        row[1] += int(tokens)
                        row[2] += int(cached)
    except (OSError, UnicodeError, envmod.EnvError) as error:
        print("model-gateway: chatgpt-stats: %s%s" % ((str(name) + ": ") if name else "", error), file=sys.stderr)
        return 1
    if not lanes:
        print("no gpt responses in " + ", ".join(files))
        return 0
    total = [0, 0, 0]
    for key, row in sorted(lanes.items()):
        print("%s responses=%d input=%d cached=%d hit=%.3f" %
              (key, *row, row[2] / row[1] if row[1] else 0))
        total = [a + b for a, b in zip(total, row)]
    print("total responses=%d input=%d cached=%d hit=%.3f" %
          (*total, total[2] / total[1] if total[1] else 0))
    return 0


COMMANDS = {
    "chatgpt-stats": (cmd_chatgpt_stats, "chatgpt-stats [LOG...]", "print the ChatGPT cache hit rate per lane"),
    "chatgpt-login": (cmd_chatgpt_login, "chatgpt-login [--device]", "log in to ChatGPT using a browser or device code"),
    "chatgpt-status": (cmd_chatgpt_status, "chatgpt-status", "show login presence, masked account and token expiry"),
    "serve": (cmd_serve, "serve", "run the gateway (the default when no command is given)"),
    "check": (cmd_check, "check [FILE]",
              "validate a settings file and its middleware (default: %s beside gateway.py)" % FILE_NAME),
    "mw": (cmd_mw, "mw NAME [ARGS...]", "run the command line of the loaded middleware NAME"),
}


def usage():
    return "usage:\n" + "".join("  gateway.py %-18s %s\n" % (synopsis, text) for _, synopsis, text in COMMANDS.values())


def usage_error():
    print(usage(), file=sys.stderr, end="")
    return 2


def main(argv, install_dir, environ=None):
    """Run the command `argv` names (default: serve); the exit status."""
    environ = envmod.snapshot() if environ is None else environ
    name = argv[0] if argv else "serve"
    if name not in COMMANDS:
        return usage_error()
    return COMMANDS[name][0](list(argv[1:]), install_dir, environ)
