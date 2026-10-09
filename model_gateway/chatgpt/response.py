"""Response-like JSON and translated SSE bodies consumed by the core's single relay."""
from http import HTTPStatus
import json
import queue
import threading

from .. import clock
from . import errors

EOF = object()


class Response:
    """The HTTPResponse surface the relay uses, with either bytes or a streaming reader."""

    def __init__(self, status, data=b"", headers=None, reader=None):
        self.status = status
        self.reason = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Error"
        self.headers = {"content-type": "text/event-stream" if reader else "application/json", **(headers or {})}
        self.data, self.reader = data, reader

    @classmethod
    def json(cls, status, body, headers=None):
        return cls(status, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(), headers)

    def getheaders(self):
        return list(self.headers.items())

    def getheader(self, name, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), default)

    def read1(self, n):
        if not self.data and self.reader:
            self.data = self.reader.next()
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def read(self):
        chunks = []
        while True:
            chunk = self.read1(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


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
                self._put(chunk)
        except Exception as error:
            self._put(error)
        finally:
            self.upstream.close()

    def close(self):
        self.stop.set()
        self.client.drop()
        self.thread.join(2.0)

    def next(self):
        """Empty means the true terminal; empty translation chunks stay inside this loop."""
        while not self.finished:
            if self.translator.terminal:
                self.finished = True
                if self.translator.error is None:
                    self.thread.join(2.0)
                if (self.translator.error is None and self.drained and not self.thread.is_alive()
                        and not self.upstream.will_close):
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
            if data:
                return data
        return b""
