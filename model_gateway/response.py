"""The in-memory response the relay reads where no upstream HTTP response exists: a hook's answer, a ChatGPT-route body.

It offers the HTTPResponse surface the relay uses (`status`, `reason`, `getheaders`, `getheader`, `read1`, `read`)
over either bytes held in memory or a streaming reader whose `next()` returns the next chunk, empty at the end.
"""
from http import HTTPStatus
import json


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
