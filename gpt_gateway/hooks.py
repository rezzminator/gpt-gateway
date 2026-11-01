"""The per-request half of the middleware contract: Context, Request, Result and the hook chain.

Hooks run in list order. A hook that raises is recorded as a `middleware_failed` event (its NAME, the
hook, `{exc!r}` and its traceback's last frame), and its module's error count goes up. A failed `on_request` (an
edit that does not serialize included) costs only its own edits and note: the body goes back to what the modules
before it left, and the later modules run; a note that is no string costs only that note. A failed `on_event` or
`on_response_end` is skipped. No hook breaks a request or cuts a stream. A hook that changes `body["model"]` has
failed: routing is the core's job.
An `on_request` hook may answer the request itself (`req.answer`): the later modules' `on_request` is
skipped, nothing goes upstream on either route, and the core relays the answer as it relays any response.
"""
import dataclasses
import json
import os
import traceback
from types import MappingProxyType

from .events import EventSplitter, json_event

ROUTE = "anthropic"
MAX_JSON_EVENT = 64 * 1024 * 1024
EMPTY = MappingProxyType({})
_MISSING = object()


def _field(name):
    return property(lambda self: self._fields[name])


class Context:
    """One object per request, shared by its hooks; read-only.

    `settings` and `state` belong to the module whose hook is running: its validated settings
    snapshot (the same for the whole request) and a dict it may keep from one hook to the next.
    """

    __slots__ = ("_fields", "_settings", "_states", "_module")

    kind = _field("kind")
    route = _field("route")
    method = _field("method")
    path = _field("path")
    headers = _field("headers")
    model = _field("model")
    request_id = _field("request_id")
    capture_dir = _field("capture_dir")
    log = _field("log")
    log_path = _field("log_path")

    def __init__(self, *, log, log_path, settings, kind=None, method=None, path=None, headers=None,
                 model=None, request_id=None, capture_dir=None, route=ROUTE):
        self._fields = {"kind": kind, "route": route, "method": method, "path": path,
                        "headers": headers if headers is not None else MappingProxyType({}),
                        "model": model, "request_id": request_id, "capture_dir": capture_dir,
                        "log": log, "log_path": log_path}
        self._settings = settings
        self._states = {}
        self._module = None

    @property
    def settings(self):
        return self._settings.get(self._module, EMPTY)

    @property
    def state(self):
        return self._states.setdefault(self._module, {})


@dataclasses.dataclass(frozen=True)
class Answer:
    """The response an on_request hook gave in place of upstream's."""

    status: int
    content_type: str
    data: bytes


def last_turn_message(body):
    """The last user or assistant message of `body["messages"]`, past the trailing role-system notices Claude Code
    appends (task notifications, hook feedback, the environment); None when there is none."""
    messages = body.get("messages") if isinstance(body, dict) else None
    for message in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or message.get("role") != "system":
            return message
    return None


class Request:
    """The request body as hooks see it: `raw` bytes (read-only) and `body`, the parsed object or None."""

    __slots__ = ("_raw", "_body", "_changed", "_answer")

    def __init__(self, raw, body):
        self._raw = raw
        self._body = body
        self._changed = False
        self._answer = None

    @property
    def raw(self):
        return self._raw

    @property
    def body(self):
        return self._body

    @property
    def changed(self):
        return self._changed

    @property
    def answered(self):
        """The Answer an on_request hook gave, or None."""
        return self._answer

    def mark_changed(self):
        if self._body is None:
            raise ValueError("mark_changed: this request has no JSON object body")
        self._changed = True

    def answer(self, status, content_type, data):
        """Answer the request here: the core relays this response and sends nothing upstream."""
        if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
            raise ValueError("answer: status must be an int from 200 to 599, got %r" % (status,))
        if not isinstance(content_type, str) or not content_type:
            raise ValueError("answer: content_type must be a non-empty string, got %r" % (content_type,))
        if not isinstance(data, bytes):
            raise TypeError("answer: data must be bytes, got %s" % type(data).__name__)
        self._answer = Answer(status, content_type, data)

    def last_turn_message(self):
        """The body's last user or assistant message, past Claude Code's trailing role-system notices, or None."""
        return last_turn_message(self._body)


@dataclasses.dataclass(frozen=True)
class Result:
    """How a response ended, for on_response_end."""

    status: int
    reason: str
    headers: tuple             # ((name, value), ...) as the upstream sent them
    error_head: bytes          # the first 600 bytes of the body when status >= 400, else b""
    completed: bool            # the response ran to its end
    client_disconnected: bool
    duration_s: float
    note: str                  # the assembled note: kind, then each on_request note


def parse_object(raw):
    """The body parsed when it is a JSON object, else None."""
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


class Chain:
    """The loaded modules in list order, and the hooks run over them with failure isolation."""

    def __init__(self, modules, event):
        self.modules = tuple(modules)
        self.event = event  # the database's event(kind, request_id=None, lane=None, **fields)
        self.with_on_event = tuple(m for m in self.modules if m.on_event is not None)

    def fail(self, module, hook, exc, ctx=None):
        module.failed()
        frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ is not None else []
        where = None
        if frames:
            last = frames[-1]
            where = "%s:%s in %s" % (os.path.basename(last.filename), last.lineno, last.name)
        self.event("middleware_failed", request_id=ctx.request_id if ctx is not None else None,
                   module=module.NAME, hook=hook, error=repr(exc), where=where)

    def call(self, module, hook, ctx, *args):
        """(True, value) from the hook, or (False, None) once its failure is recorded and counted."""
        ctx._module = module.module
        try:
            fn = getattr(module, hook)
            return True, fn(ctx, *args)
        except (Exception, SystemExit) as e:
            self.fail(module, hook, e, ctx)
            return False, None
        finally:
            ctx._module = None

    def run_request(self, ctx, req):
        """(bytes to send upstream, notes): the original bytes unless a hook marked a change.

        The body is serialized after each hook once one marked a change, so an edit that cannot be sent fails the
        hook that made it. A hook that answers ends the run: the original bytes, the notes closed by
        ` answered={NAME}`; a hook that fails leaves no answer.
        """
        model = req.body.get("model", _MISSING) if req.body is not None else _MISSING
        notes, sent = [], req.raw  # sent: the bytes of the body as the last hook that did not fail left it
        for m in self.modules:
            if m.on_request is None:
                continue
            ok, note = self.call(m, "on_request", ctx, req)
            if ok and req.body is not None and req.body.get("model", _MISSING) != model:
                ok = False
                self.fail(m, "on_request", ValueError("changed body['model']; routing is the core's job"), ctx)
            if ok and req.changed and req.answered is None:
                try:
                    serialized = json.dumps(req.body, separators=(",", ":")).encode()
                except Exception as e:
                    ok = False
                    self.fail(m, "on_request", e, ctx)
            if not ok:
                req._answer = None
                req._body, req._changed = parse_object(sent), sent is not req.raw
                continue
            if note is not None and not isinstance(note, str):
                self.fail(m, "on_request", TypeError("returned %s, not str or None" % type(note).__name__), ctx)
                note = None
            if note:
                notes.append(note)
            if req.answered is not None:
                notes.append(" answered=%s" % m.NAME)
                return req.raw, notes
            if req.changed:
                sent = serialized
        return sent, notes

    def event_sink(self, ctx, content_type):
        """An EventSink for this response, or None when no module defines on_event or the body is neither SSE nor JSON."""
        content_type = (content_type or "").lower()
        mode = "sse" if "text/event-stream" in content_type else "json" if "json" in content_type else None
        if not self.with_on_event or mode is None:
            return None
        return EventSink(self, ctx, mode)

    def run_response_end(self, ctx, result):
        for m in self.modules:
            if m.on_response_end is not None:
                self.call(m, "on_response_end", ctx, result)

    def health_entries(self, ctx):
        """`NAME[ health()] errors=N` per module, in list order; a health() that fails shows health-error."""
        out = []
        for m in self.modules:
            text = m.NAME
            if m.health is not None:
                ok, value = self.call(m, "health", ctx)
                if ok and not isinstance(value, str):
                    ok = False
                    self.fail(m, "health", TypeError("returned %s, not str" % type(value).__name__), ctx)
                text += " " + (value if ok else "health-error")
            out.append("%s errors=%d" % (text, m.errors))
        return out


class EventSink:
    """Feeds one response's bytes to the on_event hooks: per SSE event, or once for a JSON body.

    A module whose on_event fails is skipped for the rest of this response. The sink never raises:
    an error of its own is recorded as an `event_sink` event and turns it off, and the stream goes on.
    """

    def __init__(self, chain, ctx, mode):
        self.chain = chain
        self.ctx = ctx
        self.mode = mode
        self.splitter = EventSplitter()
        self.body = bytearray()
        self.skipped = set()
        self.broken = False

    def deliver(self, event):
        for m in self.chain.with_on_event:
            if m.module not in self.skipped:
                ok, _ = self.chain.call(m, "on_event", self.ctx, dict(event))
                if not ok:
                    self.skipped.add(m.module)

    def guarded(self, step):
        if self.broken:
            return
        try:
            step()
        except Exception as e:
            self.broken = True
            self.chain.event("event_sink", request_id=self.ctx.request_id, reason="error", error=repr(e))

    def feed(self, chunk):
        def step():
            if self.mode == "sse":
                overflowed = self.splitter.overflowed
                for event in self.splitter.feed(chunk):
                    self.deliver(event)
                if self.splitter.overflowed != overflowed:
                    self.chain.event("event_sink", request_id=self.ctx.request_id, reason="event_too_large",
                                     limit=self.splitter.max_pending)
            elif len(self.body) + len(chunk) <= MAX_JSON_EVENT:
                self.body += chunk
            else:
                self.broken = True
                self.chain.event("event_sink", request_id=self.ctx.request_id, reason="json_too_large",
                                 limit=MAX_JSON_EVENT)
        self.guarded(step)

    def close(self, completed):
        def step():
            if self.mode == "sse":
                for event in self.splitter.finish():
                    self.deliver(event)
            elif completed:
                self.deliver(json_event(bytes(self.body)))
        self.guarded(step)
