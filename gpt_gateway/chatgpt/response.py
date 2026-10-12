"""The reader thread behind a translated SSE body, read by the core's relay through `gpt_gateway.response.Response`."""
import queue
import threading

from .. import clock
from . import errors

EOF = object()


class Reader:
    """One cancellable reader thread; only the serving thread drives the translator."""

    def __init__(self, upstream, translator, client):
        self.upstream, self.translator, self.client = upstream, translator, client
        self.queue = queue.Queue(8)
        self.stop = threading.Event()
        self.finished = False
        self.drained = False
        self.error_head = b""
        self.transport_error = None
        self.transport_exception = None
        self.first_byte_at = None  # monotonic time of the first upstream body byte
        self.terminal_at = None  # monotonic time the translator saw the terminal event
        self.ended_at = None  # monotonic time the serving thread asked past the terminal, its last bytes relayed
        self.on_end = None
        self.next_idle = clock.monotonic() + 15
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.client.watch(self)
        self.thread.start()

    def _put(self, item):
        while not self.stop.is_set():
            try:
                self.queue.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            while not self.stop.is_set():
                chunk = self.upstream.read1(65536)
                if not chunk:
                    self.drained = True
                    self._put(EOF)
                    break
                if self.first_byte_at is None:
                    self.first_byte_at = clock.monotonic()
                self._put(chunk)
        except Exception as error:
            self._put(error)
        finally:
            self.upstream.close()

    def close(self):
        self.stop.set()
        self.client.drop()
        self.thread.join(2.0)

    def complete(self):
        """Whether the upstream's response was read to its end and its connection can serve another: the terminal event
        arrived without an error, and the reader thread drained the body. A terminal event the reader thread queued but
        the relay never asked for counts: the client that left is the only one missing it."""
        self._absorb()
        if not self.translator.terminal or self.translator.error is not None:
            return False
        self.thread.join(2.0)
        return self.drained and not self.thread.is_alive() and not self.upstream.will_close

    def abandon(self):
        """The client left. Nothing more is read from the upstream, but what it already reported and the reader queued
        still reaches the translator, so a finished response keeps its usage; any other stays unreported, never zero."""
        if self.finished:
            return
        self.finished = True
        self.stop.set()
        self.thread.join(2.0)
        self._absorb()
        if self.translator.terminal:
            self.ended_at = clock.monotonic()
            self.terminal_at = self.terminal_at or self.ended_at
            if self.on_end:
                self.on_end()

    def _absorb(self):
        """Feed the translator what the reader thread already queued, up to the terminal event, never waiting on the
        upstream; only for a relay that broke, so the translation goes nowhere."""
        while not self.translator.terminal:
            try:
                chunk = self.queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(chunk, bytes):
                self.translator.feed(chunk)

    def next(self):
        """Empty means the true terminal; empty translation chunks stay inside this loop."""
        while not self.finished:
            if self.translator.terminal:
                self.finished = True
                self.ended_at = clock.monotonic()
                if self.complete():
                    self.stop.set()
                else:
                    self.close()
                if self.on_end:
                    self.on_end()
                return b""
            try:
                chunk = self.queue.get(timeout=max(0, self.next_idle - clock.monotonic()))
            except queue.Empty:
                chunk = None
            if chunk is None:
                self.next_idle = clock.monotonic() + 15
                data = self.translator.idle()
            elif chunk is EOF:
                data = self.translator.finish()
            elif isinstance(chunk, Exception):
                self.transport_exception = chunk
                self.transport_error = str(chunk)
                self.error_head = self.transport_error.encode()[:300]
                if not self.translator.emitted_any:
                    self.translator.error = errors.classify(None, {"message": self.transport_error}, "before-output")
                    self.translator.terminal = "stream_closed"
                    data = b""
                else:
                    data = self.translator.finish()
            else:
                # Only retained for failure reporting, never logged for successful requests.
                self.error_head = (self.error_head + chunk)[:300]
                self.next_idle = clock.monotonic() + 15
                data = self.translator.feed(chunk)
            if self.terminal_at is None and self.translator.terminal:
                self.terminal_at = clock.monotonic()
            if data:
                return data
        return b""
