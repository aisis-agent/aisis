"""Конфигурационный flow: один вход — host:port quasar-server (+ опции)."""
from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import QuasarClient, QuasarApiError
from .const import CONF_DUID, CONF_HOST, CONF_PORT, CONF_TOKEN, DEFAULT_PORT, DOMAIN

_STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_DUID): str,
        vol.Optional(CONF_TOKEN): str,
    }
)


class ConfigFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Один экземпляр интеграции на пару host:port."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            return self._user_form({})
        try:
            devices = await self._fetch_devices(user_input)
        except QuasarApiError:
            return self._user_form({"base": "cannot_connect"})

        duid = (user_input.get(CONF_DUID) or "").strip()
        if duid and not any(d.get("duid") == duid for d in devices):
            return self._user_form({CONF_DUID: "duid_not_found"})
        if not duid:
            # по умолчанию — первое подключённое, иначе первое
            duid = next(
                (d["duid"] for d in devices if d.get("connected")),
                devices[0]["duid"] if devices else "",
            )
        await self.async_set_unique_id(
            f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Quasar {user_input[CONF_HOST]}",
            data={
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: user_input[CONF_PORT],
                CONF_DUID: duid,
                CONF_TOKEN: user_input.get(CONF_TOKEN) or "",
            },
        )

    def _user_form(self, errors: dict[str, str]) -> FlowResult:
        return self.async_show_form(
            step_id="user", data_schema=_STEP_SCHEMA, errors=errors
        )

    async def _fetch_devices(
        self, user_input: dict[str, Any]
    ) -> list[dict[str, Any]]:
        session = aiohttp.ClientSession()
        client = QuasarClient(
            session,
            user_input[CONF_HOST],
            user_input[CONF_PORT],
            user_input.get(CONF_TOKEN) or None,
            timeout=6.0,
        )
        try:
            return await client.get_devices()
        finally:
            await session.close()
