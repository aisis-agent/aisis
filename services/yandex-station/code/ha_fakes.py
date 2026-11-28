"""A fake Home Assistant (REST, the standard auth endpoints, an mDNS announcement) for the tests of the client, the adapter's executors and the replay of a clip.

It keeps device states and runs scripted service calls: the state of a device changes `state_after` seconds after the call arrived,
and Home Assistant's own answer comes `respond_after` seconds after it (None: never, the connection hangs), with `status`. That is
how a real Home Assistant misbehaves: the light obeys at once, the HTTP answer is slow. `stop_listening()` / `start_listening()`
make the connection refused, `starting` answers 503 «starting».

Test double only: nothing here talks to a real Home Assistant.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import secrets
import socket
import struct
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import ha_ws


def entity(entity_id: str, state: str, **attrs: Any) -> dict:
    return {"entity_id": entity_id, "state": state, "attributes": attrs, "last_changed": "2026-11-19T20:00:00+00:00"}


@dataclass
class Script:
    state_after: float = 0.0              # the device obeys this long after the call arrived (None: it never changes)
    respond_after: float | None = 0.0     # Home Assistant answers the HTTP call this long after it arrived (None: never)
    status: int = 200
    effect: Callable[[dict, dict[str, dict]], None] | None = None   # replaces the default effect of the service
    on_receive: Callable[["FakeHomeAssistant"], None] | None = None   # runs the moment the call arrives (before the answer): a lost connection, a change nobody published


def default_effect(domain: str, service: str, body: dict, states: dict[str, dict]) -> None:
    ids = body.get("entity_id")
    ids = [ids] if isinstance(ids, str) else list(ids or [])
    if ids == ["all"]:
        ids = [e for e in states if e.startswith(domain + ".")]
    for entity_id in ids:
        state = states.get(entity_id)
        if state is None:
            continue
        attrs = state["attributes"]
        if service == "turn_off":
            state["state"] = "off"
        elif service == "turn_on":
            state["state"] = "on"
            if "brightness_pct" in body:
                attrs["brightness"] = round(body["brightness_pct"] * 255 / 100)
        elif service == "toggle":
            state["state"] = "off" if state["state"] == "on" else "on"
        elif service == "set_hvac_mode":
            state["state"] = body["hvac_mode"]
        elif service == "set_temperature":
            attrs["temperature"] = body["temperature"]
            if "hvac_mode" in body:
                state["state"] = body["hvac_mode"]
        elif service == "volume_set":
            attrs["volume_level"] = body["volume_level"]
        elif service == "media_pause":
            state["state"] = "paused"
        elif service == "media_play":
            state["state"] = "playing"
        state["last_changed"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class _WsConn:
    """One WebSocket of the fake: server-side frames (unmasked out, masked in), a write lock because events come from other threads."""

    def __init__(self, handler: "_Handler") -> None:
        self.handler = handler
        self.lock = threading.Lock()
        self.subs: dict[int, set[str]] = {}
        self.authed = False

    def send(self, message: dict) -> None:
        frame = ha_ws.encode_frame(json.dumps(message).encode(), 1, mask=False)
        with self.lock:
            try:
                self.handler.wfile.write(frame)
                self.handler.wfile.flush()
            except OSError:
                pass

    def recv(self) -> dict | None:
        rfile = self.handler.rfile
        head = rfile.read(2)
        if len(head) < 2:
            return None
        size = head[1] & 0x7F
        if size == 126:
            size = struct.unpack(">H", rfile.read(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", rfile.read(8))[0]
        key = rfile.read(4) if head[1] & 0x80 else b""
        payload = rfile.read(size)
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        if head[0] & 0x0F == 8:
            return None
        return json.loads(payload.decode())

    def close(self) -> None:
        try:
            self.handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    @property
    def ha(self) -> "FakeHomeAssistant":
        return self.server.ha  # type: ignore[attr-defined]

    def _reply(self, status: int, body: Any) -> None:
        raw = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass                    # the caller gave up (its own timeout)

    def _starting(self) -> bool:
        if self.ha.starting:
            self._reply(503, {"message": "Home Assistant is starting"})
            return True
        return False

    def _authorized(self) -> bool:
        """With `require_auth` every /api/ request needs a bearer Home Assistant issued (an access token, a long-lived one)."""
        if not self.ha.require_auth:
            return True
        header = self.headers.get("Authorization") or ""
        if self.ha.token_valid(header[7:] if header.startswith("Bearer ") else ""):
            return True
        self._reply(401, "401: Unauthorized")
        return False

    def _websocket(self) -> None:
        """`/api/websocket`: the upgrade, `auth_required` -> `auth`, `subscribe_trigger` (state triggers), `call_service`."""
        if self.ha.hang_ws:
            self.ha.stopping.wait(30)
            return
        accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + ha_ws.GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        conn = _WsConn(self)
        conn.send({"type": "auth_required", "ha_version": "2026.9.3"})
        try:
            while True:
                message = conn.recv()
                if message is None:
                    return
                if message.get("type") == "auth":
                    ok = not self.ha.require_auth or self.ha.token_valid(str(message.get("access_token")))
                    conn.send({"type": "auth_ok", "ha_version": "2026.9.3"} if ok else {"type": "auth_invalid", "message": "Invalid access token"})
                    if not ok:
                        return
                    self.ha.sockets.append(conn)
                    conn.authed = True
                elif not conn.authed:
                    return
                elif message.get("type") == "subscribe_trigger":
                    wanted = message["trigger"]["entity_id"]
                    conn.subs[message["id"]] = set([wanted] if isinstance(wanted, str) else wanted)
                    conn.send({"id": message["id"], "type": "result", "success": True, "result": None})
                elif message.get("type") == "auth/current_user":
                    conn.send({"id": message["id"], "type": "result", "success": True, "result": {"id": self.ha.user_id, "name": "AISIS"}})
                elif message.get("type") == "call_service":
                    threading.Thread(target=self._call_service, args=(conn, message), daemon=True).start()
        except OSError:
            return
        finally:
            if conn in self.ha.sockets:
                self.ha.sockets.remove(conn)

    def _call_service(self, conn: "_WsConn", message: dict) -> None:
        self.ha.requests.append(("WS", f"/api/services/{message['domain']}/{message['service']}", message.get("service_data") or {}))
        status, answered = self.ha.run_script(message["domain"], message["service"], message.get("service_data") or {})
        if not answered:
            return
        if status == 200:
            conn.send({"id": message["id"], "type": "result", "success": True, "result": {"context": {"id": "ctx"}, "response": None}})
        else:
            code = {400: "invalid_format", 404: "not_found"}.get(status, "home_assistant_error")
            conn.send({"id": message["id"], "type": "result", "success": False, "error": {"code": code, "message": "service call failed"}})

    def do_GET(self):
        self.ha.requests.append(("GET", self.path, None))
        if self._starting():
            return
        if self.path == "/auth/providers":
            self._reply(200, {"providers": [{"name": "Home Assistant Local", "id": None, "type": "homeassistant"}]})
            return
        if self.path == "/api/websocket":
            self._websocket()
            return
        if self.path.startswith("/api/") and not self._authorized():
            return
        if self.ha.hang_reads:
            self.ha.stopping.wait(30)
            return
        if self.path == "/api/":
            self._reply(200, {"message": "API running."})
        elif self.path == "/api/config":
            self._reply(200, {"location_name": self.ha.name, "version": "2026.9.3"})
        elif self.path == "/api/states":
            self._reply(200, list(self.ha.states.values()))
        elif self.path.startswith("/api/states/"):
            state = self.ha.states.get(self.path.rsplit("/", 1)[1])
            self._reply(200, state) if state else self._reply(404, {"message": "Entity not found."})
        elif self.path.startswith("/api/logbook/"):
            self._reply(200, list(self.ha.logbook))
        else:
            self._reply(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = None
        self.rfile_form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(errors="replace")).items()}
        self.ha.requests.append(("POST", self.path, body if body is not None else self.rfile_form))
        if self._starting():
            return
        path = self.path.split("?")[0]
        if path.startswith("/auth/"):
            self.ha.auth(path, self.rfile_form, self)
            return
        if path.startswith("/api/") and not self._authorized():
            return
        if path == "/api/template":
            if self.ha.hang_reads:
                self.ha.stopping.wait(30)
                return
            if self.ha.template_delay and self.ha.stopping.wait(self.ha.template_delay):
                return
            self._reply(200, self.ha.render(body["template"]))
        elif path.startswith("/api/services/"):
            domain, service = path.split("/")[3:5]
            self.ha.service(domain, service, body or {}, self)
        else:
            self._reply(404, {})


class FakeHomeAssistant:
    def __init__(self, states: list[dict] | None = None) -> None:
        self.states: dict[str, dict] = {s["entity_id"]: copy.deepcopy(s) for s in (states or [])}
        self.scripts: dict[str, Script] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.starting = False
        self.hang_reads = False         # state reads and templates never answer (a Home Assistant that is busy or wedged)
        self.template_delay = 0.0       # a template takes this long to render (a slow registry)
        self.areas: dict[str, str] = {}  # entity id -> HA area name, for the adapter's registry and area_entities templates
        self.devices: dict[str, str] = {}  # entity id -> HA device id, for the media player rows of tvpause
        self.device_areas: dict[str, str] = {}  # entity id -> area of its device (an entity's own area in `areas` may differ), same rows
        self.name = "Test Home"
        self.require_auth = False       # True: /api/ answers 401 without a bearer this fake issued
        self.access_ttl = 1800.0
        self.codes: dict[str, str] = {}             # authorization code -> client id
        self.refresh_tokens: dict[str, str] = {}    # refresh token -> client id
        self.access_tokens: dict[str, float] = {}   # access token -> expiry (epoch)
        self.long_lived: set[str] = set()
        self.sockets: list[_WsConn] = []
        self.hang_ws = False            # the WebSocket upgrade is never answered (a wedged Home Assistant)
        self.logbook: list[dict] = []   # the entries `GET /api/logbook/<time>` answers with
        self.user_id = "user-aisis"     # `auth/current_user` answers with this id
        self.stopping = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.start_listening()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def issue_code(self, client_id: str) -> str:
        """What Home Assistant's login page redirects back with after the owner approved `client_id`."""
        code = secrets.token_hex(8)
        self.codes[code] = client_id
        return code

    def auth(self, path: str, form: dict[str, str], handler: _Handler) -> None:
        """`/auth/token` (authorization_code and refresh_token grants) and `/auth/revoke`, as Home Assistant answers them."""
        if path == "/auth/revoke":
            self.refresh_tokens.pop(form.get("token", ""), None)
            handler._reply(200, {})
            return
        if path != "/auth/token":
            handler._reply(404, {})
            return
        grant = form.get("grant_type")
        if grant == "authorization_code":
            client = self.codes.pop(form.get("code", ""), None)
            if client is None or client != form.get("client_id"):
                handler._reply(400, {"error": "invalid_request", "error_description": "Invalid code"})
                return
            refresh = secrets.token_hex(16)
            self.refresh_tokens[refresh] = client
        elif grant == "refresh_token":
            refresh = form.get("refresh_token", "")
            client = self.refresh_tokens.get(refresh)
            if client is None:
                handler._reply(400, {"error": "invalid_grant"})
                return
            if form.get("client_id") not in (None, client):
                handler._reply(400, {"error": "invalid_request", "error_description": "Invalid client_id"})
                return
        else:
            handler._reply(400, {"error": "unsupported_grant_type"})
            return
        access = secrets.token_hex(16)
        self.access_tokens[access] = time.time() + self.access_ttl
        body = {"access_token": access, "token_type": "Bearer", "expires_in": int(self.access_ttl)}
        if grant == "authorization_code":
            body["refresh_token"] = refresh
        handler._reply(200, body)

    def calls(self, prefix: str = "/api/services/") -> list[tuple[str, Any]]:
        return [(path, body) for method, path, body in self.requests if method == "POST" and path.startswith(prefix)]

    def script(self, service: str, **kw: Any) -> Script:
        """`script("light.turn_off", state_after=0.8, respond_after=3.5)`."""
        self.scripts[service] = Script(**kw)
        return self.scripts[service]

    def start_listening(self) -> None:
        class Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                if not isinstance(sys.exc_info()[1], ConnectionError):   # a client that gives up mid-request (a budget that ended) is not news
                    super().handle_error(request, client_address)
        self._server = Server(("127.0.0.1", self.port), _Handler)
        self._server.ha = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=lambda: self._server.serve_forever(0.02), daemon=True)
        self._thread.start()

    def stop_listening(self) -> None:
        """From now on the connection is refused."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def close(self) -> None:
        self.stopping.set()
        self.stop_listening()

    def token_valid(self, token: str) -> bool:
        expires = self.access_tokens.get(token)
        return token in self.long_lived or (expires is not None and expires > time.time())

    def _apply(self, domain: str, service: str, body: dict, script: Script) -> None:
        """Run the effect of a service call and tell every WebSocket trigger subscription which states it changed."""
        effect = script.effect or (lambda b, states: default_effect(domain, service, b, states))
        before = copy.deepcopy(self.states)
        effect(body, self.states)
        for entity_id, state in self.states.items():
            old = before.get(entity_id)
            if old is None or {k: v for k, v in old.items() if k != "last_changed"} != {k: v for k, v in state.items() if k != "last_changed"}:
                self.publish(entity_id, old, copy.deepcopy(state))

    def publish(self, entity_id: str, old: dict | None, new: dict) -> None:
        for conn in list(self.sockets):
            for sub_id, wanted in list(conn.subs.items()):
                if entity_id in wanted:
                    conn.send({"id": sub_id, "type": "event", "event": {"variables": {"trigger": {
                        "platform": "state", "entity_id": entity_id, "from_state": old, "to_state": new}}}})

    def drop_sockets(self) -> None:
        """Home Assistant goes away while a WebSocket is open."""
        for conn in list(self.sockets):
            conn.close()

    def run_script(self, domain: str, service: str, body: dict) -> tuple[int, bool]:
        """Do what the script says for one call: change the device `state_after` later, then wait `respond_after`. Returns (status,
        answered); not answered means the answer never comes (a hung Home Assistant)."""
        script = self.scripts.get(f"{domain}.{service}", Script())
        arrived = time.monotonic()
        if script.on_receive is not None:
            script.on_receive(self)
        if script.state_after is not None:
            if script.state_after <= 0:
                self._apply(domain, service, body, script)
            else:
                timer = threading.Timer(script.state_after, self._apply, (domain, service, body, script))
                timer.daemon = True
                timer.start()
        if script.respond_after is None:
            self.stopping.wait(30)
            return script.status, False
        remaining = script.respond_after - (time.monotonic() - arrived)
        if remaining > 0 and self.stopping.wait(remaining):
            return script.status, False
        return script.status, True

    def service(self, domain: str, service: str, body: dict, handler: _Handler) -> None:
        status, answered = self.run_script(domain, service, body)
        if answered:
            handler._reply(status, [] if status == 200 else {"message": "service call failed"})

    def render(self, template: str) -> str:
        """Just the templates the client and the adapter send: the entities of a domain that are not in a state
        (ha_verify.whole_domain_template), the entities of an area, the registry (entity, name, area per line), the media players
        with state, area and device (tvpause.ROOM_TEMPLATE)."""
        match = re.search(r"states\.(\w+) .*rejectattr\('state', 'eq', \"(\w+)\"\)", template)
        if match:
            domain, wanted = match.groups()
            left = [e for e, s in self.states.items() if e.startswith(domain + ".")
                    and s["state"] not in ("unavailable", "unknown") and s["state"] != wanted]
            return json.dumps(left)
        if "namespace(rows=[])" in template and "states.media_player" in template:
            return json.dumps([{"entity_id": e, "state": s["state"], "area": self.areas.get(e), "device": self.devices.get(e),
                                "device_area": self.device_areas.get(e, self.areas.get(e) if e in self.devices else None),
                                "queue": s["attributes"].get("active_queue"), "members": s["attributes"].get("group_members"),
                                "helper_members": s["attributes"].get("entity_id")}
                               for e, s in self.states.items() if e.startswith("media_player.")])
        area = re.search(r"area_entities\((\".*\")\)", template, re.DOTALL)
        if area:
            name = json.loads(area.group(1))
            return json.dumps([e for e, a in self.areas.items() if a == name])
        if "area_name(" in template:
            return "".join(f"{entity}\t{entity}\t{name}\n" for entity, name in self.areas.items())
        return ""


def mdns_response(service_type: str, instance: str, host: str, port: int, address: str, txt: dict[str, str]) -> bytes:
    """What a zeroconf responder (Home Assistant's) sends for a PTR question: PTR + SRV + TXT in answers, the A record additional."""
    def name(text: str) -> bytes:
        return b"".join(bytes([len(label)]) + label.encode() for label in text.split(".")) + b"\0"

    def record(owner: str, rtype: int, data: bytes) -> bytes:
        return name(owner) + struct.pack(">HHIH", rtype, 0x8001, 120, len(data)) + data

    full = f"{instance}.{service_type}"
    txt_data = b"".join(bytes([len(item)]) + item for item in (f"{k}={v}".encode() for k, v in txt.items()))
    answers = [record(service_type, 12, name(full)),
               record(full, 33, struct.pack(">HHH", 0, 0, port) + name(host)),
               record(full, 16, txt_data)]
    additional = [record(host, 1, socket.inet_aton(address))]
    return struct.pack(">HHHHHH", 0, 0x8400, 0, len(answers), 0, len(additional)) + b"".join(answers + additional)


class FakeMdns:
    """A responder on loopback that answers every datagram with the announcement; `group` is what `ha_client.browse` is pointed at."""

    def __init__(self, response: bytes | None) -> None:
        self.response = response
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.group = ("127.0.0.1", self.sock.getsockname()[1])
        self.queries = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                _, sender = self.sock.recvfrom(2048)
            except OSError:
                continue
            self.queries += 1
            if self.response is not None:
                self.sock.sendto(self.response, sender)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(1)
        self.sock.close()
