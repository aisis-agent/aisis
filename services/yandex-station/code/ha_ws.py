"""Home Assistant's WebSocket API (`/api/websocket`) over the one HaClient: connect, authenticate with the client's access token, send commands,
read the answers and events. A minimal RFC 6455 client (stdlib only, like ha_client.py): text frames, ping/pong, close.

The protocol is Home Assistant's own: `auth_required` -> `auth` -> `auth_ok`, then commands `{"id": n, "type": ...}` answered by `{"type": "result"}`
and, for subscriptions, `{"type": "event"}` frames. ha_call.py builds the confirmed service call on it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
from typing import Any

import ha_client

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 8 * 1024 * 1024


class WsError(ha_client.HaError):
    """The WebSocket could not be opened or broke; `kind` is the transport kind of ha_client.classify (or `protocol`)."""

    def __init__(self, message: str, kind: str = "other", status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status      # the HTTP status when the upgrade was refused


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode_frame(payload: bytes, opcode: int = 1, mask: bool = True) -> bytes:
    head = bytes([0x80 | opcode])
    size = len(payload)
    flag = 0x80 if mask else 0
    if size < 126:
        head += bytes([flag | size])
    elif size < 65536:
        head += bytes([flag | 126]) + struct.pack(">H", size)
    else:
        head += bytes([flag | 127]) + struct.pack(">Q", size)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""

    @classmethod
    def open(cls, url: str, headers: dict[str, str] | None = None, timeout: float = 3.0) -> "WebSocket":
        """The upgrade handshake against `http(s)://host:port/path` (the scheme is the server's, ws/wss follows from it)."""
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme in ("https", "wss")
        host, port = parts.hostname or "", parts.port or (443 if secure else 80)
        try:
            raw = socket.create_connection((host, port), timeout)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if secure else raw
        except (OSError, ssl.SSLError) as err:
            raise WsError(f"WebSocket не открылся: {err}", ha_client.classify(err)) from err
        key = base64.b64encode(os.urandom(16)).decode()
        request = (f"GET {parts.path or '/'}{'?' + parts.query if parts.query else ''} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   + "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()) + "\r\n")
        ws = cls(sock)
        try:
            sock.settimeout(timeout)
            sock.sendall(request.encode())
            head = ws._read_until(b"\r\n\r\n")
        except (OSError, ssl.SSLError) as err:
            sock.close()
            raise WsError(f"WebSocket не открылся: {err}", ha_client.classify(err)) from err
        lines = head.decode("latin-1").split("\r\n")
        code = (lines[0].split(" ") + ["", ""])[1]
        if code != "101":
            sock.close()
            raise WsError(f"WebSocket отклонён: {lines[0]}", "protocol", int(code) if code.isdigit() else None)
        sent = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if ":" in line)}
        if sent.get("sec-websocket-accept") != accept_key(key):
            sock.close()
            raise WsError("WebSocket: неверный ключ подтверждения", "protocol")
        return ws

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WsError("WebSocket закрыт при рукопожатии", "reset")
            self._buffer += chunk
        head, _, self._buffer = self._buffer.partition(marker)
        return head

    def _read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WsError("WebSocket закрыт", "reset")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def send_json(self, message: dict[str, Any]) -> None:
        try:
            self._sock.sendall(encode_frame(json.dumps(message).encode()))
        except (OSError, ssl.SSLError) as err:
            raise WsError(f"WebSocket оборвался: {err}", ha_client.classify(err)) from err

    def recv_json(self, timeout: float) -> dict[str, Any] | None:
        """The next message, None when nothing came within `timeout`. Pings are answered, a close or a broken stream raises WsError."""
        deadline = time.monotonic() + max(0.0, timeout)
        message = b""
        while True:
            left = deadline - time.monotonic()
            if left <= 0 and not self._buffer:
                return None
            self._sock.settimeout(max(0.01, left))
            try:
                first, second = self._read(2)
                size = second & 0x7F
                if size == 126:
                    size = struct.unpack(">H", self._read(2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", self._read(8))[0]
                if size > MAX_FRAME:
                    raise WsError("WebSocket: слишком большой кадр", "protocol")
                key = self._read(4) if second & 0x80 else b""
                payload = self._read(size)
            except (socket.timeout, TimeoutError):
                return None
            except (OSError, ssl.SSLError) as err:
                raise WsError(f"WebSocket оборвался: {err}", ha_client.classify(err)) from err
            if key:
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 8:
                raise WsError("WebSocket закрыт сервером", "reset")
            if opcode == 9:
                self._sock.sendall(encode_frame(payload, 10))
                continue
            if opcode == 10:
                continue
            message += payload
            if first & 0x80:
                try:
                    decoded = json.loads(message.decode("utf-8"))
                except ValueError as err:
                    raise WsError("WebSocket: не JSON", "protocol") from err
                return decoded if isinstance(decoded, dict) else None

    def close(self) -> None:
        try:
            self._sock.sendall(encode_frame(b"", 8))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class Session:
    """An authenticated WebSocket of the client's Home Assistant. Commands get increasing ids; `call` waits for the matching result."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._next = 0

    @classmethod
    def open(cls, client: ha_client.HaClient, timeout: float = 3.0) -> "Session":
        conn = client.connection()
        token = client.access_token(timeout=timeout)
        for attempt in (0, 1):
            ws = WebSocket.open(conn.url.rstrip("/") + "/api/websocket", timeout=timeout)
            try:
                first = ws.recv_json(timeout)
                if not first or first.get("type") != "auth_required":
                    raise WsError("Home Assistant не запросил авторизацию WebSocket", "protocol")
                ws.send_json({"type": "auth", "access_token": token})
                answer = ws.recv_json(timeout) or {}
            except BaseException:
                ws.close()
                raise
            if answer.get("type") == "auth_ok":
                return cls(ws)
            ws.close()
            if attempt == 0 and conn.kind == "oauth":
                token = client.access_token(force=True, timeout=timeout)    # the token expired between the fetch and now
                continue
            raise ha_client.AuthRevoked(str(answer.get("message") or "auth_invalid"))
        raise WsError("WebSocket: авторизация не удалась", "protocol")

    def send(self, type_: str, **fields: Any) -> int:
        self._next += 1
        self.ws.send_json({"id": self._next, "type": type_, **fields})
        return self._next

    def recv(self, timeout: float) -> dict[str, Any] | None:
        return self.ws.recv_json(timeout)

    def close(self) -> None:
        self.ws.close()
