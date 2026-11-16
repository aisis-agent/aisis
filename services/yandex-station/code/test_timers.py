import asyncio
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone

import timers
from timers import HaAnnouncer, TimerBook, due_text, left_text

UTC = timezone.utc


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSleep:
    """Sleeps block until the test releases them; releasing advances the fake clock by the requested delay."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.pending: list[tuple[float, asyncio.Future]] = []
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self.delays.append(delay)
        self.pending.append((delay, fut))
        await fut

    async def release(self) -> None:
        pending, self.pending = self.pending, []
        for delay, fut in pending:
            self.clock.t += delay
            fut.set_result(None)
        await settle()


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def ts(y, mo, d, h, mi) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


class Announcer:
    def __init__(self, results=None) -> None:
        self.texts: list[str] = []
        self.results = list(results or [])

    async def __call__(self, text: str) -> bool:
        self.texts.append(text)
        return self.results.pop(0) if self.results else True


class BookCase(unittest.IsolatedAsyncioTestCase):
    def make(self, path=None, now=ts(2026, 11, 16, 12, 0), results=None):
        self.clock = Clock(now)
        self.sleep = FakeSleep(self.clock)
        self.ann = Announcer(results)
        book = TimerBook(path, self.ann, clock=self.clock, tz=UTC, sleep=self.sleep)
        self.addAsyncCleanup(book.close)
        return book


class TimerFlow(BookCase):
    async def test_start_remaining_fire(self):
        book = self.make()
        info = await book.start(300)
        self.assertEqual(info["seconds"], 300)
        self.assertEqual(info["due"], self.clock.t + 300)
        await settle()
        self.clock.t += 100
        self.assertEqual(book.remaining(), [{"id": info["id"], "left": 200, "note": None}])
        self.clock.t -= 100
        await self.sleep.release()
        self.assertEqual(self.ann.texts, ["Таймер на 5 минут сработал."])
        self.assertEqual(book.remaining(), [])

    async def test_note_text(self):
        book = self.make()
        await book.start(60, "выключить плиту")
        await settle()
        await self.sleep.release()
        self.assertEqual(self.ann.texts, ["Напоминаю: выключить плиту."])

    async def test_cancel_counts_by_kind(self):
        book = self.make()
        await book.start(60)
        await book.start(120)
        await book.set_alarm(7, 30, None, True)
        self.assertEqual(await book.cancel(), 2)
        self.assertEqual(book.remaining(), [])
        self.assertEqual(await book.cancel("alarm"), 1)
        self.assertEqual(await book.cancel(), 0)
        await settle()
        await self.sleep.release()
        self.assertEqual(self.ann.texts, [])

    async def test_limits(self):
        book = self.make()
        for bad in (0, -5, 86401):
            with self.assertRaises(ValueError):
                await book.start(bad)
        await book.start(86400)
        for _ in range(19):
            await book.start(10)
        with self.assertRaises(ValueError):
            await book.start(10)

    async def test_retry_then_success(self):
        book = self.make(results=[False, False, True])
        await book.start(5)
        await settle()
        await self.sleep.release()
        await self.sleep.release()
        await self.sleep.release()
        self.assertEqual(len(self.ann.texts), 3)
        self.assertEqual(self.sleep.delays[1:], [20, 20])

    async def test_retry_gives_up_and_warns(self):
        book = self.make(results=[False] * 5)
        await book.start(5)
        await settle()
        with self.assertLogs("timers", "WARNING") as cm:
            for _ in range(4):
                await self.sleep.release()
        self.assertEqual(len(self.ann.texts), 3)
        self.assertNotIn("Таймер", "".join(cm.output))
        self.assertEqual(self.sleep.pending, [])

    async def test_fire_removes_before_announce(self):
        seen = {}

        async def announce(text):
            seen["left"] = book.remaining()
            return True

        clock = Clock(1000.0)
        sleep = FakeSleep(clock)
        book = TimerBook(None, announce, clock=clock, tz=UTC, sleep=sleep)
        self.addAsyncCleanup(book.close)
        await book.start(5)
        await settle()
        await sleep.release()
        self.assertEqual(seen["left"], [])


class AlarmCase(BookCase):
    async def alarm(self, now, hour, minute, part, ambiguous):
        book = self.make(now=now)
        info = await book.set_alarm(hour, minute, part, ambiguous)
        return info

    async def test_ambiguous_picks_next_of_am_pm(self):
        info = await self.alarm(ts(2026, 11, 16, 6, 0), 7, 30, None, True)
        self.assertEqual(info["due"], ts(2026, 11, 16, 7, 30))
        self.assertEqual(info["text"], "07:30")
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 7, 30, None, True)
        self.assertEqual(info["due"], ts(2026, 11, 16, 19, 30))
        self.assertEqual(info["text"], "19:30")
        info = await self.alarm(ts(2026, 11, 16, 20, 0), 7, 30, None, True)
        self.assertEqual(info["due"], ts(2026, 11, 17, 7, 30))

    async def test_explicit_part(self):
        info = await self.alarm(ts(2026, 11, 16, 6, 0), 7, 30, "вечера", True)
        self.assertEqual(info["due"], ts(2026, 11, 16, 19, 30))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 7, 30, "утра", True)
        self.assertEqual(info["due"], ts(2026, 11, 17, 7, 30))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 12, 0, "ночи", False)
        self.assertEqual(info["due"], ts(2026, 11, 17, 0, 0))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 12, 0, "дня", False)
        self.assertEqual(info["due"], ts(2026, 11, 16, 12, 0))

    async def test_unambiguous_and_24h(self):
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 7, 30, None, False)
        self.assertEqual(info["due"], ts(2026, 11, 17, 7, 30))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 18, 5, None, True)
        self.assertEqual(info["due"], ts(2026, 11, 16, 18, 5))

    async def test_exact_now_goes_to_next_day(self):
        info = await self.alarm(ts(2026, 11, 16, 7, 30), 7, 30, None, False)
        self.assertEqual(info["due"], ts(2026, 11, 17, 7, 30))

    async def test_alarm_fires_with_text(self):
        book = self.make(now=ts(2026, 11, 16, 6, 0))
        await book.set_alarm(7, 30, None, True)
        await settle()
        await self.sleep.release()
        self.assertEqual(self.ann.texts, ["Будильник. Уже 07:30."])


class Persistence(BookCase):
    async def test_round_trip_and_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "timers.json")
            book = self.make(path)
            info = await book.start(300, "чай")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
            await book.close()
            self.clock.t += 100
            book2 = TimerBook(path, self.ann, clock=self.clock, tz=UTC, sleep=self.sleep)
            self.addAsyncCleanup(book2.close)
            await book2.resume()
            self.assertEqual(book2.remaining(), [{"id": info["id"], "left": 200, "note": "чай"}])
            await settle()
            await self.sleep.release()
            self.assertEqual(self.ann.texts, ["Напоминаю: чай."])
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), [])

    async def test_late_within_window_fires_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.json")
            book = self.make(path)
            await book.start(60)
            await book.close()
            self.clock.t += 60 + 300
            book2 = TimerBook(path, self.ann, clock=self.clock, tz=UTC, sleep=self.sleep)
            self.addAsyncCleanup(book2.close)
            await book2.resume()
            await settle()
            self.assertEqual(self.ann.texts, ["Таймер на 1 минуту сработал."])
            book3 = TimerBook(path, self.ann, clock=self.clock, tz=UTC, sleep=self.sleep)
            self.addAsyncCleanup(book3.close)
            await book3.resume()
            await settle()
            self.assertEqual(len(self.ann.texts), 1)

    async def test_very_late_dropped_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.json")
            book = self.make(path)
            await book.start(60)
            await book.close()
            self.clock.t += 60 + 601
            book2 = TimerBook(path, self.ann, clock=self.clock, tz=UTC, sleep=self.sleep)
            self.addAsyncCleanup(book2.close)
            await book2.resume()
            await settle()
            self.assertEqual(self.ann.texts, [])
            self.assertEqual(book2.remaining(), [])

    async def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            book = self.make(path)
            with self.assertLogs("timers", "WARNING"):
                await book.resume()
            self.assertEqual(book.remaining(), [])


class TextHelpers(unittest.TestCase):
    def test_left_text(self):
        cases = {
            270: "осталось 4 минуты 30 секунд",
            60: "осталась 1 минута",
            1: "осталась 1 секунда",
            3600: "остался 1 час",
            7260: "осталось 2 часа 1 минута",
            3661: "остался 1 час 1 минута 1 секунда",
            300: "осталось 5 минут",
            21 * 60: "осталась 21 минута",
            11 * 60: "осталось 11 минут",
            12 * 60: "осталось 12 минут",
            22 * 60 + 2: "осталось 22 минуты 2 секунды",
            45: "осталось 45 секунд",
            5 * 3600: "осталось 5 часов",
            0: "осталось меньше секунды",
        }
        for seconds, expected in cases.items():
            self.assertEqual(left_text(seconds), expected, seconds)

    def test_due_text(self):
        self.assertEqual(due_text(None, 300), "Таймер на 5 минут сработал.")
        self.assertEqual(due_text(None, 60), "Таймер на 1 минуту сработал.")
        self.assertEqual(due_text(None, 90), "Таймер на 1 минуту 30 секунд сработал.")
        self.assertEqual(due_text(None, 3600), "Таймер на 1 час сработал.")
        self.assertEqual(due_text(None, 1), "Таймер на 1 секунду сработал.")
        self.assertEqual(due_text(None, 120), "Таймер на 2 минуты сработал.")
        self.assertEqual(due_text("выключить плиту", 300), "Напоминаю: выключить плиту.")
        self.assertEqual(due_text(None, None), "Таймер сработал.")


class AnnouncerCase(unittest.IsolatedAsyncioTestCase):
    async def run_status(self, status):
        calls = []

        async def post(path, body):
            calls.append((path, body))
            if isinstance(status, Exception):
                raise status
            return status

        ok = await HaAnnouncer(post, "tts.yandex", "media_player.station")("привет")
        return ok, calls

    async def test_statuses(self):
        for status, expected in ((200, True), (201, True), (202, True), (400, False), (500, False)):
            ok, _ = await self.run_status(status)
            self.assertEqual(ok, expected, status)

    async def test_request_shape(self):
        _, calls = await self.run_status(200)
        self.assertEqual(calls, [("/api/services/tts/speak", {
            "entity_id": "tts.yandex", "media_player_entity_id": "media_player.station", "message": "привет"})])

    async def test_exception_is_false(self):
        ok, _ = await self.run_status(OSError("down"))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
