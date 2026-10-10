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

from .. import clock
from ..upstream import UpstreamError, is_stale
from . import auth, errors, estimate, lane, request, signature, stream, turn
from .catalog import Catalog, CatalogUnavailable
from .client import Client
from .response import Reader, Response


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


@dataclass
class Measure:
    """One exchange's clock marks and connection facts, read by the `gpt` line of the attempt that answered."""
    started: float  # the gateway received the client request
    sent_at: float = 0.0  # the attempt's upstream request was sent
    reused: bool = False  # the attempt rode a kept-alive connection
    stale: bool = False  # a kept-alive connection failed before any body byte and the request went on a fresh one
    turn: str = ""  # "sent": the attempt carried the turn-state token; "issued": its response's token was kept
    transport: str = "http"  # "ws": the attempt went over the lane's WebSocket; "http": as an HTTP POST


@dataclass
class Lane:
    """What the route keeps per lane key: the token estimator, the turn-state token and the lane's effort."""
    estimator: estimate.Estimator = field(default_factory=estimate.Estimator)
    turn_state: turn.Turn = field(default_factory=turn.Turn)
    # The effort the lane's last Messages request outside the activity-summary fork and the compaction request sent.
    # Claude Code sends the fork at the agent's declared effort, which an effort tag may have changed for its turns, and
    # every compaction at high; the backend's prompt cache keys on effort, so both send this one and ride the lane's cache.
    effort: Optional[str] = None


def milliseconds(seconds):
    return int(round(seconds * 1000))


class Route:
    def __init__(self, knobs, log):
        self.log = log
        self.store = auth.source(knobs)
        self.client = Client(knobs, log)
        self.catalog = Catalog(knobs, self.client, self.store, log)
        self.lanes = {}
        self.lock = threading.Lock()

    def decide(self, method, path, body, headers):
        return decide(self.catalog, method, path, body, headers)

    def lane_state(self, key):
        with self.lock:
            if key not in self.lanes:
                if len(self.lanes) >= 1024:
                    del self.lanes[next(iter(self.lanes))]
                self.lanes[key] = Lane()
            return self.lanes[key]

    def _error_line(self, key, slug, status, attempt, action, head, credentials=None, delay=None):
        if not isinstance(head, bytes):
            head = json.dumps(head, ensure_ascii=False).encode()
        if credentials is not None:
            for secret in (credentials.access_token, credentials.account_id):
                head = head.replace(secret.encode(), b"[redacted]")
        advice = "" if delay is None else " retry_after=%g" % delay
        self.log("    gpt error lane=%s slug=%s status=%s attempt=%d action=%s%s body=%r" %
                 (key, slug, status, attempt, action, advice, head[:300]))

    def _terminal(self, key, model, translation, translator, estimator, attempt, status, reader, credentials, measure,
                  ended=None):
        """`ended`: when the client had the last byte, the stream's end before the reader's join; now when None."""
        if translator.error is not None:
            self._error_line(key, model.slug, "transport" if reader.transport_error else status,
                             attempt, "final", translator.failure or reader.error_head, credentials,
                             translator.error.delay)
        if translator.usage is not None:
            if translator.terminal == "response.completed":
                with self.lock:
                    estimator.observe(translation.estimate_chars, translator.input_tokens)
            reasoning = "-" if translator.reasoning_tokens is None else translator.reasoning_tokens
            self.log("    gpt lane=%s slug=%s status=%s input=%d cached=%d ratio=%.3f attempts=%d out=%d reasoning=%s "
                     "ttfb_ms=%d upstream_ms=%d total_ms=%d reused=%d stale=%d%s transport=%s%s" %
                     (key, model.slug, status, translator.input_tokens,
                      translator.usage["cache_read_input_tokens"], translator.cache_ratio, attempt,
                      translator.usage["output_tokens"], reasoning,
                      milliseconds(reader.first_byte_at - measure.started),
                      milliseconds(reader.terminal_at - measure.sent_at),
                      milliseconds((clock.monotonic() if ended is None else ended) - measure.started),
                      measure.reused, measure.stale, " turn_state=" + measure.turn if measure.turn else "",
                      measure.transport,
                      " notes=" + json.dumps(list(translation.notes)) if translation.notes else ""))

    # codex-rs/codex-client/src/retry.rs:15-52; retries stop at the translator commit boundary.
    def exchange(self, body, model, headers, path, cap, started=None):
        measure = Measure(clock.monotonic() if started is None else started)
        silent = False  # the one free retry of a kept-alive connection that died before any body byte
        key = lane.key(headers.get("x-claude-code-session-id", ""), headers.get("x-claude-code-agent-id", ""))
        state = self.lane_state(key)
        estimator = state.estimator
        try:
            where = turn.position(body)
            summary = where == turn.FORK or request.compaction(body)  # sent at another effort than the lane's turns
            with self.lock:
                pinned = state.effort if summary else None
            translation = request.translate(body, model, key, pinned)
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
                response = self.client.responses(translation, key, credentials, token, observe)
                reused, retried, measure.transport = self.client.reuse()
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
                                                                  reader.ended_at)
                            return Response(200, first, reader=reader)
                        data = stream.accumulate(first + Response(200, reader=reader).read())
                        code = 502 if data.get("type") == "error" else 200
                        self._terminal(key, model, translation, translator, estimator, attempt, code, reader,
                                       credentials, measure)
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
            if decision.refresh and refreshed or decision.drop_reasoning and dropped:
                can_retry = False
            action = ("refresh" if decision.refresh else "drop_reasoning" if decision.drop_reasoning else "retry")
            self._error_line(key, model.slug, status, attempt, action if can_retry else "final", head, credentials,
                             decision.delay)
            if not can_retry:
                if decision.client_status == 401:
                    return login_required()
                return Response.json(decision.client_status, decision.client_body, decision.client_headers)
            try:
                if decision.refresh:
                    refreshed = True
                    credentials = self.store.credentials(force_refresh=True)
                if decision.drop_reasoning:
                    dropped = True
                    body = without_reasoning(body)
                    translation = request.translate(body, model, key, pinned)
                    cap.sent_body(translation.body_bytes)
                    with self.lock:
                        estimated = estimator.estimate(translation.estimate_chars, attachment_count(body))
                    self.log("    gpt drop-reasoning lane=%s slug=%s" % (key, model.slug))
            except auth.LoginRequired as error:
                return login_required(str(error))
            except auth.AuthError as error:
                return route_error(503, "api_error", str(error))
            clock.sleep(errors.backoff(attempt) if decision.delay is None else decision.delay)
