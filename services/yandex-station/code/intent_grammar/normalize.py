"""Canonical form of a spoken utterance for the grammar matcher.

Ideas ported from HyperCalendarBot (same owner, no licence file): `canonical-input.ts` (ё fold, courtesy words
stripped from the edges only, every canonical token maps back to a span of the raw text, safe single-edit typo
repair of a fixed vocabulary) and the suffix stemmer of `event-reference.ts` (cut one inflection ending, never
below a minimum stem). The code is a fresh Python implementation for Russian speech, not a transliteration.

Pure functions, no I/O: the matcher calls them on the hot path (budget ~1 ms).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A token is a number (digits, optional :minutes or decimal part, optional %, optional leading sign), a word (Latin or
# Cyrillic letters, hyphens inside), or a lone percent sign. A sign glued to the digits stays part of the token
# («-5» is not a number: nothing reads it), but a hyphen after a digit is a range dash («5-10»: 5, 10).
_TOKEN = re.compile(r"(?<!\d)[+-]\d+(?:[:.,]\d+)?%?|\d+(?:[:.,]\d+)?%?|[a-zа-я]+(?:-[a-zа-я]+)*|%")
_PARTICLE_KA = re.compile(r"-ка$")

# One inflection ending is cut when at least MIN_STEM letters remain. Longest ending first. No past-tense «-л» ending
# (-ал -ял -ил -ел) is cut: «включил» must not collapse into the imperative «включи», a report is not a command.
MIN_STEM = 3   # letters that must remain after an ending is cut (4 for a one-letter ending)
_ENDINGS = tuple(sorted({
    "ыми", "ими", "ого", "его", "ому", "ему", "ами", "ями", "ешь", "ишь", "ете", "ите", "ают", "яют", "ует", "уют",
    "ать", "ять", "ить", "еть", "уть", "ает", "яет",
    "ой", "ей", "ый", "ий", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ых", "их", "ом", "ем", "ым", "им",
    "ов", "ев", "ам", "ям", "ах", "ях", "ою", "ею", "ия", "ай", "яй", "ла", "ли", "ло",
    "ть", "ти",
    "а", "я", "о", "е", "у", "ю", "ы", "и", "ь", "й",
}, key=len, reverse=True))


def fold(text: str) -> str:
    """Lower case, ё -> е, typographic dashes and the minus sign -> hyphen."""
    return text.lower().replace("ё", "е").replace("—", "-").replace("–", "-").replace("−", "-")


def stem(word: str) -> str:
    """Cut one inflection ending: включи/включай/включить -> включ, кухне/кухню -> кухн, громкость/громкости -> громкост.

    Words with a digit, with a hyphen or shorter than MIN_STEM + 1 letters are returned as they are, so short
    function words (на, не, да, в, час, сто) never collide with each other.
    """
    if len(word) <= MIN_STEM or not word.isalpha():
        return word
    for ending in _ENDINGS:
        # a one-letter ending needs four letters left: «стой» must not become «сто» (a hundred)
        if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM + (len(ending) == 1):
            cut = word[: -len(ending)]
            # «громкость» -> «громкост»: also drop the soft sign left of a consonant ending that was removed above
            return cut[:-1] if cut.endswith(("ь", "й")) and len(cut) > MIN_STEM else cut
    return word


# Spelling classes the recogniser confuses on far-field Russian speech: unstressed vowels and voiced/voiceless pairs.
_PHONETIC = str.maketrans({"ы": "и", "о": "а", "е": "и", "э": "и", "я": "а", "ю": "у", "ё": "и",
                           "б": "п", "в": "ф", "г": "к", "д": "т", "ж": "ш", "з": "с", "ь": "", "ъ": ""})


def phonetic_key(word: str) -> str:
    """A coarse sound key: unstressed vowels merged, final and paired consonants devoiced, doubles collapsed."""
    key = word.translate(_PHONETIC)
    return re.sub(r"(.)\1+", r"\1", key)


def edit_distance_le1(a: str, b: str) -> bool:
    """True when `a` and `b` differ by at most one insertion, deletion, substitution or adjacent transposition."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long = (a, b) if la < lb else (b, a)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1:]


@dataclass
class Normalised:
    """An utterance after canonicalisation.

    `tokens` are what the matcher sees; `spans[i]` is the (start, end) of token i in `raw`, so a captured slot can be
    read back from what the person actually said. `stripped` lists what was cut (wake words, courtesy fillers).
    """
    raw: str
    tokens: list[str]
    spans: list[tuple[int, int]]
    stems: list[str] = field(default_factory=list)
    stripped: list[str] = field(default_factory=list)
    woke: bool = False
    negated: bool = False
    question: bool = False

    @property
    def text(self) -> str:
        return " ".join(self.tokens)

    def slice(self, start: int, end: int) -> "Normalised":
        """A sub-utterance (token range), used to match one clause of a compound command."""
        tokens = self.tokens[start:end]
        part = Normalised(self.raw, tokens, self.spans[start:end], self.stems[start:end], [], self.woke,
                          self.negated, self.question)
        return part


def tokenize(raw: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Folded tokens with spans into the ORIGINAL string; «50%» -> «50» «процентов», «поставь-ка» -> «поставь», 7,5 -> 7.5."""
    folded = fold(raw)
    tokens: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in _TOKEN.finditer(folded):
        text = match.group()
        span = (match.start(), match.end())
        if text.endswith("%") and len(text) > 1:
            tokens.append(text[:-1].replace(",", "."))
            spans.append(span)
            tokens.append("процентов")
            spans.append(span)
        elif text == "%":
            tokens.append("процентов")
            spans.append(span)
        else:
            text = _PARTICLE_KA.sub("", text)
            if text.lstrip("+-")[:1].isdigit():
                text = text.replace(",", ".")
            if text:
                tokens.append(text)
                spans.append(span)
    return tokens, spans


@dataclass(frozen=True)
class Canon:
    """The cross-cutting vocabulary of the normaliser (from the grammar's lexicon)."""
    fillers: frozenset[str] = frozenset()      # dropped anywhere («пожалуйста», «ну»)
    lead_in: frozenset[str] = frozenset()      # dropped at the edges only (wake words, «слушай», «а»)
    wake: frozenset[str] = frozenset()         # names the speaker answers to (a subset of lead_in, flags `woke`)
    negation: frozenset[str] = frozenset()
    question_words: frozenset[str] = frozenset()
    typo_vocabulary: tuple[str, ...] = ()      # fixed command words a single-edit typo is repaired to


def normalise(raw: str, canon: Canon = Canon()) -> Normalised:
    tokens, spans = tokenize(raw)
    stripped: list[str] = []
    woke = False

    keep = [i for i, t in enumerate(tokens) if t not in canon.fillers]
    stripped.extend(tokens[i] for i in range(len(tokens)) if i not in set(keep))
    tokens = [tokens[i] for i in keep]
    spans = [spans[i] for i in keep]

    lo, hi = 0, len(tokens)
    while lo < hi and tokens[lo] in canon.lead_in:
        stripped.append(tokens[lo])
        woke = woke or tokens[lo] in canon.wake
        lo += 1
    while hi > lo and tokens[hi - 1] in canon.lead_in:
        stripped.append(tokens[hi - 1])
        woke = woke or tokens[hi - 1] in canon.wake
        hi -= 1
    if lo >= hi:  # nothing but wake words / courtesy: keep them, «Алиса.» alone is itself an utterance
        woke = any(t in canon.wake for t in tokens)
        stripped = []
    else:
        tokens, spans = tokens[lo:hi], spans[lo:hi]

    if canon.typo_vocabulary:
        tokens = [repair_typo(t, canon.typo_vocabulary) for t in tokens]

    negated = any(t in canon.negation for t in tokens)
    question = any(t in canon.question_words for t in tokens) or raw.rstrip().endswith("?")
    return Normalised(raw, tokens, spans, [stem(t) for t in tokens], stripped, woke, negated, question)


def repair_typo(token: str, vocabulary: tuple[str, ...]) -> str:
    """Single-edit repair against a fixed vocabulary, only when exactly one word is that close (abstain over guess)."""
    if len(token) < 5 or token in vocabulary:
        return token
    near = [word for word in vocabulary if abs(len(word) - len(token)) <= 1 and edit_distance_le1(token, word)]
    return near[0] if len(near) == 1 else token
