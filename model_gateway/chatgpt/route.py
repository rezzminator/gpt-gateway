"""ChatGPT routing, lane estimates and retries before the translated response commits.

Retry orchestration derived from openai/codex, changed (Apache-2.0;
see THIRD_PARTY_NOTICES.md). All translation stays in the pure translators.
"""
import copy
from dataclasses import dataclass, field
import json
import http.client
import threading
from typing import Optional

from .. import clock, db
from ..response import Response
from ..upstream import UpstreamError, is_stale
from . import auth, errors, estimate, lane, request, signature, stream, turn
from .catalog import Catalog, CatalogUnavailable
from .client import Client
from .response import Reader


def route_error(status, kind, message, headers=None):
    return Response.json(status, {"type": "error", "error": {
        "type": kind, "message": "chatgpt upstream: " + message}}, headers)


def login_required(message="ChatGPT login required: run codex login, or gateway.py chatgpt-login"):
    return route_error(403, "permission_error", message,
                       {"x-should-retry": "false"})


def decide(catalog, method, path, body, headers):
    """(route, Model or None, answer or None); only candidates touch the catalog."""
    model = body.get("model") if body is not None else None
    if (method != "POST" or path.split("?", 1)[0] not in ("/v1/messages", "/v1/messages/count_tokens")
            or not isinstance(model, str) or model.startswith("claude-")):
        return "anthropic", None, None  # no model id to route on: Anthropic validates the body, as before
    slug = model
    if slug.endswith("[1m]"):
        slug = slug[:-4]
    if slug.endswith("-fast"):
        slug = slug[:-5]
    try:
        models = catalog.models()
    except CatalogUnavailable as error:
        if isinstance(error.__cause__, auth.LoginRequired):  # H5: a missing login is never a retryable 503
            return "chatgpt", None, login_required(str(error.__cause__))
        return "chatgpt", None, route_error(503, "api_error", "no model catalog (%s)" % error)
    if slug not in models:
        return "chatgpt", None, route_error(400, "invalid_request_error",
               'unknown model "%s"; the ChatGPT catalog has: %s' % (model, ", ".join(models)))
    if not headers.get("x-claude-code-agent-id"):
        return "chatgpt", None, route_error(400, "invalid_request_error", "GPT models run as sub-agents only")
    return "chatgpt", models[slug], None


def attachment_count(body):
    """Base64 images and base64 PDF documents in user content or nested tool_result content."""
    def count(value):
        if isinstance(value, list):
            return sum(count(item) for item in value)
        if not isinstance(value, dict):
            return 0
        if value.get("type") == "image":
            return int(isinstance(value.get("source"), dict) and value["source"].get("type") == "base64")
        if value.get("type") == "document":
            source = value.get("source")
            return int(isinstance(source, dict) and source.get("type") == "base64"
                       and source.get("media_type") == "application/pdf")
        return count(value.get("content"))
    return sum(count(message) for message in body.get("messages", []))


def without_reasoning(body):
    cleaned = copy.deepcopy(body)
    for message in cleaned.get("messages", []):
        if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        message["content"] = [block for block in message["content"] if not (
            isinstance(block, dict) and block.get("type") == "thinking" and
            isinstance(signature.decode(block.get("signature")), tuple))]
    return cleaned


def notes_column(notes):
    """The translation notes as a JSON list a text column holds whole: the first ones that fit, then how many more."""
    notes, kept = list(notes), len(notes)
    text = json.dumps(notes)
    while len(text) > db.TEXT_CAP:
        kept -= 1
        text = json.dumps(notes[:kept] + ["%d more" % (len(notes) - kept)])
    return text if notes else None


@dataclass
class Measure:
    """One exchange's clock marks and connection facts, read into the request row by the attempt that answered."""
    started: float  # the gateway received the client request
    sent_at: float = 0.0  # the attempt's upstream request was sent
    reused: bool = False  # the attempt rode a kept-alive connection
    stale: bool = False  # a kept-alive connection failed before any body byte and the request went on a fresh one
    turn: str = ""  # "sent": the attempt carried the turn-state token; "issued": its response's token was kept
    transport: str = "http"  # "ws": the attempt went over the lane's WebSocket; "http": as an HTTP POST
    delta: str = ""  # over the WebSocket: "{K}/{N}", K of N input items sent continuing a response; "full:{why}"


@dataclass
class Lane:
    """What the route keeps per lane key: the token estimator, the turn-state token and the lane's effort."""
    estimator: estimate.Estimator = field(default_factory=estimate.Estimator)
    turn_state: turn.Turn = field(default_factory=turn.Turn)
    # The effort the lane's last Messages request outside the activity-summary fork and the compaction request sent.
    # Claude Code sends the fork at the agent's declared effort, which an effort tag may have changed for its turns, and
    # every compaction at high; the backend's prompt cache keys on effort, so both send this one and ride the lane's cache.
    effort: Optional[str] = None


class Route:
    def __init__(self, knobs, event):
        self.event = event
        self.agent_contract = knobs.agent_contract
        self.store = auth.source(knobs)
        self.client = Client(knobs, event)
        self.catalog = Catalog(knobs, self.client, self.store, event)
        self.lanes = {}
        self.lock = threading.Lock()
        self.retention_settings = None  # the settings snapshot the last exchange ran under
        self.retention_rejected = False  # the backend refused it: no exchange sends it until the settings change

    def decide(self, method, path, body, headers):
        return decide(self.catalog, method, path, body, headers)

    def retention_sends(self, settings):
        """The cache retention to send under the settings snapshot `settings`: None when off or refused. Any change of
        the settings, a new snapshot, re-asks, whether or not a request ran in between."""
        with self.lock:
            if settings is not self.retention_settings:
                self.retention_settings, self.retention_rejected = settings, False
            requested = settings.chatgpt_cache_retention
            return None if requested == "off" or self.retention_rejected else requested

    def retention_state(self, settings):
        """The health line's word under the settings snapshot `settings`: off, 24h, or 24h(rejected)."""
        requested = settings.chatgpt_cache_retention
        with self.lock:
            refused = self.retention_rejected and settings is self.retention_settings
        return requested + "(rejected)" if refused and requested != "off" else requested

    def reject_retention(self, settings, request_id, key, slug):
        """The backend refused the retention of `settings`: stop sending it, and say so once however many exchanges meet
        the refusal. A refusal under settings that changed since is the `retry` event's `drop_retention` alone."""
        with self.lock:
            current = settings is self.retention_settings
            first = current and not self.retention_rejected
            self.retention_rejected = self.retention_rejected or current
        if first:
            self.event("retention_rejected", request_id=request_id, lane=key, slug=slug,
                       retention=settings.chatgpt_cache_retention)

    def lane_state(self, key):
        with self.lock:
            if key not in self.lanes:
                if len(self.lanes) >= 1024:
                    del self.lanes[next(iter(self.lanes))]
                self.lanes[key] = Lane()
            return self.lanes[key]

    def _attempt_event(self, row, key, slug, status, attempt, action, head, credentials=None, delay=None):
        """A failed attempt as a `retry` event, or the last one as `final_error`: at most 300 bytes of its error,
        secrets redacted."""
        if not isinstance(head, bytes):
            head = json.dumps(head, ensure_ascii=False).encode()
        if credentials is not None:
            for secret in (credentials.access_token, credentials.account_id):
                head = head.replace(secret.encode(), b"[redacted]")
        self.event("final_error" if action == "final" else "retry", request_id=row.get("request_id"), lane=key,
                   slug=slug, status=status, attempt=attempt, action=action, retry_after=delay,
                   error_head=head[:300].decode("utf-8", "replace"))

    def _terminal(self, key, model, translation, translator, estimator, attempt, status, reader, credentials, measure,
                  row, ended=None):
        """The answering attempt's fields into the request row. `ended`: when the client had the last byte, the
        stream's end before the reader's join; now when None."""
        if translator.error is not None:
            self._attempt_event(row, key, model.slug, "transport" if reader.transport_error else status,
                                attempt, "final", translator.failure or reader.error_head, credentials,
                                translator.error.delay)
        row["attempts"] = attempt
        if translator.usage is not None:
            if translator.terminal == "response.completed":
                with self.lock:
                    estimator.observe(translation.estimate_chars, translator.input_tokens)
            row.update(
                input_tokens=translator.input_tokens, cache_read_tokens=translator.usage["cache_read_input_tokens"],
                cache_write_tokens=translator.usage["cache_creation_input_tokens"],
                output_tokens=translator.usage["output_tokens"], reasoning_tokens=translator.reasoning_tokens,
                hit=translator.cache_ratio, ttfb_ms=clock.milliseconds(reader.first_byte_at - measure.started),
                upstream_ms=clock.milliseconds(reader.terminal_at - measure.sent_at),
                total_ms=clock.milliseconds((clock.monotonic() if ended is None else ended) - measure.started),
                reused=measure.reused, stale=measure.stale, turn_state=measure.turn or None,
                transport=measure.transport, delta=measure.delta or None,
                translation_notes=notes_column(translation.notes),
                empty=translator.empty)

    # codex-rs/codex-client/src/retry.rs:15-52; retries stop at the translator commit boundary.
    def exchange(self, body, model, headers, path, cap, started=None, row=None, settings=None):
        """The response for one request; `row`, the request's row, gets the lane, the slug and the attempts' fields.
        `settings`: the request's settings snapshot, whose cache retention goes on each Responses request unless the
        backend refused it; None sends none."""
        measure = Measure(clock.monotonic() if started is None else started)
        silent = False  # the one free retry of a kept-alive connection that died before any body byte
        key = lane.key(headers.get("x-claude-code-session-id", ""), headers.get("x-claude-code-agent-id", ""))
        row = {} if row is None else row
        row.update(lane=key, slug=model.slug)
        state = self.lane_state(key)
        estimator = state.estimator
        try:
            where = turn.position(body)
            summary = where == turn.FORK or request.compaction(body)  # sent at another effort than the lane's turns
            with self.lock:
                pinned = state.effort if summary else None
            sending = None if settings is None else self.retention_sends(settings)
            translation = request.translate(body, model, key, pinned, self.agent_contract, sending)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            return route_error(400, "invalid_request_error", str(error))
        cap.sent_body(translation.body_bytes)
        with self.lock:
            estimated = estimator.estimate(translation.estimate_chars, attachment_count(body))
        if path.split("?", 1)[0] == "/v1/messages/count_tokens":
            return Response.json(200, {"input_tokens": estimated})
        with self.lock:
            if not summary:
                state.effort = translation.effort
            state.turn_state.begin(where)
        refreshed = dropped = False
        credentials = None
        attempt = 0
        while True:
            attempt += 1
            translator = stream.Translator(model.slug, translation.tool_names, estimated)
            reader = None
            try:
                credentials = credentials or self.store.credentials()
                measure.sent_at = clock.monotonic()
                with self.lock:
                    token = state.turn_state.outgoing(where)
                measure.turn = "sent" if token else ""

                def observe(issued, where=where, state=state, measure=measure):
                    with self.lock:
                        if state.turn_state.observe(where, issued):
                            measure.turn = "issued"
                response = self.client.responses(translation, key, credentials, token, observe, where == turn.FORK)
                reused, retried, measure.transport, measure.delta = self.client.reuse()
                measure.reused, measure.stale = reused, measure.stale or retried
                status = response.status
                if status != 200:
                    head = response.read(65536)
                    self.client.drop()
                    decision = errors.classify(status, head, "before-output", response.getheaders())
                else:
                    with self.lock:
                        if state.turn_state.observe(where, response.getheader(turn.HEADER)):
                            measure.turn = "issued"
                    reader = Reader(response, translator, self.client)
                    first = reader.next()
                    if first:
                        if body.get("stream"):
                            reader.on_end = lambda: self._terminal(key, model, translation, translator,
                                                                  estimator, attempt, 200, reader, credentials, measure,
                                                                  row, reader.ended_at)
                            return Response(200, first, reader=reader)
                        data = stream.accumulate(first + Response(200, reader=reader).read())
                        code = 502 if data.get("type") == "error" else 200
                        self._terminal(key, model, translation, translator, estimator, attempt, code, reader,
                                       credentials, measure, row)
                        return Response.json(code, data)
                    decision, head = translator.error, translator.failure or reader.error_head
                    if reader.transport_error:
                        status = "transport"
            except (UpstreamError, OSError, http.client.HTTPException) as error:
                self.client.drop()
                status, head = "transport", {"message": error.message if isinstance(error, UpstreamError) else str(error)}
                decision = errors.classify(None, head, "before-output")
            except auth.LoginRequired as error:
                return login_required(str(error))
            except auth.AuthError as error:
                return route_error(503, "api_error", str(error))
            finally:
                if reader is not None and not translator.emitted_any:
                    reader.close()
            if (not silent and measure.reused and reader is not None and reader.first_byte_at is None
                    and is_stale(reader.transport_exception)):
                silent, measure.stale = True, True
                attempt -= 1
                continue
            can_retry = (decision.retry and attempt <= errors.MAX_RETRIES
                         and (decision.delay is None or decision.delay <= errors.MAX_ADVISED_SLEEP))
            strip = decision.drop_retention and sending is not None
            unsent = decision.drop_retention and sending is None  # nothing to strip: judged as any other failure
            if decision.refresh and refreshed or decision.drop_reasoning and dropped or unsent and status in (400, 422):
                can_retry = False
            action = ("refresh" if decision.refresh else "drop_reasoning" if decision.drop_reasoning
                      else "drop_retention" if strip else "retry")
            self._attempt_event(row, key, model.slug, status, attempt, action if can_retry else "final", head,
                                credentials, decision.delay)
            if not can_retry:
                row["attempts"] = attempt
                if decision.client_status == 401:
                    return login_required()
                return Response.json(decision.client_status, decision.client_body, decision.client_headers)
            try:
                if decision.refresh:
                    refreshed = True
                    credentials = self.store.credentials(force_refresh=True)
                if strip:
                    self.reject_retention(settings, row.get("request_id"), key, model.slug)
                    sending = None
                if decision.drop_reasoning:
                    dropped = True
                    body = without_reasoning(body)
                if decision.drop_reasoning or strip:
                    translation = request.translate(body, model, key, pinned, self.agent_contract, sending)
                    cap.sent_body(translation.body_bytes)
                    with self.lock:
                        estimated = estimator.estimate(translation.estimate_chars, attachment_count(body))
                    if decision.drop_reasoning:
                        self.event("drop_reasoning", request_id=row.get("request_id"), lane=key, slug=model.slug)
            except auth.LoginRequired as error:
                return login_required(str(error))
            except auth.AuthError as error:
                return route_error(503, "api_error", str(error))
            clock.sleep(errors.backoff(attempt) if decision.delay is None else decision.delay)
