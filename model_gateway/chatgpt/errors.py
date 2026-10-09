"""ChatGPT upstream failures, immutable client responses and bounded retry delays.

Distilled from claude-code-proxy, Copyright (c) 2026 Raine Virta (MIT;
permission notice in THIRD_PARTY_NOTICES.md). Retry logic derived from
openai/codex, changed (Apache-2.0; see THIRD_PARTY_NOTICES.md).
"""
from dataclasses import dataclass
import json
import math
import random

from model_gateway import clock

MAX_RETRIES = 4


class FrozenDict(dict):
    """An immutable dict that remains directly JSON serializable."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("immutable error decision")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable


@dataclass(frozen=True)
class Decision:
    retry: bool
    refresh: bool
    drop_reasoning: bool
    client_status: int
    client_body: dict
    client_headers: dict


# claude-code-proxy/src/providers/codex/mod.rs:1439-1572; response envelopes.
def classify(status, body_head, phase):
    """Decision before-output; SSE error bytes after-output. No retries after bytes escape.

    body_head accepts a parsed envelope, UTF-8 bytes or text. The caller enforces
    MAX_RETRIES, one token refresh and one reasoning replay reset per request.
    Synthetic codes: empty_completion, stream_closed, idle_timeout.
    """
    if phase not in ("before-output", "after-output"):
        raise ValueError("phase must be before-output or after-output")
    if isinstance(body_head, bytes):
        body_head = body_head.decode("utf-8", "replace")
    raw = body_head if isinstance(body_head, str) else ""
    try:
        envelope = json.loads(raw) if raw else body_head
    except ValueError:
        envelope = {}
    envelope = envelope if isinstance(envelope, dict) else {}
    response = envelope.get("response") or envelope
    response = response if isinstance(response, dict) else envelope
    detail = response.get("error") or response
    detail = detail if isinstance(detail, dict) else {"message": str(detail)}
    code = str(detail.get("code") or detail.get("type") or "")
    message = str(detail.get("message") or raw or code or "request failed")
    lower = (code + " " + message).lower()
    retry, refresh, drop = False, False, False
    headers = {}
    client_status = status if isinstance(status, int) and 400 <= status <= 599 else 502
    kind = "api_error"
    if "usage_limit_reached" in lower:
        client_status, kind = 429, "rate_limit_error"
        headers.update({"x-should-retry": "false", "anthropic-ratelimit-unified-status": "rejected"})
        reset = detail.get("resets_at")
        try:
            reset = float(reset)
            if math.isfinite(reset):
                headers["retry-after"] = str(max(0, math.ceil(reset - clock.now())))
                headers["anthropic-ratelimit-unified-reset"] = str(int(reset))
        except (TypeError, ValueError, OverflowError):
            pass
    elif "context_length_exceeded" in lower or "context window" in lower:
        client_status, kind = 413, "request_too_large"
    elif "server_is_overloaded" in lower or "slow_down" in lower or status == 529:
        client_status, kind = 529, "overloaded_error"
    elif "usage_not_included" in lower or status == 403:
        client_status, kind = 403, "permission_error"
    elif "empty_completion" in lower:
        # claude-code-proxy/src/providers/codex/mod.rs:940-946: provider retry;
        # mod.rs:1484-1496: suppress client retries after that budget is spent.
        client_status, retry = 503, True
        message = "empty completion"
        headers["x-should-retry"] = "false"
    # codex-rs/codex-api/src/sse/responses_error.rs:47-84: Codex never retries these failure codes.
    elif code in ("insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
                  "project_spend_limit_exceeded"):
        client_status, kind = 429, "rate_limit_error"
        headers["x-should-retry"] = "false"
    elif code in ("invalid_prompt", "cyber_policy", "bio_policy", "misalignment_policy_violation"):
        client_status, kind = 400, "invalid_request_error"
    elif "invalid_encrypted_content" in lower:
        client_status, kind, retry, drop = 400, "invalid_request_error", True, True
    elif status == 401:
        client_status, kind, retry, refresh = 401, "authentication_error", True, True
    elif status == 429:
        client_status, kind = 429, "rate_limit_error"
    elif envelope.get("type") == "response.incomplete":
        details = response.get("incomplete_details") or {}
        message = "incomplete response: " + str(details.get("reason", "unknown"))
    elif status == 400:
        kind = "invalid_request_error"
    else:
        retry = status in (None, 0, 200) or client_status >= 500
    if code == "stream_closed":
        message = "upstream stream closed before the terminal event"
    elif code == "idle_timeout":
        message = "stream idle for 300 seconds"
    if phase == "after-output" and refresh:
        kind = "api_error"
    body = FrozenDict(type="error", error=FrozenDict(type=kind, message="chatgpt upstream: " + message))
    if phase == "after-output":
        return ("event: error\ndata: " + json.dumps(body, separators=(",", ":")) + "\n\n").encode()
    return Decision(retry, refresh, drop, client_status, body, FrozenDict(headers))


# openai/codex/codex-rs/codex-client/src/retry.rs:15-52; derived, changed.
def backoff(n, jitter=None):
    """Retry n (1..4) delay in seconds; jitter(low, high) is injectable."""
    if not isinstance(n, int) or not 1 <= n <= MAX_RETRIES:
        raise ValueError("retry number must be between 1 and 4")
    return 0.2 * 2 ** (n - 1) * (jitter or random.uniform)(0.9, 1.1)
