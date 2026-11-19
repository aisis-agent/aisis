"""Чистые функции отображения состояния quasar-сервера на сущности HA.

Вынесены отдельно, чтобы тестировались без установленного Home Assistant.
"""
from __future__ import annotations

from typing import Any


def player_state(connected: bool, raw_player_state: str | None) -> str:
    """on/off = connected; PlayerState -> playing/paused/idle."""
    if not connected:
        return "off"
    value = (raw_player_state or "").strip().lower()
    if value in {"playing", "play"}:
        return "playing"
    if value in {"paused", "pause"}:
        return "paused"
    return "idle"  # idle/stopped/пусто/неизвестно


def quasar_volume_to_ha(level: Any, max_level: Any = 10) -> float | None:
    """SoundLevel 0..SoundMaxLevel -> volume_level 0..1."""
    try:
        level = int(level)
        max_level = int(max_level or 10) or 10
    except (TypeError, ValueError):
        return None
    return round(min(max(level, 0), max_level) / max_level, 2)


def ha_volume_to_quasar(volume: float, max_level: Any = 10) -> int:
    """volume_level 0..1 -> SoundLevel 0..SoundMaxLevel."""
    try:
        max_level = int(max_level or 10) or 10
    except (TypeError, ValueError):
        max_level = 10
    volume = min(max(float(volume), 0.0), 1.0)
    return int(round(volume * max_level))


def device_info_from_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """model/manufacturer из environmentState.Devices[0].Application."""
    app: dict[str, Any] = {}
    try:
        devices = ((state or {}).get("environmentState") or {}).get("Devices")
        if isinstance(devices, list) and devices:
            app = devices[0].get("Application") or {}
    except (AttributeError, IndexError, TypeError):
        app = {}
    return {
        "model": app.get("DeviceModel"),
        "manufacturer": app.get("DeviceManufacturer"),
    }


def media_attrs(state: dict[str, Any] | None) -> dict[str, Any]:
    """title/subtitle/duration/position из deviceState.AudioPlayer."""
    ds = (state or {}).get("deviceState") or {}
    player = ds.get("AudioPlayer") or {}
    playing = player.get("CurrentlyPlaying") or {}
    duration_ms = _as_int(player.get("DurationMs"))
    played_ms = _as_int(player.get("PlayedMs"))
    attrs: dict[str, Any] = {}
    if playing.get("Title"):
        attrs["media_title"] = playing["Title"]
    if playing.get("SubTitle"):
        attrs["media_artist"] = playing["SubTitle"]
    if playing.get("StreamType"):
        attrs["media_content_type"] = str(playing["StreamType"]).lower()
    if duration_ms:
        attrs["media_duration"] = duration_ms / 1000
    if played_ms:
        attrs["media_position"] = played_ms / 1000
    return attrs


def sound_level(state: dict[str, Any] | None) -> Any:
    return ((state or {}).get("deviceState") or {}).get("SoundLevel")


def sound_max_level(state: dict[str, Any] | None) -> Any:
    return ((state or {}).get("deviceState") or {}).get("SoundMaxLevel")


def sound_muted(state: dict[str, Any] | None) -> bool:
    return bool(((state or {}).get("deviceState") or {}).get("SoundMuted"))


def wifi_type(state: dict[str, Any] | None) -> Any:
    return (((state or {}).get("deviceState") or {}).get("InternetConnection") or {}).get("Type")


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
