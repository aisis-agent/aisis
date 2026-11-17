"""The activation word at the head of what the recognizer heard.

The station's own wake word fires on the device and opens the voice stream; the audio that reaches the STT gateway
sometimes still holds the name («Алиса, включи свет»), and a speaker who pauses after it produces a segment that is the
name and nothing else («Малис.»). Both are activation, never request content: the name is cut from the text before any
routing, and a text that is only the name is an activation event the gateway drops without answering.

The audio cut comes first (wake_window.py: the gateway reads the name's place in the stream from the arrival times of the frames
and never hands it to the recognizer). This text-level strip is what remains when that cut cannot be made without guessing (no
pause behind the name, a stream that ends early) and the backstop for a name the cut left a piece of; quasar does not forward the
device's spotter spans, so no span can replace either. Whisper is never asked for the name: the recognizer prompt carries no wake
word (voice_vocab).

One list of names: the intent grammar's `lexicon.wake` (grammar/ru.json), cut wherever it stands at the head. On top of it a
bounded sound rule recognises a mishearing the list has not seen yet («Малисан», «Лиса»): within two edits of «алиса» and
containing «ис». That rule only applies when the name is the whole utterance (an activation): in front of a request such a
word may be the request's own («Полиса нет у меня» is two edits from «алиса»), so a variant is cut from a request only after
it has been added to the lexicon. Pure functions, standard library only.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

LOG = logging.getLogger("aisis.wake_word")

BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
FLOOR = frozenset(("алиса", "яндекс"))   # only when the bundle cannot be read: the gateway must never fail a turn on it
NAME = "алиса"
MAX_EDITS = 2
FUZZY_LENGTHS = range(4, 9)
LEAD_INS = frozenset(("эй", "ну", "слушай", "ой", "а"))   # cut only directly in front of a name
WORD = re.compile(r"[a-zа-яё]+", re.IGNORECASE)
SEPARATORS = " \t\r\n,.!?:;-—–…"
HEAD_SEPARATORS = re.compile(r"(?:[\s,.!?:;…]|[-—–−](?!\d))*")   # a dash before a digit is a minus sign (the grammar folds them all): «Алиса, -5 громкость»


class Split(NamedTuple):
    clean: str    # the text to route: the name cut from its head, first letter capitalised
    head: str     # the raw head that was cut ("" when there was no name)
    only: bool    # nothing but the name was said: an activation, not a request


@lru_cache(maxsize=1)
def lexicon() -> frozenset[str]:
    try:
        with open(BUNDLE, encoding="utf-8") as bundle:
            words = json.load(bundle)["data"]["lexicon"]["wake"]
        return frozenset(str(word).lower().replace("ё", "е") for word in words) or FLOOR
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("wake_lexicon_unreadable class=%s", type(exc).__name__)
        return FLOOR


def _edits(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        row = [i]
        for j, right in enumerate(b, 1):
            row.append(min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (left != right)))
        previous = row
    return previous[-1]


def is_wake(word: str, *, standalone: bool = False) -> bool:
    word = word.lower().replace("ё", "е")
    if word in lexicon():
        return True
    return standalone and len(word) in FUZZY_LENGTHS and "ис" in word and _edits(word, NAME) <= MAX_EDITS


def _cut(text: str, words: list[re.Match], standalone: bool) -> int:
    """End offset of the name at the head of `text` (0 when it does not start with one)."""
    end = cursor = 0                     # `end`: after the last name; `cursor`: after the last word taken (a lead-in waits for a name)
    lead_in = False
    first = None                         # a name said twice is one name; another word of the lexicon after it is the request's («Яндекс — это что?»)
    for match in words:
        if text[cursor:match.start()].strip(SEPARATORS):
            break                        # something other than separators (a digit, a symbol) sits between the words
        word = match.group().lower().replace("ё", "е")
        if is_wake(word, standalone=standalone) and (first in (None, word) or standalone):
            end = cursor = match.end()
            lead_in = False
            first = first or word
        elif word in LEAD_INS and not lead_in:
            cursor, lead_in = match.end(), True
        else:
            break
    return end


def split(text: str) -> Split:
    words = list(WORD.finditer(text))
    if not words or text[:words[0].start()].strip(SEPARATORS):
        return Split(text, "", False)
    # the whole utterance is the name (any sound-alike counts), else only the names of the lexicon at the head
    for standalone in (True, False):
        end = _cut(text, words, standalone)
        if not end:
            continue
        rest = text[end:]
        if not any(ch.isalnum() for ch in rest):
            return Split("", text[:end].strip(), True)
        if standalone:
            continue
        rest = rest[HEAD_SEPARATORS.match(rest).end():]
        return Split(rest[0].upper() + rest[1:], text[:end].strip(), False)
    return Split(text, "", False)
