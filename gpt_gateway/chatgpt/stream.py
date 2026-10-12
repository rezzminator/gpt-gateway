"""Incremental Responses to Anthropic SSE, with exact terminal usage.

Distilled from claude-code-proxy, Copyright (c) 2026 Raine Virta (MIT;
permission notice in THIRD_PARTY_NOTICES.md). Usage fields derived from
openai/codex, changed (Apache-2.0; see THIRD_PARTY_NOTICES.md).
"""
from dataclasses import replace
import json
import uuid

from gpt_gateway import clock
from gpt_gateway.events import EventSplitter
from gpt_gateway.chatgpt import errors, signature


# claude-code-proxy/src/providers/codex/translate/live_stream.rs:284-301.
def _anthropic_frame(kind, **fields):
    data = json.dumps(dict(type=kind, **fields), ensure_ascii=False, separators=(",", ":"))
    return ("event: " + kind + "\ndata: " + data + "\n\n").encode("utf-8")


class Translator:
    """feed/idle/finish return only new bytes; terminal calls are idempotent.

    tool_names maps upstream names back to the original Anthropic names;
    estimate is an integer. Prelude frames stay private until content arrives
    (or max_tokens completes). emitted_any records returned bytes; once true
    the caller must not retry. error is always a Decision, with
    retries disabled after output. terminal is the event kind or synthetic
    stream_closed/idle_timeout/protocol_error. input_tokens is the upstream
    total; cache_ratio is cached / total. usage is the final Anthropic usage;
    reasoning_tokens is upstream's reasoning count, None when it reports none.
    empty is true once a response completed with neither text, a tool call nor signed reasoning.
    """

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:78-106.
    def __init__(self, slug, tool_names, estimate):
        self.slug, self.tool_names, self.estimate = slug, dict(tool_names), estimate
        self.emitted_any = False
        self.terminal = None
        self.usage = None
        self.error = None
        self.failure = None  # the failing event as its type, status and error, for the gpt error line
        self.cache_ratio = None
        self.input_tokens = None
        self.reasoning_tokens = None
        self.empty = False
        self._splitter = EventSplitter()
        self._blocks = {}
        self._done = set()
        self._next_index = 0
        self._has_content = False
        self._started = False
        self._pending = bytearray()
        self._tool_use = False
        self._last_upstream = self._last_ping = clock.monotonic()

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:251-277.
    def _start(self):
        if self._started:
            return b""
        self._started = True
        return _anthropic_frame("message_start", message={
            "id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
            "model": self.slug, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.estimate, "output_tokens": 0}})

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:116-191.
    def feed(self, chunk):
        if self.terminal or not chunk:
            return b""
        self._last_upstream = clock.monotonic()
        out = self._start()
        out += self._consume(self._splitter.feed(chunk))
        if self._splitter.overflowed and not self.terminal:
            out += self._fail("protocol_error", {"message": "SSE event exceeds the size limit"})
        return self._release(out)

    # claude-code-proxy/src/providers/codex/mod.rs:900-960; hold the prelude.
    def _release(self, out):
        if self.error is not None and not self._has_content and not self.emitted_any:
            self._pending.clear()
            return b""
        self._pending.extend(out)
        if not self._has_content and not (self.terminal and self.error is None):
            return b""
        result = bytes(self._pending)
        self._pending.clear()
        if result:
            self.emitted_any = True
        return result

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:201-207.
    def idle(self):
        if self.terminal:
            return b""
        now = clock.monotonic()
        if now - self._last_upstream >= 300:
            return self._release(self._fail("idle_timeout", {"code": "idle_timeout"}))
        if self.emitted_any and now - max(self._last_upstream, self._last_ping) >= 15:
            self._last_ping = now
            return self._release(_anthropic_frame("ping"))
        return b""

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:223-249.
    def finish(self):
        if self.terminal:
            return b""
        out = self._consume(self._splitter.finish())
        if not self.terminal:
            out += self._fail("stream_closed", {"code": "stream_closed"})
        return self._release(out)

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:303-403.
    def _open(self, output_index, kind, **fields):
        if output_index in self._blocks:
            return b""
        self._blocks[output_index] = self._next_index
        self._next_index += 1
        return _anthropic_frame("content_block_start", index=self._blocks[output_index],
                                content_block=dict(type=kind, **fields))

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:404-450.
    def _delta(self, output_index, kind, **fields):
        return _anthropic_frame("content_block_delta", index=self._blocks[output_index],
                                delta=dict(type=kind, **fields))

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:959-988.
    def _close(self, output_index):
        index = self._blocks.pop(output_index, None)
        self._done.add(output_index)
        return b"" if index is None else _anthropic_frame("content_block_stop", index=index)

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:223-249,959-988.
    def _fail(self, kind, payload):
        self.terminal = kind
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        self.failure = dict({key: value for key, value in payload.items() if key != "response"},
                            **{key: response[key] for key in ("status", "error", "incomplete_details") if key in response})
        decision = errors.classify(None, payload, "before-output")
        # Content earlier in this same feed commits along with the error frame.
        if not self.emitted_any and not self._has_content:
            self.error = decision
            self._pending.clear()
            return b""
        self.error = replace(decision, retry=False, refresh=False, drop_reasoning=False, drop_retention=False)
        out = b"".join(self._close(key) for key in list(self._blocks))
        return out + errors.classify(None, payload, "after-output")

    # openai/codex/codex-rs/codex-api/src/sse/responses.rs:344-505; derived, changed.
    def _consume(self, events):
        out = bytearray()
        for event in events:
            if self.terminal:
                break
            try:
                data = event["data"]
                if data is None and event["event"] == "keepalive":
                    data = {"type": "keepalive"}
                if not isinstance(data, dict):
                    raise ValueError("invalid SSE event JSON")
                out += self._translate(data.get("type") or event["event"], data)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                out += self._fail("protocol_error", {"message": "invalid Responses event: " + str(exc)})
        return bytes(out)

    # claude-code-proxy/src/providers/codex/translate/live_stream.rs:116-191,647-772,990-1071.
    def _translate(self, kind, data):
        if kind in ("response.created", "response.in_progress", "codex.rate_limits", "keepalive"):
            self._last_ping = clock.monotonic()
            return _anthropic_frame("ping")
        if kind in ("response.failed", "error"):
            return self._fail(kind, data)
        if kind in ("response.completed", "response.incomplete"):
            return self._complete(kind, data)
        key = data.get("output_index", 0)
        if key in self._done:
            return b""
        item = data.get("item") or {}
        item_type = item.get("type")
        if kind == "response.output_item.added":
            if item_type == "message":
                return self._open(key, "text", text="")
            if item_type == "reasoning":
                return self._open(key, "thinking", thinking="", signature="")
        elif kind == "response.output_text.delta":
            text = data["delta"]
            self._has_content |= bool(text)
            return self._open(key, "text", text="") + self._delta(key, "text_delta", text=text)
        elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_summary_part.added"):
            text = "\n\n" if kind.endswith("part.added") else data["delta"]
            self._has_content |= bool(text.strip())
            return (self._open(key, "thinking", thinking="", signature="") +
                    self._delta(key, "thinking_delta", thinking=text))
        elif kind == "response.output_item.done":
            out = b""
            if item_type == "reasoning":
                out = self._open(key, "thinking", thinking="", signature="")
                if item.get("encrypted_content"):
                    value = signature.encode(self.slug, item["id"], item["encrypted_content"])
                    out += self._delta(key, "signature_delta", signature=value)
                    self._has_content = True
            elif item_type == "function_call":
                name = item["name"]
                out = self._open(key, "tool_use", id=item["call_id"],
                                 name=self.tool_names.get(name, name), input={})
                out += self._delta(key, "input_json_delta", partial_json=item["arguments"])
                self._tool_use = self._has_content = True
            return out + self._close(key)
        return b""

    # openai/codex/codex-rs/codex-api/src/sse/responses.rs:117-156; derived, changed.
    # claude-code-proxy/src/providers/codex/translate/reducer.rs:1023-1052, changed: a completion with no content ends
    # its turn, as Codex ends it and as the Messages API answers end_turn with no content; Claude Code then asks once
    # for visible output, where a retried empty regenerates the same nothing and a final error ends the agent.
    def _complete(self, kind, data):
        response = data.get("response") or {}
        reason = "tool_use" if self._tool_use else "end_turn"
        if kind == "response.incomplete":
            if (response.get("incomplete_details") or {}).get("reason") != "max_output_tokens":
                return self._fail(kind, data)
            reason = "max_tokens"
        self.empty = not self._has_content
        raw = response.get("usage") or {}
        details = raw.get("input_tokens_details") or {}
        total = max(0, int(raw.get("input_tokens") or 0))
        cached = max(0, int(details.get("cached_tokens") or 0))
        written = max(0, int(details.get("cache_write_tokens", raw.get("cache_write_tokens")) or 0))
        self.input_tokens = total
        reasoning = (raw.get("output_tokens_details") or {}).get("reasoning_tokens")
        self.reasoning_tokens = max(0, int(reasoning)) if reasoning is not None else None
        self.cache_ratio = cached / total if total else 0.0
        self.usage = {"input_tokens": max(0, total - cached - written),
                      "output_tokens": max(0, int(raw.get("output_tokens") or 0)),
                      "cache_creation_input_tokens": written, "cache_read_input_tokens": cached}
        self.terminal = kind
        out = b"".join(self._close(key) for key in list(self._blocks))
        out += _anthropic_frame("message_delta", delta={"stop_reason": reason, "stop_sequence": None},
                                usage=self.usage)
        return out + _anthropic_frame("message_stop")


# claude-code-proxy/src/providers/codex/translate/accumulate.rs:55-283.
def accumulate(events_bytes):
    """Fold translated Anthropic SSE to a message, or an error envelope on failure."""
    splitter = EventSplitter()
    message, blocks, arguments = None, {}, {}
    stopped = False
    for event in splitter.feed(events_bytes) + splitter.finish():
        data = event["data"]
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        if kind == "error":
            return data
        if kind == "message_start":
            message = data["message"]
        elif kind == "content_block_start":
            blocks[data["index"]] = data["content_block"]
        elif kind == "content_block_delta":
            index, delta = data["index"], data["delta"]
            block = blocks.get(index)
            if block is None:
                continue
            field = {"text_delta": "text", "thinking_delta": "thinking",
                     "signature_delta": "signature"}.get(delta.get("type"))
            if field:
                block[field] = block.get(field, "") + delta[field]
            elif delta.get("type") == "input_json_delta":
                arguments[index] = arguments.get(index, "") + delta["partial_json"]
        elif kind == "message_delta" and message is not None:
            message.update(data["delta"])
            message["usage"].update(data.get("usage") or {})
        elif kind == "message_stop":
            stopped = True
    if message is None or not stopped or splitter.overflowed:
        return errors.classify(None, {"code": "stream_closed"}, "before-output").client_body
    for index, text in arguments.items():
        try:
            blocks[index]["input"] = json.loads(text)
        except ValueError:
            blocks[index]["input"] = text
    message["content"] = [blocks[index] for index in sorted(blocks)]
    return message
