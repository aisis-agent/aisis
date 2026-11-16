"""Home Assistant executors for voice commands the quasar layer cannot parse (AC setpoints and deltas, light colour
and brightness). Entities are resolved through HA areas, so a room never borrows a device from another room: when the
room has no suitable entity the answer says so instead of acting elsewhere.
"""
from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable

AREAS = {
    "living": "Гостиная",
    "kitchen": "Кухня",
    "bedroom": "Спальня",
    "studio": "Студия",
    "hall": "Прихожая",
    "upstairs": "Второй этаж",
}
DEFAULT_ROOM = "living"

HVAC_MODES = {"heat": "heat", "cool": "cool", "dry": "dry", "fan": "fan_only", "auto": "auto"}
MODE_NAMES = {"heat": "обогрев", "cool": "охлаждение", "dry": "осушение", "fan": "вентиляция", "auto": "авто"}
MODE_VERBS = {"heat": "Включаю обогрев.", "cool": "Включаю охлаждение.", "dry": "Включаю осушение.",
              "fan": "Включаю вентиляцию.", "auto": "Включаю автоматический режим."}

COLOR_NAMES = {"white": "белый", "red": "красный", "blue": "синий", "green": "зелёный", "yellow": "жёлтый",
               "orange": "оранжевый", "purple": "фиолетовый", "pink": "розовый", "warm": "тёплый", "cold": "холодный"}
KELVIN = {"warm": 2700, "cold": 6500, "white": 4000}
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}

REPLY_UNKNOWN_ROOM = "Не знаю такую комнату."
REPLY_UNKNOWN_COMMAND = "Не понял, что сделать."
REPLY_NO_CLIMATE = "В комнате «{area}» нет кондиционера."
REPLY_NO_LIGHT = "В комнате «{area}» нет света."
REPLY_CLIMATE_UNAVAILABLE = "Кондиционер сейчас недоступен."
REPLY_MODE_UNSUPPORTED = "Кондиционер так не умеет."
REPLY_NO_CURRENT_TEMP = "Не знаю, какая сейчас температура на кондиционере."
REPLY_BAD_DELTA = "Не понял, на сколько изменить температуру."
REPLY_NO_COLOR = "Эта лампа не меняет цвет."
REPLY_NO_BRIGHTNESS = "Эта лампа не регулируется по яркости."
REPLY_LIGHT_UNAVAILABLE = "Свет сейчас недоступен."
REPLY_QUEUED = "Дом перезагружается, команду выполню, когда он вернётся."
REPLY_FAILED = "Не удалось выполнить команду умного дома."
REPLY_TEMPERATURE = "Ставлю {temp}."
REPLY_MODE_TEMPERATURE = "{mode}, {temp}."
REPLY_WARMER = "Делаю теплее, {temp}."
REPLY_COOLER = "Делаю прохладнее, {temp}."
REPLY_COLOR = "{color} свет."
REPLY_BRIGHTNESS = "Яркость {percent}."

_UNAVAILABLE = ("unavailable", "unknown")


class _Outcome(Exception):
    """Abort a command with the sentence to speak (queued, failed)."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(round(float(value), 1)).replace(".", ",")


def _degrees(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value)} {_plural(int(value), ('градус', 'градуса', 'градусов'))}"
    return f"{_number(value)} градуса"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class HaActions:
    def __init__(self, get_json: Callable[[str], Awaitable[Any]],
                 post_json: Callable[[str, dict], Awaitable[tuple[int, Any]]],
                 ttl: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._get = get_json
        self._post = post_json
        self._ttl = ttl
        self._clock = clock
        self._states: tuple[float, dict[str, dict]] | None = None
        self._areas: dict[str, tuple[float, list[str]]] = {}

    async def run(self, executor: str, args: dict) -> str:
        handler = {
            "ha.climate": self._climate,
            "ha.climate_delta": self._climate_delta,
            "ha.light_color": self._light_color,
            "ha.light_brightness": self._light_brightness,
        }.get(executor)
        if handler is None:
            return REPLY_UNKNOWN_COMMAND
        area = AREAS.get(args.get("room") or DEFAULT_ROOM)
        if area is None:
            return REPLY_UNKNOWN_ROOM
        try:
            return await handler(area, args)
        except _Outcome as out:
            return out.reply
        except Exception:
            return REPLY_FAILED

    # -- lookup ---------------------------------------------------------------------------------------------------

    async def _all_states(self) -> dict[str, dict]:
        now = self._clock()
        if self._states and now - self._states[0] < self._ttl:
            return self._states[1]
        raw = await self._get("/api/states")
        if not isinstance(raw, list):
            raise _Outcome(REPLY_FAILED)
        states = {s["entity_id"]: s for s in raw if isinstance(s, dict) and isinstance(s.get("entity_id"), str)}
        self._states = (now, states)
        return states

    async def _area_entity_ids(self, area: str) -> list[str]:
        now = self._clock()
        cached = self._areas.get(area)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        status, body = await self._post("/api/template", {"template": f"{{{{ area_entities('{area}') | tojson }}}}"})
        if status != 200:
            raise _Outcome(REPLY_FAILED)
        parsed = json.loads(body) if isinstance(body, str) else body
        if not isinstance(parsed, list):
            raise _Outcome(REPLY_FAILED)
        ids = [e for e in parsed if isinstance(e, str)]
        self._areas[area] = (now, ids)
        return ids

    async def _in_area(self, area: str, domain: str) -> list[dict]:
        ids = await self._area_entity_ids(area)
        states = await self._all_states()
        return [states[i] for i in ids if i.startswith(domain + ".") and i in states]

    async def _service(self, domain: str, service: str, body: dict) -> None:
        try:
            status, _ = await self._post(f"/api/services/{domain}/{service}", body)
        except Exception:
            raise _Outcome(REPLY_FAILED)
        if status == 202:
            raise _Outcome(REPLY_QUEUED)
        if not 200 <= status < 300:
            raise _Outcome(REPLY_FAILED)

    # -- climate --------------------------------------------------------------------------------------------------

    async def _climates(self, area: str) -> list[dict]:
        found = await self._in_area(area, "climate")
        if not found:
            raise _Outcome(REPLY_NO_CLIMATE.format(area=area))
        live = [c for c in found if c.get("state") not in _UNAVAILABLE]
        if not live:
            raise _Outcome(REPLY_CLIMATE_UNAVAILABLE)
        return live

    @staticmethod
    def _clamp(entity: dict, temp: float) -> float:
        attrs = entity.get("attributes") or {}
        low, high = _num(attrs.get("min_temp")), _num(attrs.get("max_temp"))
        if low is not None:
            temp = max(temp, low)
        if high is not None:
            temp = min(temp, high)
        return temp

    @staticmethod
    def _wake_mode(entity: dict, temp: float) -> str | None:
        """The mode to switch an `off` unit to before a setpoint: heat/cool by where the setpoint sits."""
        attrs = entity.get("attributes") or {}
        modes = [m for m in attrs.get("hvac_modes") or [] if isinstance(m, str) and m != "off"]
        if not modes:
            return None
        current = _num(attrs.get("current_temperature"))
        if current is not None:
            preferred = "heat" if temp > current else "cool"
            if preferred in modes:
                return preferred
        return modes[0]

    async def _set_temperature(self, entity: dict, temp: float, force_mode: str | None = None) -> float:
        entity_id = entity["entity_id"]
        temp = self._clamp(entity, temp)
        mode = force_mode
        if mode is None and entity.get("state") == "off":
            mode = self._wake_mode(entity, temp)
        if mode is not None:
            await self._service("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": mode})
            entity["state"] = mode
        await self._service("climate", "set_temperature",
                            {"entity_id": entity_id, "temperature": int(temp) if temp.is_integer() else temp})
        # States are cached for `ttl`; a repeated «теплее» inside it must start from the setpoint just sent.
        entity.setdefault("attributes", {})["temperature"] = temp
        return temp

    async def _climate(self, area: str, args: dict) -> str:
        mode_key = args.get("mode")
        temperature = _num(args.get("temperature"))
        if mode_key is not None and mode_key not in HVAC_MODES:
            return REPLY_UNKNOWN_COMMAND
        if mode_key is None and temperature is None:
            return REPLY_UNKNOWN_COMMAND
        entities = await self._climates(area)
        hvac = HVAC_MODES.get(mode_key) if mode_key else None
        if hvac is not None:
            supported = [e for e in entities if not (e.get("attributes") or {}).get("hvac_modes")
                         or hvac in e["attributes"]["hvac_modes"]]
            if not supported:
                return REPLY_MODE_UNSUPPORTED
            entities = supported
        spoken = temperature
        for entity in entities:
            if temperature is None:
                await self._service("climate", "set_hvac_mode", {"entity_id": entity["entity_id"], "hvac_mode": hvac})
                entity["state"] = hvac
            else:
                spoken = await self._set_temperature(entity, temperature, hvac)
        if temperature is None:
            return MODE_VERBS[mode_key]
        if mode_key is None:
            return REPLY_TEMPERATURE.format(temp=_degrees(spoken))
        return REPLY_MODE_TEMPERATURE.format(mode=MODE_NAMES[mode_key].capitalize(), temp=_degrees(spoken))

    async def _climate_delta(self, area: str, args: dict) -> str:
        direction = args.get("direction")
        if direction not in (1, -1):
            return REPLY_UNKNOWN_COMMAND
        delta = args.get("delta")
        amount = 1.0
        if delta:
            unit = str(delta.get("unit") or "")
            amount_value = _num(delta.get("amount"))
            if amount_value is None or amount_value <= 0 or not (unit == "" or unit.startswith("градус")):
                return REPLY_BAD_DELTA
            amount = amount_value
        entities = await self._climates(area)
        spoken: float | None = None
        for entity in entities:
            attrs = entity.get("attributes") or {}
            base = _num(attrs.get("temperature"))
            if base is None:
                base = _num(attrs.get("current_temperature"))
            if base is None:
                if len(entities) == 1:
                    return REPLY_NO_CURRENT_TEMP
                continue
            result = await self._set_temperature(entity, base + direction * amount)
            if spoken is None:
                spoken = result
        if spoken is None:
            return REPLY_NO_CURRENT_TEMP
        template = REPLY_WARMER if direction > 0 else REPLY_COOLER
        return template.format(temp=_degrees(spoken))

    # -- light ----------------------------------------------------------------------------------------------------

    async def _lights(self, area: str) -> list[dict]:
        found = await self._in_area(area, "light")
        if not found:
            raise _Outcome(REPLY_NO_LIGHT.format(area=area))
        live = [light for light in found if light.get("state") not in _UNAVAILABLE]
        if not live:
            raise _Outcome(REPLY_LIGHT_UNAVAILABLE)
        return live

    async def _light_color(self, area: str, args: dict) -> str:
        color = args.get("color")
        if color not in COLOR_NAMES:
            return REPLY_UNKNOWN_COMMAND
        done = 0
        for light in await self._lights(area):
            modes = set((light.get("attributes") or {}).get("supported_color_modes") or [])
            has_color = bool(modes & COLOR_MODES)
            has_temp = "color_temp" in modes
            if color in KELVIN and has_temp:
                body: dict[str, Any] = {"color_temp_kelvin": KELVIN[color]}
            elif color == "white" and has_color:
                body = {"color_name": "white"}
            elif color in KELVIN and has_color:
                body = {"color_temp_kelvin": KELVIN[color]}
            elif color not in KELVIN and has_color:
                body = {"color_name": color}
            else:
                continue
            await self._service("light", "turn_on", {"entity_id": light["entity_id"], **body})
            done += 1
        if not done:
            return REPLY_NO_COLOR
        return REPLY_COLOR.format(color=COLOR_NAMES[color].capitalize())

    async def _light_brightness(self, area: str, args: dict) -> str:
        percent = _num(args.get("percent"))
        if percent is None or not 1 <= percent <= 100:
            return REPLY_UNKNOWN_COMMAND
        percent = int(percent)
        done = 0
        for light in await self._lights(area):
            modes = (light.get("attributes") or {}).get("supported_color_modes")
            if modes is not None and set(modes) <= {"onoff"}:
                continue
            await self._service("light", "turn_on", {"entity_id": light["entity_id"], "brightness_pct": percent})
            done += 1
        if not done:
            return REPLY_NO_BRIGHTNESS
        return REPLY_BRIGHTNESS.format(percent=f"{percent} {_plural(percent, ('процент', 'процента', 'процентов'))}")
