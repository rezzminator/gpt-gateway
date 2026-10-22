"""ChatGPT-only client, with Codex identity: Responses over the lane's kept WebSocket, else HTTP on a connection
per serving thread.

Derived from openai/codex, changed (Apache-2.0); see THIRD_PARTY_NOTICES.md.
Model discovery distilled from codex-lb, Copyright (c) 2025 Soju06 (MIT;
permission notice in THIRD_PARTY_NOTICES.md).
"""
import json
import threading

from ..reach import error_class
from ..upstream import Upgrade, Upstream, UpstreamError
from . import continuation, identity, turn, websocket


class Client:
    def __init__(self, knobs, event):
        self.upstream = Upstream(knobs.origin, event)
        self.sockets = websocket.Sockets(self.upstream, event) if knobs.transport == "websocket" else None
        self.local = threading.local()  # the serving thread's response reader, its open Answer, its last send's facts

    # codex-rs/login/src/auth/default_client.rs:152-179; codex-api/src/requests/headers.rs:15-31.
    def _headers(self, credentials, subagent=False):
        headers = dict(identity.headers(), **{"Authorization": "Bearer " + credentials.access_token,
                       "ChatGPT-Account-ID": credentials.account_id})
        if subagent:
            headers["x-openai-subagent"] = "collab_spawn"
        return headers

    def _response_headers(self, translation, lane_key, credentials, subagent):
        # responses_metadata.rs:335-413: only owned, bounded compatibility fields become headers.
        body = json.loads(translation.body_bytes)
        metadata = body.get("client_metadata") or {}
        headers = self._headers(credentials, subagent)
        model, tier = body.get("model"), body.get("service_tier")
        values = [model] + ([] if tier is None else [tier])
        if all(isinstance(value, str) and 0 < len(value) <= 200 and
               all(c.isascii() and (c.isalnum() or c in "._-") for c in value) for value in values):
            # client.rs:1280-1296: a routing hint, never an authorization or entitlement claim.
            headers["x-codex-routing-hint"] = "model=" + model + (";tier=" + tier if tier is not None else "")
        for field, name in (("session_id", "session-id"), ("thread_id", "thread-id"),
                            ("x-codex-window-id", "x-codex-window-id"),
                            ("x-codex-parent-thread-id", "x-codex-parent-thread-id"),
                            ("x-codex-turn-metadata", "x-codex-turn-metadata")):
            value = metadata.get(field)
            if isinstance(value, str) and 0 < len(value) <= 1024 and all(32 <= ord(c) < 127 for c in value):
                headers[name] = value
        headers.setdefault("session-id", lane_key)
        headers.setdefault("thread-id", lane_key)
        headers["x-client-request-id"] = headers["thread-id"]
        return headers

    # codex-rs/core/src/client.rs:652-660,947-1071; no ingress headers cross this boundary.
    def responses(self, translation, lane_key, credentials, turn_state=None, observe=None, fork=False, request_id=None,
                  subagent=False):
        """The upstream answer: over the lane's kept WebSocket while it serves, else an HTTP POST. `observe` receives a
        turn-state token a WebSocket response issues in a `response.metadata` event; a `fork` waits longest for its
        lane's busy socket. `request_id`: the gateway's number for the request, named in an `upstream_retry` event.
        `subagent`: the lane carries an agent id, so its transport identifies it as collab_spawn."""
        self.local.transport = "http"
        if self.sockets is not None and self.sockets.usable():
            answer = self._over_websocket(translation, lane_key, credentials, turn_state, observe, fork, subagent)
            if answer is not None:
                return answer
        headers = self._response_headers(translation, lane_key, credentials, subagent)
        headers.update({"Accept": "text/event-stream", "Content-Type": "application/json",
                        "content-length": str(len(translation.body_bytes))})
        if turn_state:
            headers[turn.HEADER] = turn_state  # codex-rs/core/src/client.rs:2119-2140
        return self.upstream.send("POST", "/backend-api/codex/responses", translation.body_bytes, headers.items(),
                                  request_id=request_id)

    # codex-rs/core/src/client.rs:2336-2354: the upgrade carries the HTTP request's headers and the beta.
    def _upgrade_headers(self, lane_key, credentials, subagent, translation):
        headers = self._response_headers(translation, lane_key, credentials, subagent)
        headers["OpenAI-Beta"] = websocket.BETA
        headers["Sec-WebSocket-Extensions"] = "permessage-deflate; client_no_context_takeover; server_no_context_takeover"
        return headers.items()

    def _over_websocket(self, translation, lane_key, credentials, turn_state, observe, fork=False, subagent=False):
        """The started Answer or a Refusal; None when the upgrade showed the endpoint serves no WebSocket. A kept socket
        that fails before its first message, or that the backend asks to replace, gives way once to a fresh one."""
        request = continuation.Request(translation.body_bytes, turn_state)
        request.supports_effort_updates = translation.supports_effort_updates
        pool_key = websocket.socket_key(lane_key, credentials)
        self.local.transport, self.local.reused, self.local.stale, self.local.delta = "ws", False, False, ""
        for attempt in (1, 2):
            wait = websocket.FORK_WAIT_SECONDS if fork else websocket.LANE_WAIT_SECONDS
            kept = self.sockets.checkout(pool_key, wait if attempt == 1 else 0)
            if kept is None:
                try:
                    kept = self.sockets.open(pool_key, self._upgrade_headers(lane_key, credentials, subagent, translation))
                except UpstreamError as error:
                    self.sockets.failed(error.message)
                    raise
                if isinstance(kept, Upgrade):
                    if websocket.unserved(kept.status):
                        self.sockets.pause(websocket.UNSERVED_PAUSE_SECONDS, "the upgrade answered %d" % kept.status)
                        self.local.transport = "http"
                        return None
                    return websocket.Refusal(kept)
            self.local.reused = kept.exchanges > 0
            data, self.local.delta = request.frame(kept.baseline)
            answer = websocket.Answer(self.sockets, kept, observe, request)
            self.local.answer = answer
            try:
                answer.start(data)
                return answer
            except (websocket.Reconnect, OSError, UpstreamError) as error:
                answer.abort()
                if self.local.reused and attempt == 1:
                    self.local.stale = True
                    continue
                reason = error.message if isinstance(error, UpstreamError) else "websocket: %r" % (error,)
                if not isinstance(error, websocket.Reconnect):
                    self.sockets.failed(reason)
                raise UpstreamError(reason, error_class(error))  # the cause's class, for the row and its events

    def reuse(self):
        """(reused, stale, transport, delta) of this thread's last Responses send: `ws` with the WebSocket's
        continuation delta, or `http` and an empty one."""
        if getattr(self.local, "transport", "http") == "ws":
            return self.local.reused, self.local.stale, "ws", self.local.delta
        return self.upstream.reuse() + ("http", "")

    # codex-lb/app/core/clients/model_fetcher.py:132-153; rewritten with the core upstream façade.
    def models(self, credentials):
        try:
            resp = self.upstream.send("GET", "/backend-api/codex/models?client_version=" +
                                      identity.CODEX_CLIENT_VERSION, None, self._headers(credentials).items(),
                                      timeout=10)
            if resp.status != 200:
                raise ValueError("models HTTP %d" % resp.status)
            raw = resp.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("model catalog exceeds 8 MiB")
            return json.loads(raw)
        finally:
            self.drop()

    def release(self):
        """Let go of a response read to its end: its reader stops without a cancel, so the socket or connection it rode
        settles back in its pool; `drop` is for a response cut short."""
        reader = getattr(self.local, "reader", None)
        self.local.reader = self.local.answer = None
        if reader is not None:
            reader.stop.set()
        self.upstream.release()

    def watch(self, reader):
        self.local.reader = reader

    def drop(self):
        reader = getattr(self.local, "reader", None)
        self.local.reader = None
        if reader is not None:
            reader.stop.set()
        answer = getattr(self.local, "answer", None)
        self.local.answer = None
        if answer is not None:
            answer.abort()
        # The serving thread owns the connection; the response reader may be blocked on it.
        self.upstream.abort()

    def close(self):
        """End the keeper, close the idle sockets and this client's idle connections."""
        if self.sockets is not None:
            self.sockets.stop()
        self.upstream.close()
