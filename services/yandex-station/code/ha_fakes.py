"""A fake Home Assistant (REST, the standard auth endpoints, an mDNS announcement) for the tests of the client, the adapter's executors and the replay of a clip.

It keeps device states and runs scripted service calls: the state of a device changes `state_after` seconds after the call arrived,
and Home Assistant's own answer comes `respond_after` seconds after it (None: never, the connection hangs), with `status`. That is
how a real Home Assistant misbehaves: the light obeys at once, the HTTP answer is slow. `stop_listening()` / `start_listening()`
make the connection refused, `starting` answers 503 «starting».

Test double only: nothing here talks to a real Home Assistant.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import socket
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


def entity(entity_id: str, state: str, **attrs: Any) -> dict:
    return {"entity_id": entity_id, "state": state, "attributes": attrs, "last_changed": "2026-11-19T20:00:00+00:00"}


@dataclass
class Script:
    state_after: float = 0.0              # the device obeys this long after the call arrived (None: it never changes)
    respond_after: float | None = 0.0     # Home Assistant answers the HTTP call this long after it arrived (None: never)
    status: int = 200
    effect: Callable[[dict, dict[str, dict]], None] | None = None   # replaces the default effect of the service


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
        state["last_changed"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


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
            pass                    # the caller gave up (ha-cli's own timeout)

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
        token = header[7:] if header.startswith("Bearer ") else ""
        expires = self.ha.access_tokens.get(token)
        if token in self.ha.long_lived or (expires is not None and expires > time.time()):
            return True
        self._reply(401, "401: Unauthorized")
        return False

    def do_GET(self):
        self.ha.requests.append(("GET", self.path, None))
        if self._starting():
            return
        if self.path == "/auth/providers":
            self._reply(200, {"providers": [{"name": "Home Assistant Local", "id": None, "type": "homeassistant"}]})
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
        self.areas: dict[str, str] = {}  # entity id -> HA area name, for the adapter's registry and area_entities templates
        self.name = "Test Home"
        self.require_auth = False       # True: /api/ answers 401 without a bearer this fake issued
        self.access_ttl = 1800.0
        self.codes: dict[str, str] = {}             # authorization code -> client id
        self.refresh_tokens: dict[str, str] = {}    # refresh token -> client id
        self.access_tokens: dict[str, float] = {}   # access token -> expiry (epoch)
        self.long_lived: set[str] = set()
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

    def service(self, domain: str, service: str, body: dict, handler: _Handler) -> None:
        script = self.scripts.get(f"{domain}.{service}", Script())
        arrived = time.monotonic()
        effect = script.effect or (lambda b, states: default_effect(domain, service, b, states))
        if script.state_after is not None:
            if script.state_after <= 0:
                effect(body, self.states)
            else:
                timer = threading.Timer(script.state_after, effect, (body, self.states))
                timer.daemon = True
                timer.start()
        if script.respond_after is None:
            self.stopping.wait(30)
            return
        remaining = script.respond_after - (time.monotonic() - arrived)
        if remaining > 0 and self.stopping.wait(remaining):
            return
        handler._reply(script.status, [] if script.status == 200 else {"message": "service call failed"})

    def render(self, template: str) -> str:
        """Just the templates ha-cli and the adapter send: the entities of a domain that are not in a state
        (ha_verify.whole_domain_template), the entities of an area, the registry (entity, name, area per line)."""
        match = re.search(r"states\.(\w+) .*rejectattr\('state', 'eq', \"(\w+)\"\)", template)
        if match:
            domain, wanted = match.groups()
            left = [e for e, s in self.states.items() if e.startswith(domain + ".")
                    and s["state"] not in ("unavailable", "unknown") and s["state"] != wanted]
            return json.dumps(left)
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
