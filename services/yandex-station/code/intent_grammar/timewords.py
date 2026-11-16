"""Spoken durations and clock times for timers and alarms.

The shape follows HyperCalendarBot's `date-time-words.ts` (durations as a sum of unit terms, clock times with a day
part), re-written for spoken Russian: numerals are words in any case, the clock has spoken idioms («половина седьмого»,
«без пятнадцати восемь», «десять минут седьмого», «четверть шестого») and nothing here knows about time zones: the
executor decides which instant «семь утра» is.

`parse_duration` and `parse_clock` return every reading at `tokens[start]`, longest first, as `(end, value)`;
the matcher backtracks over them like over numbers.
"""
from __future__ import annotations

from .numerals import Number, parse_number, parse_ordinal

_UNIT_WORDS = {
    **dict.fromkeys(("секунда", "секунду", "секунды", "секунд", "сек", "секундочку"), 1),
    **dict.fromkeys(("минута", "минуту", "минуты", "минут", "мин", "минутку", "минуточку"), 60),
    **dict.fromkeys(("час", "часа", "часов", "часик"), 3600),
    **dict.fromkeys(("сутки", "суток", "день", "дня", "дней", "дни"), 86400),
}
_PARTS = {"утра": "morning", "утро": "morning", "дня": "day", "вечера": "evening", "ночи": "night"}
_HOUR_WORDS = frozenset(("час", "часа", "часов", "часу"))
_QUARTER = frozenset(("четверть", "четверти"))


def unit_seconds(token: str) -> int | None:
    """Seconds in the unit a token names: секунду/минуты/часов/сутки -> 1/60/3600/86400; None for any other word."""
    return _UNIT_WORDS.get(token)


def _terms(tokens: list[str], start: int) -> list[tuple[int, Number, int]]:
    """One duration term at `start`: (end, seconds, unit seconds). «пять минут», «минуту», «полчаса», «полторы минуты»."""
    token = tokens[start]
    terms: list[tuple[int, Number, int]] = []
    if token.startswith("пол") and token[3:] and unit_seconds(token[3:]) is not None:  # полчаса, полминуты
        unit = unit_seconds(token[3:])
        terms.append((start + 1, unit / 2, unit))
    elif token == "пол" and start + 1 < len(tokens) and unit_seconds(tokens[start + 1]) is not None:
        unit = unit_seconds(tokens[start + 1])
        terms.append((start + 2, unit / 2, unit))
    unit = unit_seconds(token)
    if unit is not None and token not in ("дня", "дней", "дни", "суток", "секунд", "минут", "часов", "мин", "сек"):
        # a bare «минуту» is one minute; a bare plural («минут», «дней») is not a quantity
        terms.append((start + 1, unit, unit))
    for end, value in parse_number(tokens, start):
        if end < len(tokens):
            unit = unit_seconds(tokens[end])
            if unit is not None:
                terms.append((end + 1, value * unit, unit))
    return terms


def parse_duration(tokens: list[str], start: int = 0) -> list[tuple[int, int]]:
    """Duration readings in whole seconds: «час тридцать минут», «две минуты», «полчаса», «1.5 часа»."""
    results: dict[int, int] = {}

    def extend(pos: int, total: Number, last_unit: int) -> None:
        results[pos] = max(results.get(pos, 0), int(total))
        nxt = pos + 1 if pos < len(tokens) and tokens[pos] == "и" else pos
        if nxt >= len(tokens):
            return
        for end, seconds, unit in _terms(tokens, nxt):
            if unit < last_unit:
                extend(end, total + seconds, unit)

    if start >= len(tokens):
        return []
    for end, seconds, unit in _terms(tokens, start):
        extend(end, seconds, unit)
    return sorted(((end, secs) for end, secs in results.items() if secs > 0), key=lambda r: -r[0])


def _hour_before(tokens: list[str], pos: int) -> list[tuple[int, int]]:
    """Genitive ordinal hour of an idiom: «седьмого» -> 7 (the clock then shows the hour before)."""
    return [(end, value) for end, value in parse_ordinal(tokens, pos) if 1 <= value <= 12]


# The hours a day part may qualify when the time is written on the 24-hour clock («23:30 ночи», «пятнадцать ноль ноль
# дня»): anything else contradicts itself and is rejected, never shifted by twelve hours.
_PART_HOURS_24 = {"morning": (0,), "day": range(13, 18), "evening": range(16, 24), "night": (0, 21, 22, 23)}


def _apply_part(hour: int, part: str | None) -> int | None:
    """The 24-hour value of `hour` said with a day part; None when the two contradict each other."""
    if part is None:
        return hour
    if hour == 0 or hour > 12:
        return hour if hour in _PART_HOURS_24[part] else None
    if part in ("day", "evening") and hour < 12:
        return hour + 12
    if part == "night":
        return 0 if hour == 12 else hour + 12 if hour >= 6 else hour
    if part == "morning" and hour == 12:
        return 0
    return hour


def _reading(hour: int, minute: int, part: str | None, ambiguous: bool) -> dict[str, object]:
    return {"hour": hour % 24, "minute": minute, "part": part, "ambiguous": ambiguous,
            "text": f"{hour % 24:02d}:{minute:02d}"}


def _clock(hour: int, minute: int, part: str | None, *, twelve: bool) -> dict[str, object] | None:
    shown = _apply_part(hour, part)
    if shown is None:
        return None
    return _reading(shown, minute, part, twelve and part is None and 1 <= shown <= 12)


def _reference_hour(hour: int, part: str) -> int:
    """The hour an idiom points at on the 24-hour clock, 1..24: «двенадцатого» in «половина двенадцатого дня» is 12:00."""
    if part == "morning":
        return hour
    if part == "day":
        return hour + 12 if hour < 12 else hour
    if part == "evening":
        return hour + 12                      # twelve in the evening is midnight: 24
    return 24 if hour == 12 else hour + 12 if hour >= 6 else hour


def _before_hour(hour: int, minute: int, part: str | None) -> dict[str, object]:
    """«половина седьмого»: `minute` past the hour BEFORE the one named. The day part qualifies the named hour, so it
    is resolved first and only then is one hour taken off («половина двенадцатого дня» is 11:30, not 23:30)."""
    if part is None:                          # a 12-hour clock with no part: the reading stays ambiguous
        return _reading((hour - 1) or 12, minute, None, True)
    return _reading(_reference_hour(hour, part) - 1, minute, part, False)


def _with_part(tokens: list[str], pos: int) -> tuple[int, str | None]:
    if pos < len(tokens) and tokens[pos] in _PARTS:
        return pos + 1, _PARTS[tokens[pos]]
    return pos, None


def _minutes(tokens: list[str], pos: int) -> list[tuple[int, int]]:
    """A minutes figure after the hour: «тридцать», «ноль пять», «15»."""
    out: list[tuple[int, int]] = []
    if pos < len(tokens) and tokens[pos] in ("ноль", "нуль"):
        for end, value in parse_number(tokens, pos + 1):
            if 0 <= value <= 9:
                out.append((end, int(value)))
        if not out:
            out.append((pos + 1, 0))
        out.sort(key=lambda r: -r[0])
        return out
    for end, value in parse_number(tokens, pos):
        if isinstance(value, int) and 0 <= value <= 59:
            out.append((end, value))
    return out


def parse_clock(tokens: list[str], start: int = 0) -> list[tuple[int, dict[str, object]]]:
    """Clock-time readings at tokens[start]: dict(hour, minute, part, ambiguous, text)."""
    if start >= len(tokens):
        return []
    token = tokens[start]
    readings: list[tuple[int, dict[str, object]]] = []

    if ":" in token:
        hh, _, mm = token.partition(":")
        if hh.isdigit() and mm.isdigit() and len(hh) <= 2 and len(mm) <= 2 and int(hh) <= 23 and int(mm) <= 59:
            end, part = _with_part(tokens, start + 1)
            clock = _clock(int(hh), int(mm), part, twelve=False)
            if clock is not None:
                readings.append((end, clock))
        return readings

    if token in ("полдень",):
        return [(start + 1, _clock(12, 0, None, twelve=False))]
    if token in ("полночь",):
        return [(start + 1, _clock(0, 0, None, twelve=False))]

    # «половина седьмого», «пол седьмого», «полседьмого» -> 6:30
    if token in ("половина", "половину", "половине", "пол") and start + 1 < len(tokens):
        for end, hour in _hour_before(tokens, start + 1):
            end2, part = _with_part(tokens, end)
            readings.append((end2, _before_hour(hour, 30, part)))
    if token.startswith("пол") and len(token) > 5:
        for end, hour in _hour_before([token[3:]], 0):
            end2, part = _with_part(tokens, start + 1)
            readings.append((end2, _before_hour(hour, 30, part)))

    # «четверть седьмого» -> 6:15; «без четверти восемь» -> 7:45; «без пятнадцати восемь» -> 7:45
    if token in _QUARTER and start + 1 < len(tokens):
        for end, hour in _hour_before(tokens, start + 1):
            end2, part = _with_part(tokens, end)
            readings.append((end2, _before_hour(hour, 15, part)))
    if token == "без" and start + 1 < len(tokens):
        pos = start + 1
        deltas: list[tuple[int, int]] = []
        if tokens[pos] in _QUARTER:
            deltas.append((pos + 1, 15))
        for end, value in parse_number(tokens, pos):
            if isinstance(value, int) and 1 <= value <= 59:
                nxt = end + 1 if end < len(tokens) and tokens[end].startswith("минут") else end
                deltas.append((nxt, value))
        for dend, minus in deltas:
            for hend, hour in parse_number(tokens, dend):
                if isinstance(hour, int) and 1 <= hour <= 12:
                    hend2, part = _with_part(tokens, hend)
                    readings.append((hend2, _before_hour(hour, 60 - minus, part)))

    # «десять минут седьмого» -> 6:10; «двадцать пять минут восьмого» -> 7:25
    for end, value in parse_number(tokens, start):
        if isinstance(value, int) and 1 <= value <= 59 and end < len(tokens) and tokens[end].startswith("минут"):
            for hend, hour in _hour_before(tokens, end + 1):
                hend2, part = _with_part(tokens, hend)
                readings.append((hend2, _before_hour(hour, value, part)))

    # «семь», «семь часов», «семь тридцать», «семь ноль пять», «двадцать три сорок», «семь утра»
    for end, hour in parse_number(tokens, start):
        if not (isinstance(hour, int) and 0 <= hour <= 23):
            continue
        pos = end + 1 if end < len(tokens) and tokens[end] in _HOUR_WORDS else end
        explicit_hour_word = pos != end
        for mend, minute in _minutes(tokens, pos) or [(pos, 0)]:
            end2, part = _with_part(tokens, mend)
            clock = _clock(hour, minute, part, twelve=hour <= 12)
            if clock is not None:
                readings.append((end2, clock))
            if explicit_hour_word or mend != pos or part is not None:
                break
    best: dict[int, dict[str, object]] = {}
    for end, value in readings:
        best.setdefault(end, value)
    return sorted(best.items(), key=lambda r: -r[0])


def plural(count: int, one: str, few: str, many: str) -> str:
    if count % 100 in (11, 12, 13, 14) or count % 10 in (0, 5, 6, 7, 8, 9):
        return many
    return one if count % 10 == 1 else few


def duration_ru(seconds: int) -> str:
    """90 -> «1 минута 30 секунд», 300 -> «5 минут», 5400 -> «1 час 30 минут»."""
    seconds = int(seconds)
    parts = []
    for size, one, few, many in ((86400, "день", "дня", "дней"), (3600, "час", "часа", "часов"),
                                 (60, "минута", "минуты", "минут"), (1, "секунда", "секунды", "секунд")):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count} {plural(count, one, few, many)}")
    return " ".join(parts) or "0 секунд"
