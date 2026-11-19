"""HTTP-клиент quasar-server (v3rt3p/quasar-server, API_PORT 31116).

Облако Яндекса не используется: колонка подключена к нашему локальному
quasar-серверу, он же отдаёт состояние и принимает directives.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class QuasarApiError(Exception):
    """Общая ошибка обращения к quasar-server."""


class QuasarDeviceOfflineError(QuasarApiError):
    """Устройство не подключено к quasar-server (404 DEVICE_NOT_CONNECTED)."""


class QuasarClient:
    """Тонкий клиент HTTP API quasar-server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = 31116,
        token: str | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def _request(
        self, method: str, path: str, payload: Any | None = None
    ) -> Any:
        url = f"{self._base}{path}"
        try:
            async with self._session.request(
                method,
                url,
                json=payload,
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                body = await resp.text()
                if resp.status == 404 and "DEVICE_NOT_CONNECTED" in body:
                    raise QuasarDeviceOfflineError(
                        f"{path}: устройство не подключено"
                    )
                if resp.status >= 400:
                    raise QuasarApiError(
                        f"{path}: HTTP {resp.status}: {body[:120]}"
                    )
                return _parse_json(body)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise QuasarApiError(f"{path}: timeout") from err
        except aiohttp.ClientError as err:
            raise QuasarApiError(f"{path}: {err}") from err

    async def get_devices(self) -> list[dict[str, Any]]:
        """GET /devices — список устройств и статус подключения."""
        data = await self._request("GET", "/devices")
        return data if isinstance(data, list) else []

    async def get_state(self, duid: str) -> dict[str, Any]:
        """GET /devices/:duid/state — внутреннее состояние устройства."""
        return await self._request("GET", f"/devices/{duid}/state")

    async def push(self, duid: str, directives: list[dict[str, Any]]) -> None:
        """POST /devices/:duid/push — список именованных directives."""
        await self._request("POST", f"/devices/{duid}/push", directives)

    async def push_text(self, duid: str, text: str) -> None:
        """POST /devices/:duid/push/text — произнести текст (TTS)."""
        await self._request(
            "POST", f"/devices/{duid}/push/text", {"eventText": text}
        )


def _parse_json(body: str) -> Any:
    import json

    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return body
