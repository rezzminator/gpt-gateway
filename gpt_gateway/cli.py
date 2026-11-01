"""The gateway's commands — serve (the default), check [FILE], mw NAME [ARGS...] — and the usage built from them."""
import hashlib
import os
import re
import sys

from . import clock
from . import db as dbmod
from . import env as envmod
from . import handles
from . import server as servermod
from .hooks import Chain, Context
from .log import Log, text_path
from .middleware import MiddlewareError
from .runtime import Runtime
from .settings import FILE_NAME, Settings, SettingsError, default_path, describe


def start_error(log, kind, error):
    if log is not None:
        log.record("error", "start-failed", "start failed: %s: %s" % (kind, error), "gpt_gateway/cli.py#start_error")
    print("gpt-gateway: %s: %s" % (kind, error), file=sys.stderr)
    return 1


def cmd_serve(args, install_dir, environ):
    if args:
        return usage_error()
    try:
        rt = Runtime(environ, default_path(install_dir))
    except envmod.EnvError as e:
        return start_error(Log(None, environ=environ), "env error", e)
    try:
        rt.load()
    except (SettingsError, envmod.EnvError) as e:
        return start_error(rt.log, "settings error", e)
    except MiddlewareError as e:
        return start_error(rt.log, "middleware error", e)
    handles.raise_limit(rt.db.event, rt.log.write)  # before the first socket: the listener, then every client and upstream
    try:
        server = servermod.bind(rt)
    except OSError as e:
        return start_error(rt.log, "cannot listen on 127.0.0.1:%d" % rt.env.port, repr(e))
    snap = rt.settings.current()
    names, python = [m.NAME for m in rt.settings.modules], sys.version.split()[0]
    code = build(os.path.dirname(os.path.abspath(__file__)))
    rt.log.record("info", "started", "start port=%d %s settings=%s file=%s upstream=%s middleware=[%s] python=%s db=%s build=%s" % (
        rt.env.port, describe(snap), snap.state, rt.settings.path, rt.env.upstream, ", ".join(names), python,
        rt.db.path, code), "gpt_gateway/cli.py#cmd_serve")
    rt.db.event("start", port=rt.env.port, settings=describe(snap), state=snap.state, file=rt.settings.path,
                upstream=rt.env.upstream, middleware=names, python=python, build=code)
    return servermod.serve(rt, server)


def cmd_check(args, install_dir, environ):
    if len(args) > 1:
        return usage_error()
    path = args[0] if args else default_path(install_dir)
    log = Log(None, environ=environ)
    settings = Settings(path, environ, log.write)
    try:
        settings.load()
    except (SettingsError, MiddlewareError) as e:
        log.record("error", "check-failed", "invalid: %s" % e, "gpt_gateway/cli.py#cmd_check")
        print("invalid: %s" % e, file=sys.stderr)
        return 1
    print("ok: %s" % path if settings.snapshot.state == "ok" else "ok: %s missing, defaults apply" % path)
    return 0


def invoke(module, argv, ctx, event, log=None):
    """`module.cli(argv, ctx)` as `gateway.py mw` runs it; its int return value (None: 0) is the exit status.
    A cli that raises is a `middleware_failed` event, its line printed on stderr too."""
    note = log.record if log is not None else (lambda *row: None)  # the harness keeps no log
    if module.cli is None:
        note("error", "middleware-cli-unavailable", "middleware %s has no cli" % module.NAME, "gpt_gateway/cli.py#invoke")
        print("gpt-gateway: middleware %s has no cli" % module.NAME, file=sys.stderr)
        return 2
    ctx._module = module.module
    try:
        code = module.cli(argv, ctx)
    except SystemExit as e:
        code = e.code
        if isinstance(code, str):
            note("error", "middleware-cli-exit", code, "gpt_gateway/cli.py#invoke")
            print(code, file=sys.stderr)
            code = 1
    except Exception as e:
        def failed(kind, request_id=None, lane=None, **fields):
            event(kind, request_id=request_id, lane=lane, **fields)
            note("error", "middleware-cli-failed", "middleware %s cli failed: %s" % (module.NAME, fields["error"]), "gpt_gateway/cli.py#invoke")
            print("middleware %s cli failed: %s%s" % (module.NAME, fields["error"],
                                                       " at " + fields["where"] if fields["where"] else ""),
                  file=sys.stderr)
        Chain((module,), failed).fail(module, "cli", e)
        return 1
    finally:
        ctx._module = None
    if code is None:
        return 0
    if isinstance(code, int):
        return int(code)
    note("error", "middleware-cli-invalid-result", "middleware %s cli returned %r, not an int" % (module.NAME, code), "gpt_gateway/cli.py#invoke")
    print("gpt-gateway: middleware %s cli returned %r, not an int" % (module.NAME, code), file=sys.stderr)
    return 1


def cmd_mw(args, install_dir, environ):
    diagnostics = Log(None, environ=environ)
    if not args:
        return usage_error()
    try:
        core = envmod.CoreEnv.parse(environ)
    except envmod.EnvError as e:
        diagnostics.record("error", "env-invalid", "env error: %s" % e, "gpt_gateway/cli.py#cmd_mw")
        print("gpt-gateway: env error: %s" % e, file=sys.stderr)
        return 1
    log = Log(text_path(environ), environ=environ)
    settings = Settings(default_path(install_dir), environ, log.write)
    try:
        settings.load()
    except (SettingsError, MiddlewareError) as e:
        diagnostics.record("error", "settings-invalid", "settings error: %s" % e, "gpt_gateway/cli.py#cmd_mw")
        print("gpt-gateway: settings error: %s" % e, file=sys.stderr)
        return 1
    module = next((m for m in settings.modules if m.NAME == args[0]), None)
    if module is None:
        diagnostics.record("error", "middleware-unknown", "unknown middleware %r" % args[0], "gpt_gateway/cli.py#cmd_mw")
        print("gpt-gateway: unknown middleware %r; loaded: %s" % (
            args[0], ", ".join(m.NAME for m in settings.modules) or "none"), file=sys.stderr)
        return 2
    db = dbmod.Db(core.db_path, log.write, lambda: settings.snapshot.db_retention_days)
    db.start()
    try:
        ctx = Context(log=lambda line: db.event("middleware_log", line=line), log_path=log.path,
                      settings=settings.snapshot.module_settings)
        return invoke(module, list(args[1:]), ctx, db.event, diagnostics)
    finally:
        db.close()


def cmd_chatgpt_login(args, install_dir, environ):
    if args not in ([], ["--device"]):
        return usage_error()
    from .chatgpt import auth, login
    try:
        knobs = envmod.ChatGPTEnv.parse(environ)
        selected = auth.source(knobs)
        if selected.codex.status() is not None:
            print("ChatGPT: a Codex login at %s is used first; this login serves only without it" %
                  selected.codex.display_path)
        store = auth.Store(knobs)
        (login.device_login if args else login.browser_login)(store)
    except (envmod.EnvError, auth.AuthError, OSError) as error:
        return start_error(Log(None, environ=environ), "ChatGPT login failed", error)
    print("ChatGPT: logged in")
    return 0


def cmd_chatgpt_status(args, install_dir, environ):
    if args:
        return usage_error()
    from .chatgpt import auth
    try:
        print(auth.source(envmod.ChatGPTEnv.parse(environ)).status())
    except (envmod.EnvError, auth.AuthError, OSError) as error:
        return start_error(Log(None, environ=environ), "ChatGPT status failed", error)
    return 0


def percentile(values, percent):
    """The nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(1, -(-len(ordered) * percent // 100)) - 1]


def build(directory):
    """The code a gateway runs, named: 12 hex digits of a SHA-256 over each .py file under `directory`, its path
    relative to it and its bytes, in path order; the same sources give the same name on any install."""
    digest, found = hashlib.sha256(), []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        found += [os.path.relpath(os.path.join(root, name), directory) for name in files if name.endswith(".py")]
    try:
        for relative in sorted(name.replace(os.sep, "/") for name in found):
            with open(os.path.join(directory, relative), "rb") as f:
                data = f.read()
            digest.update(b"%d:%s%d:" % (len(relative), relative.encode(), len(data)) + data)
    except OSError as error:
        return "unreadable: %r" % error
    return digest.hexdigest()[:12]


WINDOW = re.compile(r"^([1-9][0-9]*)([mhd])$")
WINDOW_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def cmd_chatgpt_stats(args, install_dir, environ):
    match = WINDOW.match(args[1]) if len(args) == 2 and args[0] == "--since" else None
    if args and match is None:
        return usage_error()
    since = clock.now() - int(match.group(1)) * WINDOW_SECONDS[match.group(2)] if match else 0.0
    try:
        path = envmod.CoreEnv.parse(environ).db_path
        responses = dbmod.chatgpt_responses(path, since)
        claude = dbmod.anthropic_timings(path, since)
    except (dbmod.DbError, envmod.EnvError) as error:
        Log(None, environ=environ).record("error", "stats-unreadable", "chatgpt-stats: %s" % error, "gpt_gateway/cli.py#cmd_chatgpt_stats")
        print("gpt-gateway: chatgpt-stats: %s" % error, file=sys.stderr)
        return 1
    try:  # a file no gateway of this version has written to yet lacks the transport columns; the rest still reads
        transport, unread = dbmod.anthropic_transport(path, since), None
    except dbmod.DbError as error:
        transport, unread = [], error
    lanes, timed = {}, {}
    for response in responses:
        row = lanes.setdefault(response["lane"], [0, 0, 0])
        row[0] += 1
        row[1] += response["input_tokens"]
        row[2] += response["cache_read_tokens"] or 0
        if response["ttfb_ms"] is not None and response["total_ms"] is not None:
            ttfb, whole = timed.setdefault(response["slug"], ([], []))
            ttfb.append(response["ttfb_ms"])
            whole.append(response["total_ms"])
    if not lanes:
        print("no gpt responses in %s%s" % (path, " in the last " + args[1] if match else ""))
    else:
        total = [0, 0, 0]
        for key, row in sorted(lanes.items()):
            print("%s responses=%d input=%d cached=%d hit=%.3f" %
                  (key, *row, row[2] / row[1] if row[1] else 0))
            total = [a + b for a, b in zip(total, row)]
        print("total responses=%d input=%d cached=%d hit=%.3f" %
              (*total, total[2] / total[1] if total[1] else 0))
        for slug, (ttfb, whole) in sorted(timed.items()):
            print("slug=%s timed=%d ttfb_ms p50=%d p95=%d total_ms p50=%d p95=%d" %
                  (slug, len(ttfb), percentile(ttfb, 50), percentile(ttfb, 95), percentile(whole, 50),
                   percentile(whole, 95)))
    models = {}
    for row in claude:
        for column, values in zip(("ttfb_ms", "upstream_ms", "total_ms"), models.setdefault(row["model"] or "-", ([], [], []))):
            values.append(row[column])
    for model, columns in sorted(models.items()):
        print("anthropic model=%s timed=%d %s" % (model, len(columns[0]), " ".join(
            "%s p50=%d p95=%d" % (name, percentile(values, 50), percentile(values, 95))
            for name, values in zip(("ttfb_ms", "upstream_ms", "total_ms"), columns))))
    if transport:
        classes = {}
        for row in transport:
            if row["error_class"] is not None:
                classes[row["error_class"]] = classes.get(row["error_class"], 0) + 1
        print("anthropic transport sent=%d reused=%d retried=%d failed=%d classes=%s" % (
            len(transport), sum(bool(row["reused"]) for row in transport), sum(row["attempts"] == 2 for row in transport),
            sum(classes.values()), ",".join("%s:%d" % pair for pair in sorted(classes.items())) or "-"))
    if unread is not None:
        print("anthropic transport unread: %s" % unread)
    return 0


COMMANDS = {
    "chatgpt-stats": (cmd_chatgpt_stats, "chatgpt-stats [--since 30m|24h|7d]",
                      "print the ChatGPT cache hit rate per lane and response times per slug, from the database"),
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
