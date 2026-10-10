"""The continuation of a socket's last completed response: Codex's incremental request over the Responses WebSocket.

Derived from openai/codex, changed (Apache-2.0; see THIRD_PARTY_NOTICES.md). The backend keeps a socket's last
completed response in memory, the one response a later request on it can continue with store=false
(codex-rs/core/src/client.rs:341-462,1401-1462): a request whose fields equal that response's request, and whose input
is that request's input and then the response's output items, sends only the items after them, with the response's id
as previous_response_id. Claude Code returns each output item translated back, so an output item matches by what
survives the round trip.
"""
import hashlib
import json

from .websocket import message

# What a continuation leaves out of the comparison of two requests: the input it shortens and one request's metadata
# (codex-rs/core/src/client.rs:341-392).
PER_REQUEST = ("input", "client_metadata", "stream_options", "previous_response_id")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).digest()


def _text(item):
    return "".join(part.get("text", "") for part in item.get("content") or [] if isinstance(part, dict))


def _arguments(item):
    try:
        return json.loads(item.get("arguments"))
    except (TypeError, ValueError):
        return item.get("arguments")


def _bare(item):
    return {name: value for name, value in item.items() if name not in ("id", "status")}


def produced(output, sent):
    """Whether the input item `sent` is the output item `output` as Claude Code returns it translated back: a reasoning
    item keeps its encrypted content, a call its id, name and arguments as parsed, a message its role and text; an item
    of another kind is equal but for its id and status."""
    kind = output.get("type")
    if not isinstance(sent, dict) or sent.get("type") != kind:
        return False
    if kind == "reasoning":
        return bool(output.get("encrypted_content")) and output["encrypted_content"] == sent.get("encrypted_content")
    if kind == "function_call":
        return ((output.get("call_id"), output.get("name"), _arguments(output)) ==
                (sent.get("call_id"), sent.get("name"), _arguments(sent)))
    if kind == "message":
        return output.get("role") == sent.get("role") and _text(output) == _text(sent)
    return _bare(output) == _bare(sent)


class Request:
    """One Responses request as a socket sends it: whole, or as the continuation of the socket's last completed response."""

    def __init__(self, body_bytes, turn_state=None):
        self.body_bytes, self.turn_state = body_bytes, turn_state
        self.body = json.loads(body_bytes)
        if not isinstance(self.body, dict):
            raise ValueError("a Responses body is a JSON object")
        items = self.body.get("input")
        self.items = items if isinstance(items, list) else []
        self.fields = _digest({name: value for name, value in self.body.items() if name not in PER_REQUEST})
        self.digests = [_digest(item) for item in self.items]

    def frame(self, baseline):
        """(response.create message, delta): continuing `baseline`, the request's new items and `{K}/{N}`, K of its N
        input items sent; else the whole request and `full:{why}` — none: the socket holds no completed response;
        fields: a field outside the input differs; prefix: the input does not open with the baseline's; output: the
        items after it are not the response's output."""
        tail, why = self._tail(baseline)
        if tail is None:
            return message(self.body_bytes, self.turn_state), "full:" + why
        body = dict(self.body, input=tail, previous_response_id=baseline.response_id)
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        return message(data, self.turn_state), "%d/%d" % (len(tail), len(self.items))

    def _tail(self, baseline):
        if baseline is None:
            return None, "none"
        if baseline.fields != self.fields:
            return None, "fields"
        known = len(baseline.digests)
        if self.digests[:known] != baseline.digests:
            return None, "prefix"
        end = known + len(baseline.output)
        sent = self.items[known:end]
        if len(sent) < len(baseline.output) or not all(map(produced, baseline.output, sent)):
            return None, "output"
        return self.items[end:], None

    def baseline(self, response_id, output):
        """What the socket keeps of this request once its response `response_id` completed with `output`."""
        return Baseline(self, response_id, output)


class Baseline:
    """A socket's last completed response: its request's fields and input digests, its id and its output items."""

    def __init__(self, request, response_id, output):
        self.fields, self.digests = request.fields, request.digests
        self.response_id, self.output = response_id, output
