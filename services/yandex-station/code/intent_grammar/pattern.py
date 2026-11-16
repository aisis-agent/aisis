"""Pattern mini-language of the intent grammar: parser and backtracking matcher.

Syntax (tokens are separated by spaces; the whole utterance must be consumed):

    слово        a word; matches any inflection (compared by stem: «кухня» matches кухне, кухню)
    =слово       exactly this token, no stemming
    слов*        any token that starts with «слов»
    ~слово       like a plain word, and in the fuzzy pass also one edit / one sound class away
    (а|б в|г)    alternatives, each a sequence of tokens: «а» or «б в» or «г»
    X?           X is optional (a word, a group, or a {reference})
    {name}       a lexicon set (any of its forms) or a declared slot
    {name:type}  a typed slot: number, ordinal, percent, delta, duration, clock, text, word, or a lexicon name
    _            exactly one arbitrary token
    ...          zero or more arbitrary tokens

`Matcher.match` returns every full-consumption reading (position, captures, specificity); the grammar picks one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator

from .normalize import Normalised, edit_distance_le1, fold, phonetic_key, stem
from .numerals import parse_number, parse_ordinal
from .timewords import parse_clock, parse_duration

SLOT_TYPES = ("number", "ordinal", "percent", "delta", "duration", "clock", "text", "word")


class PatternError(ValueError):
    pass


@dataclass(frozen=True)
class Word:
    kind: str    # stem | exact | prefix | fuzzy
    value: str
    stem: str


@dataclass(frozen=True)
class Alt:
    options: tuple[tuple["Node", ...], ...]


@dataclass(frozen=True)
class Opt:
    inner: "Node"


@dataclass(frozen=True)
class Any1:
    pass


@dataclass(frozen=True)
class AnyRun:
    pass


@dataclass(frozen=True)
class Slot:
    name: str
    type: str                      # one of SLOT_TYPES, or "lex"
    lexicon: tuple[tuple[str, Alt], ...] = ()   # (canonical key, forms) for a lexicon slot


Node = Word | Alt | Opt | Any1 | AnyRun | Slot

_LEX = re.compile(r"\{\s*([^{}:\s]+)\s*(?::\s*([^{}\s]+)\s*)?\}")
_TOKEN = re.compile(r"\(|\)|\||\?|\{[^{}]*\}|[^\s()|?{}]+")


@dataclass
class Resolver:
    """What the parser needs from the grammar: lexicon sets and declared slots."""
    sets: dict[str, Alt]
    keyed: dict[str, tuple[tuple[str, Alt], ...]]
    slot_types: dict[str, str]     # declared slot name -> type or lexicon name


def parse_pattern(text: str, resolver: Resolver) -> tuple[Node, ...]:
    tokens = _TOKEN.findall(text)
    if not tokens:
        raise PatternError("empty pattern")
    seq, pos = _parse_seq(tokens, 0, resolver)
    if pos != len(tokens):
        raise PatternError(f"unbalanced ')' or '|' in {text!r}")
    return seq


def _parse_seq(tokens: list[str], pos: int, resolver: Resolver) -> tuple[tuple[Node, ...], int]:
    items: list[Node] = []
    while pos < len(tokens) and tokens[pos] not in (")", "|"):
        token = tokens[pos]
        pos += 1
        if token == "(":
            options: list[tuple[Node, ...]] = []
            while True:
                seq, pos = _parse_seq(tokens, pos, resolver)
                if not seq:
                    raise PatternError("empty alternative")
                options.append(seq)
                if pos >= len(tokens):
                    raise PatternError("missing ')'")
                closer = tokens[pos]
                pos += 1
                if closer == ")":
                    break
            node: Node = Alt(tuple(options))
        elif token == "?":
            if not items:
                raise PatternError("'?' with nothing before it")
            items[-1] = Opt(items[-1])
            continue
        elif token.startswith("{"):
            node = _parse_ref(token, resolver)
        else:
            node = _parse_word(token)
        items.append(node)
    return tuple(items), pos


def _parse_word(token: str) -> Node:
    if token == "_":
        return Any1()
    if token == "...":
        return AnyRun()
    # literals are folded the way utterances are (lower case, ё -> е), or «Алиса» and «ещё» could never match
    token = fold(token)
    if token.startswith("="):
        return Word("exact", token[1:], token[1:])
    if token.startswith("~"):
        word = token[1:]
        return Word("fuzzy", word, stem(word))
    if token.endswith("*"):
        if len(token) < 3:
            raise PatternError(f"stem {token!r} is too short")
        return Word("prefix", token[:-1], token[:-1])
    return Word("stem", token, stem(token))


def _parse_ref(token: str, resolver: Resolver) -> Node:
    match = _LEX.fullmatch(token)
    if match is None:
        raise PatternError(f"bad reference {token}")
    name, kind = match.group(1), match.group(2)
    if kind is None:
        if name in resolver.slot_types:
            kind = resolver.slot_types[name]
        elif name in resolver.sets:
            return resolver.sets[name]
        else:
            raise PatternError(f"{{{name}}} is neither a lexicon set nor a declared slot")
    if kind in SLOT_TYPES:
        return Slot(name, kind)
    if kind in resolver.keyed:
        return Slot(name, "lex", resolver.keyed[kind])
    raise PatternError(f"unknown slot type {kind!r} for {{{name}}}")


def compile_forms(forms: list[str], resolver: Resolver) -> Alt:
    """A lexicon entry: a list of forms (each a short pattern), as one alternation."""
    return Alt(tuple(parse_pattern(form, resolver) for form in forms))


# ── matching ────────────────────────────────────────────────────────────────

Capture = tuple[str, object, tuple[int, int]]   # (slot name, value, (first token, end token))
Reading = tuple[int, tuple[Capture, ...], int]  # (end position, captures, specificity)

SlotCheck = Callable[[str, object], bool]


class Matcher:
    """Matches one compiled pattern against a normalised utterance."""

    def __init__(self, utterance: Normalised, *, fuzzy: bool = False, slot_ok: SlotCheck | None = None):
        self.u = utterance
        self.n = len(utterance.tokens)
        self.fuzzy = fuzzy
        self.slot_ok = slot_ok

    def full(self, seq: tuple[Node, ...]) -> Iterator[tuple[tuple[Capture, ...], int]]:
        """Readings that consume the whole utterance."""
        for end, caps, score in self._seq(seq, 0, 0):
            if end == self.n:
                yield caps, score

    def _seq(self, seq: tuple[Node, ...], index: int, pos: int) -> Iterator[Reading]:
        if index == len(seq):
            yield pos, (), 0
            return
        for npos, ncaps, nscore in self._node(seq[index], pos):
            for epos, ecaps, escore in self._seq(seq, index + 1, npos):
                yield epos, ncaps + ecaps, nscore + escore

    def _word(self, node: Word, pos: int) -> bool:
        token = self.u.tokens[pos]
        if node.kind == "stem":
            return self.u.stems[pos] == node.stem
        if node.kind == "exact":
            return token == node.value
        if node.kind == "prefix":
            return token.startswith(node.value)
        if self.u.stems[pos] == node.stem:  # fuzzy
            return True
        if not self.fuzzy:
            return False
        if len(node.value) >= 5 and edit_distance_le1(token, node.value):
            return True
        return len(node.value) >= 4 and phonetic_key(token) == phonetic_key(node.value)

    def _node(self, node: Node, pos: int) -> Iterator[Reading]:
        if isinstance(node, Word):
            if pos < self.n and self._word(node, pos):
                yield pos + 1, (), 2
        elif isinstance(node, Alt):
            for option in node.options:
                yield from self._seq(option, 0, pos)
        elif isinstance(node, Opt):
            yield from self._node(node.inner, pos)
            yield pos, (), 0
        elif isinstance(node, Any1):
            if pos < self.n:
                yield pos + 1, (), 0
        elif isinstance(node, AnyRun):
            for end in range(self.n, pos - 1, -1):
                yield end, (), 0
        else:
            yield from self._slot(node, pos)

    def _slot(self, node: Slot, pos: int) -> Iterator[Reading]:
        if pos >= self.n:
            return
        for end, value, score in self._slot_values(node, pos):
            if self.slot_ok is not None and not self.slot_ok(node.name, value):
                continue
            yield end, ((node.name, value, (pos, end)),), score

    def _slot_values(self, node: Slot, pos: int) -> Iterator[tuple[int, object, int]]:
        tokens = self.u.tokens
        kind = node.type
        if kind == "number":
            for end, value in parse_number(tokens, pos):
                yield end, value, 1
        elif kind == "ordinal":
            for end, value in parse_ordinal(tokens, pos):
                yield end, value, 1
        elif kind == "percent":
            for end, value in parse_number(tokens, pos):
                unit = end + 1 if end < self.n and tokens[end].startswith("процент") else end
                yield unit, value, 1
        elif kind == "delta":
            # «на десять процентов», «на два», «на 5 градусов»: a relative amount
            start = pos + 1 if tokens[pos] == "на" else pos
            for end, value in parse_number(tokens, start):
                unit = ""
                nxt = end
                if end < self.n and self.u.stems[end] in ("процент", "градус", "единиц", "пункт", "делени"):
                    unit = self.u.stems[end]
                    nxt = end + 1
                yield nxt, {"amount": value, "unit": unit}, 1
        elif kind == "duration":
            for end, value in parse_duration(tokens, pos):
                yield end, value, 1
        elif kind == "clock":
            for end, value in parse_clock(tokens, pos):
                yield end, value, 1
        elif kind == "word":
            yield pos + 1, tokens[pos], 0
        elif kind == "text":
            for end in range(self.n, pos, -1):
                yield end, " ".join(tokens[pos:end]), 0
        elif kind == "lex":
            for key, forms in node.lexicon:
                for end, _caps, _score in self._node(forms, pos):
                    yield end, key, 2
