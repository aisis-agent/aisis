"""«Что ты умеешь?»: the answer is built from what is configured and reachable right now, never from a fixed list.

Facts: which abilities are bound in this adapter (timers, weather-cli, music, Home Assistant actions), the rooms that really have
lights and air conditioners (`HaActions.rooms_with`), the nearest running timer, the hour. The text is impersonal, a list of what is
available, and closes with one example phrase built from a real room; `test_capabilities` reads every example with the grammar, so an
answer never offers a phrase the stack does not understand. Nothing here changes any state.

One station's follow-up («а ещё?», or the same question again) gets the next group of abilities; the cursor lasts two minutes and is
dropped when the router notes another reply to that station (`forget`). A line the quasar layer answers never reaches the router, so
the two-minute window is what bounds the cursor in that case.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import home_status
from timers import left_text

PAGE = 3                    # groups of abilities in one answer
CURSOR_WINDOW_S = 120.0     # how long «а ещё?» continues an answer
HOME_TIMEOUT_S = 1.5        # Home Assistant must answer within this, else the home abilities are left out
MAX_ROOMS = 4

HOME_DOWN = "Умный дом сейчас не отвечает, поэтому свет и климат недоступны."
WRAP = "Это все основные команды. Остальное идёт в обычный разговор."
INTROS = ("Доступно: {items}.", "Сейчас доступны: {items}.", "Из команд сейчас работают: {items}.")
MORE_INTROS = ("Ещё доступно: {items}.", "Ещё: {items}.")
EXAMPLE = " Например: «{example}»."


@dataclass(frozen=True)
class Facts:
    home_bound: bool                       # Home Assistant actions are configured in this adapter
    home_up: bool                          # ... and it answered just now
    light_areas: tuple[str, ...] = ()
    climate_areas: tuple[str, ...] = ()
    timers: bool = False
    timer_left: str | None = None          # «осталось 4 минуты» of the nearest running timer
    weather: bool = False
    music: bool = False


def rooms(areas: tuple[str, ...]) -> str:
    """«в гостиной, на кухне и в спальне»; more than MAX_ROOMS ends with «и в других комнатах»."""
    spoken = [home_status.locative(area) for area in areas[:MAX_ROOMS]]
    if len(areas) > MAX_ROOMS:
        spoken.append("в других комнатах")
    return home_status.and_list(spoken)


def _known_where(areas: tuple[str, ...]) -> str:
    """The first area the grammar's rooms can name, as it is said in a command («на втором этаже»); empty when there is none."""
    for area in areas:
        if area in home_status.LOCATIVE:
            return " " + home_status.LOCATIVE[area]
    return ""


def _with(text: str, spoken_rooms: str) -> str:
    return text.replace(" {rooms}", f" {spoken_rooms}" if spoken_rooms else "")


@dataclass(frozen=True)
class Ability:
    key: str
    available: Callable[[Facts], bool]
    clauses: tuple[str, ...]               # nominal phrases for the list; ` {rooms}` is the rooms, dropped when there are none
    example: Callable[[Facts], str]
    where: Callable[[Facts], tuple[str, ...]] = lambda facts: ()


ABILITIES = (
    Ability("light", lambda f: f.home_up and bool(f.light_areas),
            ("управление светом {rooms}", "свет {rooms}: включение, цвет и яркость"),
            lambda f: "включи свет" + _known_where(f.light_areas), lambda f: f.light_areas),
    Ability("climate", lambda f: f.home_up and bool(f.climate_areas),
            ("кондиционеры {rooms}: режим и температура", "управление климатом {rooms}"),
            lambda f: "включи кондиционер" + _known_where(f.climate_areas) + " на обогрев", lambda f: f.climate_areas),
    Ability("timers", lambda f: f.timers, ("таймеры, будильники и напоминания",), lambda f: "поставь таймер на десять минут"),
    Ability("weather", lambda f: f.weather, ("погода на сегодня, завтра и неделю",), lambda f: "какая погода завтра"),
    Ability("music", lambda f: f.music, ("музыка: любая или по имени исполнителя",), lambda f: "включи музыку"),
    Ability("clock", lambda f: True, ("время, дата и счёт",), lambda f: "который час"),
    Ability("status", lambda f: f.home_up and bool(f.climate_areas),
            ("справка о том, что включено и в каком режиме кондиционер",), lambda f: "какой кондиционер включён"),
)
BY_KEY = {ability.key: ability for ability in ABILITIES}

MORNING = ("weather", "timers", "light", "climate", "music", "clock", "status")
EVENING = ("light", "climate", "music", "timers", "weather", "clock", "status")
DAY = tuple(ability.key for ability in ABILITIES)


def order(hour: int) -> tuple[str, ...]:
    """Morning asks for the forecast and the timer first, evening for light and climate."""
    if 5 <= hour < 11:
        return MORNING
    if hour >= 17 or hour < 5:
        return EVENING
    return DAY


def _clause(ability: Ability, facts: Facts, pick: Callable[[list[str]], str]) -> str:
    text = _with(pick(list(ability.clauses)), rooms(ability.where(facts)))
    if ability.key == "timers" and facts.timer_left:
        text += f" (сейчас идёт таймер: {facts.timer_left})"
    return text


def render_page(facts: Facts, now: datetime, offset: int, pick: Callable[[list[str]], str]) -> tuple[str, int]:
    """The answer for the groups from `offset` on, and the offset after them; an offset past the last group is the closing line."""
    keys = [key for key in order(now.hour) if BY_KEY[key].available(facts)]
    if offset >= len(keys):
        return WRAP, len(keys)
    page = [BY_KEY[key] for key in keys[offset:offset + PAGE]]
    items = "; ".join(_clause(ability, facts, pick) for ability in page)
    text = pick(list(MORE_INTROS if offset else INTROS)).format(items=items)
    if offset == 0 and facts.home_bound and not facts.home_up:
        text = f"{HOME_DOWN} {text}"
    return text + EXAMPLE.format(example=page[0].example(facts)), offset + len(page)


TOPIC_TEXT = {
    "light": ("Со светом доступно: включить и выключить {rooms}, поменять цвет и яркость.",
              "Свет {rooms}: включение и выключение, цвет, яркость."),
    "climate": ("С кондиционерами {rooms} доступно: включить, выбрать режим и температуру, узнать, какой включён.",
                "Кондиционеры {rooms}: включение, режим, температура и справка, какой включён."),
    "timers": ("Можно поставить таймер на любое время, будильник на нужный час и напоминание через столько-то минут.",),
    "weather": ("Погода: сейчас, на завтра, на неделю, нужен ли зонт и как одеться.",),
    "music": ("Музыка: «включи музыку» или «включи» и имя исполнителя, а также пауза и «дальше».",),
    "volume": ("Громкость: громче, тише, уровень от нуля до десяти или в процентах.",),
    "tv": ("Телевизор и проектор: включить и выключить.",),
}
TOPIC_EXAMPLE = {"volume": "громкость на пять", "tv": "включи телевизор"}
TOPIC_ABILITY = {"light": "light", "climate": "climate", "timers": "timers", "weather": "weather", "music": "music"}
TOPIC_MISSING = {
    "light": "Управление светом сейчас недоступно.", "climate": "Кондиционеры сейчас недоступны.",
    "timers": "Таймеры и будильники сейчас недоступны.", "weather": "Погода сейчас недоступна.",
    "music": "Музыка сейчас недоступна.", "tv": "Телевизор сейчас недоступен.", "home": "Управление умным домом сейчас недоступно.",
}
NO_LIGHTS = "Света в умном доме не нашлось."
NO_CLIMATE = "Кондиционеров в умном доме не нашлось."


def topic_text(topic: str, facts: Facts, pick: Callable[[list[str]], str]) -> str | None:
    """The answer to «что ты умеешь со светом»: what is available on the topic, with a real example; what is not available, said so."""
    if topic == "home":
        keys = [key for key in ("light", "climate", "status") if BY_KEY[key].available(facts)]
        if not keys:
            return HOME_DOWN if facts.home_bound and not facts.home_up else TOPIC_MISSING["home"]
        items = "; ".join(_clause(BY_KEY[key], facts, pick) for key in keys)
        return f"Умный дом: {items}." + EXAMPLE.format(example=BY_KEY[keys[0]].example(facts))
    if topic in ("light", "climate", "tv") and facts.home_bound and not facts.home_up:
        return HOME_DOWN
    if topic == "light" and facts.home_up and not facts.light_areas:
        return NO_LIGHTS
    if topic == "climate" and facts.home_up and not facts.climate_areas:
        return NO_CLIMATE
    if topic in ("volume", "tv"):
        return pick(list(TOPIC_TEXT[topic])) + EXAMPLE.format(example=TOPIC_EXAMPLE[topic])
    ability = BY_KEY.get(TOPIC_ABILITY.get(topic, ""))
    if ability is None or not ability.available(facts):
        return TOPIC_MISSING.get(topic)
    text = _with(pick(list(TOPIC_TEXT[topic])), rooms(ability.where(facts)))
    return text + EXAMPLE.format(example=ability.example(facts))


class Capabilities:
    """Gathers the facts and keeps each station's cursor. `ha` is `HaActions` (or None), `timers` the TimerBook (or None), `skills` the
    voice skills (or None)."""

    def __init__(self, *, ha: Any | None, timers: Any | None, skills: Any | None,
                 now: Callable[[], datetime], pick: Callable[[list[str]], str] = random.choice,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._ha, self._timers, self._skills = ha, timers, skills
        self._now, self._pick, self._monotonic = now, pick, monotonic
        self._cursor: dict[str, tuple[int, float, str]] = {}    # station -> (next offset, when, the answer it followed)

    def forget(self, station: str, said: str) -> None:
        """The station heard `said`: a cursor that followed another line is over."""
        cursor = self._cursor.get(station)
        if cursor is not None and cursor[2] != said:
            del self._cursor[station]

    def _fresh(self, station: str) -> tuple[int, float, str] | None:
        cursor = self._cursor.get(station)
        if cursor is not None and self._monotonic() - cursor[1] > CURSOR_WINDOW_S:
            del self._cursor[station]
            return None
        return cursor

    async def facts(self) -> Facts:
        home_bound = self._ha is not None
        light: tuple[str, ...] = ()
        climate: tuple[str, ...] = ()
        home_up = False
        if home_bound:
            try:
                light, climate = await asyncio.wait_for(self._rooms(), HOME_TIMEOUT_S)
                home_up = True
            except Exception:   # Home Assistant or the registry did not answer in time: the home abilities are left out
                pass
        left = None
        if self._timers is not None:
            running = self._timers.remaining()
            left = left_text(running[0]["left"]) if running else None
        weather = self._skills is not None and self._skills.weather_available()
        return Facts(home_bound, home_up, light, climate, self._timers is not None, left, weather, self._skills is not None)

    async def _rooms(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return await self._ha.rooms_with("light"), await self._ha.rooms_with("climate")

    async def answer(self, station: str, *, topic: str | None = None, more: bool = False) -> str | None:
        """The text to speak. None: nothing to say (a follow-up with nothing to follow), the turn takes its old path."""
        cursor = self._fresh(station)
        if more and cursor is None and topic is None:
            return None
        facts = await self.facts()
        if topic is not None:
            return topic_text(topic, facts, self._pick)
        text, offset = render_page(facts, self._now(), cursor[0] if cursor else 0, self._pick)
        if text == WRAP:
            self._cursor.pop(station, None)
        else:
            self._cursor[station] = (offset, self._monotonic(), text)
        return text
