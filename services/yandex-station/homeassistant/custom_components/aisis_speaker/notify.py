"""Notify-сущность: notify.send_message -> POST /devices/:duid/push/text."""
from __future__ import annotations

from typing import Any

from homeassistant.components.notify import NotifyEntity

from . import AisisSpeakerEntity
from .api import QuasarApiError
from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [AisisNotifyEntity(data["coordinator"], data["client"], entry.data.get("duid", ""))]
    )


class AisisNotifyEntity(AisisSpeakerEntity, NotifyEntity):
    """TTS через quasar-server (без облака)."""

    _attr_has_entity_name = True
    _attr_translation_key = "notify"

    @property
    def unique_id(self) -> str:
        return f"{self._duid}_notify"

    async def async_send_message(self, message: str, **kwargs: Any) -> None:
        if not (message or "").strip():
            return
        try:
            await self._client.push_text(self._duid, message.strip())
        except QuasarApiError as err:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(str(err)) from err
