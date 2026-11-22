"""Spoken state of the home from facts that were read: the air conditioners («какой кондиционер включён?»).

Pure: units and a clock in, one sentence out; nothing here reads Home Assistant or sends anything (`ha_actions.HaActions.
climate_status` reads the cached states and builds the units). Wording is impersonal: nothing in a sentence names who
switched or does anything («колонка», «я»), because the answer is about the device, not about an actor.

Durations are approximations and sound like it: «около 20 минут», «больше часа», «с утра». The table is in
docs/specs/150-dynamic-intents-and-weighted-answers.md (§4.2).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from intent_grammar.timewords import plural

# HA area name -> where, as spoken («в гостиной»). An area that is not listed is spoken by its name.
LOCATIVE = {
    "Гостиная": "в гостиной",
    "Кухня": "на кухне",
    "Спальня": "в спальне",
    "Студия": "в студии",
    "Прихожая": "в прихожей",
    "Второй этаж": "на втором этаже",
}

MODE_NAMES = {"heat": "обогрев", "cool": "охлаждение", "dry": "осушение", "fan_only": "вентиляция", "auto": "автоматический режим",
              "heat_cool": "автоматический режим"}   # Home Assistant reports auto as heat_cool on some units (ha_actions.STATE_MODES)
DEAD_STATES = ("unavailable", "unknown")

STATUS_UNAVAILABLE = "Состояние кондиционеров сейчас узнать не удаётся."
NO_UNIT_IN_AREA = "В комнате «{area}» нет кондиционера."
NO_UNITS = "Кондиционеров в доме не найдено."

MORNING_SWITCHED_BEFORE = 10     # «с утра»: switched before this hour ...
MORNING_SPOKEN_AFTER = 12        # ... and asked at or after this one, the same day


def locative(area: str) -> str:
    return LOCATIVE.get(area) or f"в комнате «{area}»"


def and_list(items: Sequence[str]) -> str:
    """«A», «A и B», «A, B и C»."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " и " + items[-1]


def degrees(value: float) -> str:
    """30 -> «30 градусов», 19.8 -> «19,8 градуса»."""
    if float(value).is_integer():
        number = int(value)
        return f"{number} {plural(number, 'градус', 'градуса', 'градусов')}"
    return f"{str(round(float(value), 1)).replace('.', ',')} градуса"


@dataclass(frozen=True)
class Unit:
    """One air conditioner as the states report it. `where` is spoken («на втором этаже»); `since` is the entity's `last_changed`
    (aware), None when the state has no usable one."""
    where: str | None
    state: str
    setpoint: float | None = None
    current: float | None = None
    since: datetime | None = None

    @property
    def dead(self) -> bool:
        return self.state in DEAD_STATES

    @property
    def on(self) -> bool:
        return not self.dead and self.state != "off"

    @property
    def detail(self) -> str:
        """«обогрев, 30 градусов»: what is known about a running unit, nothing for the rest."""
        parts = [MODE_NAMES[self.state]] if self.state in MODE_NAMES else []
        if self.setpoint is not None:
            parts.append(degrees(self.setpoint))
        return ", ".join(parts)


def since_text(now: datetime, since: datetime | None) -> str | None:
    """How long ago the state began, rounded to what can be said without false precision; None when it is not known.
    Fits «уже …» («уже около 20 минут», «уже с утра»)."""
    if since is None:
        return None
    local = since.astimezone(now.tzinfo)
    minutes = int((now - local).total_seconds() // 60)
    if minutes < 0:
        return None
    if minutes < 1:
        return "меньше минуты"
    if minutes < 2:
        return "около минуты"
    if minutes == 2:
        return "пару минут"
    if minutes < 10:
        return f"около {minutes} {plural(minutes, 'минуты', 'минут', 'минут')}"
    if minutes < 55:
        rounded = int(minutes / 5.0 + 0.5) * 5
        if rounded >= 55:
            return "около часа"
        return f"около {rounded} {plural(rounded, 'минуты', 'минут', 'минут')}"
    if minutes < 70:
        return "около часа"
    if minutes < 120:
        return "больше часа"
    days = (now.date() - local.date()).days
    if days == 0:
        if local.hour < MORNING_SWITCHED_BEFORE and now.hour >= MORNING_SPOKEN_AFTER:
            return "с утра"
        hours = int(minutes / 60.0 + 0.5)
        if hours <= 5:
            return f"около {hours} {plural(hours, 'часа', 'часов', 'часов')}"
        return "несколько часов"
    if minutes < 24 * 60:
        return "со вчерашнего вечера" if local.hour >= 17 or local.hour < 5 else "со вчерашнего дня"
    if minutes < 48 * 60:
        return "больше суток"
    return "несколько дней"


def _where(unit: Unit) -> str:
    return f" {unit.where}" if unit.where else ""


def _since_clause(now: datetime, unit: Unit, lead: str = " уже ") -> str:
    spoken = since_text(now, unit.since)
    return f"{lead}{spoken}" if spoken else ""


def _one_on(now: datetime, unit: Unit, pick: Callable[[list[str]], str]) -> str:
    detail = f": {unit.detail}" if unit.detail else ""
    since_c = _since_clause(now, unit, ", уже ")
    since_i = _since_clause(now, unit)
    return pick([
        f"Сейчас включён кондиционер{_where(unit)}{detail}{since_c}.",
        f"Кондиционер{_where(unit)} включён{since_i}{detail}.",
        f"Работает кондиционер{_where(unit)}{detail}{since_c}.",
    ])


def _off_one(now: datetime, unit: Unit, pick: Callable[[list[str]], str]) -> str:
    since_i = _since_clause(now, unit)
    return pick([f"Кондиционер{_where(unit)} выключен{since_i}.", f"Сейчас кондиционер{_where(unit)} выключен{since_i}."])


def _dead_one(unit: Unit) -> str:
    return f"Кондиционер{_where(unit)} сейчас не отвечает."


def _power(now: datetime, units: Sequence[Unit], pick: Callable[[list[str]], str]) -> str:
    on = [unit for unit in units if unit.on]
    off = [unit for unit in units if unit.state == "off"]
    dead = [unit for unit in units if unit.dead]
    sentences: list[str] = []
    if on:
        keys = {(unit.detail, since_text(now, unit.since)) for unit in on}
        if len(on) > 1 and len(keys) == 1 and all(unit.where for unit in on):
            detail, since = next(iter(keys))
            who = "оба кондиционера" if len(on) == 2 else "все кондиционеры"
            tail = ", ".join(part for part in (detail, f"уже {since}" if since else "") if part)
            sentences.append(f"Включены {who}: {and_list([unit.where for unit in on])}" + (f", {tail}." if tail else "."))
        else:
            sentences.extend(_one_on(now, unit, pick) for unit in on)
    if len(off) > 1 and not on and not dead:   # «все выключены» is never said while a unit's state is unknown
        sentences.append(pick(["Сейчас все кондиционеры выключены.", "Все кондиционеры выключены."]))
    elif on:
        sentences.extend(f"Кондиционер{_where(unit)} выключен." for unit in off)
    else:
        sentences.extend(_off_one(now, unit, pick) for unit in off)
    sentences.extend(_dead_one(unit) for unit in dead)
    return " ".join(sentences)


def _temp(now: datetime, units: Sequence[Unit]) -> str:
    sentences = []
    for unit in units:
        if unit.dead:
            sentences.append(_dead_one(unit))
        elif not unit.on:
            current = f", в комнате {degrees(unit.current)}" if unit.current is not None else ""
            sentences.append(f"Кондиционер{_where(unit)} выключен{current}.")
        else:
            parts = []
            if unit.setpoint is not None:
                parts.append(f"выставлен на {degrees(unit.setpoint)}")
            if unit.current is not None:
                parts.append(f"сейчас в комнате {degrees(unit.current)}")
            if not parts:
                parts.append(f"включён, {unit.detail}" if unit.detail else "включён")
            sentences.append(f"Кондиционер{_where(unit)} " + ", ".join(parts) + ".")
    return " ".join(sentences)


def _mode(now: datetime, units: Sequence[Unit]) -> str:
    sentences = []
    for unit in units:
        if unit.dead:
            sentences.append(_dead_one(unit))
        elif not unit.on:
            sentences.append(f"Кондиционер{_where(unit)} сейчас выключен.")
        else:
            name = MODE_NAMES.get(unit.state)   # a state with no name (HA may report more than it documents) is not spoken as the raw token
            sentences.append(f"Кондиционер{_where(unit)} работает в режиме «{name}»." if name else f"Кондиционер{_where(unit)} включён, режим определить не удаётся.")
    return " ".join(sentences)


def _since(now: datetime, units: Sequence[Unit]) -> str:
    sentences = []
    for unit in units:
        if unit.dead:
            sentences.append(_dead_one(unit))
            continue
        word = "включён" if unit.on else "выключен"
        spoken = since_text(now, unit.since)
        sentences.append(f"Кондиционер{_where(unit)} {word}" + (f" {spoken}." if spoken else "."))
    return " ".join(sentences)


def climate_text(units: Sequence[Unit], now: datetime, *, aspect: str | None = None,
                 pick: Callable[[list[str]], str] = random.choice) -> str:
    """One impersonal sentence per unit (or one for several alike). `aspect`: None/`power` (which are on, in what mode, since when),
    `mode`, `temp` (the set-point and the room's temperature) or `since`."""
    if not units:
        return NO_UNITS
    if all(unit.dead for unit in units):
        return STATUS_UNAVAILABLE
    if aspect == "temp":
        return _temp(now, units)
    if aspect == "mode":
        return _mode(now, units)
    if aspect == "since":
        return _since(now, units)
    return _power(now, units, pick)
