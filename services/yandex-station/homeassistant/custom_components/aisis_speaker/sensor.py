"""Сенсоры: connected (connectivity) и уровень громкости 0..SoundMaxLevel."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import STATE_OFF, STATE_ON

from . import AisisSpeakerEntity
from .const import DOMAIN
from .mapping import sound_level, sound_max_level


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator, client = data["coordinator"], data["client"]
    duid = entry.data.get("duid", "")
    async_add_entities(
        [
            AisisConnectedSensor(coordinator, client, duid),
            AisisSoundLevelSensor(coordinator, client, duid),
        ]
    )


class AisisConnectedSensor(AisisSpeakerEntity, SensorEntity):
    """В сети/не в сети = колонка подключена к quasar-server."""

    # CONNECTIVITY остался только у binary_sensor; для sensor — ENUM on/off
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATE_ON, STATE_OFF]
    _attr_translation_key = "connected"
    _attr_has_entity_name = True

    def __init__(self, coordinator, client, duid) -> None:
        AisisSpeakerEntity.__init__(self, coordinator, client, duid)
        self._attr_suggested_object_id = f"aisis_speaker_{duid.lower()}_connected"

    @property
    def unique_id(self) -> str:
        return f"{self._duid}_connected"

    @property
    def native_value(self) -> str:
        return STATE_ON if self.quasar_device.get("connected") else STATE_OFF


class AisisSoundLevelSensor(AisisSpeakerEntity, SensorEntity):
    """SoundLevel колонки (0..SoundMaxLevel, обычно 0..10)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "sound_level"
    _attr_native_unit_of_measurement = "lvl"
    _attr_has_entity_name = True

    def __init__(self, coordinator, client, duid) -> None:
        AisisSpeakerEntity.__init__(self, coordinator, client, duid)
        self._attr_suggested_object_id = f"aisis_speaker_{duid.lower()}_sound_level"

    @property
    def unique_id(self) -> str:
        return f"{self._duid}_sound_level"

    @property
    def native_value(self) -> int | None:
        try:
            return int(sound_level(self.quasar_state))
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict:
        return {"max": sound_max_level(self.quasar_state)}
