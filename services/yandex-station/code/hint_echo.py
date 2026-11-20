"""A transcript that is only the recogniser's own prompt read back is not a request (ticket #258, the family of #158 and #59).

Whisper treats its initial prompt as the text that came just before the audio, so on room noise it sometimes continues the prompt
instead of hearing anything: clip 20261120T133321-8c9f82 (owner verdict «шум», tg#31304) was «Комнаты, гирлянда, люстра.»: the label of
one prompt list and the last two items of another. The adapter answered it with the «Не расслышал. Повтори» reprompt and opened the
microphone. Before the prompt was rewritten without one-word imperatives (#93) the same thing came back as «Громкость.» and
«Пауза. Дальше.»; the stored archive has 13 of those among the 59 utterances sent in 2026-11-09 .. 11-15.

What tells it from a request: every comma- or full-stop-separated piece of it is a prompt LIST entry (a list label such as «Комнаты», or one
item of a list: a room, a device, an artist) and it is a list read, not a command: at least two pieces, with a label among them or at
least three items. A request has a verb; «кухня, студия» (two rooms, the answer to «где?») stays a request. The prompt's example commands
(«Включи свет на кухне») and its climate sentence are NOT list entries: a recogniser that echoes them still answers with what could be a
real command, and the station's own spotter transcribes exactly those words (the archive's 15 spotter clips, «включи свет»), so the rule
never looks at them. The audio's level does not tell the two apart either (the owner's noise verdict and two real verdicts have the same
rms and contrast), so the rule reads the text only.

The prompt is the one the recogniser was given: the wyoming-whisper unit's `WHISPER_INITIAL_PROMPT` env file (refresh-whisper-prompt.sh
writes it; a stock Wyoming server takes it as `--initial-prompt`, so the gateway never sees it on the wire), re-read when the file
changes, plus the `context` prompt the gateway sends to an stt-cli recogniser. Pure functions and a small file reader, standard library
only.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from voice_vocab import PROMPT_LISTS

DEFAULT_MODE = "shadow"   # log what would be dropped, drop nothing: a request that is only list words is measured before it can be lost
REJECTED = "hint_echo"    # the gate reason of a dropped transcript
DEFAULT_PROMPT_ENV = "~/.config/aisis/whisper-prompt.env"
MIN_PIECES = 2
MIN_ITEMS_WITHOUT_LABEL = 3
_PIECE_SEPARATORS = re.compile(r"[,.;:!?…]+")
_ENV_LINE = re.compile(r'^WHISPER_INITIAL_PROMPT="(.*)"$', re.MULTILINE)
_NEXT_LIST = r"(?=\s[A-ZА-ЯЁ][\w-]*:|$)"   # a list runs to the next «Label:» or the prompt's end: an item may hold a full stop («Dr. Dre», «лампа 2.0»)


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-zа-я0-9 ]+", " ", text.lower().replace("ё", "е"))).strip()


@dataclass(frozen=True)
class Hint:
    """The list part of a recogniser prompt: the list labels and the items listed under them, folded."""
    labels: frozenset[str] = frozenset()
    items: frozenset[str] = frozenset()

    @classmethod
    def of(cls, prompts) -> "Hint":
        """The lists of every prompt in `prompts`, labels as voice_vocab.PROMPT_LISTS names them («Комнаты: гостиная, кухня.»)."""
        labels, items = set(), set()
        for prompt in prompts:
            for label, _ in PROMPT_LISTS:
                for match in re.finditer(re.escape(label) + r":\s*(.*?)" + _NEXT_LIST, prompt):
                    labels.add(fold(label))
                    items.update(filter(None, (fold(item) for item in _PIECE_SEPARATORS.split(match.group(1)))))   # the pieces of a transcript are cut the same way
        return cls(frozenset(labels), frozenset(items))


def is_echo(text: str, hint: Hint) -> bool:
    """True when `text` is the prompt's lists read back: only label and item pieces, at least two, a label among them or at least three of them."""
    pieces = [piece for piece in (fold(part) for part in _PIECE_SEPARATORS.split(text)) if piece]
    if len(pieces) < MIN_PIECES or not all(piece in hint.labels or piece in hint.items for piece in pieces):
        return False
    return any(piece in hint.labels for piece in pieces) or len(pieces) >= MIN_ITEMS_WITHOUT_LABEL


class PromptFile:
    """The initial prompt the wyoming-whisper unit starts with, read from its EnvironmentFile; re-read when the file changes."""

    def __init__(self, path: str | None = None):
        self.path = Path(os.path.expanduser(path or DEFAULT_PROMPT_ENV))
        self.stamp: tuple[int, int] | None = None
        self.prompt = ""

    def read(self) -> str:
        """The prompt, or "" when the file is missing or holds none (the guard then has nothing to compare with and never fires)."""
        try:
            status = self.path.stat()
            stamp = (status.st_mtime_ns, status.st_size)
            if stamp != self.stamp:
                found = _ENV_LINE.search(self.path.read_text(encoding="utf-8"))
                self.prompt = re.sub(r'\\(["\\])', r"\1", found.group(1)) if found else ""   # refresh-whisper-prompt.sh escapes \ and "
                self.stamp = stamp
        except (OSError, UnicodeDecodeError):
            self.stamp, self.prompt = None, ""
        return self.prompt
