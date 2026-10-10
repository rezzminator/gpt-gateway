"""ChatGPT-only client, with Codex identity: Responses over the lane's kept WebSocket, else HTTP on a connection
per serving thread.

Derived from openai/codex, changed (Apache-2.0); see THIRD_PARTY_NOTICES.md.
Model discovery distilled from codex-lb, Copyright (c) 2025 Soju06 (MIT;
permission notice in THIRD_PARTY_NOTICES.md).
"""
import json
import threading

from ..upstream import Upgrade, Upstream, UpstreamError
from . import identity, turn, websocket


class Client:
    def __init__(self, knobs, log):
        self.upstream = Upstream(knobs.origin, log)
        self.sockets = websocket.Sockets(self.upstream, log) if knobs.transport == "websocket" else None
        self.local = threading.local()  # the serving thread's response reader, its open Answer, its last send's facts

    # codex-rs/login/src/auth/default_client.rs:152-179; codex-api/src/requests/headers.rs:15-31.
    def _headers(self, credentials):
        return dict(identity.headers(), **{"Authorization": "Bearer " + credentials.access_token,
                    "ChatGPT-Account-ID": credentials.account_id})

    # codex-rs/core/src/client.rs:652-660,947-1071; no ingress headers cross this boundary.
    def responses(self, translation, lane_key, credentials, turn_state=None, observe=None, fork=False):
        """The upstream answer: over the lane's kept WebSocket while it serves, else an HTTP POST. `observe` receives a
        turn-state token a WebSocket response issues in a `response.metadata` event; a `fork` waits for its lane's busy
        socket rather than open another."""
        self.local.transport = "http"
        if self.sockets is not None and self.sockets.usable():
            answer = self._over_websocket(translation, lane_key, credentials, turn_state, observe, fork)
            if answer is not None:
                return answer
        headers = self._headers(credentials)
        headers.update({"Accept": "text/event-stream", "Content-Type": "application/json",
                        "session-id": lane_key, "x-client-request-id": lane_key,
                        "x-openai-subagent": "collab_spawn", "content-length": str(len(translation.body_bytes))})
        if translation.lite:
            headers["x-openai-internal-codex-responses-lite"] = "true"
        if turn_state:
            headers[turn.HEADER] = turn_state  # codex-rs/core/src/client.rs:2119-2140
        return self.upstream.send("POST", "/backend-api/codex/responses", translation.body_bytes, headers.items())

    # codex-rs/core/src/client.rs:2336-2354: the upgrade carries the HTTP request's headers and the beta.
    def _upgrade_headers(self, translation, lane_key, credentials):
        headers = self._headers(credentials)
        headers.update({"OpenAI-Beta": websocket.BETA, "session-id": lane_key, "thread-id": lane_key,
                        "x-client-request-id": lane_key, "x-openai-subagent": "collab_spawn"})
        if translation.lite:
            headers["x-openai-internal-codex-responses-lite"] = "true"
        return headers.items()

    def _over_websocket(self, translation, lane_key, credentials, turn_state, observe, fork=False):
        """The started Answer or a Refusal; None when the upgrade showed the endpoint serves no WebSocket. A kept socket
        that fails before its first message, or that the backend asks to replace, gives way once to a fresh one."""
        data = websocket.message(translation.body_bytes, turn_state)
        pool_key = websocket.socket_key(lane_key, translation.lite, credentials)
        self.local.transport, self.local.reused, self.local.stale = "ws", False, False
        for attempt in (1, 2):
            kept = self.sockets.checkout(pool_key, websocket.FORK_WAIT_SECONDS if fork and attempt == 1 else 0)
            if kept is None:
                try:
                    kept = self.sockets.open(pool_key, self._upgrade_headers(translation, lane_key, credentials))
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
            answer = websocket.Answer(self.sockets, kept, observe)
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
                raise UpstreamError(reason)

    def reuse(self):
        """(reused, stale, transport) of this thread's last Responses send: `ws` or `http`."""
        if getattr(self.local, "transport", "http") == "ws":
            return self.local.reused, self.local.stale, "ws"
        return self.upstream.reuse() + ("http",)

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
