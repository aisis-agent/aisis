"""ha_ws: the WebSocket client keeps what it has read when a frame arrives in pieces, and one deadline covers the whole connection setup."""
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_client  # noqa: E402
import ha_fakes  # noqa: E402
import ha_ws  # noqa: E402


def frame(payload: bytes, opcode: int = 1, final: bool = True) -> bytes:
    """A server frame (unmasked): the 7-bit length form is enough for the short messages here."""
    return bytes([(0x80 if final else 0) | opcode, len(payload)]) + payload


class FramesInPieces(unittest.TestCase):
    def setUp(self):
        self.near, self.far = socket.socketpair()
        self.addCleanup(self.near.close)
        self.addCleanup(self.far.close)
        self.ws = ha_ws.WebSocket(self.near)

    def test_a_payload_that_arrives_after_its_header_is_not_lost(self):
        message = frame(json.dumps({"id": 7, "type": "result"}).encode())
        self.far.sendall(message[:2])                                   # the header, then silence for longer than one receive window
        self.assertIsNone(self.ws.recv_json(0.05))
        self.far.sendall(message[2:])
        self.assertEqual(self.ws.recv_json(0.5), {"id": 7, "type": "result"})

    def test_a_message_in_two_fragments_is_joined_across_receive_calls(self):
        text = json.dumps({"id": 8, "type": "event"}).encode()
        self.far.sendall(frame(text[:6], opcode=1, final=False))
        self.assertIsNone(self.ws.recv_json(0.05))                     # only the first fragment is here
        self.far.sendall(frame(text[6:], opcode=0, final=True))
        self.assertEqual(self.ws.recv_json(0.5), {"id": 8, "type": "event"})

    def test_a_ping_between_the_pieces_is_answered_and_the_message_survives(self):
        text = json.dumps({"id": 9, "type": "result"}).encode()
        self.far.sendall(frame(text[:4], opcode=1, final=False) + frame(b"hi", opcode=9) + frame(text[4:], opcode=0, final=True))
        self.assertEqual(self.ws.recv_json(0.5), {"id": 9, "type": "result"})
        self.far.settimeout(0.5)
        self.assertEqual(self.far.recv(16)[0] & 0x0F, 10)              # the pong


class OneDeadlineForTheSetup(unittest.TestCase):
    def test_a_slow_token_leaves_the_upgrade_only_what_is_left_of_the_deadline(self):
        ha = ha_fakes.FakeHomeAssistant()
        ha.hang_ws = True
        self.addCleanup(ha.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ha_client.Store(Path(tmp.name) / "connection.json")
        store.save(ha_client.Connection(ha.url, "long_lived", "llat"))
        client = ha_client.HaClient(store, discover_fn=lambda **_: [])

        def slow_token(force=False, timeout=3.0):
            time.sleep(0.25)
            return "llat"
        started = time.monotonic()
        with mock.patch.object(client, "access_token", slow_token), self.assertRaises(ha_ws.WsError):
            ha_ws.Session.open(client, timeout=0.3)
        self.assertLess(time.monotonic() - started, 0.45)              # 0.25 s of token and 0.3 s of upgrade would be 0.55 s

    def test_a_server_that_trickles_the_upgrade_answer_cannot_hold_the_setup_past_the_deadline(self):
        import threading
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        done = threading.Event()
        self.addCleanup(done.set)

        def trickle():
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                while not done.is_set():
                    try:
                        conn.sendall(b"X-Slow: 1\r\n")         # never the blank line, new bytes before every read window ends
                    except OSError:
                        return
                    done.wait(0.05)
        threading.Thread(target=trickle, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(ha_ws.WsError) as caught:
            ha_ws.WebSocket.open(f"http://127.0.0.1:{listener.getsockname()[1]}/api/websocket", timeout=0.3)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
