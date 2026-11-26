"""The dashboard's Home Assistant page: connect in a couple of clicks, with nothing typed.

  /ha                 the status (every step, the failing one named) and, while not connected, the server that was found on the network
                      with a Connect button; below, folded away, the advanced options (an address typed by hand, a long-lived token)
  /ha.json            the same status for the dashboard SPA (never a secret)
  POST /ha/connect    sends the browser to Home Assistant's own login page (/auth/authorize); the owner logs in there and approves
  GET  /ha/callback   Home Assistant sends the browser back with a code; it becomes a refresh token in the connection store
  POST /ha/disconnect revokes the grant on Home Assistant and forgets it (Connect again to reconnect)
  POST /ha/manual     advanced: a long-lived access token, kept only after Home Assistant accepted it

The OAuth client id is the dashboard's own origin (`http://<the host the browser used>/`) and the redirect is `/ha/callback` on it: the same
origin, so Home Assistant needs no client page. `state` binds the callback to the Connect press that started it and works once.
"""
from __future__ import annotations

import html
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

import ha_client

PENDING_TTL_S = 600.0
SCAN_TTL_S = 300.0

Response = tuple[int, dict[str, str], str]


@dataclass(frozen=True)
class _Pending:
    url: str
    client_id: str
    uuid: str | None
    name: str | None
    expires: float


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=ru><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>body{{font:16px system-ui;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#222}}"
            "li{margin:.3rem 0}.ok{color:#0a7a2f}.bad{color:#b3261e}button{font:inherit;padding:.5rem 1rem;margin:.3rem 0;cursor:pointer}"
            "input{font:inherit;padding:.4rem;width:100%;box-sizing:border-box;margin:.2rem 0}details{margin-top:2rem}"
            ".note{background:#fff4d6;padding:.6rem 1rem;border-radius:.4rem}.msg{background:#e6f4ea;padding:.6rem 1rem;border-radius:.4rem}"
            f".err{{background:#fde7e9;padding:.6rem 1rem;border-radius:.4rem}}</style><body>{body}</body></html>")


class Onboarding:
    def __init__(self, client: ha_client.HaClient, discover: Callable[..., list[ha_client.Found]] = ha_client.discover,
                 clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self._discover = discover
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._scan: tuple[float, list[ha_client.Found]] = (float("-inf"), [])

    # -- routing (the dashboard hands every /ha request here)

    def handle(self, method: str, path: str, query: dict[str, list[str]], form: dict[str, str], headers: dict[str, str]) -> Response | None:
        if not (path == "/ha" or path.startswith("/ha/") or path == "/ha.json"):
            return None
        origin = self._origin(headers)
        if method == "POST":
            sent = headers.get("Origin")
            if sent and sent != origin:
                return self._message(403, "Запрос с другого сайта отклонён.")
        if method == "GET" and path == "/ha.json":
            return 200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(self.status_json(), ensure_ascii=False)
        if method == "GET" and path == "/ha":
            return self.page(query)
        if method == "GET" and path == "/ha/callback":
            return self.callback(query, origin)
        if method == "POST" and path == "/ha/connect":
            return self.connect(form.get("url", ""), origin)
        if method == "POST" and path == "/ha/disconnect":
            return self.disconnect()
        if method == "POST" and path == "/ha/manual":
            return self.manual(form.get("url", ""), form.get("token", ""))
        return self._message(404, "Нет такой страницы.")

    @staticmethod
    def _origin(headers: dict[str, str]) -> str:
        return f"{headers.get('X-Forwarded-Proto') or 'http'}://{headers.get('Host') or 'localhost'}"

    # -- status

    def status_json(self) -> dict:
        status = self.client.status()
        failing = status.failing
        return {"connected": status.connected, "url": status.url, "name": status.name, "kind": status.kind,
                "steps": [{"id": s.id, "ok": s.ok, "detail": s.detail} for s in status.steps],
                "failing": {"id": failing.id, "detail": failing.detail} if failing else None}

    def _found(self, rescan: bool = False) -> list[ha_client.Found]:
        at, found = self._scan
        if rescan or self._clock() - at > SCAN_TTL_S:
            stored = self.client.store.load()
            found = self._discover(hint=stored.url if stored else None)
            self._scan = (self._clock(), found)
        return found

    # -- the page

    def page(self, query: dict[str, list[str]]) -> Response:
        status = self.client.status()
        notes = []
        if "connected" in query:
            notes.append("<p class=msg>Готово: Home Assistant подключён.</p>")
        if "disconnected" in query:
            notes.append("<p class=msg>Подключение удалено, доступ отозван в Home Assistant.</p>")
        body = ["<h1>Home Assistant</h1>", *notes]
        steps = "".join(f"<li class={'ok' if s.ok else 'bad'}>{'✓' if s.ok else '✗'} {html.escape(s.detail)}</li>" for s in status.steps)
        if status.connected:
            name = html.escape(status.name or "Home Assistant")
            body += [f"<p class=ok><b>Подключено</b>: {name}, {html.escape(status.url or '')}</p>", f"<ul>{steps}</ul>",
                     "<form method=post action=\"/ha/disconnect\"><button>Отключить и отозвать доступ</button></form>"]
        else:
            failing = status.failing
            if status.steps and len(status.steps) > 1 and failing:
                body.append(f"<p class=err>Не работает шаг «{html.escape(failing.id)}»: {html.escape(failing.detail)}</p>")
            body.append(f"<ul>{steps}</ul>")
            found = self._found(rescan=True)
            if found:
                body.append("<h2>Найден в сети</h2>")
                for item in found:
                    label = f"Подключить «{item.name}»" if item.name else "Подключить"
                    body.append("<form method=post action=\"/ha/connect\">"
                                f"<input type=hidden name=url value=\"{html.escape(item.url, quote=True)}\">"
                                f"<button>{html.escape(label)} — {html.escape(item.url)}</button></form>")
                body.append("<p>Откроется страница входа Home Assistant: войдите и нажмите «Разрешить». Ничего вводить и копировать не нужно.</p>")
            else:
                body.append("<p class=err>Home Assistant в сети не найден (объявление _home-assistant._tcp не пришло). "
                            "Проверьте, что сервер включён и в одной сети с этой машиной, или откройте «Расширенно».</p>")
        body.append("<details><summary>Расширенно</summary>"
                    "<p>Адрес вручную, если автопоиск не видит сервер (вход всё равно через страницу Home Assistant):</p>"
                    "<form method=post action=\"/ha/connect\"><input name=url placeholder=\"http://адрес:8123\"><button>Подключить по адресу</button></form>"
                    "<p class=note>Долгоживущий токен — только если вход через страницу Home Assistant невозможен. "
                    "Он создаётся вручную в профиле Home Assistant и не обновляется сам.</p>"
                    "<form method=post action=\"/ha/manual\"><input name=url placeholder=\"http://адрес:8123\">"
                    "<input name=token type=password placeholder=\"долгоживущий токен\" autocomplete=off><button>Сохранить токен</button></form></details>")
        return 200, {"Content-Type": "text/html; charset=utf-8"}, _page("Home Assistant", "".join(body))

    def _message(self, status: int, text: str) -> Response:
        return status, {"Content-Type": "text/html; charset=utf-8"}, _page("Home Assistant", f"<p class=err>{html.escape(text)}</p><p><a href=\"/ha\">Назад</a></p>")

    # -- Connect

    def connect(self, url: str, origin: str) -> Response:
        url = url.strip().rstrip("/")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or not ha_client.looks_like_home_assistant(url):
            return self._message(400, f"По адресу {url or '(пусто)'} не отвечает Home Assistant.")
        now = self._clock()
        self._pending = {k: v for k, v in self._pending.items() if v.expires > now}
        known = next((f for f in self._found() if f.url == url), None)
        client_id = origin + "/"
        state = secrets.token_urlsafe(24)
        self._pending[state] = _Pending(url, client_id, known.uuid if known else None, known.name if known else None, now + PENDING_TTL_S)
        target = ha_client.authorize_url(url, client_id, client_id + "ha/callback", state)
        return 303, {"Location": target, "Content-Type": "text/plain; charset=utf-8"}, ""

    def callback(self, query: dict[str, list[str]], origin: str) -> Response:
        first = lambda key: (query.get(key) or [""])[0]   # noqa: E731
        pending = self._pending.pop(first("state"), None)
        if pending is None or pending.expires < self._clock():
            return self._message(400, "Эта ссылка входа устарела или не от этой страницы. Нажмите «Подключить» ещё раз.")
        if first("error"):
            return 200, {"Content-Type": "text/html; charset=utf-8"}, _page(
                "Home Assistant", f"<p class=err>Home Assistant не подтвердил вход: {html.escape(first('error'))}</p><p><a href=\"/ha\">Назад</a></p>")
        if not first("code"):
            return self._message(400, "Home Assistant не прислал код входа.")
        try:
            tokens = ha_client.exchange_code(pending.url, pending.client_id, first("code"))
        except ha_client.HaError as err:
            return self._message(502, f"Не удалось обменять код входа: {err}")
        if not tokens.refresh_token:
            return self._message(502, "Home Assistant не выдал токен обновления.")
        previous = self.client.store.load()
        name = pending.name or self._location_name(pending.url, tokens.access_token)
        self.client.store.save(ha_client.Connection(pending.url, "oauth", tokens.refresh_token, pending.client_id, pending.uuid, name, self._clock()))
        if previous is not None and previous.kind == "oauth" and previous.secret != tokens.refresh_token:
            ha_client.revoke(previous.url, previous.secret)     # a reconnect replaces the old grant: it must not stay valid
        return 303, {"Location": "/ha?connected=1", "Content-Type": "text/plain; charset=utf-8"}, ""

    @staticmethod
    def _location_name(url: str, access_token: str) -> str | None:
        reply = ha_client.http("GET", url + "/api/config", headers={"Authorization": "Bearer " + access_token}, timeout=3.0)
        try:
            name = reply.json().get("location_name") if reply.ok else None
        except (ValueError, AttributeError):
            return None
        return name if isinstance(name, str) else None

    # -- Disconnect, advanced

    def disconnect(self) -> Response:
        conn = self.client.store.load()
        if conn is not None and conn.kind == "oauth":
            ha_client.revoke(conn.url, conn.secret)
        self.client.store.clear()
        return 303, {"Location": "/ha?disconnected=1", "Content-Type": "text/plain; charset=utf-8"}, ""

    def manual(self, url: str, token: str) -> Response:
        url, token = url.strip().rstrip("/"), token.strip()
        if not url or not token:
            return self._message(400, "Нужны адрес и токен.")
        reply = ha_client.http("GET", url + "/api/config", headers={"Authorization": "Bearer " + token}, timeout=4.0)
        if reply.status != 200:
            return self._message(400, f"Home Assistant не принял токен ({reply.status or reply.error}).")
        name = reply.json().get("location_name") if isinstance(reply.json(), dict) else None
        known = next((f for f in self._found() if f.url == url), None)     # the instance id, when the network announces this very server
        self.client.store.save(ha_client.Connection(url, "long_lived", token, None, known.uuid if known else None,
                                                    name if isinstance(name, str) else None, self._clock()))
        return 303, {"Location": "/ha?connected=1", "Content-Type": "text/plain; charset=utf-8"}, ""
