"""A fake Home Assistant REST API for the tests of ha-cli, the adapter's executors and the replay of a clip.

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
import threading
import time
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

    def do_GET(self):
        self.ha.requests.append(("GET", self.path, None))
        if self._starting():
            return
        if self.ha.hang_reads:
            self.ha.stopping.wait(30)
            return
        if self.path == "/api/":
            self._reply(200, {"message": "API running."})
        elif self.path == "/api/states":
            self._reply(200, list(self.ha.states.values()))
        elif self.path.startswith("/api/states/"):
            state = self.ha.states.get(self.path.rsplit("/", 1)[1])
            self._reply(200, state) if state else self._reply(404, {"message": "Entity not found."})
        else:
            self._reply(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            body = None
        self.ha.requests.append(("POST", self.path, body))
        if self._starting():
            return
        path = self.path.split("?")[0]
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
        self.stopping = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0
        self.start_listening()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

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
