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
import time
from pathlib import Path
from typing import Any

import utterance_trace

LOG = logging.getLogger(__name__)

WEATHER_FAILED = "Не получилось узнать погоду, попробуй чуть позже."
MUSIC_FAILED = "Не получилось включить музыку."
MUSIC_UNAVAILABLE = "Музыка сейчас недоступна."
WEATHER_TIMEOUT_S = 8.0
DEFAULT_HACLI_URL = "http://127.0.0.1:8795"

_NON_WORD = re.compile(r"[^a-zа-я0-9\s\-]")
_SPACES = re.compile(r"\s+")


def _tokens(text: str) -> list[str]:
    """Lower-cased words without punctuation, ё folded to е (the same normalisation the quasar parsers use)."""
    return _SPACES.sub(" ", _NON_WORD.sub(" ", text.lower().replace("ё", "е"))).split()


def _has(tokens: list[str], stems: tuple[str, ...] = (), words: frozenset[str] = frozenset()) -> bool:
    """True when a token starts with a stem (inflected weather words) or equals a word (short or ambiguous ones)."""
    return any(token in words or token.startswith(stems) for token in tokens)


# ── weather ──────────────────────────────────────────────────────────────────
# A weather request is made of weather words ONLY: the whole utterance (minus lead-ins) must come from this
# vocabulary, so «как погода влияет на здоровье», «погода на Марсе», «где купить зонт», «как одеться на свадьбу» or
# «прогноз курса доллара» stay with the brain. The quasar layer hands this adapter only the utterances listed in
# overlays/weather-via-cli/diverted_phrases.json; test_weather_skills.py proves every one is answered here.

_WEATHER_LEAD_IN = frozenset(("алиса", "а", "ну", "слушай", "скажи", "расскажи", "подскажи", "пожалуйста", "мне",
                              "нам", "дай", "узнай", "посмотри", "глянь", "можешь", "ка", "покажи", "проверь"))
# The same places the quasar layer reads as «outside» (OUTDOOR_STEMS): a question it hands over must parse here.
_OUTDOOR = ("улиц", "уличн", "снаруж", "балкон")
# «за окном» only: «Что на окне?» is a question about the window sill, not the weather.
_OUTDOOR_WORDS = frozenset(("двор", "дворе", "окном"))
# Exact words, not prefixes: a prefix would also accept «погоди», «градусник», «ветеринар», «завтрак», «Грозный»,
# «Ливны». Prefixes stay only where the stem is unambiguous (дожд, снег, зонт, прогноз, температур, ...).
_WEATHER_NOUN_WORDS = frozenset(("погода", "погоды", "погоду", "погоде", "погодой"))
_TEMPERATURE = ("температур",)
_TEMPERATURE_WORDS = frozenset(("градус", "градуса", "градусов", "градусы", "градусах"))
_WIND_WORDS = frozenset(("ветер", "ветра", "ветру", "ветром", "ветре"))
_FEELING = ("холодн", "жарк", "прохладн")
_FEELING_WORDS = frozenset(("тепло", "тепла", "теплее", "теплей", "теплый", "теплая", "теплое", "теплую", "теплой",
                            "теплые", "теплых", "теплым", "теплом", "теплому"))
_UMBRELLA = ("зонт",)
_DRESS = ("одеть", "одеват", "надеть", "надеват", "одежд")
_PRECIPITATION = ("дожд",)
_PRECIPITATION_WORDS = frozenset(("снег", "снега", "снегу", "снегом", "снеге", "снегопад", "снегопада", "снегопаду",
                                  "снегопадом", "снегопаде", "метель", "метели", "метелью", "гроза", "грозы", "грозу", "грозой", "грозе", "град", "града", "градом", "ливень",
                                  "ливни", "ливня", "ливнем",
                                  "ливневый", "ливневая", "ливневое", "ливневые", "ливневого", "ливневой", "ливневую",
                                  "грозовой", "грозовая", "грозовое", "грозовые", "грозового", "грозовую",
                                  "снежный", "снежная", "снежное", "снежные", "снежного", "снежной", "снежную"))
# «во дворе», «в дождь», «в снегу», «в какой одежде», «в чём одеться»: words after «в»/«во» that are not places. Exact forms only:
# «Снегири» and «Дождевка» merely start like weather stems.
_AFTER_IN_WORDS = _OUTDOOR_WORDS | _PRECIPITATION_WORDS | frozenset(("дождь", "дожде", "дождя", "дождем", "дожди",
                                                                         "какой", "какую", "каком", "чем"))
_PRECIPITATION_VERBS = frozenset(("ли", "будет", "идет", "пойдет", "ожидается", "намечается", "собирается"))
_TOMORROW = ("завтрашн",)
_TOMORROW_WORDS = frozenset(("завтра",))
_WEEKDAYS = ("понедельн", "вторник", "четверг", "пятниц", "суббот", "воскрес")
_WEEK = ("недел", "выходн", "ближайш") + _WEEKDAYS
_WEEK_WORDS = frozenset(("среда", "среду", "среде", "послезавтра", "дни", "дней"))
_QUESTION_LEAD = frozenset(("что", "как", "какая", "какой", "какое", "сколько", "там", "ли", "холодно", "тепло",
                            "жарко"))
# The home place is Belgrade: «погода завтра в Белграде» is the home forecast. Any other city is not vocabulary,
# so «погода в Париже» goes to the brain.
_HOME_CITY_WORDS = frozenset(("белград", "белграде", "белграда", "белграду", "белградом"))
# Demonstratives in any case: «в эти выходные», «в это воскресенье», «на этой неделе». They are only vocabulary;
# the day word next to them decides the mode.
_DEMONSTRATIVES = frozenset(("этой", "эту", "эти", "это", "этот", "этих", "этом", "этим", "следующей", "следующую",
                             "следующие", "следующий", "следующем"))
# weather-cli has no hour-of-day mode and answers one day: such qualifiers are not vocabulary (no confident answer to
# another question), but a weather question with only them in it gets WEATHER_QUALIFIER_HINT, not the brain.
# «и» is vocabulary («погода и температура», «какая погода и сколько градусов»); it is unsupported only when it
# joins two different days («сегодня и завтра»), see _joins_two_days.
_UNSUPPORTED_QUALIFIERS = frozenset(("вечером", "утром", "ночью", "днем"))
# Inflected words (prefix) that may appear next to the weather words; everything else must match exactly.
_WEATHER_STEMS = (_OUTDOOR + _TEMPERATURE + _FEELING + _UMBRELLA + _DRESS + _PRECIPITATION + _TOMORROW + _WEEK
                  + ("прогноз", "одет", "одев", "надет", "надев", "сегодн"))
_WEATHER_WORDS = (frozenset(("какая", "какой", "какое", "какие", "каков", "какова", "каково", "как", "что", "чем", "там", "у", "нас", "на", "в",
                             "во", "по", "с", "собой", "стоит", "лучше", "надо", "нужен", "нужна", "нужно", "брать",
                             "взять", "понадобится", "пригодится", "выходить", "выйти", "идти", "пойти", "мы",
                             "сейчас", "сегодня", "опять", "же", "и", "о", "об", "обо", "воздуха", "цельсия", "текущая", "текущую", "текущей", "текущее", "текущий", "сколько", "за", "день"))
                  | _OUTDOOR_WORDS | _WEATHER_NOUN_WORDS | _TEMPERATURE_WORDS | _WIND_WORDS | _FEELING_WORDS
                  | _PRECIPITATION_WORDS | _PRECIPITATION_VERBS | _TOMORROW_WORDS | _WEEK_WORDS | _HOME_CITY_WORDS
                  | _DEMONSTRATIVES)

WEATHER_MODES = ("now", "tomorrow", "week", "umbrella", "dress")


# «Какая погода? Спасибо.»: the quasar layer judges each sentence alone and hands the weather one over, the adapter then
# gets the whole utterance. Thanks are not vocabulary the question depends on.
_CLOSING_WORDS = frozenset(("спасибо", "благодарю"))


def _weather_phrase_after_in(rest: list[str]) -> bool:
    """«в холодную погоду», «в тёплую дождливую погоду», «в холодный день»: weather adjectives, then a weather noun or «день»."""
    adjectives = _FEELING + ("дождлив", "снежн")
    index = 0
    while index < len(rest) and (rest[index] in _FEELING_WORDS or rest[index].startswith(adjectives)):
        index += 1
    return 0 < index < len(rest) and (rest[index] in _WEATHER_NOUN_WORDS or rest[index] in ("день", "дня"))


def _demonstrative_day(word: str, rest: list[str]) -> bool:
    """«в эти выходные», «в этот день»: a demonstrative counts only when a day word follows, not «в этой Дождевке»."""
    return word in _DEMONSTRATIVES and bool(rest) and (rest[0] in _WEEK_WORDS or rest[0] in ("день", "дня")
                                                       or rest[0].startswith(_WEEK + _TOMORROW))


def _names_another_place(tokens: list[str]) -> bool:
    """«в Снегирях», «во Львове»: a place after «в»/«во» that is not the home city, a day or a demonstrative.

    The whole-utterance vocabulary already refuses most places; this catches the ones that merely start like a
    weather stem (Снегири, Дождевка).
    """
    return any(
        token in ("в", "во") and not (following in _HOME_CITY_WORDS or following in _AFTER_IN_WORDS
                                      or _demonstrative_day(following, tokens[index + 2:]) or following in _WEEK_WORDS
                                      or following.startswith(_WEEK + _TOMORROW)
                                      or _weather_phrase_after_in(tokens[index + 1:]))
        for index, (token, following) in enumerate(zip(tokens, tokens[1:])))


def _in_vocabulary(tokens: list[str], extra: frozenset[str] = frozenset()) -> bool:
    words = [token for token in tokens if token not in _WEATHER_LEAD_IN and token not in _CLOSING_WORDS]
    return bool(words) and not _names_another_place(words) and all(token in _WEATHER_WORDS or token in extra or token.startswith(_WEATHER_STEMS)
                               for token in words)


def _is_next_day(tokens: list[str]) -> bool:
    """«на следующий день»: tomorrow, said another way."""
    return any(token.startswith("следующ") and following in ("день", "дня")
               for token, following in zip(tokens, tokens[1:]))


def _joins_two_days(tokens: list[str]) -> bool:
    """«сегодня и завтра», «завтра и на выходные»: one answer cannot cover both."""
    days = (_has(tokens, ("сегодн",), frozenset(("сейчас",))),
            _is_next_day(tokens) or _has(tokens, _TOMORROW, _TOMORROW_WORDS), _has(tokens, _WEEK, _WEEK_WORDS))
    return "и" in tokens and sum(days) > 1


def parse_weather_ask(text: str) -> str | None:
    """Utterance -> weather-cli mode (now|tomorrow|week|umbrella|dress), or None when it is not a weather question."""
    tokens = _tokens(text)
    if not tokens or len(tokens) > 10:
        return None
    if "за" in tokens and "окном" not in tokens:  # «что за зонт», «что за одежда»
        return None
    if not _in_vocabulary(tokens) or _joins_two_days(tokens):
        return None
    time = ("week" if _has(tokens, _WEEK, _WEEK_WORDS) else "tomorrow" if _is_next_day(tokens) or _has(tokens, _TOMORROW, _TOMORROW_WORDS)
            else "now")
    outdoor = _has(tokens, _OUTDOOR, _OUTDOOR_WORDS)
    if _has(tokens, _DRESS):
        return "dress"
    if _has(tokens, _UMBRELLA):
        return "umbrella" if time == "now" else time
    if _has(tokens, ("прогноз",), _WEATHER_NOUN_WORDS):
        return time
    # «будет ли дождь», «дождь идёт?», «на улице дождь», «там дождь?»
    if (_has(tokens, _PRECIPITATION, _PRECIPITATION_WORDS)
            and (_has(tokens, words=_PRECIPITATION_VERBS) or outdoor or "там" in tokens)):
        return "umbrella" if time == "now" else time
    # «сколько градусов», «какая температура завтра»: a temperature with no room or device word in the utterance
    # (the whole-utterance vocabulary above keeps «температура в комнате» out) is the temperature outside.
    if _has(tokens, _TEMPERATURE, _TEMPERATURE_WORDS):
        return time
    # «какой ветер на улице»
    if _has(tokens, words=_WIND_WORDS) and outdoor:
        return time
    # «что на улице», «как там на улице», «холодно ли на улице»
    if outdoor and _has(tokens, words=_QUESTION_LEAD):
        return time
    if _has(tokens, _FEELING, _FEELING_WORDS) and outdoor:
        return time
    return None


WEATHER_QUALIFIER_HINT = "Так не умею. Могу сказать погоду сейчас, на завтра или на неделю."


def is_unsupported_weather_qualifier(text: str) -> bool:
    """A weather question that is weather vocabulary plus only a qualifier weather-cli cannot answer («вечером», «и»).

    Needs an explicit weather noun, rain, umbrella or outdoor word, so bare «какая температура утром» is not caught
    (the quasar indoor handler may own it). Such a turn must not reach the brain: it would answer «переспроси».
    """
    tokens = _tokens(text)
    if not tokens or len(tokens) > 10:
        return False
    if not _joins_two_days(tokens) and not any(token in _UNSUPPORTED_QUALIFIERS for token in tokens):
        return False
    if not _in_vocabulary(tokens, _UNSUPPORTED_QUALIFIERS):
        return False
    return _has(tokens, ("прогноз", "дожд", "зонт") + _OUTDOOR, _WEATHER_NOUN_WORDS | _OUTDOOR_WORDS | _PRECIPITATION_WORDS)


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

def configured_weather_command(env: dict[str, str]) -> list[str] | None:
    """AISIS_WEATHER_COMMAND as a JSON argv array; None when unset. An argument with a NUL cannot be passed to a
    process, so it is refused here, not at the first question."""
    configured = env.get("AISIS_WEATHER_COMMAND", "").strip()
    if not configured:
        return None
    argv = json.loads(configured)
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise ValueError("AISIS_WEATHER_COMMAND must be a JSON argv array")
    if any("\0" in item for item in argv):
        raise ValueError("AISIS_WEATHER_COMMAND arguments must not contain NUL")
    return argv


# What the weather-cli child may inherit. The adapter's own environment carries API keys and tokens that a
# forecast lookup must never see, so the child gets an allowlist: process basics, XDG dirs, and WEATHER_* settings.
_WEATHER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
_WEATHER_ENV_PREFIX = "WEATHER_"


def weather_child_env(env: dict[str, str], ha_url: str) -> dict[str, str]:
    """The environment weather-cli runs in: the allowlist above, PATH extended with ~/.local/bin, and the ha-cli
    proxy as its Home Assistant (the real token stays in ha-cli). `scripts/install-weather-cli.sh --check` runs the
    command under this exact environment."""
    child = {key: value for key, value in env.items() if key in _WEATHER_ENV_KEYS or key.startswith(_WEATHER_ENV_PREFIX)}
    child.setdefault("HOME", str(Path.home()))
    child["PATH"] = os.pathsep.join((child.get("PATH", os.defpath), str(Path.home() / ".local" / "bin")))
    child.setdefault("WEATHER_HA_URL", ha_url)
    child.setdefault("WEATHER_HA_TOKEN", "via-ha-cli")
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
        self.configured_weather_command = configured_weather_command(env)
        # weather-cli reads Home Assistant through the ha-cli proxy (state cache, the real token stays there).
        self.ha_url = env.get("AISIS_HACLI_URL", DEFAULT_HACLI_URL)

    async def answer(self, text: str) -> str | None:
        mode = parse_weather_ask(text)
        if mode is not None:
            return await self.weather(mode)
        if is_unsupported_weather_qualifier(text):
            return WEATHER_QUALIFIER_HINT
        if is_generic_music_request(text):
            return await self.music()
        return None

    def weather_command(self, path: str) -> list[str] | None:
        """The configured argv, else `weather` found on `path` at call time (installing it needs no adapter restart)."""
        if self.configured_weather_command is not None:
            return self.configured_weather_command
        found = shutil.which("weather", path=path)
        return [found] if found else None

    def weather_available(self) -> bool:
        """weather-cli is configured or found where a weather question would look for it (nothing is run)."""
        return self.weather_command(weather_child_env(self.env, self.ha_url)["PATH"]) is not None

    async def weather(self, mode: str) -> str:
        started = time.monotonic()
        reply = await self._weather(mode)
        utterance_trace.call("weather-cli", argv=f"weather say --{mode}", ok=reply != WEATHER_FAILED, ms=int((time.monotonic() - started) * 1000))
        return reply

    async def _weather(self, mode: str) -> str:
        env = weather_child_env(self.env, self.ha_url)
        command = self.weather_command(env["PATH"])
        if command is None:
            LOG.warning("weather_failed reason=weather_cli_not_found hint=run scripts/install-weather-cli.sh")
            utterance_trace.error("weather-cli: not installed")
            return WEATHER_FAILED
        try:
            proc = await asyncio.create_subprocess_exec(*command, "say", f"--{mode}", env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as err:  # missing or non-executable configured command
            LOG.warning("weather_failed reason=launch err=%s", err)
            utterance_trace.error("weather-cli: launch failed")
            return WEATHER_FAILED
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), WEATHER_TIMEOUT_S)
        except asyncio.TimeoutError:
            _kill_group(proc.pid)
            await proc.wait()
            LOG.warning("weather_failed reason=timeout mode=%s", mode)
            utterance_trace.error(f"weather-cli: timeout {WEATHER_TIMEOUT_S:g} s")
            return WEATHER_FAILED
        except asyncio.CancelledError:
            _kill_group(proc.pid)
            await proc.wait()
            raise
        reply = stdout.decode("utf-8", "replace").strip()
        if proc.returncode or not reply:
            err = stderr.decode("utf-8", "replace").strip()
            LOG.warning("weather_failed reason=exit_%s mode=%s err=%s", proc.returncode, mode, err[:200])
            utterance_trace.error(f"weather-cli: exit {proc.returncode}" + ("" if reply else ", empty answer"))
            return WEATHER_FAILED
        LOG.info("weather_answered mode=%s", mode)
        return reply

    async def music(self) -> str:
        import music_airplay
        started = time.monotonic()
        result: dict[str, Any] = await asyncio.get_running_loop().run_in_executor(None, music_airplay.music_any)
        matched = result.get("matched") or {}
        utterance_trace.call("music", ok=bool(result.get("ok")), action=result.get("action"), name=matched.get("name"),
                             error=None if result.get("ok") else str(result.get("err"))[:80], ms=int((time.monotonic() - started) * 1000))
        if not result.get("ok"):
            LOG.warning("music_any_failed err=%s", str(result.get("err"))[:200])
            return MUSIC_FAILED if result.get("err") == "empty_library" else MUSIC_UNAVAILABLE
        action = result.get("action")
        if action == "already":
            return "Музыка уже играет."
        LOG.info("music_any_started name=%s", matched.get("name"))
        if not matched.get("name"):
            return "Включаю."
        return f"Включаю {matched['name']}{', ' + matched['artist'] if matched.get('artist') else ''}."
