"""One utterance heard by several speakers is answered once.

Why: two speakers in earshot of each other open a speech session for the same «Алиса, …». The STT gateway
turns both into text and the stack acts on both, or on the one the owner did not mean. The gateway cannot
tell speakers apart (a session is an anonymous websocket from the quasar), so the unit here is the session,
and two sessions are the same utterance only when ALL of these hold:

  * their open intervals overlap (one speaker never has two sessions open at once, so a person who repeats a
    command on one speaker is never mistaken for two speakers),
  * they opened within `window` seconds of each other (the wake word reached both at the same moment), and
  * their transcripts say the same thing (`same_utterance`). Two different commands given at the same moment in
    two rooms are both executed: when in doubt the action goes through.

Among same-utterance sessions exactly one wins: the loudest voice (the speaker closest to the person); levels
within `LEVEL_TIE_DB` tie, then the session opened first wins, then the smaller id. A session that has its
transcript while an overlapping partner is still recognizing waits for it, at most `grace` seconds; a lone
speaker has no partner and never waits. A partner whose transcript arrives after the winner acted loses
whatever its level.

Single-threaded by design: everything runs on the gateway's event loop, no locks.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import itertools
import math
import re
import time
from typing import Callable

WINDOW_S = 2.0
GRACE_S = 1.2
LEVEL_TIE_DB = 3.0
SIMILARITY = 0.7
# A closed session stays in the ledger so a slow partner still finds that the winner acted: the gateway's longest
# utterance is 30 s, the rest covers recognizer latency.
RETAIN_S = 45.0

ACT = "act"
DUPLICATE = "duplicate"

_WAKE = re.compile(r"^(алиса|алис|лиса)\b\s*")


def _words(text: str) -> list[str]:
    folded = " ".join(re.sub(r"[^a-zа-я0-9 ]+", " ", text.lower().replace("ё", "е")).split())
    return _WAKE.sub("", folded).split()


_OFF = re.compile(r"^(вы|от)ключ")
_NUMBERS = frozenset(("ноль", "один", "одну", "два", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
                      "пятнадцать", "двадцать", "тридцать", "сорок", "пятьдесят", "сто"))


def _misheard(a: str, b: str) -> bool:
    """Could two different words be one word heard twice? Never when they differ in polarity (включи / выключи / отключи) or
    in a number: those are different commands, however alike they look."""
    if bool(_OFF.match(a)) != bool(_OFF.match(b)):
        return False
    if any(ch.isdigit() for ch in a + b) or a in _NUMBERS or b in _NUMBERS:
        return False
    return SequenceMatcher(None, a, b).ratio() >= SIMILARITY


def same_utterance(a: str, b: str) -> bool:
    """True when two transcripts are one sentence as two microphones heard it: the same words, the wake word or the tail
    cut off in one transcript, or single words misheard (`_misheard`). Conservative: any other difference in the words,
    in an action, a target or a number, makes them different commands."""
    first, second = _words(a), _words(b)
    if not first or not second:
        return False
    short, long = sorted((first, second), key=len)
    if set(short) <= set(long):
        return True
    if len(first) != len(second):
        return False
    return all(x == y or _misheard(x, y) for x, y in zip(first, second))


@dataclass(eq=False)
class Session:
    id: int
    opened: float
    closed: float | None = None
    text: str | None = None     # None: still listening or recognizing; "": nothing to act on (so far)
    level: float = 0.0          # voiced RMS of the raw audio: the closer the speaker, the larger
    verdict: str | None = None  # ACT / DUPLICATE once claimed
    winner: int | None = None   # the session that acted instead, for the log

    def overlaps(self, other: "Session") -> bool:
        return (other.opened < (math.inf if self.closed is None else self.closed)
                and self.opened < (math.inf if other.closed is None else other.closed))

    @property
    def pending(self) -> bool:
        return self.text is None and self.closed is None


@dataclass
class Arbiter:
    window: float = WINDOW_S
    grace: float = GRACE_S
    clock: Callable[[], float] = time.monotonic
    sessions: list[Session] = field(default_factory=list)
    _ids: "itertools.count[int]" = field(default_factory=lambda: itertools.count(1))
    _changed: asyncio.Condition | None = None

    def _cond(self) -> asyncio.Condition:
        if self._changed is None:
            self._changed = asyncio.Condition()
        return self._changed

    def open(self) -> Session:
        now = self.clock()
        self.sessions = [s for s in self.sessions if s.closed is None or now - s.closed < RETAIN_S]
        session = Session(next(self._ids), now)
        self.sessions.append(session)
        return session

    async def _notify(self) -> None:
        async with self._cond():
            self._cond().notify_all()

    async def close(self, session: Session) -> None:
        session.closed = self.clock()
        await self._notify()

    async def abstain(self, session: Session) -> None:
        """The session's transcript was rejected or empty: partners stop waiting for it."""
        session.text = ""
        await self._notify()

    def partners(self, me: Session) -> list[Session]:
        return [s for s in self.sessions if s is not me and abs(s.opened - me.opened) <= self.window and me.overlaps(s)]

    @staticmethod
    def _best(sessions: list[Session]) -> Session:
        """The earliest-opened session whose voice is within LEVEL_TIE_DB of the loudest one."""
        def db(session: Session) -> float:
            return 20 * math.log10(max(session.level, 1.0))
        loudest = max(db(session) for session in sessions)
        return min((s for s in sessions if db(s) >= loudest - LEVEL_TIE_DB), key=lambda s: (s.opened, s.id))

    def _verdict(self, me: Session, pending_over: bool, commit_over: bool) -> str | None:
        """ACT / DUPLICATE once certain, None while the outcome may still change. DUPLICATE only once another session has
        committed to act: while the chosen winner has not, waiting is safe, giving up the answer is not."""
        partners = self.partners(me)
        for other in partners:
            if other.verdict == ACT and same_utterance(me.text, other.text):
                me.winner = other.id
                return DUPLICATE
        if not pending_over and any(other.pending for other in partners):
            return None
        # A rival is a session that may still act: one that is closed, or already decided, cannot take the answer from me.
        rivals = [other for other in partners if other.text and other.verdict is None and other.closed is None
                  and same_utterance(me.text, other.text)]
        best = self._best(rivals + [me])
        if best is me:
            return ACT
        me.winner = best.id
        return ACT if commit_over else None  # acting twice beats acting never

    async def claim(self, session: Session, text: str, level: float) -> str:
        """The session has a transcript to act on: ACT or DUPLICATE (another session answers the same utterance). Waits at
        most `grace` for a partner still recognizing and at most twice that for the chosen winner to commit."""
        session.text, session.level = text, level
        cond = self._cond()
        try:
            async with cond:
                cond.notify_all()
                began = self.clock()
                verdict = self._verdict(session, pending_over=False, commit_over=False)
                while verdict is None:
                    remaining = began + 2 * self.grace - self.clock()
                    if remaining > 0:
                        try:
                            await asyncio.wait_for(cond.wait(), remaining)
                        except asyncio.TimeoutError:
                            pass
                    now = self.clock() - began
                    verdict = self._verdict(session, pending_over=now >= self.grace, commit_over=now >= 2 * self.grace)
                session.verdict = verdict
                cond.notify_all()
        except BaseException:
            session.text = ""   # cancelled or failed before it could act: no partner may defer to this session
            raise
        return verdict
