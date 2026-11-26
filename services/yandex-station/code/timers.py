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
KIND_REMINDER = "reminder"   # a timer with a note: «отмени таймер» must not cancel it
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


def duration_accusative(seconds: int) -> str:
    """«1 минуту», «21 минуту», «5 минут»: a duration after «на» / «через»."""
    return " ".join(f"{n} {unit[3] if _plural_index(n) == 0 else unit[_plural_index(n)]}" for n, unit in _parts(seconds))


def due_text(note: str | None, seconds: int | None) -> str:
    if note:
        return REMINDER_DUE.format(note=note)
    if seconds:
        return TIMER_DUE_FOR.format(duration=duration_accusative(int(seconds)))
    return TIMER_DUE


def _alarm_hour_candidates(hour: int, ambiguous: bool) -> list[int]:
    """The 24-hour values to try. The grammar resolves the hour with its day part («7 вечера» arrives as 19); only a bare
    12-hour reading (`ambiguous`: «будильник на семь») is left open between the morning and the evening hour."""
    if ambiguous and 1 <= hour <= 12:
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
        entry = {"id": uuid.uuid4().hex[:8], "kind": KIND_REMINDER if note else KIND_TIMER, "due": self._clock() + seconds,
                 "seconds": int(seconds), "note": note or None}
        self._add(entry)
        return {"id": entry["id"], "due": entry["due"], "seconds": entry["seconds"], "note": entry["note"]}

    async def set_alarm(self, hour: int, minute: int, part: str | None, ambiguous: bool) -> dict:
        """`hour` is the 24-hour value the grammar resolved: it already carries the day part `part` it was said with.
        `ambiguous` marks a bare 12-hour reading that may mean the morning or the evening hour: the next of the two rings."""
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time out of range")
        now = self._clock()
        today = datetime.fromtimestamp(now, self._tz).date()
        due: float | None = None
        for h in _alarm_hour_candidates(hour, ambiguous):
            # Today, tomorrow and the day after, both occurrences of a wall-clock time the clock passes twice (the DST
            # fall-back hour): the nearest real moment after now wins. A time the clock skips (the spring-forward gap)
            # does not exist, so it is dropped: set the evening before, the alarm falls on the day after the gap day.
            for day in (0, 1, 2):
                date = today + timedelta(days=day)
                for fold in (0, 1):
                    moment = datetime(date.year, date.month, date.day, h, minute, tzinfo=self._tz, fold=fold).timestamp()
                    local = datetime.fromtimestamp(moment, self._tz)
                    if moment > now and (local.hour, local.minute) == (h, minute) and (due is None or moment < due):
                        due = moment
        if due is None:
            raise ValueError("no such time")
        local = datetime.fromtimestamp(due, self._tz)
        text = f"{local.hour:02d}:{local.minute:02d}"
        entry = {"id": uuid.uuid4().hex[:8], "kind": KIND_ALARM, "due": due, "seconds": None, "note": None, "text": text}
        self._add(entry)
        return {"id": entry["id"], "due": entry["due"], "text": text}

    async def cancel(self, kind: str = KIND_TIMER) -> int:
        ids = [i for i, e in self._entries.items() if e["kind"] == kind]
        removed = {i: self._entries.pop(i) for i in ids}
        if ids:
            try:
                self._persist()
            except Exception:
                self._entries.update(removed)   # a cancel that was not saved did not happen: the file would bring them back
                raise
        for i in ids:
            task = self._tasks.pop(i, None)
            if task:
                task.cancel()
        return len(ids)

    def remaining(self) -> list[dict]:
        """Timers and reminders, the nearest first."""
        now = self._clock()
        left = [{"id": e["id"], "left": max(0, int(round(e["due"] - now))), "note": e["note"]}
                for e in self._entries.values() if e["kind"] in (KIND_TIMER, KIND_REMINDER)]
        return sorted(left, key=lambda item: item["left"])

    async def resume(self) -> None:
        """Re-arm what the file holds. Never raises: a timers file that cannot be written must not stop the adapter."""
        for entry in self._load():
            self._entries[entry["id"]] = entry
        now = self._clock()
        stale = [i for i, e in self._entries.items() if e["due"] < now - LATE_FIRE_WINDOW_S]
        for i in stale:
            self._entries.pop(i)
        if stale:
            try:
                self._persist()
            except OSError as err:
                log.warning("timers file not writable, expired entries stay in it: %s", type(err).__name__)
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
        try:
            self._persist()
        except Exception:
            del self._entries[entry["id"]]   # a timer nobody was told about must not linger, count or come back from the file
            raise
        self._arm(entry)

    def _arm(self, entry: dict[str, Any]) -> None:
        self._tasks[entry["id"]] = asyncio.get_running_loop().create_task(self._run(entry["id"]))

    async def _run(self, entry_id: str) -> None:
        try:
            entry = self._entries.get(entry_id)
            if entry is None:
                return
            delay = entry["due"] - self._clock()
            if delay > 0:
                await self._sleep(delay)
            if self._entries.pop(entry_id, None) is None:
                return
            try:
                self._persist()
            except OSError as err:   # a full disk must not silence the alarm: at worst it rings once more after a restart
                log.warning("timers file not writable at fire time: %s", type(err).__name__)
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
        finally:
            if self._tasks.get(entry_id) is asyncio.current_task():   # registered until the announcement is over: close() cancels it
                del self._tasks[entry_id]

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
                    and e.get("kind") in (KIND_TIMER, KIND_REMINDER, KIND_ALARM):
                if e["kind"] == KIND_TIMER and e.get("note"):
                    e = {**e, "kind": KIND_REMINDER}   # written before reminders had their own kind
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
    """Speaks through Home Assistant `tts.speak`; only Home Assistant's own acceptance counts as sent (an unreachable Home Assistant is False)."""

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
        return status in (200, 201)
