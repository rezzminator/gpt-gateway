"""ChatGPT upstream failures, immutable client responses and bounded retry delays.

Distilled from claude-code-proxy, Copyright (c) 2026 Raine Virta (MIT;
permission notice in THIRD_PARTY_NOTICES.md). Retry logic derived from
openai/codex, changed (Apache-2.0; see THIRD_PARTY_NOTICES.md).
"""
from dataclasses import dataclass
import email.utils
import json
import math
import random
import re
from typing import Optional

from model_gateway import clock

MAX_RETRIES = 4
MAX_ADVISED_SLEEP = 60  # seconds: longer server advice ends the request, the advice handed to Claude Code
# codex-api/src/sse/responses_error.rs:90-96, 138-143: a stream's rate limit, and the delay its message names.
_RATE_CODES = ("rate_limit_exceeded", "slow_down")
_HINT = re.compile(r"(?i)try again in\s*(\d+(?:\.\d+)?)\s*(s|ms|seconds?)")


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
    delay: Optional[float] = None  # seconds the server advised before a retry, None when it advised none


# claude-code-proxy/src/providers/codex/mod.rs:1439-1572; response envelopes.
def classify(status, body_head, phase, headers=None):
    """Decision before-output; SSE error bytes after-output. No retries after bytes escape.

    body_head accepts a parsed envelope, UTF-8 bytes or text; headers, the upstream answer's
    headers as a mapping or (name, value) pairs, carry its retry advice. The caller enforces
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
    stream_limit = status is None and code in _RATE_CODES
    delay = _header_delay(headers)
    if delay is None and stream_limit:
        delay = _hint_delay(message)
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
                delay = max(0.0, reset - clock.now())
                headers["retry-after"] = str(math.ceil(delay))
                headers["anthropic-ratelimit-unified-reset"] = str(int(reset))
        except (TypeError, ValueError, OverflowError):
            pass
    elif "context_length_exceeded" in lower or "context window" in lower:
        client_status, kind = 413, "request_too_large"
        if "context window" not in message.lower():  # Claude Code compacts on a 413 only when its message names it
            message = "context window exceeded: " + message
    elif stream_limit:  # codex-rs/protocol/src/error.rs:395-457: RateLimitExceeded is always retried
        client_status, kind, retry = 429, "rate_limit_error", True
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
    if delay is not None and "retry-after" not in headers:
        headers.update({"retry-after": str(math.ceil(delay)), "retry-after-ms": str(round(delay * 1000))})
    body = FrozenDict(type="error", error=FrozenDict(type=kind, message="chatgpt upstream: " + message))
    if phase == "after-output":
        return ("event: error\ndata: " + json.dumps(body, separators=(",", ":")) + "\n\n").encode()
    return Decision(retry, refresh, drop, client_status, body, FrozenDict(headers), delay)


def _header_delay(headers):
    """Seconds a retry-after-ms or Retry-After header advises, never negative; None when neither reads.

    The OpenAI SDKs' order: retry-after-ms, then Retry-After as seconds, then as an HTTP date."""
    pairs = headers.items() if isinstance(headers, dict) else headers or ()
    named = {str(name).lower(): str(value).strip() for name, value in pairs}
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1)):
        if name not in named:
            continue
        try:
            seconds = float(named[name]) * scale
        except ValueError:
            seconds = None
            if name == "retry-after":
                try:
                    seconds = email.utils.parsedate_to_datetime(named[name]).timestamp() - clock.now()
                except (TypeError, ValueError, IndexError, OverflowError):
                    pass
        if seconds is not None and math.isfinite(seconds):
            return max(0.0, seconds)
    return None


def _hint_delay(message):
    """Seconds a rate-limit message's "try again in Xs" names, None without one."""
    match = _HINT.search(message)
    if match is None:
        return None
    value = float(match.group(1))
    return value / 1000 if match.group(2).lower() == "ms" else value


# openai/codex/codex-rs/codex-client/src/retry.rs:15-52; derived, changed.
def backoff(n, jitter=None):
    """Retry n (1..4) delay in seconds; jitter(low, high) is injectable."""
    if not isinstance(n, int) or not 1 <= n <= MAX_RETRIES:
        raise ValueError("retry number must be between 1 and 4")
    return 0.2 * 2 ** (n - 1) * (jitter or random.uniform)(0.9, 1.1)
