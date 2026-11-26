"""The one way AISIS reaches Home Assistant: its address is found, its sign-in is the standard login flow, its API is the real REST/WebSocket API.

Everything that talks to Home Assistant (adapter, dashboard, the vocabulary builders, weather, the quasar layer) takes the address and the credential
from ONE place, the connection store (`~/.config/aisis/ha/connection.json`, 0600 in a 0700 directory that containers mount read-only):

  url        where Home Assistant answered last; changed only by the owner (the Connect page)
  uuid       the instance id from the mDNS announcement: the Connect page uses it to say that a server found at another address is the same one
  client_id  the OAuth client id the refresh token was issued to (the dashboard's own origin)
  auth       {"kind": "oauth", "refresh_token"}  the standard flow (/auth/authorize + /auth/token), nothing typed by hand
             {"kind": "long_lived", "token"}     advanced: a long-lived access token pasted on the /ha page

Finding the server: mDNS `_home-assistant._tcp.local` (what Home Assistant's zeroconf integration announces), then the `homeassistant.local`
host name, then the previous address. A candidate counts only when `/auth/providers` answers like Home Assistant. When the stored address
stops answering, `rediscover` can tell where the same instance announces itself, but the stored address is changed only by the owner
signing in at the new one: the instance id is public, so the credential never follows an address the network claims by itself.

Signing in: `authorize_url` -> the owner logs in on Home Assistant's own page and approves -> `exchange_code` -> a refresh token. Access tokens
(30 minutes) are fetched from it on demand and refreshed before they expire; refresh tokens do not rotate, so every process may refresh on its
own. `revoke` ends the grant on Home Assistant's side.

Stdlib only: the dashboard and the installers run on the system Python, the adapter in its venv. Blocking calls; async callers use
`asyncio.to_thread`.
"""
from __future__ import annotations

import errno
import fcntl
from http.client import HTTPException
import json
import os
import socket
import ssl
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

STORE_PATH = Path(os.environ.get("AISIS_HA_STORE") or Path.home() / ".config" / "aisis" / "ha" / "connection.json")
SERVICE_TYPE = "_home-assistant._tcp.local"
MDNS_GROUP = ("224.0.0.251", 5353)
HOSTNAME_CANDIDATES = ("homeassistant.local", "homeassistant")
DEFAULT_PORT = 8123
ACCESS_MARGIN_S = 60.0        # an access token is renewed this long before Home Assistant expires it
# Errors that say nobody listens at the stored address (versus a refusal of the request by Home Assistant itself).

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # the smart home is local: no HTTP(S)_PROXY of the environment


class HaError(Exception):
    """Base of the errors a caller may want to tell apart."""


class Unreachable(HaError):
    """Home Assistant gave no answer; `kind` is why (refused, reset, dns, unreachable, timeout, tls, other)."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"Home Assistant не ответил ({kind})")
        self.kind = kind


class NotConnected(HaError):
    """No connection store yet: the owner has not pressed Connect."""


class AuthRevoked(HaError):
    """Home Assistant no longer accepts the stored grant (it was revoked there, or the user was removed): sign in again."""


# -- the connection store --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Connection:
    url: str
    kind: str                        # "oauth" | "long_lived"
    secret: str = field(repr=False)  # the refresh token, or the long-lived access token
    client_id: str | None = None
    uuid: str | None = None
    name: str | None = None
    connected_at: float | None = None


def _parse(raw: Any) -> Connection | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or not isinstance(raw.get("auth"), dict):
        return None
    auth = raw["auth"]
    kind = auth.get("kind")
    secret = auth.get("refresh_token") if kind == "oauth" else auth.get("token") if kind == "long_lived" else None
    if not isinstance(secret, str) or not secret:
        return None
    text = lambda key: raw[key] if isinstance(raw.get(key), str) else None   # noqa: E731
    stamp = raw.get("connected_at")
    return Connection(raw["url"].rstrip("/"), kind, secret, text("client_id"), text("uuid"), text("name"),
                      float(stamp) if isinstance(stamp, (int, float)) else None)


class Store:
    """`ha-connection.json`: read by every process, written atomically (temp file, 0600, fsync, rename) under a file lock."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else STORE_PATH
        self._seen: tuple[int, int] | None = None     # (mtime_ns, size) behind `_cached`
        self._cached: Connection | None = None
        self._mutex = threading.Lock()

    def load(self) -> Connection | None:
        with self._mutex:
            try:
                stat = self.path.stat()
            except OSError:
                self._seen, self._cached = None, None
                return None
            key = (stat.st_mtime_ns, stat.st_size)
            if key != self._seen:
                try:
                    self._cached = _parse(json.loads(self.path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    self._cached = None
                self._seen = key
            return self._cached

    def save(self, conn: Connection) -> None:
        auth = {"kind": conn.kind, "refresh_token" if conn.kind == "oauth" else "token": conn.secret}
        body = {"version": 1, "url": conn.url, "uuid": conn.uuid, "name": conn.name, "client_id": conn.client_id,
                "auth": auth, "connected_at": conn.connected_at}
        self._write(body)

    def set_url(self, url: str) -> None:
        """The owner confirmed another address; everything else stays. A no-op when nothing is stored."""
        with self._locked():
            current = self._read_raw()
            if current is None or current.get("url") == url:
                return
            current["url"] = url
            self._replace(current)

    def clear(self) -> None:
        with self._locked():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _write(self, body: dict) -> None:
        with self._locked():
            self._replace(body)

    def _read_raw(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _replace(self, body: dict) -> None:
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ha-connection-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o600)
                json.dump(body, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _locked(self) -> "_FileLock":
        return _FileLock(self.path.with_name(self.path.name + ".lock"))


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fh = open(self.path, "a")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_: object) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


# -- one HTTP request ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Reply:
    status: int                  # HTTP status; 0 when nothing came back
    raw: bytes = b""
    error: str | None = None     # kind of the failure when status is 0: refused|reset|dns|unreachable|timeout|tls|other

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.raw.decode("utf-8")) if self.raw else None

    def text(self) -> str:
        return self.raw.decode("utf-8", "replace")


def classify(err: BaseException) -> str:
    reason = err.reason if isinstance(err, urllib.error.URLError) else err
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ConnectionRefusedError):
        return "refused"
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "reset"
    if isinstance(reason, OSError) and reason.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
        return "unreachable"
    if isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED:
        return "refused"
    return "other"


def http(method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = 3.0) -> Reply:
    """One request, never raises: an HTTP error status is a reply, a transport failure is a reply with `error`."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return Reply(response.status, response.read())
    except urllib.error.HTTPError as err:
        try:
            return Reply(err.code, err.read())
        finally:
            err.close()
    except (urllib.error.URLError, OSError, ValueError, HTTPException) as err:     # ValueError: a malformed URL
        return Reply(0, b"", classify(err))


# -- mDNS discovery --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Service:
    name: str
    host: str
    port: int
    addresses: tuple[str, ...]
    txt: dict[str, str]


def _qname(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        out += bytes([len(label)]) + label.encode()
    return out + b"\0"


def mdns_query(name: str) -> bytes:
    """A PTR question for `name`; the QU bit asks the responder to answer straight to us."""
    return struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + _qname(name) + struct.pack(">HH", 12, 0x8001)


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    end = -1
    for _ in range(128):
        length = buf[off]
        if length == 0:
            off += 1
            break
        if length & 0xC0 == 0xC0:
            if end < 0:
                end = off + 2
            off = ((length & 0x3F) << 8) | buf[off + 1]
            continue
        labels.append(buf[off + 1:off + 1 + length].decode("utf-8", "replace"))
        off += 1 + length
    else:
        raise ValueError("name loop")
    return ".".join(labels), end if end >= 0 else off


def parse_mdns(packet: bytes, service_type: str = SERVICE_TYPE) -> list[Service]:
    """The services of `service_type` that one mDNS response describes (PTR + SRV + TXT + A, in answers or additional records)."""
    try:
        _, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", packet[:12])
        if not flags & 0x8000:
            return []
        off = 12
        for _ in range(qd):
            _, off = _read_name(packet, off)
            off += 4
        ptrs: list[str] = []
        srv: dict[str, tuple[str, int]] = {}
        txt: dict[str, dict[str, str]] = {}
        addr: dict[str, list[str]] = {}
        for _ in range(an + ns + ar):
            name, off = _read_name(packet, off)
            rtype, _, _, rdlen = struct.unpack(">HHIH", packet[off:off + 10])
            off += 10
            data = packet[off:off + rdlen]
            if rtype == 12:
                ptrs.append(_read_name(packet, off)[0])
            elif rtype == 33 and len(data) >= 7:
                port = struct.unpack(">H", data[4:6])[0]
                srv[name] = (_read_name(packet, off + 6)[0], port)
            elif rtype == 16:
                txt[name] = _parse_txt(data)
            elif rtype == 1 and rdlen == 4:
                addr.setdefault(name, []).append(socket.inet_ntoa(data))
            off += rdlen
    except (struct.error, IndexError, ValueError):
        return []
    found = []
    suffix = "." + service_type
    for instance in dict.fromkeys(ptrs):
        if not instance.endswith(suffix) or instance not in srv:
            continue
        host, port = srv[instance]
        found.append(Service(instance[:-len(suffix)], host, port, tuple(addr.get(host, ())), txt.get(instance, {})))
    return found


def _parse_txt(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    off = 0
    while off < len(data):
        length = data[off]
        item = data[off + 1:off + 1 + length]
        off += 1 + length
        key, sep, value = item.partition(b"=")
        if key and sep:
            out[key.decode("utf-8", "replace").lower()] = value.decode("utf-8", "replace")
    return out


def browse(service_type: str = SERVICE_TYPE, window: float = 1.5, group: tuple[str, int] = MDNS_GROUP) -> list[Service]:
    """Ask the local network which `service_type` instances exist and collect the answers for `window` seconds."""
    query = mdns_query(service_type)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, Service] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        deadline = time.monotonic() + window
        resend = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= resend:
                try:
                    sock.sendto(query, group)
                except OSError:
                    pass
                resend = now + 0.5
            sock.settimeout(max(0.05, min(0.25, deadline - now)))
            try:
                packet, _ = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            for service in parse_mdns(packet, service_type):
                found[service.name] = service
    finally:
        sock.close()
    return list(found.values())


@dataclass(frozen=True)
class Found:
    url: str
    name: str | None = None
    uuid: str | None = None
    version: str | None = None
    via: str = "mdns"


def _hostport_url(host: str, port: int, scheme: str = "http") -> str:
    return f"{scheme}://{host}:{port}"


def looks_like_home_assistant(url: str, timeout: float = 1.5) -> bool:
    """`/auth/providers` is the one unauthenticated endpoint every Home Assistant answers with `{"providers": [...]}`."""
    reply = http("GET", url.rstrip("/") + "/auth/providers", timeout=timeout)
    if reply.status != 200:
        return False
    try:
        return isinstance(reply.json(), dict) and "providers" in reply.json()
    except ValueError:
        return False


def discover(hint: str | None = None, uuid: str | None = None, window: float = 1.5,
             group: tuple[str, int] = MDNS_GROUP, hostnames: tuple[str, ...] = HOSTNAME_CANDIDATES) -> list[Found]:
    """Home Assistant servers on the local network, the best first: mDNS announcements, then well-known host names, then `hint`.

    With `uuid` (the instance the owner connected before) only that instance counts, so a neighbour's Home Assistant is never adopted when
    the address changed; a candidate whose identity cannot be read (host name, hint) is accepted only if it is the stored address itself.
    """
    candidates: list[Found] = []
    for service in browse(SERVICE_TYPE, window, group):
        txt = service.txt
        base = txt.get("internal_url")      # the local address first: `base_url` is the external (cloud) one when remote access is set up
        if not base and service.addresses:
            base = _hostport_url(service.addresses[0], service.port)
        base = base or txt.get("base_url")
        if base:
            candidates.append(Found(base.rstrip("/"), txt.get("location_name") or service.name, txt.get("uuid"), txt.get("version"), "mdns"))
    for host in hostnames:
        candidates.append(Found(_hostport_url(host, DEFAULT_PORT), None, None, None, "hostname"))
    if hint:
        candidates.append(Found(hint.rstrip("/"), None, None, None, "hint"))
    seen: set[str] = set()
    result: list[Found] = []
    for candidate in candidates:
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        if uuid and candidate.uuid != uuid and not (candidate.uuid is None and candidate.url == (hint or "").rstrip("/")):
            continue
        if looks_like_home_assistant(candidate.url):
            result.append(candidate)
    return result


# -- the standard sign-in (OAuth authorization code flow of Home Assistant's own clients) ------------------------------------

@dataclass(frozen=True)
class Tokens:
    access_token: str
    expires_in: float
    refresh_token: str | None = None


def authorize_url(base: str, client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
    return f"{base.rstrip('/')}/auth/authorize?{query}"


def _token_call(base: str, form: dict[str, str], timeout: float) -> Tokens:
    reply = http("POST", base.rstrip("/") + "/auth/token", body=urllib.parse.urlencode(form).encode(),
                 headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=timeout)
    if reply.status == 0:
        raise Unreachable(reply.error or "other")
    try:
        data = reply.json()
    except ValueError:
        data = None
    if reply.status == 400 and isinstance(data, dict) and data.get("error") in ("invalid_grant", "invalid_request"):
        raise AuthRevoked(str(data.get("error_description") or data["error"]))
    if reply.status != 200 or not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
        raise HaError(f"Home Assistant ответил на /auth/token статусом {reply.status}")
    refresh = data.get("refresh_token")
    return Tokens(data["access_token"], float(data.get("expires_in") or 1800), refresh if isinstance(refresh, str) else None)


def exchange_code(base: str, client_id: str, code: str, timeout: float = 5.0) -> Tokens:
    return _token_call(base, {"grant_type": "authorization_code", "code": code, "client_id": client_id}, timeout)


def refresh_access(base: str, client_id: str | None, refresh_token: str, timeout: float = 3.0) -> Tokens:
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_id:
        form["client_id"] = client_id
    return _token_call(base, form, timeout)


def revoke(base: str, refresh_token: str, timeout: float = 3.0) -> bool:
    """End the grant on Home Assistant's side (it answers 200 whatever the token was)."""
    reply = http("POST", base.rstrip("/") + "/auth/revoke", body=urllib.parse.urlencode({"token": refresh_token, "action": "revoke"}).encode(),
                 headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=timeout)
    return reply.status == 200


# -- the client ------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    id: str          # store | address | reach | auth | api
    ok: bool
    detail: str


@dataclass(frozen=True)
class Status:
    connected: bool
    steps: tuple[Step, ...]
    url: str | None = None
    name: str | None = None
    kind: str | None = None

    @property
    def failing(self) -> Step | None:
        return next((step for step in self.steps if not step.ok), None)


class HaClient:
    def __init__(self, store: Store | None = None, discover_fn: Callable[..., list[Found]] = discover,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.store = store or Store()
        self._discover = discover_fn
        self._clock = clock
        self._lock = threading.Lock()
        self._access: tuple[str, str, float] | None = None       # (secret it came from, token, valid until)

    # -- credentials

    def connection(self) -> Connection:
        conn = self.store.load()
        if conn is None:
            raise NotConnected("Home Assistant не подключён: откройте дашборд /ha и нажмите «Подключить»")
        return conn

    @property
    def url(self) -> str:
        return self.connection().url

    def access_token(self, force: bool = False, timeout: float = 3.0) -> str:
        conn = self.connection()
        if conn.kind == "long_lived":
            return conn.secret
        with self._lock:
            held = self._access
            if not force and held and held[0] == conn.secret and held[2] - ACCESS_MARGIN_S > self._clock():
                return held[1]
            tokens = refresh_access(conn.url, conn.client_id, conn.secret, timeout)
            self._access = (conn.secret, tokens.access_token, self._clock() + tokens.expires_in)
            return tokens.access_token

    # -- requests

    def request(self, method: str, path: str, body: Any = None, *, timeout: float = 3.0, headers: dict[str, str] | None = None) -> Reply:
        """One authenticated request to `path` (with its query). A 401 renews the access token once. Never raises for transport
        failures; raises NotConnected / AuthRevoked when there is no usable credential."""
        started = self._clock()
        conn = self.connection()
        try:
            reply = self._send(conn, method, path, body, timeout, headers)
            if reply.status == 401 and conn.kind == "oauth":
                left = max(0.2, timeout - (self._clock() - started))
                token = self.access_token(force=True, timeout=left)
                reply = self._send(conn, method, path, body, left, headers, token)
        except Unreachable as err:      # the token endpoint did not answer either: Home Assistant is not there
            reply = Reply(0, b"", err.kind)
        return reply

    def _send(self, conn: Connection, method: str, path: str, body: Any, timeout: float, headers: dict[str, str] | None,
              token: str | None = None) -> Reply:
        token = token or self.access_token(timeout=timeout)
        merged = {"Authorization": "Bearer " + token, **(headers or {})}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            merged.setdefault("Content-Type", "application/json")
        return http(method, conn.url + path, body=data, headers=merged, timeout=timeout)

    def get(self, path: str, timeout: float = 3.0) -> Reply:
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, body: Any = None, timeout: float = 3.0) -> Reply:
        return self.request("POST", path, body if body is not None else {}, timeout=timeout)

    def service(self, domain: str, service: str, data: dict | None = None, *, return_response: bool = False, timeout: float = 3.0) -> Reply:
        """`POST /api/services/<domain>/<service>`; with `return_response` Home Assistant answers with the service's data."""
        query = "?return_response" if return_response else ""
        return self.post(f"/api/services/{domain}/{service}{query}", data or {}, timeout)

    def template(self, template: str, timeout: float = 3.0) -> Reply:
        return self.post("/api/template", {"template": template}, timeout)

    # -- the address

    def rediscover(self, window: float = 1.5) -> str | None:
        """Look for the connected instance on the network now and return the address it announces, None when it was not found or the
        address is unchanged. Nothing is stored: an announcement is public and anybody on the network can copy the instance id into one,
        so the credential never follows an address the network merely claims. The dashboard shows the address and the owner confirms it
        by signing in there (ha_connect). A connection that never learned its instance id is not looked for at all."""
        conn = self.store.load()
        if conn is None or conn.uuid is None:
            return None
        for candidate in self._discover(hint=conn.url, uuid=conn.uuid, window=window):
            return candidate.url if candidate.url != conn.url else None
        return None

    # -- what the dashboard shows

    def status(self, timeout: float = 3.0) -> Status:
        conn = self.store.load()
        if conn is None:
            return Status(False, (Step("store", False, "Home Assistant не подключён: нажмите «Подключить»"),))
        steps = [Step("store", True, "подключение сохранено" + (" (вход через Home Assistant)" if conn.kind == "oauth"
                                                              else " (долгоживущий токен, расширенный режим)"))]
        steps.append(Step("address", True, conn.url))
        if not looks_like_home_assistant(conn.url, timeout):
            steps.append(Step("reach", False, f"{conn.url} не отвечает как Home Assistant"))
            return Status(False, tuple(steps), conn.url, conn.name, conn.kind)
        steps.append(Step("reach", True, "сервер отвечает"))
        try:
            self.access_token(timeout=timeout)
        except AuthRevoked as err:
            steps.append(Step("auth", False, f"Home Assistant отозвал доступ ({err}): подключите заново"))
            return Status(False, tuple(steps), conn.url, conn.name, conn.kind)
        except HaError as err:
            steps.append(Step("auth", False, str(err)))
            return Status(False, tuple(steps), conn.url, conn.name, conn.kind)
        steps.append(Step("auth", True, "токен доступа получен"))
        try:
            reply = self.get("/api/config", timeout)
        except AuthRevoked as err:     # revoked after the token check: the page must still be reachable to connect again
            steps.append(Step("api", False, f"Home Assistant отозвал доступ ({err}): подключите заново"))
            return Status(False, tuple(steps), conn.url, conn.name, conn.kind)
        if reply.status == 200:
            steps.append(Step("api", True, "API отвечает"))
            return Status(True, tuple(steps), conn.url, conn.name, conn.kind)
        steps.append(Step("api", False, f"/api/config ответил {reply.status or reply.error}"))
        return Status(False, tuple(steps), conn.url, conn.name, conn.kind)


_default: HaClient | None = None
_default_lock = threading.Lock()


def default() -> HaClient:
    """The process-wide client on the default store."""
    global _default
    with _default_lock:
        if _default is None:
            _default = HaClient()
        return _default
