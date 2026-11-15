"""Spoken requests the adapter answers itself, before the chat brain.

The brain (ChatGPT through pm) has no tools and no view of this house. Left to guess it refuses
(«не могу проверить погоду», «музыку включить не могу, скажи штатной командой колонки»); a rooted
station has no stock Alice to refer to. Two requests the quasar fast-intent layer deliberately hands
over reach the adapter in text form, so they are executed here with the real services:

- weather questions («погода завтра», «нужен ли зонт», «как одеться») -> `weather say` (weather-cli,
  Home Assistant + Open-Meteo), spoken exactly as the CLI words it;
- «включи музыку» with no artist -> Music Assistant on the speaker (music_airplay.music_any: leave what plays
  alone, else random library tracks).

Parsing is pure and local (no I/O); the runners return the sentence to speak. A failing service gets an
honest short sentence, never an invented answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

WEATHER_FAILED = "Не получилось узнать погоду, попробуй чуть позже."
MUSIC_FAILED = "Не получилось включить музыку."
MUSIC_UNAVAILABLE = "Музыка сейчас недоступна."
WEATHER_TIMEOUT_S = 8.0

_NON_WORD = re.compile(r"[^a-zа-я0-9\s\-]")
_SPACES = re.compile(r"\s+")


def _tokens(text: str) -> list[str]:
    """Lower-cased words without punctuation, ё folded to е (the same normalisation the quasar parsers use)."""
    return _SPACES.sub(" ", _NON_WORD.sub(" ", text.lower().replace("ё", "е"))).split()


def _has(tokens: list[str], stems: tuple[str, ...]) -> bool:
    return any(token.startswith(stem) for token in tokens for stem in stems)


# ── weather ──────────────────────────────────────────────────────────────────
# A weather request is made of weather words ONLY: the whole utterance (minus lead-ins) must come from this
# vocabulary, so «как погода влияет на здоровье», «погода на Марсе», «где купить зонт», «как одеться на свадьбу» or
# «прогноз курса доллара» stay with the brain. Same intent grammar as the quasar overlay's weather-parse.ts.

_WEATHER_LEAD_IN = frozenset(("алиса", "а", "ну", "слушай", "скажи", "расскажи", "подскажи", "пожалуйста", "мне",
                              "нам", "дай", "узнай", "посмотри", "глянь", "можешь", "ка"))
_OUTDOOR = ("улиц", "снаруж", "двор", "окном")
_TEMPERATURE = ("градус", "температур")
_FEELING = ("холодн", "тепл", "жарк", "прохладн")
_UMBRELLA = ("зонт",)
_DRESS = ("одеть", "одеват", "надеть", "надеват", "одежд")
_PRECIPITATION = ("дожд", "снег", "гроз", "ливен", "ливн", "град", "метел")
_PRECIPITATION_VERBS = ("ли", "будет", "идет", "пойдет", "ожидается", "намечается", "собирается")
_TOMORROW = ("завтра", "завтрашн")
_WEEKDAYS = ("понедельн", "вторник", "среда", "среду", "среде", "четверг", "пятниц", "суббот", "воскрес")
_WEEK = ("недел", "выходн", "ближайш", "послезавтра", "дни", "дней") + _WEEKDAYS
_QUESTION_LEAD = ("что", "как", "какая", "какой", "сколько", "там", "ли", "холодно", "тепло", "жарко")
# Inflected words (prefix) and small function words (exact) that may appear next to the weather words.
_WEATHER_STEMS = (_OUTDOOR + _TEMPERATURE + _FEELING + _UMBRELLA + _DRESS + _PRECIPITATION + _PRECIPITATION_VERBS
                  + _TOMORROW + _WEEK + ("погод", "прогноз", "одет", "одев", "надет", "надев", "белград", "сегодн",
                                         "дн", "день"))
_WEATHER_WORDS = frozenset(("какая", "какой", "какое", "каков", "как", "что", "чем", "там", "у", "нас", "на", "в", "во",
                            "по", "с", "собой", "ли", "будет", "стоит", "лучше", "надо", "нужен", "нужна", "нужно",
                            "брать", "взять", "понадобится", "пригодится", "выходить", "выйти", "идти", "пойти", "мы",
                            "сейчас", "сегодня", "опять", "сколько"))

WEATHER_MODES = ("now", "tomorrow", "week", "umbrella", "dress")


def parse_weather_ask(text: str) -> str | None:
    """Utterance -> weather-cli mode (now|tomorrow|week|umbrella|dress), or None when it is not a weather question."""
    tokens = _tokens(text)
    if not tokens or len(tokens) > 8:
        return None
    words = [token for token in tokens if token not in _WEATHER_LEAD_IN]
    if not words or not all(token in _WEATHER_WORDS or _has([token], _WEATHER_STEMS) for token in words):
        return None
    time = "week" if _has(tokens, _WEEK) else "tomorrow" if _has(tokens, _TOMORROW) else "now"
    outdoor = _has(tokens, _OUTDOOR)
    if _has(tokens, _DRESS):
        return "dress"
    if _has(tokens, _UMBRELLA):
        return "umbrella" if time == "now" else time
    if _has(tokens, ("погод", "прогноз")):
        return time
    if _has(tokens, _PRECIPITATION) and _has(tokens, _PRECIPITATION_VERBS):
        return "umbrella" if time == "now" else time
    # «какая температура завтра», «сколько градусов на улице»: a temperature asked about outdoors or a later day.
    if _has(tokens, _TEMPERATURE) and (outdoor or time != "now"):
        return time
    # «что на улице», «как там на улице», «холодно ли на улице»
    if outdoor and _has(tokens, _QUESTION_LEAD):
        return time
    if _has(tokens, _FEELING) and outdoor:
        return time
    return None


# ── music without a name ─────────────────────────────────────────────────────
# «включи музыку», «поставь что-нибудь», «ну давай музыку какую-нибудь». Anything with a name, a device or a
# negation in it is not this request (the quasar layer sends named requests to Music Assistant itself).

_MUSIC_VERBS = frozenset(("включи", "включите", "включить", "включай", "вруби", "врубите", "врубай", "врубить",
                          "поставь", "поставьте", "поставить", "запусти", "запустите", "запустить", "сыграй",
                          "сыграйте", "играй", "давай", "послушаем", "послушать"))
_MUSIC_FILLER = frozenset(("алиса", "ка", "ну", "а", "мне", "нам", "пожалуйста", "хочу", "давай", "давайте", "какую",
                           "нибудь", "какую-нибудь", "любую", "любимую", "что", "что-нибудь", "чтонибудь",
                           "какой-нибудь", "что-то", "чего-нибудь", "фоном", "фоновую", "на", "колонке", "здесь",
                           "тут", "сейчас", "в", "гостиной", "быстрее", "уже"))
_MUSIC_NOUNS = frozenset(("музыку", "музыка", "музон", "музончик", "песню", "песенку", "песни", "треки",
                          "что-нибудь", "чтонибудь", "что-то", "чего-нибудь"))


def is_generic_music_request(text: str) -> bool:
    """True for a play request that names nothing: a play verb, filler words, and «музыку»/«что-нибудь» only."""
    tokens = _tokens(text)
    if not tokens or len(tokens) > 8:
        return False
    if not any(token in _MUSIC_VERBS for token in tokens):
        return False
    if not all(token in _MUSIC_VERBS or token in _MUSIC_FILLER or token in _MUSIC_NOUNS for token in tokens):
        return False
    return any(token in _MUSIC_NOUNS for token in tokens)


# ── runners ──────────────────────────────────────────────────────────────────

def _configured_weather_command(env: dict[str, str]) -> list[str] | None:
    """AISIS_WEATHER_COMMAND as a JSON argv array; None when unset."""
    configured = env.get("AISIS_WEATHER_COMMAND", "").strip()
    if not configured:
        return None
    argv = json.loads(configured)
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise ValueError("AISIS_WEATHER_COMMAND must be a JSON argv array")
    return argv


# What the weather-cli child may inherit. The adapter's own environment carries API keys and tokens that a
# forecast lookup must never see, so the child gets an allowlist: process basics, XDG dirs, and WEATHER_* settings.
_WEATHER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
_WEATHER_ENV_PREFIX = "WEATHER_"


def _weather_child_env(env: dict[str, str]) -> dict[str, str]:
    child = {key: value for key, value in env.items() if key in _WEATHER_ENV_KEYS or key.startswith(_WEATHER_ENV_PREFIX)}
    child.setdefault("HOME", str(Path.home()))
    child["PATH"] = os.pathsep.join((child.get("PATH", os.defpath), str(Path.home() / ".local" / "bin")))
    return child


def _kill_group(pid: int) -> None:
    """SIGKILL the child's process group; a group that already exited is not an error."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class Skills:
    """Weather and nameless-music answers; `answer(text)` returns the sentence to speak or None to leave it to the brain."""

    def __init__(self, env: dict[str, str]):
        self.env = env
        self.configured_weather_command = _configured_weather_command(env)
        # weather-cli reads Home Assistant through the ha-cli proxy (state cache, the real token stays there).
        self.ha_url = env.get("AISIS_HACLI_URL", "http://127.0.0.1:8795")

    async def answer(self, text: str) -> str | None:
        mode = parse_weather_ask(text)
        if mode is not None:
            return await self.weather(mode)
        if is_generic_music_request(text):
            return await self.music()
        return None

    def weather_command(self, path: str) -> list[str] | None:
        """The configured argv, else `weather` found on `path` at call time (installing it needs no adapter restart)."""
        if self.configured_weather_command is not None:
            return self.configured_weather_command
        found = shutil.which("weather", path=path)
        return [found] if found else None

    async def weather(self, mode: str) -> str:
        env = _weather_child_env(self.env)
        command = self.weather_command(env["PATH"])
        if command is None:
            LOG.warning("weather_failed reason=weather_cli_not_found")
            return WEATHER_FAILED
        env.setdefault("WEATHER_HA_URL", self.ha_url)
        env.setdefault("WEATHER_HA_TOKEN", "via-ha-cli")
        try:
            proc = await asyncio.create_subprocess_exec(*command, "say", f"--{mode}", env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as err:  # missing or non-executable configured command
            LOG.warning("weather_failed reason=launch err=%s", err)
            return WEATHER_FAILED
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), WEATHER_TIMEOUT_S)
        except asyncio.TimeoutError:
            _kill_group(proc.pid)
            await proc.wait()
            LOG.warning("weather_failed reason=timeout mode=%s", mode)
            return WEATHER_FAILED
        except asyncio.CancelledError:
            _kill_group(proc.pid)
            await proc.wait()
            raise
        reply = stdout.decode("utf-8", "replace").strip()
        if proc.returncode or not reply:
            LOG.warning("weather_failed reason=exit_%s mode=%s err=%s", proc.returncode, mode,
                        stderr.decode("utf-8", "replace").strip()[:200])
            return WEATHER_FAILED
        LOG.info("weather_answered mode=%s", mode)
        return reply

    async def music(self) -> str:
        import music_airplay
        result: dict[str, Any] = await asyncio.get_running_loop().run_in_executor(None, music_airplay.music_any)
        if not result.get("ok"):
            LOG.warning("music_any_failed err=%s", str(result.get("err"))[:200])
            return MUSIC_FAILED if result.get("err") == "empty_library" else MUSIC_UNAVAILABLE
        action = result.get("action")
        if action == "already":
            return "Музыка уже играет."
        matched = result.get("matched") or {}
        LOG.info("music_any_started name=%s", matched.get("name"))
        if not matched.get("name"):
            return "Включаю."
        return f"Включаю {matched['name']}{', ' + matched['artist'] if matched.get('artist') else ''}."
