"""Last line of defence against the brain's «not heard» habits.

The brain has no catalog of the home and no state, so it can only say it did not understand («Повтори?», «Непонятно.»), or ask
«Урок из пяти чего: минут, упражнений, слов? Не расслышал, уточни.» (owner, tg#28376). tidy() removes the «не расслышал, уточни» tail and
collapses an option list after a colon, so the speaker says at most one short question. A reply that is nothing but a request to repeat
is no reply: `is_unheard` says so, and the adapter asks the clarifier (clarify.py, #307) for the likeliest actions or for a line that
says what was heard and what can be asked for, instead of ever speaking the bare request (owner, tg#32074).
"""
from __future__ import annotations

import re

_TAIL = re.compile(r"\s*(?:извини[,!.]?\s*)?не\s+(?:расслыша\w*|понял\w*)[\s,.!—-]*"
                   r"(?:уточни\w*|повтори\w*|скажи\s+ещё\s+раз|можешь\s+повторить)[^.?!]*[.?!]*\s*$", re.IGNORECASE)
# «Урок из пяти чего: минут, упражнений, слов?»: a question whose colon opens a list of guesses.
_OPTION_LIST = re.compile(r"^(?P<head>[^:?!.]{3,}):\s*(?P<items>[^?!.]*(?:,|\sили\s)[^?!.]*)\?\s*$")
# A whole reply that only asks to repeat or says nothing was understood: no other content word.
_BARE = re.compile(
    r"^(?:извини(?:те)?[,!.\s]*)?(?:"
    r"(?:не\s+(?:расслыша\w*|понял\w*|поня\w*)|непонятно|не\s+ясно)[\s,.!?—-]*(?:(?:повтор\w*|уточн\w*|скажи\w*\s+ещё\s+раз)[\s,.!?—-]*)?"
    r"|(?:повтор\w*|уточн\w*)(?:[\s,]+(?:ещё\s+раз|пожалуйста|вопрос))*[\s,.!?—-]*"
    r"|(?:что|как|чего)(?:\s+именно)?[\s.!?—-]*"
    r"|ещё\s+раз[\s.!?—-]*"
    r"|(?:скажи\w*)\s+ещё\s+раз[\s,.!?—-]*(?:пожалуйста)?[\s.!?—-]*"
    r")$", re.IGNORECASE)


def tidy(reply: str) -> str:
    """`reply` without the not-heard tail and without a trailing option list; empty if nothing is left."""
    text = _TAIL.sub("", reply.strip()).strip()
    sentences = re.split(r"(?<=[.?!])\s+", text)
    if sentences and sentences[-1].endswith("?"):
        listed = _OPTION_LIST.match(sentences[-1])
        if listed:
            sentences[-1] = listed.group("head").strip() + "?"
            text = " ".join(sentences)
    return text


def is_unheard(reply: str) -> bool:
    """The reply says nothing but «repeat» / «not understood» (or nothing at all)."""
    text = reply.strip()
    return not _TAIL.sub("", text).strip() or _BARE.match(text) is not None
