"""A socket's proven input/output prefix, shortened only when Claude Code's round trip preserves its meaning.

Derived from openai/codex, changed (Apache-2.0; see THIRD_PARTY_NOTICES.md).
Codex compares complete ResponseItems (core/src/client.rs:1530-1595). The Messages bridge loses output IDs and
serialization details, so only explicitly reversible projections qualify; unsupported output forces a full request.
"""
import hashlib
import json

from .websocket import message

# Authorization and delivery apply to this response, not the referenced context (client.rs:383-460).
PER_REQUEST = ("input", "client_metadata", "stream_options", "previous_response_id", "access_programs")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).digest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate function argument key")
        result[key] = value
    return result


def _arguments(item):
    return _json(json.loads(item["arguments"], object_pairs_hook=_unique))


def _text(item):
    parts = item.get("content")
    if not isinstance(parts, list):
        return None
    for part in parts:
        if (not isinstance(part, dict) or part.get("type") != "output_text" or
                not isinstance(part.get("text"), str) or
                set(part) - {"type", "text", "annotations", "logprobs"} or
                part.get("annotations") or part.get("logprobs")):
            return None
    return "".join(part["text"] for part in parts)


def produced(output, sent):
    """Prove a known projection, never equate unknown fields, refusals, namespaces or annotated content to plain text."""
    if not isinstance(output, dict) or not isinstance(sent, dict) or output.get("type") != sent.get("type"):
        return False
    if output == sent:
        return _digest(output) == _digest(sent)  # JSON booleans and numbers must remain distinct.
    for name in ("id", "status"):
        if name in sent and sent[name] != output.get(name):
            return False
    kind = output.get("type")
    if kind == "reasoning":
        allowed = {"type", "id", "summary", "encrypted_content", "content"}
        return (not (set(output) - allowed or set(sent) - allowed) and
                output.get("content") in (None, []) and sent.get("content") in (None, []) and
                bool(output.get("encrypted_content")) and
                output.get("id") == sent.get("id") and output["encrypted_content"] == sent.get("encrypted_content") and
                (sent.get("summary") == [] or _digest(sent.get("summary")) == _digest(output.get("summary"))))
    if kind == "function_call":
        allowed = {"type", "id", "status", "call_id", "name", "arguments"}
        if set(output) - allowed or set(sent) - allowed:
            return False
        try:
            return ((output.get("call_id"), output.get("name"), _arguments(output)) ==
                    (sent.get("call_id"), sent.get("name"), _arguments(sent)))
        except (KeyError, TypeError, ValueError):
            return False  # A malformed or duplicate-key payload cannot prove the round trip.
    if kind == "message":
        allowed = {"type", "id", "status", "role", "content"}
        if set(output) - allowed or set(sent) - allowed:
            return False
        text = _text(output)
        return text is not None and output.get("role") == sent.get("role") and text == _text(sent)
    return False


class Request:
    """One full request, with an optional proven delta against the socket's last completed response."""

    def __init__(self, body_bytes, turn_state=None):
        self.body_bytes, self.turn_state = body_bytes, turn_state
        self.body = json.loads(body_bytes)
        if not isinstance(self.body, dict):
            raise ValueError("a Responses body is a JSON object")
        items = self.body.get("input")
        self.items = items if isinstance(items, list) else []
        properties = {name: value for name, value in self.body.items() if name not in PER_REQUEST}
        self.fields = _digest(properties)
        self.reasoning = self.wire_reasoning = self.body.get("reasoning") or {}
        without_effort = dict(self.reasoning)
        without_effort.pop("effort", None)
        self.effort_fields = _digest(dict(properties, reasoning=without_effort))
        self.digests = [_digest(item) for item in self.items]
        self.supports_effort_updates = False

    def frame(self, baseline):
        """(response.create, K/N or full:reason); an effort control is counted among the K items sent."""
        tail, why = self._tail(baseline)
        if tail is None:
            self.wire_reasoning = self.reasoning
            return message(self.body_bytes, self.turn_state), "full:" + why
        body = dict(self.body, input=tail, previous_response_id=baseline.response_id)
        if self.supports_effort_updates:
            self.wire_reasoning = baseline.wire_reasoning
            body["reasoning"] = self.wire_reasoning
            if self.reasoning.get("effort") != baseline.reasoning.get("effort"):
                body["input"] = [{"type": "configuration_update", "reasoning": {
                    "effort": self.reasoning["effort"]}}] + tail
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        return message(data, self.turn_state), "%d/%d" % (len(body["input"]), len(self.items))

    def _tail(self, baseline):
        if baseline is None or not isinstance(baseline.response_id, str) or not baseline.response_id:
            return None, "none"
        if not self.supports_effort_updates and baseline.wire_reasoning != self.reasoning:
            return None, "fields"
        if baseline.fields != self.fields:
            if not (self.supports_effort_updates and self.reasoning.get("effort") and
                    baseline.reasoning.get("effort") and baseline.effort_fields == self.effort_fields):
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
        """Retain only this socket's last completed response; a full retry needs no hidden effort controls."""
        return Baseline(self, response_id, output)


class Baseline:
    """The original Messages projection and effective effort alongside the socket's actual wire reasoning."""

    def __init__(self, request, response_id, output):
        self.fields, self.digests = request.fields, request.digests
        self.effort_fields = request.effort_fields
        self.reasoning, self.wire_reasoning = request.reasoning, request.wire_reasoning
        self.response_id, self.output = response_id, output
