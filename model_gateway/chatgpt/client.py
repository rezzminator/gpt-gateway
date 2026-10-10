"""ChatGPT-only HTTP client, with Codex identity and a connection per serving thread.

Derived from openai/codex, changed (Apache-2.0); see THIRD_PARTY_NOTICES.md.
Model discovery distilled from codex-lb, Copyright (c) 2025 Soju06 (MIT;
permission notice in THIRD_PARTY_NOTICES.md).
"""
import json
import threading

from ..upstream import Upstream
from . import identity, turn


class Client:
    def __init__(self, knobs, log):
        self.upstream = Upstream(knobs.origin, log)
        self.local = threading.local()  # the serving thread's response reader

    # codex-rs/login/src/auth/default_client.rs:152-179; codex-api/src/requests/headers.rs:15-31.
    def _headers(self, credentials):
        return dict(identity.headers(), **{"Authorization": "Bearer " + credentials.access_token,
                    "ChatGPT-Account-ID": credentials.account_id})

    # codex-rs/core/src/client.rs:652-660,947-1071; no ingress headers cross this boundary.
    def responses(self, translation, lane_key, credentials, turn_state=None):
        headers = self._headers(credentials)
        headers.update({"Accept": "text/event-stream", "Content-Type": "application/json",
                        "session-id": lane_key, "x-client-request-id": lane_key,
                        "x-openai-subagent": "collab_spawn", "content-length": str(len(translation.body_bytes))})
        if translation.lite:
            headers["x-openai-internal-codex-responses-lite"] = "true"
        if turn_state:
            headers[turn.HEADER] = turn_state  # codex-rs/core/src/client.rs:2119-2140
        return self.upstream.send("POST", "/backend-api/codex/responses", translation.body_bytes, headers.items())

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
        # The serving thread owns the connection; the response reader may be blocked on it.
        self.upstream.abort()
