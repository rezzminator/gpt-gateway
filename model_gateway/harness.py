"""The middleware test harness: one middleware file against a request and a recorded response,
in process and with no network, through the gateway's own settings resolution, hook chain,
SSE splitting and log-line assembly. A middleware's tests import it from a model-gateway checkout:

    sys.path.insert(0, "<model-gateway checkout>")
    from model_gateway.harness import run, run_cli, load

    out = run("my_mw.py", body={"model": "m", "messages": []},
              response=b'event: message_stop\\ndata: {"type":"message_stop"}\\n\\n',
              settings={"enabled": True}, chunk_size=7)
    out.forwarded   # the bytes that would go upstream
    out.changed     # whether they differ from the request's bytes
    out.log         # the gateway log lines, timestamps cut
    out.errors      # the hook failure lines among them
    out.events      # the recorded response's events, in order, as on_event is offered them
    out.result      # the Result on_response_end received
    out.note        # the request's log note; out.health its health entry

`load()` raises SettingsError or MiddlewareError exactly where the gateway's start would fail.
"""
import contextlib
import dataclasses
import io
import json
import os
import shutil
import tempfile
from types import MappingProxyType

from . import cli
from .events import EventSplitter, json_event
from .headers import Headers
from .hooks import Chain, Context, Request, Result, parse_object
from .log import Log
from .middleware import load_module
from .proxy import ERROR_HEAD, assemble_note, logs_line, request_line, request_meta
from .settings import SettingsError, middleware_entries, module_settings

STAMP = len("YYYY-mm-dd HH:MM:SS ")


@dataclasses.dataclass(frozen=True)
class Outcome:
    forwarded: bytes
    changed: bool
    note: str
    log: tuple
    errors: tuple
    events: tuple
    result: Result
    health: str
    settings: object


def load(path, settings=None, env=None):
    """(Module, its validated settings) for the middleware file at `path`.

    `settings` is the file's `settings` object for the module; `env` the environment it sees.
    """
    path = os.path.abspath(path)
    if not path.endswith(".py"):
        raise SettingsError("harness: %s is not a .py file" % path)
    stem = os.path.basename(path)[:-3]
    ((stem, values),) = middleware_entries([{"module": stem, "settings": {} if settings is None else settings}],
                                           "harness")
    module = load_module(stem, os.path.dirname(path))
    return module, module_settings(module, values, dict(env or {}), "harness")


def split(data, chunk_size):
    step = chunk_size or max(len(data), 1)
    return [data[i:i + step] for i in range(0, len(data), step)]


def recorded_events(chunks, content_type):
    if "text/event-stream" in content_type:
        splitter = EventSplitter()
        events = [e for chunk in chunks for e in splitter.feed(chunk)]
        return tuple(events + splitter.finish())
    if "json" in content_type:
        return (json_event(b"".join(chunks)),)
    return ()


def run(path, *, body, headers=None, response=b"", status=200, reason="OK", content_type=None,
        response_headers=(), chunk_size=None, settings=None, env=None, method="POST",
        url_path="/v1/messages", request_id=1):
    """Run the middleware at `path` over one exchange; an Outcome.

    body: the request body, bytes as sent or an object (sent as `json.dumps(body)`).
    headers: the request headers {name: value}; `x-claude-code-agent-id` makes the kind `sub`.
    response: the recorded response body: SSE bytes, or an object sent as JSON.
    chunk_size: relay the response in chunks of this many bytes (default: one chunk).
    """
    module, values = load(path, settings, env)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    if isinstance(response, bytes):
        data, ctype = response, content_type or "text/event-stream"
    else:
        data, ctype = json.dumps(response).encode(), content_type or "application/json"
    chunks = split(data, chunk_size)
    resp_headers = tuple(response_headers) + (("content-type", ctype),)
    request_headers = Headers((headers or {}).items())
    kind = "sub" if request_headers.get("x-claude-code-agent-id") else "main"
    per_module = MappingProxyType({module.module: values})
    tmp = tempfile.mkdtemp(prefix="model-gateway-harness-")
    try:
        log = Log(os.path.join(tmp, "model-gateway.log"))
        chain = Chain((module,), log.write)
        parsed = parse_object(raw)
        meta = request_meta(parsed)
        ctx = Context(log=log.write, log_path=log.path, settings=per_module, kind=kind, method=method,
                      path=url_path, headers=request_headers,
                      model=parsed.get("model") if parsed is not None else None, request_id=request_id)
        forwarded, notes = chain.run_request(ctx, Request(raw, parsed))
        note = assemble_note(kind, notes)
        sink = chain.event_sink(ctx, ctype)
        error_head = b""
        for chunk in chunks:
            if status >= 400 and len(error_head) < ERROR_HEAD:
                error_head += chunk[:ERROR_HEAD - len(error_head)]
            if sink is not None:
                sink.feed(chunk)
        if sink is not None:
            sink.close(True)
        if logs_line(method, url_path, status):
            log.write(request_line(method, url_path, status, note, meta, resp_headers, error_head))
        result = Result(status, reason, resp_headers, error_head if status >= 400 else b"", True, False, 0.0, note)
        chain.run_response_end(ctx, result)
        health = ", ".join(chain.health_entries(Context(log=log.write, log_path=log.path, settings=per_module)))
        lines = []
        if os.path.exists(log.path):
            with open(log.path, encoding="utf-8") as f:
                lines = [line[STAMP:].rstrip("\n") for line in f]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    errors = tuple(line for line in lines if line.startswith("middleware ") and " failed: " in line)
    return Outcome(forwarded, forwarded is not raw, note, tuple(lines), errors,
                   recorded_events(chunks, ctype.lower()), result, health, values)


def run_cli(path, argv, *, settings=None, env=None, log_path=None):
    """(exit status, stdout, stderr) of `gateway.py mw NAME argv...` for the middleware at `path`.

    log_path: the gateway log the cli reads and writes through ctx (default: none, writes dropped).
    """
    module, values = load(path, settings, env)
    log = Log(log_path)
    ctx = Context(log=log.write, log_path=log_path, settings=MappingProxyType({module.module: values}))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.invoke(module, list(argv), ctx, log)
    return code, out.getvalue(), err.getvalue()
