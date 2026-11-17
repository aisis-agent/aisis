import asyncio
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import timers
from intent_route import load_grammar
from timers import HaAnnouncer, TimerBook, due_text, duration_accusative, left_text

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
    def make(self, path=None, now=ts(2026, 11, 16, 12, 0), results=None, tz=UTC):
        self.clock = Clock(now)
        self.sleep = FakeSleep(self.clock)
        self.ann = Announcer(results)
        book = TimerBook(path, self.ann, clock=self.clock, tz=tz, sleep=self.sleep)
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
    async def alarm(self, now, hour, minute, part, ambiguous, tz=UTC):
        book = self.make(now=now, tz=tz)
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
        """What the grammar sends: the 24-hour value with its day part already applied, `ambiguous` only for a bare 12-hour reading."""
        info = await self.alarm(ts(2026, 11, 16, 6, 0), 19, 30, "evening", False)       # «семь тридцать вечера»
        self.assertEqual(info["due"], ts(2026, 11, 16, 19, 30))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 7, 30, "morning", False)        # «семь тридцать утра»
        self.assertEqual(info["due"], ts(2026, 11, 17, 7, 30))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 0, 0, "night", False)           # «двенадцать ночи»
        self.assertEqual(info["due"], ts(2026, 11, 17, 0, 0))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 12, 0, "day", False)            # «двенадцать дня»
        self.assertEqual(info["due"], ts(2026, 11, 16, 12, 0))
        info = await self.alarm(ts(2026, 11, 16, 8, 0), 12, 30, None, True)             # «половина первого»
        self.assertEqual(info["due"], ts(2026, 11, 16, 12, 30))

    async def test_alarm_from_the_grammar_end_to_end(self):
        grammar = load_grammar()
        for phrase, now, due in (("поставь будильник на семь вечера", ts(2026, 11, 16, 6, 0), ts(2026, 11, 16, 19, 0)),
                                 ("поставь будильник на семь утра", ts(2026, 11, 16, 8, 0), ts(2026, 11, 17, 7, 0)),
                                 ("поставь будильник на половину первого", ts(2026, 11, 16, 8, 0), ts(2026, 11, 16, 12, 30))):
            slot = grammar.decide(phrase).action["args"]["time"]
            info = await self.alarm(now, slot["hour"], slot["minute"], slot.get("part"), bool(slot.get("ambiguous")))
            self.assertEqual(info["due"], due, phrase)

    async def test_the_repeated_hour_of_a_dst_fall_back_rings_at_its_nearest_occurrence(self):
        belgrade = ZoneInfo("Europe/Belgrade")
        # 2026-10-25 02:45 CEST: the clock goes back to 02:00 CET at 03:00 CEST, so 02:30 comes round again in 45 minutes
        now = datetime(2026, 10, 25, 2, 45, tzinfo=belgrade, fold=0).timestamp()
        info = await self.alarm(now, 2, 30, None, False, tz=belgrade)
        self.assertEqual(info["due"], now + 45 * 60)
        self.assertEqual(info["text"], "02:30")
        # the second pass of 02:30 has begun: the next one is the next day's
        now = datetime(2026, 10, 25, 2, 45, tzinfo=belgrade, fold=1).timestamp()
        info = await self.alarm(now, 2, 30, None, False, tz=belgrade)
        self.assertEqual(info["due"], datetime(2026, 10, 26, 2, 30, tzinfo=belgrade).timestamp())
        # a bare «два тридцать» may also mean 14:30: the nearer of the readings rings
        now = datetime(2026, 10, 25, 2, 45, tzinfo=belgrade, fold=0).timestamp()
        info = await self.alarm(now, 2, 30, None, True, tz=belgrade)
        self.assertEqual(info["due"], now + 45 * 60)

    async def test_a_wall_clock_time_the_spring_forward_skips_falls_on_the_next_day(self):
        belgrade = ZoneInfo("Europe/Belgrade")
        now = datetime(2026, 3, 29, 1, 30, tzinfo=belgrade).timestamp()       # 02:00 -> 03:00 that night: 02:30 does not exist
        info = await self.alarm(now, 2, 30, None, False, tz=belgrade)
        self.assertEqual(info["due"], datetime(2026, 3, 30, 2, 30, tzinfo=belgrade).timestamp())

    async def test_an_alarm_set_the_day_before_a_spring_forward_gap_skips_the_gap_day(self):
        belgrade = ZoneInfo("Europe/Belgrade")
        for now, gap_day_after in ((datetime(2027, 3, 27, 23, 0, tzinfo=belgrade), (2027, 3, 29)),    # Saturday night, clocks jump on Sunday
                                   (datetime(2026, 3, 28, 3, 0, tzinfo=belgrade), (2026, 3, 30))):
            info = await self.alarm(now.timestamp(), 2, 30, "night", False, tz=belgrade)               # «два тридцать ночи»
            self.assertEqual(info["due"], datetime(*gap_day_after, 2, 30, tzinfo=belgrade).timestamp(), now)
            self.assertEqual(info["text"], "02:30")

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


class DiskFailures(BookCase):
    """A timers file that cannot be written must neither silence a timer, leave a ghost behind, nor stop the adapter."""

    async def test_a_failed_write_at_fire_time_still_announces(self):
        book = self.make()
        await book.start(5)
        await settle()
        with mock.patch.object(book, "_persist", side_effect=OSError("disk full")), self.assertLogs("timers", "WARNING"):
            await self.sleep.release()
        self.assertEqual(self.ann.texts, ["Таймер на 5 секунд сработал."])

    async def test_a_failed_write_when_starting_leaves_nothing_behind(self):
        book = self.make()
        with mock.patch.object(book, "_persist", side_effect=OSError("disk full")):
            for _ in range(timers.MAX_TIMERS + 5):      # more tries than the book holds: ghosts would exhaust it
                with self.assertRaises(OSError):
                    await book.start(60)
            with self.assertRaises(OSError):
                await book.set_alarm(7, 0, "morning", False)
        self.assertEqual(book.remaining(), [])
        await book.start(60)                            # the disk is back: the book is not full of ghosts
        await settle()
        await self.sleep.release()
        self.assertEqual(self.ann.texts, ["Таймер на 1 минуту сработал."])

    async def test_a_cancel_that_was_not_saved_did_not_happen(self):
        book = self.make()
        await book.start(60)
        await settle()
        with mock.patch.object(book, "_persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await book.cancel()
        self.assertEqual(len(book.remaining()), 1)
        await self.sleep.release()
        self.assertEqual(self.ann.texts, ["Таймер на 1 минуту сработал."])   # still armed

    async def test_resume_never_raises_and_writes_only_what_it_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.json")
            book = self.make(path)
            await book.start(60)
            await book.close()
            with open(path, encoding="utf-8") as fh:
                saved = fh.read()
            fresh = TimerBook(path, self.ann, clock=self.clock, tz=UTC, sleep=self.sleep)
            self.addAsyncCleanup(fresh.close)
            with mock.patch.object(fresh, "_persist", side_effect=OSError("read-only")) as persist:
                await fresh.resume()                    # nothing expired: no write, so nothing to fail
            persist.assert_not_called()
            self.assertEqual(len(fresh.remaining()), 1)
            self.clock.t += 60 + 601                    # now the entry is stale and has to be dropped from the file
            stale = TimerBook(path, self.ann, clock=self.clock, tz=UTC, sleep=self.sleep)
            self.addAsyncCleanup(stale.close)
            with mock.patch.object(stale, "_persist", side_effect=OSError("read-only")), self.assertLogs("timers", "WARNING"):
                await stale.resume()
            self.assertEqual(stale.remaining(), [])
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), saved)

    async def test_an_unwritable_timers_path_does_not_stop_startup(self):
        with tempfile.NamedTemporaryFile() as blocker:
            book = self.make(os.path.join(blocker.name, "sub", "t.json"))     # a "directory" that is a file
            await book.resume()
            self.assertEqual(book.remaining(), [])


class Reminders(BookCase):
    async def test_cancelling_timers_keeps_the_reminders(self):
        book = self.make()
        await book.start(20 * 60, "выключить плиту")
        await book.start(5 * 60)
        self.assertEqual(await book.cancel("timer"), 1)
        self.assertEqual([item["note"] for item in book.remaining()], ["выключить плиту"])
        await settle()
        await self.sleep.release()
        self.assertEqual(self.ann.texts, ["Напоминаю: выключить плиту."])

    async def test_how_much_is_left_starts_with_the_nearest(self):
        book = self.make()
        await book.start(3600, "позвонить")
        await book.start(300)
        await book.start(900)
        self.assertEqual([item["left"] for item in book.remaining()], [300, 900, 3600])

    async def test_a_reminder_saved_before_the_split_is_not_a_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.json")
            entry = {"id": "r1", "kind": "timer", "due": ts(2026, 11, 16, 13, 0), "seconds": 3600, "note": "плита"}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump([entry], fh)
            book = self.make(path)
            await book.resume()
            self.assertEqual(await book.cancel("timer"), 0)
            self.assertEqual(len(book.remaining()), 1)
            self.assertEqual(await book.cancel("reminder"), 1)                 # it is cancelled as a reminder, by voice too
            self.assertEqual(book.remaining(), [])


class Shutdown(BookCase):
    async def test_close_cancels_an_announcement_in_progress(self):
        cancelled = []
        gate = asyncio.Event()

        async def announce(text):
            try:
                await gate.wait()
            except asyncio.CancelledError:
                cancelled.append(text)
                raise
            return True

        clock = Clock(1000.0)
        sleep = FakeSleep(clock)
        book = TimerBook(None, announce, clock=clock, tz=UTC, sleep=sleep)
        await book.start(5)
        await settle()
        await sleep.release()                  # the timer is due: the announcement is now running
        await book.close()
        self.assertEqual(cancelled, ["Таймер на 5 секунд сработал."])

    async def test_close_cancels_the_retry_sleep_of_an_announcement(self):
        book = self.make(results=[False, False, False])
        await book.start(5)
        await settle()
        await self.sleep.release()             # announced once, failed: now in the 20 s retry sleep
        self.assertEqual(len(self.ann.texts), 1)
        (_, retry), = self.sleep.pending
        await book.close()
        self.assertTrue(retry.cancelled())
        self.assertEqual(len(self.ann.texts), 1)

    async def test_a_finished_timer_leaves_no_task_behind(self):
        book = self.make()
        await book.start(5)
        await settle()
        await self.sleep.release()
        self.assertEqual(book._tasks, {})      # deregistered once the announcement is over
        await book.close()
        self.assertEqual(self.ann.texts, ["Таймер на 5 секунд сработал."])


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

    def test_duration_accusative(self):
        for seconds, expected in ((60, "1 минуту"), (21 * 60, "21 минуту"), (5 * 60, "5 минут"), (22 * 60, "22 минуты"),
                                  (3600, "1 час"), (90, "1 минуту 30 секунд"), (1, "1 секунду")):
            self.assertEqual(duration_accusative(seconds), expected, seconds)


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
