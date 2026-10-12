"""Server-sent events split out of a byte stream across chunk boundaries, and the one-call JSON event.

An event is `{"event": name or None, "data": parsed JSON or None, "raw": bytes}`; `raw` is the
event's lines without the blank line that ended it. Splitting never holds a chunk back: the caller
relays first and feeds the splitter after.
"""
import json

SEPARATORS = (b"\r\n\r\n", b"\n\n", b"\r\r")
MAX_PENDING = 16 * 1024 * 1024


def parse_event(block):
    """The event of one SSE block, or None for a block of comments and blank lines only."""
    name, data = None, []
    for line in block.splitlines():
        if not line or line.startswith(b":"):
            continue
        field, _, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        if field == b"event":
            name = value.decode("utf-8", "replace")
        elif field == b"data":
            data.append(value)
    if name is None and not data:
        return None
    parsed = None
    if data:
        try:
            parsed = json.loads(b"\n".join(data))
        except ValueError:
            parsed = None
    return {"event": name, "data": parsed, "raw": bytes(block)}


def json_event(body):
    """The single event of a non-streaming response: its whole body, parsed when it is JSON."""
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    return {"event": None, "data": data, "raw": bytes(body)}


class EventSplitter:
    """feed(chunk) returns the events the chunk completed; finish() returns an unterminated last one.

    The pending bytes are scanned once: a search resumes where the last one ended, less the
    longest separator's length, so a large event arriving in many chunks costs linear time.
    """

    LOOKBACK = max(len(sep) for sep in SEPARATORS) - 1

    def __init__(self, max_pending=MAX_PENDING):
        self.pending = bytearray()
        self.scanned = 0  # pending[:scanned] holds no complete separator
        self.max_pending = max_pending
        self.overflowed = 0  # events dropped because they grew past max_pending without ending

    def next_separator(self):
        best = None
        for sep in SEPARATORS:
            i = self.pending.find(sep, self.scanned)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, len(sep))
        return best

    def feed(self, chunk):
        self.pending += chunk
        out = []
        while True:
            found = self.next_separator()
            if found is None:
                self.scanned = max(0, len(self.pending) - self.LOOKBACK)
                break
            i, n = found
            block = bytes(self.pending[:i])
            del self.pending[:i + n]
            self.scanned = 0
            event = parse_event(block)
            if event is not None:
                out.append(event)
        if len(self.pending) > self.max_pending:
            self.pending = bytearray()
            self.scanned = 0
            self.overflowed += 1
        return out

    def finish(self):
        block = bytes(self.pending)
        self.pending, self.scanned = bytearray(), 0
        event = parse_event(block) if block.strip() else None
        return [event] if event is not None else []
