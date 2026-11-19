"""Media player: состояние колонки из quasar-server, управление directives."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.util import dt as dt_util

from . import AisisSpeakerEntity
from .const import (
    DIRECTIVE_NEXT,
    DIRECTIVE_PAUSE,
    DIRECTIVE_PLAY,
    DIRECTIVE_PREVIOUS,
    DIRECTIVE_VOLUME,
    DIRECTIVE_VOLUME_DOWN,
    DIRECTIVE_VOLUME_UP,
    DOMAIN,
)
from .mapping import (
    ha_volume_to_quasar,
    media_attrs,
    player_state,
    quasar_volume_to_ha,
    sound_level,
    sound_max_level,
)

_SUPPORTED = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_STEP
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            AisisMediaPlayerEntity(
                data["coordinator"], data["client"], entry.data.get("duid", "")
            )
        ]
    )


class AisisMediaPlayerEntity(AisisSpeakerEntity, MediaPlayerEntity):
    """media_player поверх deviceState.AudioPlayer + directives."""

    _attr_should_poll = False
    _attr_supported_features = _SUPPORTED
    _attr_has_entity_name = True

    def __init__(self, coordinator, client, duid) -> None:
        AisisSpeakerEntity.__init__(self, coordinator, client, duid)
        # детерминированный entity_id: media_player.aisis_speaker_<duid>
        self._attr_suggested_object_id = f"aisis_speaker_{duid.lower()}"



    @property
    def unique_id(self) -> str:
        return f"{self._duid}_media_player"

    @property
    def state(self) -> MediaPlayerState:
        raw = ((self.quasar_state or {}).get("deviceState") or {})
        raw_player = (raw.get("AudioPlayer") or {}).get("PlayerState")
        return MediaPlayerState(
            player_state(bool(self.quasar_device.get("connected")), raw_player)
        )

    @property
    def volume_level(self) -> float | None:
        raw = (self.quasar_state or {}).get("deviceState") or {}
        return quasar_volume_to_ha(
            sound_level(self.quasar_state), sound_max_level(self.quasar_state)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"duid": self._duid}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.async_add_listener(self.async_write_ha_state)

    # --- обновление атрибутов медиа при каждом состоянии ---
    def _sync_media(self) -> None:
        attrs = media_attrs(self.quasar_state)
        self._attr_media_title = attrs.get("media_title")
        self._attr_media_artist = attrs.get("media_artist")
        self._attr_media_content_type = attrs.get("media_content_type") or "music"
        self._attr_media_duration = attrs.get("media_duration")
        self._attr_media_position = attrs.get("media_position")
        ts = _last_play_timestamp(self.quasar_state)
        self._attr_media_position_updated_at = (
            dt_util.utc_from_timestamp(ts / 1000) if ts else None
        )

    @property
    def media_title(self) -> str | None:
        self._sync_media()
        return self._attr_media_title

    @property
    def media_artist(self) -> str | None:
        self._sync_media()
        return self._attr_media_artist

    @property
    def media_content_type(self) -> str | None:
        self._sync_media()
        return self._attr_media_content_type

    @property
    def media_duration(self) -> float | None:
        self._sync_media()
        return self._attr_media_duration

    @property
    def media_position(self) -> float | None:
        self._sync_media()
        return self._attr_media_position

    @property
    def media_position_updated_at(self) -> datetime | None:
        self._sync_media()
        return self._attr_media_position_updated_at

    # --- управление ---
    async def async_set_volume_level(self, volume: float) -> None:
        max_level = sound_max_level(self.quasar_state) or 10
        await self._push(
            DIRECTIVE_VOLUME, {"volume": ha_volume_to_quasar(volume, max_level)}
        )

    async def async_volume_up(self) -> None:
        await self._push(DIRECTIVE_VOLUME_UP)

    async def async_volume_down(self) -> None:
        await self._push(DIRECTIVE_VOLUME_DOWN)

    async def async_media_play(self) -> None:
        await self._push(DIRECTIVE_PLAY)

    async def async_media_pause(self) -> None:
        await self._push(DIRECTIVE_PAUSE)

    async def async_media_next_track(self) -> None:
        await self._push(DIRECTIVE_NEXT)

    async def async_media_previous_track(self) -> None:
        await self._push(DIRECTIVE_PREVIOUS)


def _last_play_timestamp(state: dict[str, Any] | None) -> int | None:
    player = ((state or {}).get("deviceState") or {}).get("AudioPlayer") or {}
    try:
        return int(player.get("LastPlayTimestamp") or 0)
    except (TypeError, ValueError):
        return None
