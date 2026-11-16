"""Voice timers and alarms for the Yandex-Station adapter.

Timers live in memory and, when a path is given, in a small JSON file so that a restart of the adapter does not lose
them. A timer is removed and persisted BEFORE it is announced: a crash between the two loses one announcement instead
of repeating it after every restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timedelta, tzinfo
from typing import Any, Awaitable, Callable

log = logging.getLogger("timers")

MAX_TIMERS = 20
MAX_SECONDS = 86400
# A timer that expired while the adapter was down is still worth announcing only if it is fresh.
LATE_FIRE_WINDOW_S = 600
ANNOUNCE_ATTEMPTS = 3
ANNOUNCE_RETRY_S = 20

KIND_TIMER = "timer"
KIND_ALARM = "alarm"

LEFT_VERB_PLURAL = "осталось"
LEFT_VERB_FEM = "осталась"
LEFT_VERB_MASC = "остался"
LEFT_LESS_THAN_SECOND = "меньше секунды"
TIMER_DUE = "Таймер сработал."
TIMER_DUE_FOR = "Таймер на {duration} сработал."
REMINDER_DUE = "Напоминаю: {note}."
ALARM_DUE = "Будильник. Уже {text}."

# (nominative forms for 1 / 2-4 / 5+, accusative for 1, grammatical gender of the unit)
_HOUR = ("час", "часа", "часов", "час", "m")
_MINUTE = ("минута", "минуты", "минут", "минуту", "f")
_SECOND = ("секунда", "секунды", "секунд", "секунду", "f")


def _plural_index(n: int) -> int:
    if n % 10 == 1 and n % 100 != 11:
        return 0
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 1
    return 2


def _parts(seconds: int) -> list[tuple[int, tuple[str, str, str, str, str]]]:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return [(n, unit) for n, unit in ((hours, _HOUR), (minutes, _MINUTE), (secs, _SECOND)) if n]


def left_text(seconds: int) -> str:
    parts = _parts(max(0, int(seconds)))
    if not parts:
        return f"{LEFT_VERB_PLURAL} {LEFT_LESS_THAN_SECOND}"
    first_n, first_unit = parts[0]
    if _plural_index(first_n) == 0:
        verb = LEFT_VERB_FEM if first_unit[4] == "f" else LEFT_VERB_MASC
    else:
        verb = LEFT_VERB_PLURAL
    body = " ".join(f"{n} {unit[_plural_index(n)]}" for n, unit in parts)
    return f"{verb} {body}"


def _duration_accusative(seconds: int) -> str:
    return " ".join(f"{n} {unit[3] if _plural_index(n) == 0 else unit[_plural_index(n)]}" for n, unit in _parts(seconds))


def due_text(note: str | None, seconds: int | None) -> str:
    if note:
        return REMINDER_DUE.format(note=note)
    if seconds:
        return TIMER_DUE_FOR.format(duration=_duration_accusative(int(seconds)))
    return TIMER_DUE


def _alarm_hour_candidates(hour: int, part: str | None, ambiguous: bool) -> list[int]:
    if hour > 12 or hour == 0:
        return [hour % 24]
    if part in ("утра", "ночи"):
        return [hour % 12]
    if part in ("дня", "вечера"):
        return [hour % 12 + 12]
    if ambiguous:
        return [hour % 12, hour % 12 + 12]
    return [hour]


class TimerBook:
    def __init__(self, path: str | None, announce: Callable[[str], Awaitable[bool]],
                 clock: Callable[[], float] = time.time, tz: tzinfo | None = None,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self._path = path
        self._announce = announce
        self._clock = clock
        self._tz = tz
        self._sleep = sleep
        self._entries: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # -- public ---------------------------------------------------------------------------------------------------

    async def start(self, seconds: int, note: str | None = None) -> dict:
        if not 1 <= seconds <= MAX_SECONDS:
            raise ValueError("seconds out of range")
        entry = {"id": uuid.uuid4().hex[:8], "kind": KIND_TIMER, "due": self._clock() + seconds,
                 "seconds": int(seconds), "note": note or None}
        self._add(entry)
        return {"id": entry["id"], "due": entry["due"], "seconds": entry["seconds"], "note": entry["note"]}

    async def set_alarm(self, hour: int, minute: int, part: str | None, ambiguous: bool) -> dict:
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time out of range")
        now = datetime.fromtimestamp(self._clock(), self._tz)
        best: datetime | None = None
        for h in _alarm_hour_candidates(hour, part, ambiguous):
            cand = now.replace(hour=h, minute=minute, second=0, microsecond=0)
            if cand <= now:
                cand += timedelta(days=1)
            if best is None or cand < best:
                best = cand
        assert best is not None
        text = f"{best.hour:02d}:{best.minute:02d}"
        entry = {"id": uuid.uuid4().hex[:8], "kind": KIND_ALARM, "due": best.timestamp(), "seconds": None,
                 "note": None, "text": text}
        self._add(entry)
        return {"id": entry["id"], "due": entry["due"], "text": text}

    async def cancel(self, kind: str = KIND_TIMER) -> int:
        ids = [i for i, e in self._entries.items() if e["kind"] == kind]
        for i in ids:
            self._entries.pop(i, None)
            task = self._tasks.pop(i, None)
            if task:
                task.cancel()
        if ids:
            self._persist()
        return len(ids)

    def remaining(self) -> list[dict]:
        now = self._clock()
        return [{"id": e["id"], "left": max(0, int(round(e["due"] - now))), "note": e["note"]}
                for e in self._entries.values() if e["kind"] == KIND_TIMER]

    async def resume(self) -> None:
        for entry in self._load():
            self._entries[entry["id"]] = entry
        now = self._clock()
        for entry in list(self._entries.values()):
            if entry["due"] < now - LATE_FIRE_WINDOW_S:
                self._entries.pop(entry["id"])
        self._persist()
        for entry in self._entries.values():
            self._arm(entry)

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals ------------------------------------------------------------------------------------------------

    def _add(self, entry: dict[str, Any]) -> None:
        if len(self._entries) >= MAX_TIMERS:
            raise ValueError("too many timers")
        self._entries[entry["id"]] = entry
        self._persist()
        self._arm(entry)

    def _arm(self, entry: dict[str, Any]) -> None:
        self._tasks[entry["id"]] = asyncio.get_running_loop().create_task(self._run(entry["id"]))

    async def _run(self, entry_id: str) -> None:
        entry = self._entries.get(entry_id)
        if entry is None:
            return
        delay = entry["due"] - self._clock()
        if delay > 0:
            await self._sleep(delay)
        if self._entries.pop(entry_id, None) is None:
            return
        self._tasks.pop(entry_id, None)
        self._persist()
        if entry["kind"] == KIND_ALARM:
            text = ALARM_DUE.format(text=entry.get("text", ""))
        else:
            text = due_text(entry.get("note"), entry.get("seconds"))
        for attempt in range(ANNOUNCE_ATTEMPTS):
            try:
                if await self._announce(text):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if attempt < ANNOUNCE_ATTEMPTS - 1:
                await self._sleep(ANNOUNCE_RETRY_S)
        log.warning("timer %s announcement failed after %d attempts", entry_id, ANNOUNCE_ATTEMPTS)

    def _load(self) -> list[dict[str, Any]]:
        if not self._path or not os.path.exists(self._path):
            return []
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            log.warning("timers file unreadable, starting empty")
            return []
        out = []
        for e in data if isinstance(data, list) else []:
            if isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("due"), (int, float)) \
                    and e.get("kind") in (KIND_TIMER, KIND_ALARM):
                out.append(e)
        return out[:MAX_TIMERS]

    def _persist(self) -> None:
        if not self._path:
            return
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".timers-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(list(self._entries.values()), fh, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class HaAnnouncer:
    """Speaks through Home Assistant `tts.speak`. 202 = hacli queued the call for HA recovery, which counts as sent."""

    def __init__(self, post: Callable[[str, dict], Awaitable[int]], tts_entity: str, player_entity: str) -> None:
        self._post = post
        self._tts = tts_entity
        self._player = player_entity

    async def __call__(self, text: str) -> bool:
        body = {"entity_id": self._tts, "media_player_entity_id": self._player, "message": text}
        try:
            status = await self._post("/api/services/tts/speak", body)
        except Exception:
            return False
        return status in (200, 201, 202)
