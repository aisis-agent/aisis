"""What a Home Assistant service call must leave in the device state, and whether the state read afterwards says so.

A command is done when the DEVICE says so, not when Home Assistant's HTTP answer came back: the answer can time out after the
device already obeyed (2026-11-19 21:21: `light.turn_off` took longer than the old proxy's 2 s, the light was off 0.85 s after the
command and the speaker still announced a reboot). ha_call.py runs every service call against these expectations, over Home
Assistant's own WebSocket events; the adapter reads the outcome from the verdict it returns. Stdlib only.

`expectation(domain, service, body, before)` -> `Expectation` or None (nothing in the device state tells whether the call
worked: tts.speak, scripts, scenes, a call that targets an area or a device instead of entities).
`Expectation.check(states)` -> `Verdict` over freshly read states `{entity_id: state dict | None}`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

UNAVAILABLE = ("unavailable", "unknown")
MAX_ENTITIES = 8             # a call on more explicit entities than this is not polled one by one
ON_OFF_DOMAINS = ("light", "switch", "fan", "input_boolean", "humidifier", "siren", "automation", "remote")
WHOLE_DOMAINS = ("light", "switch", "fan")   # `entity_id: all` is verified for these, through one template read
BRIGHTNESS_TOLERANCE = 6     # of 255: devices with fewer levels round the requested value
KELVIN_TOLERANCE = 200       # colour temperature is quantized in mireds
TEMPERATURE_TOLERANCE = 0.51 # a climate entity rounds the setpoint to its own step
VOLUME_TOLERANCE = 0.025


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _attrs(state: dict) -> dict:
    attrs = state.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def state_text(state: dict | None) -> str:
    """The part of a state worth a log line: the state, plus the attribute a command of its domain sets. No tokens, no names."""
    if not isinstance(state, dict):
        return "?"
    value = str(state.get("state", "?"))
    attrs = _attrs(state)
    domain = str(state.get("entity_id", "")).partition(".")[0]
    extra: list[str] = []
    if domain == "light" and value == "on":
        brightness = _num(attrs.get("brightness"))
        if brightness is not None:
            extra.append(f"{round(brightness / 255 * 100)}%")
        kelvin = _num(attrs.get("color_temp_kelvin"))
        if kelvin is not None:
            extra.append(f"{round(kelvin)}K")
    elif domain == "climate":
        setpoint = _num(attrs.get("temperature"))
        if setpoint is not None:
            extra.append(f"{setpoint:g}°")
    elif domain == "media_player":
        volume = _num(attrs.get("volume_level"))
        if volume is not None:
            extra.append(f"vol {volume:.2f}")
        if attrs.get("is_volume_muted") is True:
            extra.append("muted")
    elif domain == "cover":
        position = _num(attrs.get("current_position"))
        if position is not None:
            extra.append(f"{position:g}%")
    return value + (" " + " ".join(extra) if extra else "")


def _listed(body: Any) -> list[str] | None:
    """The entity ids a service call body lists; None when it addresses anything else (areas, devices, labels, nothing)."""
    if not isinstance(body, dict):
        return None
    if any(body.get(key) for key in ("area_id", "device_id", "floor_id", "label_id")):
        return None
    raw = body.get("entity_id")
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) and item for item in raw):
        return None
    return raw


def entity_ids(body: Any) -> tuple[str, ...] | None:
    """Every explicit entity id a call body addresses, however many; None for `all`, areas, devices or anything unparsable."""
    raw = _listed(body)
    if raw is None or "all" in raw or any("." not in item for item in raw):
        return None
    return tuple(dict.fromkeys(raw))


def targets(body: Any) -> tuple[tuple[str, ...], str | None] | None:
    """(explicit entity ids, whole domain or None) a service call body addresses; None when it addresses anything else
    (areas, devices, labels, nothing) or too many entities to poll."""
    raw = _listed(body)
    if raw == ["all"]:
        return (), "all"
    ids = entity_ids(body)
    return (ids, None) if ids is not None and len(ids) <= MAX_ENTITIES else None


class Unchecked(str):
    """What a rule returns when the state cannot say whether this part of the request happened (not a mismatch, not a confirmation).
    The value is the attribute name, or the comma-joined names when several rules of one request could not be checked."""

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.split(","))


Rule = Callable[[dict], "str | None"]   # a state -> None when it is the expected one, else what it is instead


def _state_in(*wanted: str) -> Rule:
    return lambda state: None if state.get("state") in wanted else str(state.get("state"))


def _attr_close(name: str, wanted: float, tolerance: float, scale: float = 1.0) -> Rule:
    def rule(state: dict) -> str | None:
        have = _num(_attrs(state).get(name))
        if have is not None and abs(have * scale - wanted) <= tolerance:
            return None
        return state_text(state)
    return rule


def _light_attr_close(name: str, wanted: float, tolerance: float, scale: float = 1.0) -> Rule:
    """`_attr_close` for a light attribute the lamp may be unable to report: a lamp that cannot dim (on/off only) or whose colour
    is not a temperature (hs/rgb/xy only: Home Assistant turns the kelvin it was sent into a colour) leaves it out of the state,
    and a lamp whose range is narrower than the request reports the end of its range."""
    def rule(state: dict) -> str | None:
        attrs = _attrs(state)
        modes = attrs.get("supported_color_modes")
        if isinstance(modes, list):
            if name == "brightness" and set(modes) <= {"onoff"}:
                return Unchecked(name)      # an on/off lamp cannot show a brightness: on is confirmed, the level is not
            if name == "color_temp_kelvin" and "color_temp" not in modes:
                return Unchecked(name)      # Home Assistant turned the kelvin into a colour of an hs/rgb/xy lamp: no attribute holds it
        target = wanted
        if name == "color_temp_kelvin":
            low, high = _num(attrs.get("min_color_temp_kelvin")), _num(attrs.get("max_color_temp_kelvin"))
            if low is not None:
                target = max(target, low)
            if high is not None:
                target = min(target, high)
        return _attr_close(name, target, tolerance, scale)(state)
    return rule


def _attr_equals(name: str, wanted: Any) -> Rule:
    return lambda state: None if _attrs(state).get(name) == wanted else state_text(state)


def _all_of(*rules: Rule) -> Rule:
    def rule(state: dict) -> str | None:
        unchecked: list[str] = []
        for item in rules:
            miss = item(state)
            if isinstance(miss, Unchecked):
                unchecked.extend(miss.names)
            elif miss is not None:
                return miss
        return Unchecked(",".join(dict.fromkeys(unchecked))) if unchecked else None
    return rule


def _light_on(body: dict) -> Rule | None:
    rules: list[Rule] = [_state_in("on")]
    pct, raw = _num(body.get("brightness_pct")), _num(body.get("brightness"))
    if pct is not None and pct <= 0 or raw is not None and raw <= 0:
        return _state_in("off")             # HA turns a light off for a brightness of zero
    transitioning = (_num(body.get("transition")) or 0) > 0
    if not transitioning:
        if pct is not None:
            rules.append(_light_attr_close("brightness", pct * 255 / 100, BRIGHTNESS_TOLERANCE))
        elif raw is not None:
            rules.append(_light_attr_close("brightness", raw, BRIGHTNESS_TOLERANCE))
        kelvin = _num(body.get("color_temp_kelvin")) or _num(body.get("kelvin"))
        if kelvin is not None:
            rules.append(_light_attr_close("color_temp_kelvin", kelvin, KELVIN_TOLERANCE))
    return _all_of(*rules)


def _cover(service: str, body: dict) -> Rule | None:
    if service == "open_cover":
        return _state_in("open", "opening")
    if service == "close_cover":
        return _state_in("closed", "closing")
    position = _num(body.get("position"))
    if service == "set_cover_position" and position is not None:
        near = _attr_close("current_position", position, 3.0)
        moving = _state_in("opening", "closing")
        return lambda state: None if near(state) is None or moving(state) is None else state_text(state)
    return None


def _climate(service: str, body: dict) -> Rule | None:
    if service == "set_hvac_mode" and isinstance(body.get("hvac_mode"), str):
        return _state_in(body["hvac_mode"])
    if service == "set_temperature":
        wanted = _num(body.get("temperature"))
        if wanted is None:
            return None
        rule = _attr_close("temperature", wanted, TEMPERATURE_TOLERANCE)
        mode = body.get("hvac_mode")
        return _all_of(_state_in(mode), rule) if isinstance(mode, str) else rule
    if service == "turn_off":
        return _state_in("off")
    if service == "set_fan_mode" and isinstance(body.get("fan_mode"), str):
        return _attr_equals("fan_mode", body["fan_mode"])
    if service == "set_preset_mode" and isinstance(body.get("preset_mode"), str):
        return _attr_equals("preset_mode", body["preset_mode"])
    return None


def _media_player(service: str, body: dict) -> Rule | None:
    if service == "volume_set":
        level = _num(body.get("volume_level"))
        return _attr_close("volume_level", level, VOLUME_TOLERANCE) if level is not None else None
    if service == "volume_mute" and isinstance(body.get("is_volume_muted"), bool):
        return _attr_equals("is_volume_muted", body["is_volume_muted"])
    if service == "select_source" and isinstance(body.get("source"), str):
        return _attr_equals("source", body["source"])
    if service == "turn_on":
        return lambda state: None if state.get("state") not in ("off", *UNAVAILABLE) else str(state.get("state"))
    return {
        "media_play": _state_in("playing", "buffering"),
        "media_pause": _state_in("paused", "idle"),    # some players report «idle» once paused: not playing is what a pause is for
        "media_stop": _state_in("idle", "off", "standby", "stopped"),
        "turn_off": _state_in("off", "standby"),
    }.get(service)


def _lock(service: str, _body: dict) -> Rule | None:
    return {"lock": _state_in("locked", "locking"), "unlock": _state_in("unlocked", "unlocking")}.get(service)


def _value(service: str, body: dict, domain: str) -> Rule | None:
    if domain in ("number", "input_number") and service == "set_value":
        value = _num(body.get("value"))
        if value is None:
            return None
        return lambda state: None if _num(state.get("state")) == value else str(state.get("state"))
    if domain in ("select", "input_select") and service == "select_option" and isinstance(body.get("option"), str):
        return _state_in(body["option"])
    if domain == "input_text" and service == "set_value" and isinstance(body.get("value"), str):
        return _state_in(body["value"])
    return None


def _rule(domain: str, service: str, body: dict, before: dict | None) -> Rule | None:
    """The check of one entity of `domain` for the call `service`; None: this call leaves nothing to check."""
    if domain in ON_OFF_DOMAINS:
        if service == "turn_off":
            return _state_in("off")
        if service == "turn_on":
            return _light_on(body) if domain == "light" else _state_in("on")
        if service == "toggle" and isinstance(before, dict):
            opposite = {"on": "off", "off": "on"}.get(str(before.get("state")))
            return _state_in(opposite) if opposite else None
        return None
    if domain == "cover":
        return _cover(service, body)
    if domain == "climate":
        return _climate(service, body)
    if domain == "media_player":
        return _media_player(service, body)
    if domain == "lock":
        return _lock(service, body)
    return _value(service, body, domain)


@dataclass(frozen=True)
class Verdict:
    result: str                     # "ok" | "failed" | "pending" (some entity could not be read yet)
    reason: str                     # state_matched | state_mismatch | device_unavailable | state_unreadable
    after: dict[str, str] = field(default_factory=dict)    # entity id -> state_text
    wrong: dict[str, str] = field(default_factory=dict)    # the entities that are not as expected -> what they are instead
    unchecked: tuple[str, ...] = ()                        # parts of the request the state cannot confirm (an ok verdict with these is partial)


@dataclass(frozen=True)
class Expectation:
    domain: str
    service: str
    entities: tuple[str, ...]
    rules: dict[str, Rule]
    want: str                        # what the call should have left, for a log line: «off», «brightness 30%»
    whole_domain: str | None = None  # `entity_id: all`: the entities of this domain, read through one template
    whole_state: str | None = None   # ... and the state none of them may differ from (unavailable ones are not counted)

    def check(self, states: dict[str, dict | None]) -> Verdict:
        after: dict[str, str] = {}
        wrong: dict[str, str] = {}
        unreadable = False
        unchecked: list[str] = []
        unavailable: list[str] = []
        for entity_id in self.entities:
            state = states.get(entity_id)
            if not isinstance(state, dict):
                unreadable = True
                continue
            after[entity_id] = state_text(state)
            if state.get("state") in UNAVAILABLE:
                unavailable.append(entity_id)
                wrong[entity_id] = str(state.get("state"))
                continue
            miss = self.rules[entity_id](state)
            if isinstance(miss, Unchecked):
                unchecked.extend(miss.names)
            elif miss is not None:
                wrong[entity_id] = miss
        if wrong:
            reason = "device_unavailable" if set(wrong) == set(unavailable) else "state_mismatch"
            return Verdict("failed", reason, after, wrong)
        if unreadable:
            return Verdict("pending", "state_unreadable", after)
        return Verdict("ok", "state_matched", after, unchecked=tuple(dict.fromkeys(unchecked)))


def whole_domain_template(domain: str, wanted: str) -> str:
    """Template that lists the entities of `domain` that are available and not in the state `wanted` (JSON list)."""
    return ("{{ states." + domain + " | rejectattr('state', 'in', ['unavailable', 'unknown'])"
            " | rejectattr('state', 'eq', " + json.dumps(wanted) + ") | map(attribute='entity_id') | list | tojson }}")


def whole_domain_verdict(raw: Any) -> Verdict:
    """The verdict of the answer to `whole_domain_template`: nothing left in another state means the call worked."""
    try:
        left = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        left = None
    if not isinstance(left, list):
        return Verdict("pending", "state_unreadable")
    if not left:
        return Verdict("ok", "state_matched")
    shown = {str(item): "differs" for item in left[:5]}
    return Verdict("failed", "state_mismatch", {}, shown | ({"…": f"+{len(left) - 5}"} if len(left) > 5 else {}))


def expectation(domain: str, service: str, body: Any, before: dict[str, dict | None] | None = None) -> Expectation | None:
    """What the call `domain.service(body)` must leave behind; None when the device state cannot tell."""
    found = targets(body)
    if found is None:
        return None
    entities, whole = found
    body = body if isinstance(body, dict) else {}
    if whole is not None:
        wanted = {"turn_on": "on", "turn_off": "off"}.get(service)
        if domain in WHOLE_DOMAINS and wanted is not None:
            return Expectation(domain, service, (), {}, wanted, whole_domain=domain, whole_state=wanted)
        return None
    rules: dict[str, Rule] = {}
    for entity_id in entities:
        entity_domain = entity_id.partition(".")[0]
        rule_domain = entity_domain if domain == "homeassistant" else domain
        if rule_domain != entity_domain:
            return None                     # a call addressed to an entity of another domain: not ours to judge
        rule = _rule(rule_domain, service, body, (before or {}).get(entity_id))
        if rule is None:
            return None
        rules[entity_id] = rule
    return Expectation(domain, service, entities, rules, _want_text(service, body))


WANT_KEYS = ("brightness_pct", "brightness", "color_temp_kelvin", "hvac_mode", "temperature", "volume_level", "is_volume_muted",
             "option", "value", "position", "source", "fan_mode", "preset_mode")


def _want_text(service: str, body: dict) -> str:
    """The call as a log line states what it wants: `turn_off`, `turn_on brightness_pct=40`, `set_temperature temperature=24`.
    Only the arguments the expectation is built from: a service call body can carry anything."""
    wanted = " ".join(f"{key}={str(body[key])[:40]}" for key in WANT_KEYS if isinstance(body.get(key), (str, int, float, bool)))
    return f"{service} {wanted}".strip()
