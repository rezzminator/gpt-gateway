"""The middleware test harness: one middleware file against a request and a recorded response,
in process and with no network, through the gateway's own settings resolution, hook chain,
SSE splitting and request row. A middleware's tests import it from a gpt-gateway checkout:

    sys.path.insert(0, "<gpt-gateway checkout>")
    from gpt_gateway.harness import run, run_cli, load

    out = run("my_mw.py", body={"model": "m", "messages": []},
              response=b'event: message_stop\\ndata: {"type":"message_stop"}\\n\\n',
              settings={"enabled": True}, chunk_size=7)
    out.forwarded   # the bytes that would go upstream, None when a hook answered
    out.changed     # whether they differ from the request's bytes
    out.answer      # the Answer a hook gave (status, content_type, data), or None; when set, the
                    # events and the result are the answer's, relayed in place of `response`
    out.log         # the lines the module wrote through ctx.log, in order
    out.errors      # each hook failure as the database records it: module, hook, error, where
    out.events      # the recorded response's events, in order, as on_event is offered them
    out.result      # the Result on_response_end received
    out.row         # the request's row as the database records it, its times and process left out
    out.note        # the request's note (Result.note); out.health its health entry

`load()` raises SettingsError or MiddlewareError exactly where the gateway's start would fail.
"""
import contextlib
import dataclasses
import io
import json
import os
import sys
from types import MappingProxyType

from . import cli
from .db import Db, cell
from .events import EventSplitter, json_event
from .headers import Headers
from .hooks import Chain, Context, Request, Result, parse_object
from .middleware import load_module
from .proxy import ERROR_HEAD, assemble_note, metered, note_row, relay_row, request_row
from .response import Response
from .settings import SettingsError, middleware_entries, module_settings
from .usage import Meter


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
    answer: object  # the hook's Answer, or None
    row: dict


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
    return module, module_settings(module, values, dict(env or {}), "harness",
                                   lambda text: print("gpt-gateway harness: " + text, file=sys.stderr))


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
        url_path="/v1/messages", request_id=1, route="anthropic"):
    """Run the middleware at `path` over one exchange; an Outcome.

    body: the request body, bytes as sent or an object (sent as `json.dumps(body)`).
    headers: the request headers {name: value}; `x-claude-code-agent-id` makes the kind `sub`.
    response: the recorded response body: SSE bytes, or an object sent as JSON.
    chunk_size: relay the response in chunks of this many bytes (default: one chunk).
    A hook's answer is relayed in place of `response`, as the gateway relays it.
    """
    module, values = load(path, settings, env)
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    if isinstance(response, bytes):
        data, ctype = response, content_type or "text/event-stream"
    else:
        data, ctype = json.dumps(response).encode(), content_type or "application/json"
    resp_headers = tuple(response_headers) + (("content-type", ctype),)
    request_headers = Headers((headers or {}).items())
    kind = "sub" if request_headers.get("x-claude-code-agent-id") else "main"
    per_module = MappingProxyType({module.module: values})
    lines, failures = [], []

    def event(kind, request_id=None, lane=None, **fields):
        if kind == "middleware_failed":
            failures.append(fields)

    chain = Chain((module,), event)
    parsed = parse_object(raw)
    ctx = Context(log=lines.append, log_path=None, settings=per_module, kind=kind, method=method,
                  path=url_path, headers=request_headers, route=route,
                  model=parsed.get("model") if parsed is not None else None, request_id=request_id)
    req = Request(raw, parsed)
    forwarded, notes = chain.run_request(ctx, req)
    note = assemble_note(kind, notes)
    row = request_row(method, url_path, parsed, request_headers, kind, route, request_id)
    note_row(row, notes, req.answered is not None)
    if req.answered is not None:  # relayed in place of the recorded response, as the gateway relays it
        given = req.answered
        answer = Response(given.status, given.data, {"content-type": given.content_type})
        forwarded, data, ctype = None, given.data, given.content_type
        status, reason, resp_headers = answer.status, answer.reason, tuple(answer.getheaders())
    chunks = split(data, chunk_size)
    sink = chain.event_sink(ctx, ctype)
    meter = Meter(ctype if metered(method, url_path, status) else None)
    error_head = b""
    for chunk in chunks:
        if status >= 400 and len(error_head) < ERROR_HEAD:
            error_head += chunk[:ERROR_HEAD - len(error_head)]
        if sink is not None:
            sink.feed(chunk)
        meter.feed(chunk)
    if sink is not None:
        sink.close(True)
    meter.finish()
    relay_row(row, status, True, False, None, error_head, meter)
    result = Result(status, reason, resp_headers, error_head if status >= 400 else b"", True, False, 0.0, note)
    chain.run_response_end(ctx, result)
    health = ", ".join(chain.health_entries(Context(log=lines.append, log_path=None, settings=per_module)))
    return Outcome(forwarded, forwarded is not None and forwarded is not raw, note, tuple(lines), tuple(failures),
                   recorded_events(chunks, ctype.lower()), result, health, values, req.answered,
                   {name: cell(value) for name, value in row.items()})


def run_cli(path, argv, *, settings=None, env=None, log_path=None, db_path=None):
    """(exit status, stdout, stderr) of `gateway.py mw NAME argv...` for the middleware at `path`.

    log_path: what ctx.log_path names. db_path: the database the cli's ctx.log lines and a failure go to, as
    `gateway.py mw` records them (default: none, both dropped).
    """
    module, values = load(path, settings, env)
    db = Db(db_path, lambda line: print(line, file=sys.stderr)) if db_path else None
    event = db.event if db else (lambda kind, request_id=None, lane=None, **fields: None)
    ctx = Context(log=lambda line: event("middleware_log", line=line), log_path=log_path,
                  settings=MappingProxyType({module.module: values}))
    out, err = io.StringIO(), io.StringIO()
    if db:
        db.start()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.invoke(module, list(argv), ctx, event)
    finally:
        if db:
            db.close()
    return code, out.getvalue(), err.getvalue()
