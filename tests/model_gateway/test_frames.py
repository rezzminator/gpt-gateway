"""Tests for model_gateway.frames: RFC 6455 frames at every length form, fragments, pings, closes and idle checks."""
import socket
import struct
import threading
import unittest

from model_gateway import frames
from model_gateway.upstream import UpstreamError


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
        with self.assertRaisesRegex(UpstreamError, "masked"):
            frames.parse_frame(bytearray(b"\x81\x81abcdx"))
        with self.assertRaisesRegex(UpstreamError, "reserved bits"):
            frames.parse_frame(bytearray(b"\xc1\x00"))
        with self.assertRaisesRegex(UpstreamError, "exceeds the limit"):
            frames.parse_frame(bytearray(b"\x81\x7f" + struct.pack("!Q", frames.MESSAGE_LIMIT + 1)))

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
                  "closed by the server: 1001 going away"),
                 (b"", ConnectionResetError, "without a close frame"),
                 (server_frame(frames.BINARY, b"\x00"), UpstreamError, "binary message"),
                 (server_frame(frames.CONTINUATION, b"x"), UpstreamError, "out of sequence")]
        for data, error, text in cases:
            with self.subTest(text=text):
                ws, peer = self.pair()
                peer.sendall(data)
                if not data:
                    peer.shutdown(socket.SHUT_WR)
                with self.assertRaisesRegex(error, text):
                    ws.receive()

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
