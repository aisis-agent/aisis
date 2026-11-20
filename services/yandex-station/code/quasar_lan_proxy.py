#!/usr/bin/env python3
"""TCP-прокси: LISTEN_HOST:31116 -> 127.0.0.1:31116 (quasar-server API), только для ALLOWED_PEERS.

quasar-server в docker слушает только loopback; Home Assistant на другой
машине не может достучаться. Прокси не трогает контейнер
(пересоздание рвёт сессию колонки) и поднимается отдельным systemd-сервисом.

API за этим прокси без аутентификации: любой, кто до него дотянулся, может заставить
колонки произнести произвольный текст, послать им сырые директивы и переименовать/перенастроить
их (`POST /devices/all/push/text`, `/push/raw`, `PATCH /devices/:duid`). Поэтому прокси принимает
соединения только с адресов из ALLOWED_PEERS (адреса или CIDR через запятую: адрес Home Assistant)
и не стартует без этой переменной. `ALLOWED_PEERS=0.0.0.0/0` возвращает прежнее открытое поведение
осознанно. Остальные адреса получают немедленный разрыв соединения, не доходя до quasar.
"""
import ipaddress
import os
import socket
import socketserver
import sys
import threading
import time

LISTEN_HOST = os.environ.get("LISTEN_HOST", "192.168.0.56")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "31116"))
TARGET_HOST = os.environ.get("TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "31116"))

REFUSAL_LOG_INTERVAL_S = 60.0


def parse_allowed_peers(value: str) -> list:
    """Addresses and CIDR ranges, comma separated. An empty list is an error, never "everybody"."""
    networks = []
    for item in value.split(","):
        item = item.strip()
        if item:
            networks.append(ipaddress.ip_network(item, strict=False))
    if not networks:
        raise ValueError("ALLOWED_PEERS is empty: name the Home Assistant address (or 0.0.0.0/0 to keep the proxy open)")
    return networks


def peer_allowed(address: str, allowed_peers: list) -> bool:
    try:
        peer = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    return any(peer.version == network.version and peer in network for network in allowed_peers)


class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        if not peer_allowed(self.client_address[0], self.server.allowed_peers):
            self.server.note_refusal(self.client_address[0])
            return
        upstream = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
        sockets = [self.request, upstream]
        try:
            while True:
                self._pump(sockets)
        except OSError:
            pass
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _pump(sockets):
        import select

        ready, _, _ = select.select(sockets, [], [], 30)
        if not ready:
            return
        for src in ready:
            dst = sockets[1] if src is sockets[0] else sockets[0]
            data = src.recv(65536)
            if not data:
                raise OSError("closed")
            dst.sendall(data)


class ThreadingProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, allowed_peers):
        super().__init__(address, handler)
        self.allowed_peers = allowed_peers
        self._refusal_lock = threading.Lock()
        self._refusal_logged: dict[str, float] = {}

    def note_refusal(self, address: str) -> None:
        """One journal line per refused address and minute: a scan must not flush the journal."""
        now = time.monotonic()
        with self._refusal_lock:
            if len(self._refusal_logged) > 256:
                self._refusal_logged.clear()
            if now - self._refusal_logged.get(address, -REFUSAL_LOG_INTERVAL_S) < REFUSAL_LOG_INTERVAL_S:
                return
            self._refusal_logged[address] = now
        print(f"quasar LAN proxy refused {address}: not in ALLOWED_PEERS", flush=True)


if __name__ == "__main__":
    try:
        allowed = parse_allowed_peers(os.environ.get("ALLOWED_PEERS", ""))
    except ValueError as error:
        print(f"quasar LAN proxy: {error}", file=sys.stderr)
        sys.exit(2)
    server = ThreadingProxyServer((LISTEN_HOST, LISTEN_PORT), Proxy, allowed)
    print(f"quasar LAN proxy {LISTEN_HOST}:{LISTEN_PORT} -> {TARGET_HOST}:{TARGET_PORT}, "
          f"peers: {', '.join(str(network) for network in allowed)}")
    server.serve_forever()
