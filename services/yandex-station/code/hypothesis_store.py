"""The latest recognition evidence (NBest) per station, held for a few seconds.

The STT gateway publishes what a second recognizer thought of an utterance (second_opinion.py); the adapter keeps it here so the
quasar hook (`/intent/decide`, which receives only a phrase and no station) and the correction dialog can ask "what did the audio
behind this text say?" by the phrase they were given. Plain data in, plain data out: the clock is the caller's `now`, the phrase
comparison is stt-cli's `same_text` (imported on first use: a venv without the core refuses evidence instead of failing the adapter).

Station key: the gateway's websocket names its station (x-aisis-station, set by the quasar fork); a client that names none (an older
quasar image) is stored under None (`quasar` is the same "unknown").
A lookup for a named station finds its own entries and the unknown-station ones; a lookup with station None looks across all.
"""
from __future__ import annotations

from typing import Callable

TTL_S = 15.0
MAX_ENTRIES = 64
UNKNOWN_STATIONS = (None, "quasar")


def _core_same_text() -> Callable[[str, str], bool]:
    from stt_cli.hypotheses.model import same_text
    return same_text


class HypothesisStore:
    def __init__(self, same_text: Callable[[str, str], bool] | None = None,
                 load_same_text: Callable[[], Callable[[str, str], bool]] = _core_same_text,
                 ttl: float = TTL_S, capacity: int = MAX_ENTRIES):
        self._same_text = same_text
        self._load = load_same_text
        self._ttl = ttl
        self._capacity = capacity
        self._unavailable = False
        self._entries: list[dict] = []   # oldest first
        self._counts = {"puts": 0, "hits": 0, "misses": 0, "evicted": 0, "expired": 0, "refused": 0}

    def _compare(self) -> Callable[[str, str], bool] | None:
        if self._same_text is None and not self._unavailable:
            try:
                self._same_text = self._load()
            except Exception:  # core not installed (or broken): evidence is refused, never an adapter failure
                self._unavailable = True
        return self._same_text

    def _expire(self, now: float) -> None:
        live = [entry for entry in self._entries if 0 <= now - entry["at"] <= self._ttl]
        self._counts["expired"] += len(self._entries) - len(live)
        self._entries = live

    def put(self, station: str | None, primary_text: str, nbest: dict, *, source: str, ms: int | None, now: float) -> bool:
        """Keep the evidence for `primary_text`; a newer one for the same phrase of the same station replaces it. False when refused."""
        same = self._compare()
        if same is None or not isinstance(primary_text, str) or not primary_text.strip():
            self._counts["refused"] += 1
            return False
        self._expire(now)
        self._entries = [entry for entry in self._entries if not (entry["station"] == station and same(entry["text"], primary_text))]
        self._entries.append({"station": station, "text": primary_text, "source": source, "nbest": nbest, "ms": ms, "at": now})
        overflow = len(self._entries) - self._capacity
        if overflow > 0:
            del self._entries[:overflow]
            self._counts["evicted"] += overflow
        self._counts["puts"] += 1
        return True

    def get(self, station: str | None, text: str, now: float) -> dict | None:
        """The newest live entry whose primary text is `text` (same_text) for this station, or None."""
        same = self._compare()
        self._expire(now)
        if same is not None and isinstance(text, str) and text.strip():
            for entry in reversed(self._entries):
                if (station is None or entry["station"] == station or entry["station"] in UNKNOWN_STATIONS) and same(entry["text"], text):
                    self._counts["hits"] += 1
                    return dict(entry)
        self._counts["misses"] += 1
        return None

    def stats(self) -> dict:
        return {"available": not self._unavailable, "entries": len(self._entries), "ttl_s": self._ttl, "capacity": self._capacity, **self._counts}
