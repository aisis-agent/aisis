"""Home Assistant executors for voice commands the quasar layer cannot parse (AC setpoints and deltas, light colour
and brightness). Entities are resolved through HA areas, so a room never borrows a device from another room: when the
room has no suitable entity the answer says so instead of acting elsewhere.

A command without a room is about the room of the speaker that heard it. That room is read from Home Assistant the way
the quasar overlay's speaker-room layer reads it (overlays/speaker-room): the area of the station's `media_player`
entity, whose id ends in the station id; the adapter keeps no station table of its own. A station whose room Home
Assistant does not tell is asked for the room, never guessed (the living room is no default).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import utterance_trace

AREAS = {
    "living": "Гостиная",
    "kitchen": "Кухня",
    "bedroom": "Спальня",
    "studio": "Студия",
    "hall": "Прихожая",
    "upstairs": "Второй этаж",
}

# Entity registry of the quasar layer and hacli (`entity \t friendly name \t area` per entity that has a device). hacli
# answers this very template from its cache, with status 200, while Home Assistant is down.
REGISTRY_TEMPLATE = (
    "{% for e in states %}{% set d = device_id(e.entity_id) %}"
    "{% if d %}{{ e.entity_id }}\t{{ e.attributes.friendly_name }}\t"
    "{{ area_name(d) }}\n{% endif %}{% endfor %}"
)
OWN_PLAYER_PREFIX = "media_player.aisis_speaker_"   # the AISIS speaker's own player wins over the Yandex integration's one
# Equipment lights (printers' LEDs) that are never part of «свет»: the same filter as the quasar layer's JUNK_LIGHT.
JUNK_LIGHT = re.compile(r"(k1se|snapmaker|cavity)", re.IGNORECASE)

HVAC_MODES = {"heat": "heat", "cool": "cool", "dry": "dry", "fan": "fan_only", "auto": "auto"}
MODE_NAMES = {"heat": "обогрев", "cool": "охлаждение", "dry": "осушение", "fan": "вентиляция", "auto": "авто"}
MODE_VERBS = {"heat": "Включаю обогрев.", "cool": "Включаю охлаждение.", "dry": "Включаю осушение.",
              "fan": "Включаю вентиляцию.", "auto": "Включаю автоматический режим."}

COLOR_NAMES = {"white": "белый", "red": "красный", "blue": "синий", "green": "зелёный", "yellow": "жёлтый",
               "orange": "оранжевый", "purple": "фиолетовый", "pink": "розовый", "warm": "тёплый", "cold": "холодный"}
KELVIN = {"warm": 2700, "cold": 6500, "white": 4000}
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}

REPLY_WHICH_ROOM = "В какой комнате? Назови комнату в команде."
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
# What is said after a command is decided by what was OBSERVED (ha-cli verifies every call against the device state): the state said
# yes, the state said no, or nothing could be read. A cause (a restart, an outage) is only named when ha-cli observed it.
REPLY_QUEUED = "Home Assistant недоступен, команду выполню, когда он вернётся."
REPLY_QUEUED_STARTING = "Home Assistant запускается, команду выполню, когда он вернётся."
REPLY_QUEUED_UNKNOWN = "Не получилось проверить ответ Home Assistant, команда ждёт в очереди."
REPLY_UNVERIFIED = "Не получилось проверить ответ Home Assistant."
REPLY_NOT_CONFIRMED = "Команду отправил, но не вижу, чтобы устройство её выполнило."
REPLY_DEVICE_UNAVAILABLE = "Home Assistant видит это устройство недоступным."
REPLY_REJECTED = "Home Assistant отклонил команду."
REPLY_FAILED = "Не удалось выполнить команду умного дома."
REPLY_TEMPERATURE = "Ставлю {temp}."
REPLY_MODE_TEMPERATURE = "{mode}, {temp}."
REPLY_WARMER = "Делаю теплее, {temp}."
REPLY_COOLER = "Делаю прохладнее, {temp}."
REPLY_COLOR = "{color} свет."
REPLY_BRIGHTNESS = "Яркость {percent}."

_UNAVAILABLE = ("unavailable", "unknown")


@dataclass(frozen=True)
class HaReply:
    """What a POST to ha-cli answered. `verify` is ha-cli's verdict on the device state after the call (hacli.py, ha_verify.py):
    {"result": ok|failed|unknown|skipped, "reason", "ms", "attempts", "after": {entity: state}}; None from a proxy that does not
    verify. `ha`: Home Assistant's own outcome as ha-cli saw it (an HTTP status or an error kind), `down`: the evidence that
    Home Assistant is gone, on a call that was queued."""
    status: int
    body: Any = None
    verify: dict | None = None
    action: str | None = None
    ha: str | None = None
    down: str | None = None

    @classmethod
    def from_response(cls, status: int, body: Any, headers: Any) -> "HaReply":
        result = headers.get("X-Aisis-Verify")
        verify = None
        if result:
            after = {}
            for item in (headers.get("X-Aisis-Verified-State") or "").split(","):
                entity, _, state = item.partition("=")
                if entity and state:
                    after[entity] = state
            ms, attempts = headers.get("X-Aisis-Verify-Ms"), headers.get("X-Aisis-Verify-Attempts")
            verify = {"result": result, "reason": headers.get("X-Aisis-Verify-Reason"),
                      "ms": int(ms) if ms and ms.isdigit() else None, "attempts": int(attempts) if attempts and attempts.isdigit() else None,
                      "after": after or None}
            unchecked = [part for part in (headers.get("X-Aisis-Verify-Unchecked") or "").split(",") if part]
            if unchecked:
                verify["unchecked"] = unchecked
        return cls(status, body, verify, headers.get("X-Aisis-Action"), headers.get("X-Aisis-Ha-Status"), headers.get("X-Aisis-Down"))


class _Outcome(Exception):
    """Abort a command with the sentence to speak (no such device, Home Assistant failed) and the reason code of that sentence."""

    def __init__(self, reply: str, why: str = "not_executed") -> None:
        super().__init__(reply)
        self.reply = reply
        self.why = why


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


class _Command:
    """The service calls of one voice command. hacli answers 202 when it OBSERVED Home Assistant gone and queued the call for the
    recovery: the command goes on, so that every operation of it (all the entities, the mode and the setpoint) is queued.
    `queued` is None, or the evidence of the outage hacli gave (empty from a proxy that gave none)."""

    def __init__(self) -> None:
        self.queued: str | None = None
        self.verified = 0           # calls whose device state was read and was as expected
        self.partial = 0            # calls whose state matched but part of the request (a colour the lamp cannot show) could not be checked
        self.unverified = 0         # calls Home Assistant acknowledged without a verdict
        self.failure: _Outcome | None = None   # the first independent call that was not confirmed: spoken once the others were sent

    def raise_failure(self) -> None:
        if self.failure is not None:
            raise self.failure


def _registry(body: Any) -> dict[str, str]:
    """entity id -> area name from the tab-separated registry (`REGISTRY_TEMPLATE`); entities without an area are left out."""
    if not isinstance(body, str):
        return {}
    areas: dict[str, str] = {}
    for line in body.splitlines():
        entity_id, _, rest = line.partition("\t")
        area = rest.rpartition("\t")[2].strip()
        if entity_id and area and area != "None":
            areas[entity_id] = area
    return areas


def speaker_area(areas: dict[str, str], station: str | None) -> str | None:
    """The HA area of the station `station` (speaker-room.ts `speakerArea`): the area of its own player entity, else of any
    player whose id ends in `_<station id>`; None when Home Assistant has no such player or it has no area."""
    ident = (station or "").strip().lower()
    if not ident:
        return None
    other: str | None = None
    for entity_id, area in areas.items():
        if not entity_id.startswith("media_player.") or not entity_id.endswith("_" + ident):
            continue
        if entity_id.startswith(OWN_PLAYER_PREFIX):
            return area
        other = other or area
    return other


class HaActions:
    def __init__(self, get_json: Callable[[str], Awaitable[Any]],
                 post_json: Callable[[str, dict], Awaitable[HaReply]],
                 ttl: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._get = get_json
        self._post = post_json
        self._ttl = ttl
        self._clock = clock
        self._states: tuple[float, dict[str, dict]] | None = None
        self._areas: dict[str, tuple[float, list[str]]] = {}
        self._registry: tuple[float, dict[str, str]] | None = None

    @staticmethod
    def _queued_reply(evidence: str) -> str:
        return {"": REPLY_QUEUED_UNKNOWN, "starting": REPLY_QUEUED_STARTING}.get(evidence, REPLY_QUEUED)

    async def run(self, executor: str, args: dict, station: str | None = None) -> str:
        """`station`: the speaker that heard the command, for a command that names no room."""
        handler = {
            "ha.climate": self._climate,
            "ha.climate_delta": self._climate_delta,
            "ha.light_color": self._light_color,
            "ha.light_brightness": self._light_brightness,
        }.get(executor)
        if handler is None:
            return REPLY_UNKNOWN_COMMAND
        command = _Command()
        try:
            area = await self._room(args.get("room"), station)
            utterance_trace.note(room=area)
            reply = await handler(area, args, command)
        except _Outcome as out:
            utterance_trace.note(outcome=out.reply, reply_why=out.why)
            if command.queued is not None:
                # Other calls of the command were queued for Home Assistant's return: the failure is not the whole story.
                return f"{out.reply} {self._queued_reply(command.queued)}"
            return out.reply
        except Exception as err:
            utterance_trace.error("ha: " + type(err).__name__)
            utterance_trace.note(reply_why="ha_exception")
            return REPLY_FAILED
        if command.queued is not None:
            utterance_trace.note(reply_why="ha_down_observed" if command.queued else "ha_queued_unverified")
            return self._queued_reply(command.queued)
        if not command.verified and not command.partial and not command.unverified:
            utterance_trace.note(reply_why="not_executed")     # a reply that needed no Home Assistant call at all
        elif command.partial:
            utterance_trace.note(reply_why="ha_partially_verified")
        else:
            utterance_trace.note(reply_why="ha_verified" if command.verified and not command.unverified else "ha_acknowledged")
        return reply

    async def _room(self, room: str | None, station: str | None) -> str:
        """The HA area the command is about: the room said, else the room of the speaker that heard it."""
        if room:
            area = AREAS.get(room)
            if area is None:
                raise _Outcome(REPLY_UNKNOWN_ROOM)
            return area
        area = speaker_area(await self._area_registry(), station)
        if area is None:
            raise _Outcome(REPLY_WHICH_ROOM)
        return area

    # -- lookup ---------------------------------------------------------------------------------------------------

    async def _all_states(self) -> dict[str, dict]:
        now = self._clock()
        if self._states and now - self._states[0] < self._ttl:
            return self._states[1]
        raw = await self._get("/api/states")
        if not isinstance(raw, list):
            raise _Outcome(REPLY_FAILED, "ha_read_failed")
        states = {s["entity_id"]: s for s in raw if isinstance(s, dict) and isinstance(s.get("entity_id"), str)}
        self._states = (now, states)
        return states

    async def _area_registry(self) -> dict[str, str]:
        now = self._clock()
        if self._registry and now - self._registry[0] < self._ttl:
            return self._registry[1]
        reply = await self._post("/api/template", {"template": REGISTRY_TEMPLATE})
        if reply.status != 200:
            raise _Outcome(REPLY_FAILED, "ha_read_failed")
        areas = _registry(reply.body)
        self._registry = (now, areas)
        return areas

    async def _area_entity_ids(self, area: str) -> list[str]:
        now = self._clock()
        cached = self._areas.get(area)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        # The area name is data (Home Assistant's, or the station's registry entry): it reaches the template only as an escaped
        # string literal. json.dumps escapes the double quote, the backslash and control characters, which Jinja's string
        # tokenizer decodes back; an apostrophe or `') if … else area_entities('` in a name stays inside the string.
        template = f"{{{{ area_entities({json.dumps(area, ensure_ascii=False)}) | tojson }}}}"
        reply = await self._post("/api/template", {"template": template})
        if reply.status != 200:
            raise _Outcome(REPLY_FAILED, "ha_read_failed")
        body = reply.body
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
        except ValueError:
            # hacli answers every template with its cached registry while Home Assistant is down: read the area off that
            registry = _registry(body)
            parsed = [entity_id for entity_id, name in registry.items() if name == area] if registry else None
        if not isinstance(parsed, list):
            raise _Outcome(REPLY_FAILED, "ha_read_failed")
        ids = [e for e in parsed if isinstance(e, str)]
        self._areas[area] = (now, ids)
        return ids

    async def _in_area(self, area: str, domain: str) -> list[dict]:
        ids = await self._area_entity_ids(area)
        states = await self._all_states()
        return [states[i] for i in ids if i.startswith(domain + ".") and i in states]

    async def _service(self, command: _Command, domain: str, service: str, body: dict) -> None:
        started = time.monotonic()
        fields = {"service": f"{domain}.{service}", "entity": body.get("entity_id"),
                  "data": {k: v for k, v in body.items() if k != "entity_id"} or None}
        try:
            reply = await self._post(f"/api/services/{domain}/{service}", body)
        except Exception as err:
            utterance_trace.call("ha", **fields, ok=False, error=type(err).__name__, ms=int((time.monotonic() - started) * 1000))
            raise _Outcome(REPLY_UNVERIFIED, "ha_unreachable")
        verify = reply.verify or {}
        result = verify.get("result")
        ok = reply.status == 202 or result == "ok" or (result not in ("failed", "unknown") and 200 <= reply.status < 300)
        utterance_trace.call("ha", **fields, status=reply.status, ok=ok, queued=True if reply.status == 202 else None,
                             verify=reply.verify, aid=reply.action, ha=reply.ha, down=reply.down,
                             ms=int((time.monotonic() - started) * 1000))
        if reply.status == 202:
            command.queued = reply.down or ""
        elif result == "ok":
            if verify.get("unchecked"):
                command.partial += 1
            else:
                command.verified += 1
        elif result == "failed":
            unavailable = verify.get("reason") == "device_unavailable"
            raise _Outcome(REPLY_DEVICE_UNAVAILABLE if unavailable else REPLY_NOT_CONFIRMED,
                           "ha_device_unavailable" if unavailable else "ha_not_confirmed")
        elif result == "unknown":
            raise _Outcome(REPLY_UNVERIFIED, "ha_unverified")
        elif 400 <= reply.status < 500:
            raise _Outcome(REPLY_REJECTED, "ha_rejected")
        elif 200 <= reply.status < 300:
            command.unverified += 1
        else:
            raise _Outcome(REPLY_FAILED, "ha_error")

    async def _each(self, command: _Command, call: Awaitable[Any]) -> tuple[bool, Any]:
        """One of several calls that do not depend on each other (the lamps of a room, the units of an area): a call that was not
        confirmed does not stop the others, the first such failure is spoken after all of them were sent. An unreachable ha-cli
        still stops the command: every further call would wait for the same timeout."""
        try:
            return True, await call
        except _Outcome as outcome:
            if outcome.why == "ha_unreachable":
                raise
            command.failure = command.failure or outcome
            return False, None

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

    async def _set_temperature(self, command: _Command, entity: dict, temp: float, force_mode: str | None = None) -> float:
        entity_id = entity["entity_id"]
        temp = self._clamp(entity, temp)
        mode = force_mode
        if mode is None and entity.get("state") == "off":
            mode = self._wake_mode(entity, temp)
        if mode is not None:
            await self._service(command, "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": mode})
            entity["state"] = mode
        await self._service(command, "climate", "set_temperature",
                            {"entity_id": entity_id, "temperature": int(temp) if temp.is_integer() else temp})
        # States are cached for `ttl`; a repeated «теплее» inside it must start from the setpoint just sent.
        entity.setdefault("attributes", {})["temperature"] = temp
        return temp

    async def _climate(self, area: str, args: dict, command: _Command) -> str:
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
                sent, _ = await self._each(command, self._service(command, "climate", "set_hvac_mode",
                                                                  {"entity_id": entity["entity_id"], "hvac_mode": hvac}))
                if sent:
                    entity["state"] = hvac
            else:
                sent, result = await self._each(command, self._set_temperature(command, entity, temperature, hvac))
                if sent:
                    spoken = result
        command.raise_failure()
        if temperature is None:
            return MODE_VERBS[mode_key]
        if mode_key is None:
            return REPLY_TEMPERATURE.format(temp=_degrees(spoken))
        return REPLY_MODE_TEMPERATURE.format(mode=MODE_NAMES[mode_key].capitalize(), temp=_degrees(spoken))

    async def _climate_delta(self, area: str, args: dict, command: _Command) -> str:
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
            sent, result = await self._each(command, self._set_temperature(command, entity, base + direction * amount))
            if sent and spoken is None:
                spoken = result
        command.raise_failure()
        if spoken is None:
            return REPLY_NO_CURRENT_TEMP
        template = REPLY_WARMER if direction > 0 else REPLY_COOLER
        return template.format(temp=_degrees(spoken))

    # -- light ----------------------------------------------------------------------------------------------------

    async def _lights(self, area: str) -> list[dict]:
        found = [light for light in await self._in_area(area, "light") if not JUNK_LIGHT.search(light["entity_id"])]
        if not found:
            raise _Outcome(REPLY_NO_LIGHT.format(area=area))
        live = [light for light in found if light.get("state") not in _UNAVAILABLE]
        if not live:
            raise _Outcome(REPLY_LIGHT_UNAVAILABLE)
        return live

    async def _light_color(self, area: str, args: dict, command: _Command) -> str:
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
            await self._each(command, self._service(command, "light", "turn_on", {"entity_id": light["entity_id"], **body}))
            done += 1
        command.raise_failure()
        if not done:
            return REPLY_NO_COLOR
        return REPLY_COLOR.format(color=COLOR_NAMES[color].capitalize())

    async def _light_brightness(self, area: str, args: dict, command: _Command) -> str:
        percent = _num(args.get("percent"))
        if percent is None or not 1 <= percent <= 100:
            return REPLY_UNKNOWN_COMMAND
        percent = int(percent)
        done = 0
        for light in await self._lights(area):
            modes = (light.get("attributes") or {}).get("supported_color_modes")
            if modes is not None and set(modes) <= {"onoff"}:
                continue
            await self._each(command, self._service(command, "light", "turn_on",
                                                    {"entity_id": light["entity_id"], "brightness_pct": percent}))
            done += 1
        command.raise_failure()
        if not done:
            return REPLY_NO_BRIGHTNESS
        return REPLY_BRIGHTNESS.format(percent=f"{percent} {_plural(percent, ('процент', 'процента', 'процентов'))}")
