"""Tests for gpt_gateway.frames: RFC 6455 frames at every length form, fragments, pings, closes and idle checks."""
import socket
import struct
import threading
import unittest
from unittest import mock
import zlib

from gpt_gateway import frames
from gpt_gateway.upstream import UpstreamError


def server_frame(opcode, payload, fin=True):
    """A frame as a server sends it: unmasked."""
    n = len(payload)
    size = bytes([n]) if n < 126 else (bytes([126]) + struct.pack("!H", n) if n < 65536 else bytes([127]) + struct.pack("!Q", n))
    return bytes([(0x80 if fin else 0) | opcode]) + size + payload


def read_client_frame(sock):
    """(opcode, unmasked payload) of the next frame a client sent; it must be masked."""
    def exact(n):
        data = b""
        while len(data) < n:
            data += sock.recv(n - len(data))
        return data
    head = exact(2)
    assert head[1] & 0x80, "a client frame is masked"
    n = head[1] & 0x7F
    n = struct.unpack("!H", exact(2))[0] if n == 126 else struct.unpack("!Q", exact(8))[0] if n == 127 else n
    key = exact(4)
    return head[0] & 0x0F, frames.mask(exact(n), key)


class FramesTest(unittest.TestCase):
    def pair(self):
        left, right = socket.socketpair()
        for s in (left, right):
            s.settimeout(5)
            self.addCleanup(s.close)
        return frames.WebSocket(left), right

    def test_negotiated_deflate_compresses_sends_and_expands_fragmented_messages(self):
        ws, peer = self.pair()
        # Set on an existing socket so the unfixed implementation fails an assertion, not a signature error.
        ws.compression = True
        ws.server_no_context_takeover = True
        data = b"large shared prefix " * 1000
        ws.send(data)
        opcode, payload = read_client_frame(peer)
        self.assertLess(len(payload), len(data) // 10)
        self.assertEqual(opcode, frames.TEXT)
        self.assertEqual(zlib.decompressobj(-15).decompress(payload + b"\x00\x00\xff\xff"), data)
        compressor = zlib.compressobj(wbits=-15)
        compressed = compressor.compress(data) + compressor.flush(zlib.Z_SYNC_FLUSH)
        compressed = compressed[:-4]
        first = bytearray(server_frame(frames.TEXT, compressed[:5], fin=False)); first[0] |= 0x40
        peer.sendall(first + server_frame(frames.PING, b"p") + server_frame(frames.CONTINUATION, compressed[5:]))
        self.assertEqual(ws.receive(), data)
        self.assertEqual(read_client_frame(peer), (frames.PONG, b"p"))

    def test_compression_negotiation_and_invalid_frames_fail_closed(self):
        self.assertTrue(callable(getattr(frames, 'deflate_options', None)))
        self.assertEqual(frames.deflate_options(None), (False, False))
        self.assertEqual(frames.deflate_options('permessage-deflate; server_no_context_takeover'), (True, True))
        for value in ('unknown', 'permessage-deflate; unknown', 'permessage-deflate; server_no_context_takeover=1',
                      'permessage-deflate, permessage-deflate',
                      'permessage-deflate; client_no_context_takeover; client_no_context_takeover'):
            with self.subTest(value=value), self.assertRaisesRegex(UpstreamError, 'extension'):
                frames.deflate_options(value)
        for frame in (b"\xc9\x00", b"\xc0\x00", b"\xe1\x00"):
            with self.subTest(frame=frame), self.assertRaisesRegex(UpstreamError, 'reserved'):
                frames.parse_frame(bytearray(frame), True)
        ws, peer = self.pair(); ws.compression = True
        peer.sendall(b"\xc1\x01\xff")
        with self.assertRaisesRegex(UpstreamError, 'deflate'):
            ws.receive()

    def test_server_can_negotiate_a_smaller_window_without_an_offer(self):
        for bits in range(8, 16):
            for spelling in (str(bits), '"%d"' % bits):
                with self.subTest(bits=spelling):
                    extension = 'permessage-deflate; server_max_window_bits=' + spelling
                    try:
                        options = frames.deflate_options(extension)
                    except UpstreamError as error:
                        self.fail('valid server window negotiation rejected: %s' % error)
                    self.assertEqual(options, (True, False))
        for bits in range(9, 16):  # zlib's compressor does not support an eight-bit raw window.
            with self.subTest(inflated_bits=bits):
                plain, peer = self.pair()
                ws = frames.WebSocket(plain.sock, extension=(
                    'permessage-deflate; server_no_context_takeover; server_max_window_bits=%d' % bits))
                text = b'small-window content ' * 100
                compressor = zlib.compressobj(wbits=-bits)
                payload = (compressor.compress(text) + compressor.flush(zlib.Z_SYNC_FLUSH))[:-4]
                peer.sendall(server_frame(frames.TEXT | 0x40, payload))
                self.assertEqual(ws.receive(), text)
        for parameter in ('server_max_window_bits', 'server_max_window_bits=7', 'server_max_window_bits=16',
                          'server_max_window_bits=09', 'server_max_window_bits=""15""',
                          'server_max_window_bits=14; server_max_window_bits=15', 'client_max_window_bits=15'):
            with self.subTest(invalid=parameter), self.assertRaisesRegex(UpstreamError, 'extension'):
                frames.deflate_options('permessage-deflate; ' + parameter)

    def test_deflate_context_takeover_and_both_inflated_and_wire_limits(self):
        ws, peer = self.pair(); ws.compression = True
        compressor = zlib.compressobj(wbits=-15)
        for _ in range(2):
            text = b"shared dictionary content " * 100
            payload = (compressor.compress(text) + compressor.flush(zlib.Z_SYNC_FLUSH))[:-4]
            peer.sendall(server_frame(frames.TEXT | 0x40, payload))
            self.assertEqual(ws.receive(), text)
        ws, peer = self.pair(); ws.compression = True
        compressor = zlib.compressobj(wbits=-15)
        payload = (compressor.compress(b"x" * 65) + compressor.flush(zlib.Z_SYNC_FLUSH))[:-4]
        peer.sendall(server_frame(frames.TEXT | 0x40, payload))
        with mock.patch.object(frames, 'MESSAGE_LIMIT', 64), self.assertRaisesRegex(UpstreamError, 'limit'):
            ws.receive()
        ws, peer = self.pair(); ws.compression = True
        compressor = zlib.compressobj(wbits=-15)
        payload = b"".join(compressor.compress(b"x") + compressor.flush(zlib.Z_SYNC_FLUSH) for _ in range(50))[:-4]
        self.assertGreater(len(payload), 64)
        chunks = [payload[i:i + 32] for i in range(0, len(payload), 32)]
        peer.sendall(b"".join(server_frame(frames.TEXT | 0x40 if i == 0 else frames.CONTINUATION,
                                          chunk, fin=i == len(chunks) - 1) for i, chunk in enumerate(chunks)))
        with mock.patch.object(frames, 'MESSAGE_LIMIT', 64), self.assertRaisesRegex(UpstreamError, 'limit'):
            ws.receive()

    def test_mask_and_parse_frame_at_every_length_form(self):
        key = b"\x01\x02\x03\x04"
        self.assertEqual(frames.mask(b"abcdef", key), bytes(b ^ key[i % 4] for i, b in enumerate(b"abcdef")))
        self.assertEqual(frames.mask(b"", key), b"")
        for n in (0, 1, 125, 126, 65535, 65536, 70001):
            with self.subTest(n=n):
                payload = bytes(range(256)) * (n // 256) + bytes(n % 256)
                self.assertEqual(frames.mask(frames.mask(payload, key), key), payload)
                frame = server_frame(frames.TEXT, payload)
                self.assertEqual(frames.parse_frame(bytearray(frame + b"next")), (True, frames.TEXT, payload, len(frame)))
                for cut in {1, len(frame) - 1} - {0}:
                    self.assertIsNone(frames.parse_frame(bytearray(frame[:cut])))
        for data, text in ((b"\x81\x81abcdx", "masked"), (b"\xc1\x00", "reserved bits"),
                           (b"\x81\x7f" + struct.pack("!Q", frames.MESSAGE_LIMIT + 1), "exceeds the limit")):
            with self.subTest(text=text), self.assertRaisesRegex(UpstreamError, text) as cm:
                frames.parse_frame(bytearray(data))
            self.assertEqual(cm.exception.error_class, "protocol")  # the row's class of a frame the protocol forbids

    def test_receive_joins_fragments_answers_pings_and_sends_masked_frames(self):
        ws, peer = self.pair()
        peer.sendall(server_frame(frames.TEXT, b"he", fin=False) + server_frame(frames.PING, b"p") +
                     server_frame(frames.PONG, b"") + server_frame(frames.CONTINUATION, b"llo"))
        self.assertEqual(ws.receive(), b"hello")
        self.assertEqual(read_client_frame(peer), (frames.PONG, b"p"))
        big, got = b"x" * 70001, []
        reader = threading.Thread(target=lambda: got.append(read_client_frame(peer)))
        reader.start()  # the pair's buffer holds less than the frame: the peer reads while it is sent
        ws.send(big)
        reader.join(5)
        self.assertEqual(got, [(frames.TEXT, big)])

    def test_close_eof_binary_and_out_of_sequence_frames_end_the_receive(self):
        cases = [(server_frame(frames.CLOSE, struct.pack("!H", 1001) + b"going away"), ConnectionResetError,
                  "closed by the server: 1001 going away", None),  # a dropped socket, classed by its type: reset
                 (b"", ConnectionResetError, "without a close frame", None),
                 (server_frame(frames.BINARY, b"\x00"), UpstreamError, "binary message", "protocol"),
                 (server_frame(frames.CONTINUATION, b"x"), UpstreamError, "out of sequence", "protocol")]
        for data, error, text, cls in cases:
            with self.subTest(text=text):
                ws, peer = self.pair()
                peer.sendall(data)
                if not data:
                    peer.shutdown(socket.SHUT_WR)
                with self.assertRaisesRegex(error, text) as cm:
                    ws.receive()
                self.assertEqual(getattr(cm.exception, "error_class", None), cls)

    def test_alive_answers_idle_pings_and_sees_a_close_or_a_stray_message(self):
        ws, peer = self.pair()
        self.assertTrue(ws.alive())
        peer.sendall(server_frame(frames.PING, b"idle"))
        self.assertTrue(ws.alive())
        self.assertEqual(read_client_frame(peer), (frames.PONG, b"idle"))
        self.assertEqual(ws.sock.gettimeout(), 5)
        peer.sendall(server_frame(frames.TEXT, b"{}"))
        self.assertFalse(ws.alive())
        self.assertFalse(ws.alive(), "a socket found dead read as live once its stray frame was consumed")
        ws, peer = self.pair()
        peer.sendall(server_frame(frames.CLOSE, b"\x03\xe8"))  # the server ended it, its TCP still open
        self.assertFalse(ws.alive())
        self.assertFalse(ws.alive())
        ws, peer = self.pair()
        peer.close()
        self.assertFalse(ws.alive())
        ws.abort()
        self.assertFalse(ws.alive())

    def test_close_sends_a_normal_close_frame(self):
        ws, peer = self.pair()
        ws.close()
        self.assertEqual(read_client_frame(peer), (frames.CLOSE, struct.pack("!H", 1000)))
        self.assertTrue(ws.closed)


if __name__ == "__main__":
    unittest.main()
