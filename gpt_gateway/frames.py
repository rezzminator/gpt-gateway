"""RFC 6455 frames over a socket `gpt_gateway/upstream.py` upgraded: masked frames out, whole text messages in.

The client masks every frame it sends (§ 5.3); a server never masks its own. RFC 7692 permessage-deflate is used
only after negotiation; inflation and fragmented messages share the same bounded message limit.
"""
import os
import socket
import ssl
import struct
import zlib

from .upstream import UpstreamError

CONTINUATION, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA  # § 5.2
MESSAGE_LIMIT = 64 * 1024 * 1024
SEND_SECONDS = 2  # how long `alive` waits to send a pong or a ping: it runs on a thread that must not hang


def mask(data, key):
    """`data` XOR the 4-byte `key` repeated (RFC 6455 § 5.3), as one integer operation: a 2 MB body in milliseconds."""
    n = len(data)
    if not n:
        return b""
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


def deflate_options(value):
    """Validate the response to our fixed RFC 7692 offer; return (enabled, server_no_context_takeover)."""
    if value is None:
        return False, False
    fields = [part.strip().lower() for part in value.split(";")]
    parameters = [tuple(part.strip() for part in field.partition("=")) for field in fields[1:]]
    names = [name for name, _, _ in parameters]
    valid = fields[0] == "permessage-deflate" and len(names) == len(set(names))
    for name, equals, argument in parameters:
        if name == "server_max_window_bits":
            # RFC 7692 §7.1.2.1 permits this even without an offer. Our -15 inflater accepts every smaller window.
            valid = valid and bool(equals) and argument in tuple(
                spelling for bits in range(8, 16) for spelling in (str(bits), '"%d"' % bits))
        else:
            valid = valid and not equals and name in {"client_no_context_takeover", "server_no_context_takeover"}
    if not valid:
        raise UpstreamError("websocket protocol error: unsupported deflate extension %r" % value[:200], "protocol")
    return True, "server_no_context_takeover" in names


def parse_frame(buffer, compression=False):
    """(fin, opcode, payload, size) of the whole frame at the start of `buffer`, or None while it is incomplete."""
    if len(buffer) < 2:
        return None
    first, second = buffer[0], buffer[1]
    if first & 0x30 or first & 0x40 and (not compression or first & 0x0F not in (TEXT, BINARY)):
        raise UpstreamError("websocket protocol error: reserved bits %#x without a negotiated extension" % first,
                            "protocol")
    if second & 0x80:
        raise UpstreamError("websocket protocol error: the server masked a frame", "protocol")
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
        raise UpstreamError("websocket protocol error: a %d-byte frame exceeds the limit" % length, "protocol")
    if len(buffer) < start + length:
        return None
    return bool(first & 0x80), first & 0x4F, bytes(buffer[start:start + length]), start + length


class WebSocket:
    """One upgraded connection, driven by one thread at a time: masked frames out, whole text messages in, pings
    answered. A close frame, EOF or a reset raises ConnectionResetError; a frame the protocol forbids, UpstreamError."""

    def __init__(self, sock, pending=b"", event=None, extension=None):
        self.sock, self.pending = sock, bytearray(pending)
        self.event = event if event is not None else (lambda kind, **fields: None)
        self.closed = False
        self.compression, self.server_no_context_takeover = deflate_options(extension)
        self.inflater = None

    def send(self, data, opcode=TEXT):
        compressed = self.compression and opcode == TEXT and bool(data)
        if compressed:
            compressor = zlib.compressobj(wbits=-15)
            data = (compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH))[:-4]
        key = os.urandom(4)
        opcode |= 0x40 if compressed else 0
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
            frame = parse_frame(self.pending, self.compression)
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
        parts, kind, compressed, size, wire_size = [], None, False, 0, 0
        while True:
            fin, opcode, payload = self._frame()
            flagged, opcode = bool(opcode & 0x40), opcode & 0x0F
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
                kind, compressed = opcode, flagged
                if compressed and (self.inflater is None or self.server_no_context_takeover):
                    self.inflater = zlib.decompressobj(-15)
            elif opcode != CONTINUATION or kind is None:
                raise UpstreamError("websocket protocol error: opcode %#x out of sequence" % opcode, "protocol")
            wire_size += len(payload)
            if wire_size > MESSAGE_LIMIT:
                raise UpstreamError("websocket protocol error: message exceeds the wire limit", "protocol")
            if compressed:
                try:
                    payload = self.inflater.decompress(payload + (b"\x00\x00\xff\xff" if fin else b""),
                                                       MESSAGE_LIMIT - size + 1)
                except zlib.error as error:
                    raise UpstreamError("websocket protocol error: invalid deflate payload: %s" % error, "protocol") from error
                if self.inflater.unused_data:
                    raise UpstreamError("websocket protocol error: unexpected end of deflate stream", "protocol")
            size += len(payload)
            if size > MESSAGE_LIMIT:
                raise UpstreamError("websocket protocol error: message exceeds the limit", "protocol")
            parts.append(payload)
            if fin:
                if kind == BINARY:
                    raise UpstreamError("websocket protocol error: a binary message", "protocol")
                return b"".join(parts)

    def alive(self, ping=False):
        """Whether a socket nobody read while it idled is still open; answers the pings the server sent meanwhile, and
        with `ping` sends one of its own after them. False once the server closed it, reset it, or sent anything but a
        ping or pong. Sends wait at most SEND_SECONDS."""
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
            self.sock.settimeout(SEND_SECONDS)
            try:
                while True:
                    frame = self._frame(block=False)
                    if frame is None:
                        break
                    if frame[1] == PING:
                        self.send(frame[2], PONG)
                    elif frame[1] != PONG:  # a close or a stray message: dead for good, whoever asks next
                        self.closed = True
                        return False
                if ping:
                    self.send(b"", PING)
                return True
            finally:
                self.sock.settimeout(timeout)
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
            self.event("upstream_socket", what="websocket_close", error=repr(e))
