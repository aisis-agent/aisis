"""quasar_lan_proxy: only ALLOWED_PEERS reach the quasar API, and the proxy never starts without them."""
import os
import socket
import subprocess
import sys
import threading
import unittest
from pathlib import Path

import quasar_lan_proxy as proxy

HERE = Path(__file__).resolve().parent


class ParseTests(unittest.TestCase):
    def test_addresses_and_ranges(self):
        networks = proxy.parse_allowed_peers("192.0.2.7, 198.51.100.0/24 ,2001:db8::1")
        self.assertEqual([str(n) for n in networks], ["192.0.2.7/32", "198.51.100.0/24", "2001:db8::1/128"])

    def test_an_empty_list_is_an_error_not_everybody(self):
        for value in ("", " , ,"):
            with self.assertRaises(ValueError):
                proxy.parse_allowed_peers(value)

    def test_garbage_is_an_error(self):
        with self.assertRaises(ValueError):
            proxy.parse_allowed_peers("192.0.2.7, printer")

    def test_the_open_behaviour_is_an_explicit_choice(self):
        self.assertTrue(proxy.peer_allowed("203.0.113.9", proxy.parse_allowed_peers("0.0.0.0/0")))


class PeerTests(unittest.TestCase):
    def setUp(self):
        self.allowed = proxy.parse_allowed_peers("192.0.2.0/24, 2001:db8::1")

    def test_listed_and_unlisted(self):
        self.assertTrue(proxy.peer_allowed("192.0.2.7", self.allowed))
        self.assertTrue(proxy.peer_allowed("2001:db8::1", self.allowed))
        self.assertFalse(proxy.peer_allowed("198.51.100.9", self.allowed))
        self.assertFalse(proxy.peer_allowed("2001:db8::2", self.allowed))

    def test_a_mapped_ipv4_peer_is_matched_as_ipv4(self):
        self.assertTrue(proxy.peer_allowed("::ffff:192.0.2.7", self.allowed))
        self.assertFalse(proxy.peer_allowed("::ffff:198.51.100.9", self.allowed))

    def test_an_unreadable_address_is_refused(self):
        self.assertFalse(proxy.peer_allowed("not-an-address", self.allowed))


class ForwardingTests(unittest.TestCase):
    """Real sockets on loopback: an upstream that echoes, and the proxy in front of it."""

    def setUp(self):
        self.upstream = socket.socket()
        self.upstream.bind(("127.0.0.1", 0))
        self.upstream.listen()
        self.upstream_connections = 0
        threading.Thread(target=self._echo, daemon=True).start()
        self.addCleanup(self.upstream.close)
        previous = proxy.TARGET_PORT
        proxy.TARGET_PORT = self.upstream.getsockname()[1]
        self.addCleanup(setattr, proxy, "TARGET_PORT", previous)

    def _echo(self):
        while True:
            try:
                conn, _ = self.upstream.accept()
            except OSError:
                return
            self.upstream_connections += 1
            conn.sendall(conn.recv(1024))
            conn.close()

    def _serve(self, allowed: str) -> int:
        server = proxy.ThreadingProxyServer(("127.0.0.1", 0), proxy.Proxy, proxy.parse_allowed_peers(allowed))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _roundtrip(self, port: int) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.settimeout(3)
            try:
                client.sendall(b"ping")
                return client.recv(1024)
            except OSError:
                return b""

    def test_a_listed_peer_is_forwarded(self):
        self.assertEqual(self._roundtrip(self._serve("127.0.0.1")), b"ping")

    def test_an_unlisted_peer_gets_nothing_and_never_reaches_quasar(self):
        self.assertEqual(self._roundtrip(self._serve("192.0.2.0/24")), b"")
        self.assertEqual(self.upstream_connections, 0)


class StartupTests(unittest.TestCase):
    def test_without_allowed_peers_the_proxy_does_not_start(self):
        env = {key: value for key, value in os.environ.items() if key != "ALLOWED_PEERS"}
        done = subprocess.run([sys.executable, str(HERE / "quasar_lan_proxy.py")], env=env, capture_output=True, text=True,
                              timeout=10)
        self.assertEqual(done.returncode, 2)
        self.assertIn("ALLOWED_PEERS is empty", done.stderr)


if __name__ == "__main__":
    unittest.main()
