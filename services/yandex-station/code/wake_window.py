"""The activation word, cut from the audio before the recognizer hears it.

The station's wake word fires on the device. Quasar has no span for it (the overlay does not forward one and the protocol
facts the gateway does have are the arrival times of the audio frames), but the stream itself gives it away. Every
activation stream opens with the audio the station buffered before its spotter fired, sent faster than real time (the
«history», 2.32 s with a 0.04 s spread over 98 of 103 sessions in the gateway journal 2026-11-10 … 11-17), then live audio. A
follow-up turn (no wake word) is live from the first frame. The name ends 0.2-0.4 s before the end of the history; the
request's first word can start 0.08 s after the name, so neither the end of the history nor a fixed trim is a safe cut. The end
of the history is only known while the burst holds nothing but the history: a socket opened late sends the first live speech
inside the burst (2026-11-20 11:18: 3.65 s in 0.18 s), the lead grows past any history and the window would sit on the request.

`history_ms` reads the burst from arrival times, `locate` finds the pause that follows the name in the frame levels of the
history and `Gate` holds the first audio of a stream until both are known, then hands on only what follows the name. When
the name cannot be located without guessing (no pause after it, speech running through the whole window, a quiet history) the
audio is handed on whole and the text-level strip (wake_word.py) stays the fallback. Pure functions, standard library only.
"""
from __future__ import annotations

import array
import math
from typing import NamedTuple

FRAME_MS = 20
DECIDE_AFTER_MS = 600          # the burst is over by then (the station's 2.3 s arrive within ~0.4 s); the decision needs live audio behind it
MIN_HISTORY_MS = 1500          # a lead shorter than any real history is a stream that is simply live
MAX_HISTORY_MS = 2450          # a longer lead has live speech in the burst (a late socket, a catch-up) or is a fast sender: the longest history measured is 2.43 s (2.41 s since the cut went live): a lead of +0.1 s over the mean already puts 6% of short-pause splices at risk (bench_wake_window.py gate)
LOOKAHEAD_MS = 100             # frame levels are computed this far past the history's end
PEAK_BEFORE_MS = 1000          # the name's loudest frame lies this far before the end of the history …
PEAK_UNTIL_MS = 330            # … and no later than this (the request's first word may be louder and starts 0.22 s before the end)
LAST_PAUSE_MS = 180            # the pause behind the name starts before this mark (the name ends 0.2-0.4 s before the history's end); a later pause is a dip inside the request's first word
MAX_NAME_MS = 900              # a longer burst is a name with the request run into it
ALONE_MS = 400                 # the name burst has nothing before it for this long (not the speaker's other words, not the burst's own earlier part)
NAME_PREROLL_MS = 200          # the speaker check hears the name with the preroll the Endpoint gave an utterance before the cut existed
CLICK_MS = 120                 # every stream starts with a click this long: it is not speech
BRIDGE_FRAMES = 5              # a dip this short (100 ms) is inside a word, not a pause: the name's «ма» and «лисан» are one burst
MIN_PAUSE_FRAMES = 3           # 60 ms of quiet: the shortest pause behind the name in the real sessions is 80 ms
GUARD_FRAMES = 2               # the cut sits this far into the pause (40 ms of the name's decay stay out of the recognizer, the request's soft onset stays in)
MIN_PEAK = 8.0                 # RMS units of the station's uplink (a quiet room is 1-6, the name 8-40)
NAME_OVER_FLOOR = 2.5
QUIET_OF_PEAK = 0.30
QUIET_CAP_OF_PEAK = 0.60


class Located(NamedTuple):
    end_ms: int | None     # stream position where what the recognizer hears starts; None: not cut
    reason: str            # why not ("" when cut)
    voiced_ms: int = 0     # the name's burst, for the archive row of the removed audio
    start_ms: int = 0      # where the name's burst starts


def history_ms(audio_ms: int, elapsed_ms: int) -> int | None:
    """Audio that arrived ahead of real time, when that looks like the station's history; else None."""
    lead = audio_ms - elapsed_ms
    return lead if MIN_HISTORY_MS <= lead <= MAX_HISTORY_MS else None


def frame_levels(pcm: bytes, rate: int) -> list[float]:
    """RMS of every whole 20 ms frame of 16-bit mono audio."""
    size = rate // 1000 * FRAME_MS
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    return [math.sqrt(sum(s * s for s in samples[i:i + size]) / size) for i in range(0, len(samples) - size + 1, size)]


def locate(levels: list[float], history: int) -> Located:
    """The cut that removes the name from the head of a history of `history` ms with these frame levels."""
    boundary = history // FRAME_MS
    lo = max(0, (history - PEAK_BEFORE_MS) // FRAME_MS)
    hi = min(len(levels), (history - PEAK_UNTIL_MS) // FRAME_MS + 1)
    if hi - lo < 5 or len(levels) < boundary - 5:
        return Located(None, "short")
    body = sorted(levels[10:boundary])
    floor = body[len(body) // 4] if body else 0.0
    window = levels[lo:hi]
    peak = max(window)
    if peak < max(MIN_PEAK, NAME_OVER_FLOOR * floor):
        return Located(None, "no_name")
    at = lo + window.index(peak)
    quiet = min(QUIET_CAP_OF_PEAK * peak, max(QUIET_OF_PEAK * peak, 1.5 * floor + 1.0))
    start = at
    gap = 0
    for i in range(at - 1, -1, -1):
        if levels[i] > quiet:
            start, gap = i, 0
        else:
            gap += 1
            if gap > BRIDGE_FRAMES:
                break
    if any(v > quiet for v in levels[max(CLICK_MS // FRAME_MS, start - ALONE_MS // FRAME_MS):start]):
        return Located(None, "not_alone")
    last = (history - LAST_PAUSE_MS) // FRAME_MS
    index = at
    while index <= last:
        if levels[index] <= quiet:
            end = index
            while end + 1 < len(levels) and levels[end + 1] <= quiet:
                end += 1
            if end - index + 1 >= MIN_PAUSE_FRAMES or end + 1 >= len(levels):
                if (index - start) * FRAME_MS > MAX_NAME_MS:
                    return Located(None, "long")
                cut = index + min(GUARD_FRAMES, (end - index + 1) // 2)
                return Located(cut * FRAME_MS, "", sum(1 for v in levels[start:index] if v > quiet) * FRAME_MS, start * FRAME_MS)
            index = end + 1
        else:
            index += 1
    return Located(None, "no_pause")


class Gate:
    """The first audio of a stream, held until the burst is over, then handed on without the name.

    `push(chunk, elapsed_ms)` takes audio as it arrives (`elapsed_ms`: time since the stream opened) and returns the audio to hand
    on (nothing while the decision is pending, then everything that is kept, then each chunk as it comes). `release(elapsed_ms)`
    decides what can be decided and returns what is held (the stream ended or went quiet). `removed` is the cut audio, `located` the
    verdict, once there is one."""

    def __init__(self, rate: int, enabled: bool = True):
        self.rate = rate
        self.removed: bytes | None = None
        self.name: bytes = b""     # the name's burst and the preroll before it (not the rest of the history)
        self.located: Located | None = None
        self.history: int | None = None
        self.decided = not enabled
        self.held = bytearray()

    def audio_ms(self) -> int:
        return len(self.held) * 1000 // (2 * self.rate)

    def push(self, chunk: bytes, elapsed_ms: int) -> bytes:
        if self.decided:
            return chunk
        self.held.extend(chunk)
        if elapsed_ms >= DECIDE_AFTER_MS or self.audio_ms() - elapsed_ms > MAX_HISTORY_MS:   # the latter: more audio ahead of the clock than any history holds, nothing to wait for
            return self.decide(elapsed_ms)
        return b""

    def release(self, elapsed_ms: int) -> bytes:
        if self.decided:
            return b""
        return self.decide(elapsed_ms) if elapsed_ms >= DECIDE_AFTER_MS else self.pass_all()

    def pass_all(self) -> bytes:
        self.decided = True
        out = bytes(self.held)
        self.held = bytearray()
        return out

    def decide(self, elapsed_ms: int) -> bytes:
        lead = self.audio_ms() - elapsed_ms
        if lead > MAX_HISTORY_MS:
            # Live speech is inside the burst: where the history ends is unknown, and a window laid on a guess sat on the request's
            # first word (2026-11-20 11:18: its stressed vowel was taken for the name and the cut removed the word). Nothing is cut.
            self.history, self.located = lead, Located(None, "burst")
            return self.pass_all()
        history = self.history = history_ms(self.audio_ms(), elapsed_ms)
        if history is None:
            return self.pass_all()
        size = self.rate // 1000 * FRAME_MS
        upto = (history + LOOKAHEAD_MS) // FRAME_MS * size * 2
        self.located = locate(frame_levels(bytes(self.held[:upto]), self.rate), history)
        if self.located.end_ms is None:
            return self.pass_all()
        cut = self.located.end_ms * self.rate // 1000 * 2
        self.decided = True
        self.removed = bytes(self.held[:cut])
        self.name = bytes(self.held[max(0, self.located.start_ms - NAME_PREROLL_MS) * self.rate // 1000 * 2:cut])
        out = bytes(self.held[cut:])
        self.held = bytearray()
        return out
