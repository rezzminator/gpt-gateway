"""RFC 6455 frames over a socket `model_gateway/upstream.py` upgraded: masked frames out, whole text messages in.

The client masks every frame it sends (§ 5.3); a server never masks its own. No extension is negotiated, so a frame
with a reserved bit set is a protocol error.
"""
import os
import socket
import ssl
import struct

from .upstream import UpstreamError

CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA  # § 5.2
MESSAGE_LIMIT = 64 * 1024 * 1024


def mask(data, key):
    """`data` XOR the 4-byte `key` repeated (RFC 6455 § 5.3), as one integer operation: a 2 MB body in milliseconds."""
    n = len(data)
    if not n:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


def parse_frame(buffer):
    """(fin, opcode, payload, size) of the whole frame at the start of `buffer`, or None while it is incomplete."""
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    if first & 0x70:
        raise UpstreamError("websocket protocol error: reserved bits %#x without a negotiated extension" % first)
    if second & 0x80:
        raise UpstreamError("websocket protocol error: the server masked a frame")
    length, start = second & 0x7F, 2
    if length == 126:
        if len(buffer) < 4:
            return None
        length, start = struct.unpack_from("!H", buffer, 2)[0], 4
    elif length == 127:
        if len(buffer) < 10:
            return None
        length, start = struct.unpack_from("!Q", buffer, 2)[0], 10
    if length > MESSAGE_LIMIT:
        raise UpstreamError("websocket protocol error: a %d-byte frame exceeds the limit" % length)
    if len(buffer) < start + length:
        return None
    return bool(first & 0x80), first & 0x0F, bytes(buffer[start:start + length]), start + length


class WebSocket:
    """One upgraded connection, driven by one thread at a time: masked frames out, whole text messages in, pings
    answered. A close frame, EOF or a reset raises ConnectionResetError; a frame the protocol forbids, UpstreamError."""

    def __init__(self, sock, pending=b"", log=None):
        self.sock, self.pending = sock, bytearray(pending)
        self.log = log if log is not None else (lambda line: None)
        self.closed = False

    def send(self, data, opcode=TEXT):
        key = os.urandom(4)
        n = len(data)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(head + key + mask(data, key))

    def _frame(self, block=True):
        while True:
            frame = parse_frame(self.pending)
            if frame is not None:
                del self.pending[:frame[3]]
                return frame[:3]
            if not block:
                return None
            got = self.sock.recv(65536)
            if not got:
                self.closed = True
                raise ConnectionResetError("websocket closed without a close frame")
            self.pending += got

    def receive(self):
        """The next text message's bytes; ping, pong and fragmentation stay inside."""
        parts, kind = [], None
        while True:
            fin, opcode, payload = self._frame()
            if opcode == PING:
                self.send(payload, PONG)
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                self.closed = True
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                raise ConnectionResetError("websocket closed by the server: %d %s" % (
                    code, payload[2:202].decode("utf-8", "replace")))
            if opcode in (TEXT, BINARY) and kind is None:
                kind = opcode
            elif opcode != CONTINUATION or kind is None:
                raise UpstreamError("websocket protocol error: opcode %#x out of sequence" % opcode)
            parts.append(payload)
            if fin:
                if kind == BINARY:
                    raise UpstreamError("websocket protocol error: a binary message")
                return b"".join(parts)

    def alive(self):
        """Whether a socket nobody read while it idled is still open; answers the pings the server sent meanwhile.
        False once the server closed it, reset it, or sent anything but a ping or pong."""
        if self.closed:
            return False
        try:
            timeout = self.sock.gettimeout()
            self.sock.setblocking(False)
            try:
                while True:
                    got = self.sock.recv(65536)
                    if not got:
                        return False
                    self.pending += got
            except (BlockingIOError, InterruptedError, ssl.SSLWantReadError):
                pass
            finally:
                self.sock.settimeout(timeout)
            while True:
                frame = self._frame(block=False)
                if frame is None:
                    return True
                if frame[1] == PING:
                    self.send(frame[2], PONG)
                elif frame[1] != PONG:
                    return False
        except (OSError, UpstreamError):
            return False

    def close(self):
        """A normal close frame while the socket is open, then the socket closed; never raises."""
        if not self.closed:
            self.closed = True
            try:
                self.sock.settimeout(1)
                self.send(struct.pack("!H", 1000), CLOSE)
            except OSError:
                pass
        self._release()

    def abort(self):
        """Shut the socket down, so a thread blocked reading it returns, then close it."""
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already shut or never connected: the close below is what matters
        self._release()

    def _release(self):
        try:
            self.sock.close()
        except OSError as e:
            self.log("    upstream websocket close failed: %r" % (e,))
