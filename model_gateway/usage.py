"""The request row's token usage and error code, read off a relayed response after each chunk is relayed.

An SSE body gives its usage in `message_start` (`message.usage`), overlaid by each `message_delta` usage, whose counts
are cumulative; an `event: error` gives its `error.type`. A JSON body gives its `usage`, or an error reply's
`error.type`. Only those events are parsed: any other event is skipped at its `event:` line, and one past MAX_EVENT
bytes is dropped rather than held, so the meter never keeps the body. A body it cannot read says why in `unread`.
"""
import json

WANTED = ("message_start", "message_delta", "error")
SEPARATORS = (b"\r\n\r\n", b"\n\n")
MAX_EVENT = 1024 * 1024  # an SSE event the meter would hold past this is dropped
MAX_JSON = 16 * 1024 * 1024  # a JSON body past this is not read
HEAD = 600  # bytes of an error event the row keeps


def count(value):
    """A token count as the API sends it, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class Meter:
    """Fed the relayed chunks of one response; `fields()` is its share of the request row."""

    def __init__(self, content_type):
        content_type = (content_type or "").lower()
        self.mode = "sse" if "text/event-stream" in content_type else "json" if "json" in content_type else None
        self.pending = bytearray()
        self.scanned = 0  # bytes of `pending` already searched for a separator
        self.skipping = False  # inside an event dropped for its size
        self.separators = SEPARATORS  # the stream's own once its first event ends, so each search stops there
        self.usage = None
        self.error_code = self.error_head = None
        self.unread = None  # why no usage could be read: too_large, not_json, no_usage, bad_usage

    def feed(self, chunk):
        if self.mode == "json":
            if len(self.pending) + len(chunk) > MAX_JSON:
                self.mode, self.unread, self.pending = None, "too_large", bytearray()
            else:
                self.pending += chunk
        elif self.mode == "sse":
            self.pending += chunk
            self._split()

    def _split(self):
        while True:
            start = max(0, self.scanned - 3)
            found = [(i, sep) for sep in self.separators for i in (self.pending.find(sep, start),) if i >= 0]
            if not found:
                self.scanned = len(self.pending)
                if len(self.pending) > MAX_EVENT:
                    self.pending, self.scanned, self.skipping = bytearray(), 0, True
                return
            end, sep = min(found)
            self.separators = (sep,)
            block = bytes(self.pending[:end])
            del self.pending[:end + len(sep)]
            self.scanned = 0
            if self.skipping:
                self.skipping = False  # the dropped event's tail
            else:
                self._event(block)

    def _event(self, block):
        name, data = None, []
        for line in block.splitlines():
            field, _, value = line.partition(b":")
            value = value[1:] if value.startswith(b" ") else value
            if field == b"event":
                name = value.decode("utf-8", "replace")
                if name not in WANTED:
                    return
            elif field == b"data":
                data.append(value)
        if name not in WANTED or not data:
            return
        try:
            event = json.loads(b"\n".join(data))
        except (ValueError, RecursionError):  # RecursionError: a nesting too deep to parse
            self.unread = "not_json"
            return
        if not isinstance(event, dict):
            return
        if name == "error":
            self._error(event, b"\n".join(data))
        elif name == "message_start":
            message = event.get("message")
            self._usage(message.get("usage") if isinstance(message, dict) else None, replace=True)
        else:
            self._usage(event.get("usage"), replace=False)

    def _usage(self, usage, replace):
        if not isinstance(usage, dict):
            return
        if replace or self.usage is None:
            self.usage = dict(usage)
        else:
            self.usage.update((k, v) for k, v in usage.items() if v is not None)

    def _error(self, body, raw):
        error = body.get("error")
        kind = error.get("type") if isinstance(error, dict) else None
        if isinstance(kind, str):
            self.error_code, self.error_head = kind, raw[:HEAD].decode("utf-8", "replace")

    def finish(self):
        """Read a JSON body once it has all arrived."""
        if self.mode != "json":
            return
        self.mode = None
        try:
            body = json.loads(bytes(self.pending))
        except (ValueError, RecursionError):  # RecursionError: a nesting too deep to parse
            self.unread = "not_json"
            return
        finally:
            self.pending = bytearray()
        if isinstance(body, dict):
            if body.get("type") == "error":
                error = body.get("error")
                if isinstance(error, dict) and isinstance(error.get("type"), str):
                    self.error_code = error["type"]
            self._usage(body.get("usage"), replace=True)

    def fields(self):
        """The row's token columns when a usage arrived, and its error code when an error did."""
        out = {}
        if self.error_code is not None:
            out["error_code"] = self.error_code
            if self.error_head is not None:
                out["error_head"] = self.error_head
        if self.usage is None:
            self.unread = self.unread or "no_usage"
            return out
        fresh, read, written = (count(self.usage.get(k, 0)) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        output = count(self.usage.get("output_tokens", 0))
        details = self.usage.get("output_tokens_details")
        thinking = count(details.get("thinking_tokens")) if isinstance(details, dict) else None
        if None in (fresh, read, written, output):
            self.unread = "bad_usage"
            return out
        total = fresh + read + written
        out.update(input_tokens=total, cache_read_tokens=read, cache_write_tokens=written, output_tokens=output,
                   reasoning_tokens=thinking, hit=read / total if total else 0.0)
        return out
