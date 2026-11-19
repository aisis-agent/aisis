"""AISIS Speaker: интеграция колонки Яндекс.Станция через локальный quasar-server.

Облако Яндекса не используется. Источник правды — HTTP API quasar-server:
GET /devices, GET /devices/:duid/state, POST /devices/:duid/push[/text].
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import QuasarApiError, QuasarClient
from .const import (
    CONF_DUID,
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.SENSOR,
    Platform.NOTIFY,
]

_SPEAK_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Optional(CONF_DUID): cv.string,
    }
)

_SEND_DIRECTIVE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): cv.string,
        vol.Required("arguments", default={}): dict,
        vol.Optional(CONF_DUID): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Доменные сервисы: speak (TTS) и send_directive (raw directive)."""

    def _client(duid: str | None) -> tuple[QuasarClient, str]:
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("Интеграция aisis_speaker не настроена")
        entry = entries[0]
        data = hass.data[DOMAIN][entry.entry_id]
        client: QuasarClient = data["client"]
        resolved = duid or entry.data.get(CONF_DUID)
        if not resolved:
            raise HomeAssistantError("Не указан duid устройства")
        return client, resolved

    async def speak(call: ServiceCall) -> None:
        client, duid = _client(call.data.get(CONF_DUID))
        try:
            await client.push_text(duid, call.data["text"])
        except QuasarApiError as err:
            raise HomeAssistantError(str(err)) from err

    async def send_directive(call: ServiceCall) -> None:
        client, duid = _client(call.data.get(CONF_DUID))
        try:
            await client.push(
                duid,
                [{"name": call.data["name"], "arguments": call.data["arguments"]}],
            )
        except QuasarApiError as err:
            raise HomeAssistantError(str(err)) from err

    hass.services.async_register(DOMAIN, "speak", speak, schema=_SPEAK_SCHEMA)
    hass.services.async_register(
        DOMAIN, "send_directive", send_directive, schema=_SEND_DIRECTIVE_SCHEMA
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = QuasarClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data.get(CONF_TOKEN) or None,
    )

    async def async_update_data() -> dict[str, Any]:
        try:
            devices = await client.get_devices()
        except QuasarApiError as err:
            raise UpdateFailed(str(err)) from err
        duid = entry.data.get(CONF_DUID)
        device = next((d for d in devices if d.get("duid") == duid), None)
        if device is None and devices:
            device = devices[0]
        state = None
        if device and device.get("connected"):
            try:
                state = await client.get_state(device["duid"])
            except QuasarApiError as err:
                _LOGGER.debug("state %s недоступен: %s", device.get("duid"), err)
        return {"device": device, "state": state}

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{entry.data[CONF_HOST]}",
        update_method=async_update_data,
        update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


class AisisSpeakerEntity(CoordinatorEntity[DataUpdateCoordinator]):
    """База: доступ к client/coordinator и DeviceInfo по duid."""

    def __init__(self, coordinator: DataUpdateCoordinator, client: QuasarClient, duid: str) -> None:
        super().__init__(coordinator)
        self._client = client
        self._duid = duid

    @property
    def data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def quasar_device(self) -> dict[str, Any]:
        return self.data.get("device") or {}

    @property
    def quasar_state(self) -> dict[str, Any] | None:
        return self.data.get("state")

    @property
    def device_info(self) -> dict[str, Any]:
        from .mapping import device_info_from_state

        info = device_info_from_state(self.quasar_state)
        name = self.quasar_device.get("name") or self._duid
        return {
            "identifiers": {(DOMAIN, self._duid)},
            "name": name,
            "manufacturer": info.get("manufacturer") or "Yandex (local quasar)",
            "model": info.get("model") or "Yandex Station",
        }

    async def _push(self, name: str, arguments: dict[str, Any] | None = None) -> None:
        try:
            await self._client.push(
                self._duid, [{"name": name, "arguments": arguments or {}}]
            )
        except QuasarApiError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
