"""Spoken Russian numbers: digits, cardinal words in every case, «полтора», «N с половиной», ordinals.

`parse_number(tokens, start)` returns every valid reading starting at `start`, longest first, as
`(end, value)` pairs, so the matcher can backtrack when a longer reading breaks the rest of the pattern
(«два тридцать» is a clock, «двадцать три» is one number). Tokens are already folded (ё -> е, lower case).

Ordinals (`parse_ordinal`) cover 1..60 in every case and gender: «седьмого», «второй этаж», «двадцать первый».
"""
from __future__ import annotations

Number = int | float


def _forms(value: int, *forms: str) -> dict[str, int]:
    return {form: value for form in forms}


_UNITS: dict[str, int] = {}
for _value, _words in (
    (0, "ноль нуль ноля нуля нолю нулю нолем нулем ноле нуле"),
    (1, "один одна одно одну одного одной одному одним одном"),
    (2, "два две двух двум двумя"),
    (3, "три трех трем тремя"),
    (4, "четыре четырех четырем четырьмя"),
    (5, "пять пяти пятью"),
    (6, "шесть шести шестью"),
    (7, "семь семи семью"),
    (8, "восемь восьми восемью"),
    (9, "девять девяти девятью"),
    (10, "десять десяти десятью"),
    (11, "одиннадцать одиннадцати одиннадцатью"),
    (12, "двенадцать двенадцати двенадцатью"),
    (13, "тринадцать тринадцати тринадцатью"),
    (14, "четырнадцать четырнадцати четырнадцатью"),
    (15, "пятнадцать пятнадцати пятнадцатью"),
    (16, "шестнадцать шестнадцати шестнадцатью"),
    (17, "семнадцать семнадцати семнадцатью"),
    (18, "восемнадцать восемнадцати восемнадцатью"),
    (19, "девятнадцать девятнадцати девятнадцатью"),
):
    _UNITS.update(_forms(_value, *_words.split()))

_TENS: dict[str, int] = {}
for _value, _words in (
    (20, "двадцать двадцати двадцатью"),
    (30, "тридцать тридцати тридцатью"),
    (40, "сорок сорока"),
    (50, "пятьдесят пятидесяти пятьюдесятью"),
    (60, "шестьдесят шестидесяти шестьюдесятью"),
    (70, "семьдесят семидесяти семьюдесятью"),
    (80, "восемьдесят восьмидесяти восемьюдесятью"),
    (90, "девяносто девяноста"),
):
    _TENS.update(_forms(_value, *_words.split()))

_HUNDREDS: dict[str, int] = {}
for _value, _words in (
    (100, "сто ста"),
    (200, "двести двухсот двумстам двумястами"),
    (300, "триста трехсот трехстам тремястами"),
    (400, "четыреста четырехсот четырехстам четырьмястами"),
    (500, "пятьсот пятисот пятистам пятьюстами"),
    (600, "шестьсот шестисот шестистам шестьюстами"),
    (700, "семьсот семисот семистам семьюстами"),
    (800, "восемьсот восьмисот восьмистам восемьюстами"),
    (900, "девятьсот девятисот девятистам девятьюстами"),
):
    _HUNDREDS.update(_forms(_value, *_words.split()))

_THOUSAND = frozenset(("тысяча", "тысячи", "тысяч", "тысячу", "тысячей", "тысячам", "тыс"))
_HALF_ONE = frozenset(("полтора", "полторы"))          # 1.5
_WITH_HALF = frozenset(("половиной", "половиною"))     # «пять с половиной»

# Ordinals: stem -> value; the ending is checked against a closed set of adjective endings.
_ORD_STEMS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9,
    "десят": 10, "одиннадцат": 11, "двенадцат": 12, "тринадцат": 13, "четырнадцат": 14, "пятнадцат": 15,
    "шестнадцат": 16, "семнадцат": 17, "восемнадцат": 18, "девятнадцат": 19, "двадцат": 20, "тридцат": 30,
    "сороков": 40, "пятидесят": 50, "шестидесят": 60,
}
_ORD_ENDINGS = frozenset(("ый", "ой", "ий", "ого", "ому", "ым", "ом", "ая", "ую", "ое", "ые", "ых", "ыми",
                          "ья", "ье", "ьи", "ьего", "ьему", "ьим", "ьем", "ьей", "ью", "ьих", "ьими", "ей"))


def _is_digits(token: str) -> bool:
    return token.isdigit()


def _is_decimal(token: str) -> bool:
    if not token or not token[0].isdigit():
        return False
    head, _, tail = token.partition(".")
    return head.isdigit() and (tail == "" or tail.isdigit()) and ":" not in token


def _numeric(token: str) -> Number:
    value = float(token)
    return int(value) if value == int(value) and "." not in token else value


def _hundreds_unit(token: str) -> tuple[str, int] | None:
    if token in _HUNDREDS:
        return "h", _HUNDREDS[token]
    if token in _TENS:
        return "t", _TENS[token]
    if token in _UNITS:
        return "u", _UNITS[token]
    return None


def _group(tokens: list[str], start: int) -> list[tuple[int, int]]:
    """Readings 0..999 of a hundreds/tens/units run starting at `start`: (end, value), longest first."""
    readings: list[tuple[int, int]] = []
    pos, total, stage = start, 0, 0  # stage: 0 nothing, 1 hundreds seen, 2 tens seen, 3 units/teens seen
    while pos < len(tokens):
        kind = _hundreds_unit(tokens[pos])
        if kind is None:
            break
        cls, value = kind
        if cls == "h" and stage < 1:
            stage = 1
        elif cls == "t" and stage < 2:
            stage = 2
        elif cls == "u" and stage < 3 and (stage < 2 or value < 10) and not (stage == 2 and value == 0):
            stage = 3
        else:
            break
        total += value
        pos += 1
        readings.append((pos, total))
        if stage == 3:
            break
    readings.reverse()
    return readings


def parse_number(tokens: list[str], start: int = 0) -> list[tuple[int, Number]]:
    """All number readings that begin at tokens[start], longest first: (end index exclusive, value)."""
    if start >= len(tokens):
        return []
    token = tokens[start]
    readings: list[tuple[int, Number]] = []

    if _is_decimal(token):
        value = _numeric(token)
        pos = start + 1
        readings.append((pos, value))
        if pos < len(tokens) and tokens[pos] in _THOUSAND and isinstance(value, int):
            readings.insert(0, (pos + 1, value * 1000))
        elif pos + 1 < len(tokens) and tokens[pos] == "с" and tokens[pos + 1] in _WITH_HALF:
            readings.insert(0, (pos + 2, value + 0.5))
        readings.sort(key=lambda r: -r[0])
        return readings

    if token in _HALF_ONE:
        return [(start + 1, 1.5)]

    if token in _THOUSAND:  # bare «тысяча» = 1000
        return [(start + 1, 1000)]

    # «[group] тысяч [group]» and plain groups
    for end, value in _group(tokens, start):
        results = [(end, value)]
        if end < len(tokens) and tokens[end] in _THOUSAND and value > 0:
            thousands = value * 1000
            results.insert(0, (end + 1, thousands))
            for tail_end, tail in _group(tokens, end + 1):
                results.insert(0, (tail_end, thousands + tail))
        elif end + 1 < len(tokens) and tokens[end] == "с" and tokens[end + 1] in _WITH_HALF:
            results.insert(0, (end + 2, value + 0.5))
        readings.extend(results)
    seen: dict[int, Number] = {}
    for end, value in readings:
        seen.setdefault(end, value)
    return sorted(seen.items(), key=lambda r: -r[0]) if seen else []


def parse_ordinal(tokens: list[str], start: int = 0) -> list[tuple[int, int]]:
    """Ordinal readings at tokens[start]: «седьмого», «второй», «двадцать первый» -> (end, value), longest first."""
    if start >= len(tokens):
        return []
    results: list[tuple[int, int]] = []
    token = tokens[start]
    value = _ordinal_word(token)
    if value is not None:
        results.append((start + 1, value))
    if token in _TENS and start + 1 < len(tokens):
        tail = _ordinal_word(tokens[start + 1])
        if tail is not None and tail < 10:
            results.insert(0, (start + 2, _TENS[token] + tail))
    return results


def _ordinal_word(token: str) -> int | None:
    for stem_, value in _ORD_STEMS.items():
        if token.startswith(stem_) and token[len(stem_):] in _ORD_ENDINGS:
            return value
    if token.endswith(("ый", "ой", "ий")) and token[:-2].isdigit():
        return int(token[:-2])
    return None

