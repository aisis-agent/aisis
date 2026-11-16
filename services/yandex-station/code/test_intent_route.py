"""The adapter's grammar route: classification, execution, the /intent/decide and /asr/correct endpoints."""
import asyncio
import dataclasses
import itertools
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import intent_route
import timers as timers_module
from adapter import ERROR_REPLY, Adapter
from intent_grammar import backtest
from intent_grammar.backtest import Row

NOW = datetime(2026, 11, 16, 17, 5, tzinfo=timezone.utc)


class FakeTimers:
    def __init__(self):
        self.calls = []

    async def start(self, seconds, note=None):
        self.calls.append(("start", seconds, note))
        return {"id": "t1", "seconds": seconds, "note": note}

    async def set_alarm(self, hour, minute, part, ambiguous):
        self.calls.append(("alarm", hour, minute, part, ambiguous))
        return {"id": "a1", "text": f"{hour:02d}:{minute:02d}"}

    async def cancel(self, kind="timer"):
        self.calls.append(("cancel", kind))
        return 1

    def remaining(self):
        return [{"id": "t1", "left": 270, "note": None}]


class StatefulTimers(FakeTimers):
    """Remembers what is running: a cancel counts and clears the active timers or alarms."""

    def __init__(self):
        super().__init__()
        self.active = 0
        self.alarms = 0

    async def start(self, seconds, note=None):
        self.active += 1
        return await super().start(seconds, note)

    async def set_alarm(self, hour, minute, part, ambiguous):
        self.alarms += 1
        return await super().set_alarm(hour, minute, part, ambiguous)

    async def cancel(self, kind="timer"):
        self.calls.append(("cancel", kind))
        if kind == "alarm":
            count, self.alarms = self.alarms, 0
        else:
            count, self.active = self.active, 0
        return count


class FakeHa:
    def __init__(self, reply="Готово."):
        self.calls = []
        self.stations = []
        self.reply = reply

    async def run(self, executor, args, station=None):
        self.calls.append((executor, args))
        self.stations.append(station)
        return self.reply


def router(**kwargs):
    grammar = intent_route.load_grammar()
    kwargs.setdefault("now", lambda: NOW)
    kwargs.setdefault("pick", lambda options: options[0])
    return intent_route.IntentRouter(grammar, **kwargs)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.router = router()

    def intent(self, text, phase=None):
        decision = self.router.classify(text, phase)
        return decision and decision.intent

    def test_the_shipped_bundle_loads(self):
        self.assertIsNotNone(intent_route.load_grammar())
        self.assertIsNone(intent_route.load_grammar("/nonexistent/ru.json"))

    def test_real_intents_beat_skills_and_catch_alls(self):
        self.assertEqual(self.intent("Алиса, который час?"), "time.now")
        self.assertEqual(self.intent("Два плюс семь."), "skill.math")     # trailing full stop, as Whisper writes it
        self.assertEqual(self.intent("Алиса, 12+12"), "skill.math")
        self.assertEqual(self.intent("Какая погода завтра?"), "skill.weather")
        self.assertEqual(self.intent("не включай свет"), "negation.noop")
        self.assertEqual(self.intent("Продолжение следует..."), "unaddressed.caption")

    def test_catch_alls_read_only_what_everything_else_left(self):
        self.assertEqual(self.intent("голи со спортом"), "asr.garbage_short")
        self.assertIsNone(self.intent("что такое фотосинтез"))                       # a question: the brain's
        self.assertIsNone(self.intent("расскажи мне историю про дракона и принцессу"))
        self.assertIsNone(self.intent("расскажи шутку"))                              # a request for the brain
        self.assertEqual(self.intent("закрой шторы"), "refuse.unknown_device")

    def test_pre_phase_keeps_to_what_may_answer_before_the_quasar_layer(self):
        self.assertEqual(self.intent("который час", "pre"), "time.now")
        self.assertEqual(self.intent("поставь таймер на пять минут", "pre"), "timer.set")
        self.assertIsNone(self.intent("кондиционер 23", "pre"))                         # climate waits for the quasar layer
        self.assertIsNone(self.intent("стоп", "pre"))
        self.assertIsNone(self.intent("голи со спортом", "pre"))

    def test_catchall_switch(self):
        quiet = router(catchall=False)
        self.assertIsNone(quiet.classify("голи со спортом"))
        self.assertEqual(quiet.classify("который час").intent, "time.now")

    def test_measured_route_combines_pre_quasar_and_rest(self):
        decide = intent_route.measured_route(intent_route.measured_router(self.router.grammar), mode="combined")
        row = lambda text, quasar: Row("r", text, "LIGHT_CMD", "real", "dev", quasar)
        self.assertEqual(decide(row("который час", True)).intent, "time.now")
        self.assertEqual(decide(row("выключи ночник", True)).intent, "quasar.handled")
        self.assertEqual(decide(row("голи со спортом", False)).intent, "asr.garbage_short")
        self.assertIsNone(decide(row("что такое фотосинтез", False)))
        baseline = intent_route.measured_route(self.router, mode="quasar")
        self.assertEqual(baseline(row("выключи ночник", True)).intent, "quasar.handled")
        self.assertIsNone(baseline(row("который час", False)))

    def test_the_measurement_counts_only_what_the_runtime_acts_on(self):
        decide = intent_route.measured_route(intent_route.measured_router(self.router.grammar), mode="combined")
        row = lambda text, quasar=False: Row("r", text, "VOLUME", "real", "dev", quasar)
        self.assertIsNone(decide(row("громкость на пять")))          # the turn cannot change the volume: the adapter returns none
        self.assertEqual(decide(row("прибавь громкость на два")).intent, "volume.delta_up")   # the hook can: it runs before the quasar layer
        self.assertIsNone(decide(row("выключи свет на кухне")))      # a quasar-owned switch that the quasar layer did not answer
        self.assertIsNone(decide(row("Ключи гирлянду.")))            # a guess waits for the owner's «да»: the turn goes to the brain
        self.assertEqual(decide(row("какая погода")).intent, "skill.weather")
        # a router without timers, Home Assistant or skills declines those decisions at run time: they are no answers
        bare = intent_route.measured_route(self.router, mode="combined")
        self.assertIsNone(bare(row("поставь таймер на пять минут")))
        self.assertIsNone(bare(row("сделай теплее")))
        self.assertIsNone(bare(row("какая погода")))
        self.assertEqual(bare(row("который час")).intent, "time.now")

    def test_a_replay_hit_keeps_the_tier_the_quasar_layer_answered_with(self):
        decide = intent_route.measured_route(intent_route.measured_router(self.router.grammar), mode="combined")
        grammar = self.router.grammar
        for via, tier, completed in (("fast-execute", "execute", True), ("fast-clarify", "clarify", False),
                                     ("fast-confirm", "confirm", False), ("fast-query", "query", True), ("media-key", "execute", True)):
            row = Row("r", "громкость", "VOLUME", "real", "dev", True, quasar_via=via)
            decision = decide(row)
            self.assertEqual((decision.intent, decision.tier, decision.labels), ("quasar.handled", tier, ()), via)
            self.assertEqual(backtest.classify(row, decision, grammar), "correct", via)      # judged on the tier, not on a copied label
            self.assertEqual(backtest.metrics(backtest.evaluate(grammar, [row], decide), grammar)["completed"], int(completed), via)

    def test_a_replay_hit_is_judged_on_its_tier_against_the_corpus_expectation(self):
        grammar = self.router.grammar
        decide = intent_route.measured_route(intent_route.measured_router(grammar), mode="combined")
        expecting = lambda tier: Row("r", "освещение 30 процентов", "light", "synthetic", "dev", True,
                                     expect={"kind": "cmd", "domain": "light", "tier": tier}, quasar_via="fast-confirm")
        self.assertEqual(backtest.classify(expecting("confirm"), decide(expecting("confirm")), grammar), "correct")
        self.assertEqual(backtest.classify(expecting("execute"), decide(expecting("execute")), grammar), "wrong")


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_canned_clarify_and_reprompt(self):
        r = router()
        wake = await r.route("st", "Алиса.")
        self.assertEqual((wake.kind, wake.text, wake.listen, wake.source), ("reply", "Слушаю.", False, "grammar"))
        ask = await r.route("st", "Громкость.")
        self.assertEqual((ask.kind, ask.text, ask.listen), ("reply", "Какую громкость поставить?", True))

    async def test_reprompt_is_said_once_a_minute_per_station(self):
        clock = iter([100.0, 110.0, 170.0, 100.0])
        r = router(monotonic=lambda: next(clock))
        first = await r.route("st", "голи со спортом")
        self.assertEqual((first.kind, first.text, first.listen), ("reply", "Не расслышал. Повтори, пожалуйста.", True))
        self.assertEqual((await r.route("st", "лица в ключике")).kind, "none")      # 10 s later: the brain takes it
        self.assertEqual((await r.route("st", "голи со спортом")).kind, "reply")      # 70 s later: again
        self.assertEqual((await r.route("other", "голи со спортом")).kind, "reply")   # another station has its own clock

    async def test_a_clarification_answer_is_joined_to_its_question(self):
        timers = FakeTimers()
        clock = [100.0]
        r = router(timers=timers, monotonic=lambda: clock[0])
        ask = await r.route("st", "поставь таймер")
        self.assertEqual((ask.kind, ask.listen), ("reply", True))
        done = await r.route("st", "на пять минут")
        self.assertEqual((done.kind, done.text), ("reply", "Таймер на 5 минут."))
        self.assertEqual(timers.calls, [("start", 300, None)])
        self.assertEqual((await r.route("st", "на пять минут")).intent, "asr.garbage_short")   # the question is answered once

    async def test_a_late_or_unrelated_answer_is_not_joined(self):
        timers = FakeTimers()
        clock = [100.0]
        r = router(timers=timers, monotonic=lambda: clock[0])
        await r.route("st", "поставь таймер")
        clock[0] += 31.0
        self.assertEqual((await r.route("st", "на пять минут")).intent, "asr.garbage_short")
        await r.route("st", "поставь таймер")
        self.assertEqual((await r.route("st", "который час")).intent, "time.now")
        self.assertEqual(timers.calls, [])

    async def test_the_same_timer_start_runs_once_in_five_seconds(self):
        timers = FakeTimers()
        clock = [100.0]
        r = router(timers=timers, monotonic=lambda: clock[0])
        first = await r.route("quasar", "поставь таймер на пять минут")
        clock[0] += 2.0
        second = await r.route("st", "поставь таймер на пять минут")
        self.assertEqual((first.text, second.text), ("Таймер на 5 минут.", "Таймер на 5 минут."))
        self.assertEqual(timers.calls, [("start", 300, None)])
        clock[0] += 6.0
        await r.route("st", "поставь таймер на пять минут")
        self.assertEqual(len(timers.calls), 2)

    async def test_only_a_timer_start_or_cancel_delivered_twice_is_deduplicated(self):
        timers, ha = StatefulTimers(), FakeHa()
        r = router(timers=timers, ha=ha, monotonic=lambda: 100.0)      # the clock never moves: all of it inside the window
        for _ in range(2):
            await r.route("st", "сделай теплее")
        self.assertEqual(len(ha.calls), 2)                              # a repeated «теплее» is a second step
        self.assertEqual([(await r.route("st", text)).text for text in (
            "поставь таймер на пять минут", "отмени таймер", "поставь таймер на пять минут", "отмени таймер", "отмени таймер")],
            ["Таймер на 5 минут.", "Таймер отменён.", "Таймер на 5 минут.", "Таймер отменён.", "Таймер отменён."])
        # every start and cancel that changed something ran; the last «отмени таймер» was the same cancel delivered twice
        self.assertEqual(timers.calls, [("start", 300, None), ("cancel", "timer"), ("start", 300, None), ("cancel", "timer")])
        self.assertEqual(timers.active, 0)

    async def test_the_hook_and_the_turn_cancelling_the_same_timer_both_hear_that_it_worked(self):
        timers = StatefulTimers()
        r = router(timers=timers, monotonic=lambda: 100.0)
        await r.route("st", "поставь таймер на пять минут")
        hook = await r.route("quasar", "отмени таймер", phase="pre", via_quasar=True)
        turn = await r.route("st", "отмени таймер")                        # the hook timed out: the turn carries the phrase again
        self.assertEqual((hook.text, turn.text), ("Таймер отменён.", "Таймер отменён."))
        self.assertEqual(timers.calls.count(("cancel", "timer")), 1)
        await r.route("st", "заведи будильник на семь утра")                # any start makes the remembered reply stale
        self.assertEqual((await r.route("st", "отмени таймер")).text, "Таймеров нет.")
        self.assertEqual(timers.calls.count(("cancel", "timer")), 2)
        alarm = await r.route("st", "отмени будильник")
        again = await r.route("st", "отмени будильник")
        self.assertEqual((alarm.text, again.text), ("Будильник отменён.", "Будильник отменён."))
        self.assertEqual(timers.calls.count(("cancel", "alarm")), 1)

    async def test_a_failed_cancel_is_not_remembered(self):
        class Flaky(StatefulTimers):
            fail = True

            async def cancel(self, kind="timer"):
                if self.fail:
                    raise OSError("disk full")
                return await super().cancel(kind)
        timers = Flaky()
        r = router(timers=timers, monotonic=lambda: 100.0)
        await r.route("st", "поставь таймер на пять минут")
        self.assertEqual((await r.route("st", "отмени таймер")).kind, "none")
        timers.fail = False
        self.assertEqual((await r.route("st", "отмени таймер")).text, "Таймер отменён.")

    async def test_a_cancel_and_a_start_in_one_phrase_really_start_a_timer(self):
        book = self.real_book()
        r = router(timers=book, monotonic=lambda: 100.0)
        # the first start's cleanup callback is still queued when the next phrase arrives (overlapping requests)
        with patch.object(r, "_finished", lambda *args: None):
            await r.route("st", "поставь таймер на пять минут")
            got = await r.route("st", "отмени таймер и поставь таймер на пять минут")
        self.assertEqual(got.text, "Таймер отменён. Таймер на 5 минут.")
        self.assertEqual([item["left"] for item in book.remaining()], [300])      # a timer exists, not only its confirmation

    def real_book(self):
        async def never(_delay):
            await asyncio.Event().wait()
        book = timers_module.TimerBook(None, AsyncMock(return_value=True), clock=lambda: 1000.0, tz=timezone.utc, sleep=never)
        self.addAsyncCleanup(book.close)
        return book

    async def test_a_reminder_has_its_own_cancel(self):
        book = self.real_book()
        clock = [100.0]
        r = router(timers=book, monotonic=lambda: clock[0])

        async def say(text):
            clock[0] += intent_route.DEDUPE_S + 1       # each phrase is its own, not a second delivery of the previous one
            return (await r.route("st", text)).text
        await say("напомни через десять минут выключить плиту")
        self.assertEqual(await say("отмени таймер"), intent_route.TIMER_CANCEL_REMINDER_HINT)
        self.assertEqual(len(book.remaining()), 1)                          # «отмени таймер» left it alone
        self.assertEqual(await say("отмени напоминание"), "Напоминание отменено.")
        self.assertEqual(book.remaining(), [])
        self.assertEqual(await say("удали напоминание"), "Напоминаний нет.")
        self.assertEqual(await say("отмени таймер"), "Таймеров нет.")
        for text in ("напомни через десять минут выключить плиту", "напомни через час позвонить маме"):
            await say(text)
        self.assertEqual(await say("отмени все напоминания"), "Напоминания отменены.")
        self.assertEqual(book.remaining(), [])

    async def test_a_timer_phrase_without_timers_answers_even_as_part_of_a_compound(self):
        r = router()
        for text in ("поставь таймер на пять минут и таймер на минуту", "заведи будильник на семь утра и поставь таймер на минуту"):
            got = await r.route("st", text)
            self.assertEqual((got.kind, got.text), ("reply", intent_route.TIMERS_OFF), text)
        self.assertEqual((await r.route("st", "поставь таймер на пять минут и выключи свет")).kind, "none")   # the quasar layer's clause
        self.assertEqual((await router(timers=FakeTimers()).route("st", "поставь таймер на минуту и таймер на две минуты")).intent, "compound")

    async def test_a_failed_or_queued_home_assistant_reply_is_not_replayed(self):
        ha = FakeHa(reply="Не удалось выполнить команду умного дома.")
        r = router(ha=ha, monotonic=lambda: 100.0)
        await r.route("st", "сделай теплее")
        ha.reply = "Готово."
        self.assertEqual((await r.route("st", "сделай теплее")).text, "Готово.")
        self.assertEqual(len(ha.calls), 2)

    async def test_a_failed_timer_start_is_not_remembered(self):
        class Flaky(FakeTimers):
            fail = True

            async def start(self, seconds, note=None):
                if self.fail:
                    raise OSError("disk full")
                return await super().start(seconds, note)
        timers = Flaky()
        r = router(timers=timers, monotonic=lambda: 100.0)
        self.assertEqual((await r.route("st", "поставь таймер на пять минут")).kind, "none")
        timers.fail = False
        self.assertEqual((await r.route("st", "поставь таймер на пять минут")).text, "Таймер на 5 минут.")
        self.assertEqual(timers.calls, [("start", 300, None)])

    async def test_concurrent_duplicates_of_a_timer_start_run_once(self):
        class Slow(FakeTimers):
            async def start(self, seconds, note=None):
                for _ in range(5):
                    await asyncio.sleep(0)          # the first start is still running when the duplicate arrives
                return await super().start(seconds, note)
        timers = Slow()
        r = router(timers=timers)
        first, second = await asyncio.gather(r.route("quasar", "поставь таймер на пять минут"),
                                             r.route("st", "поставь таймер на пять минут"))
        self.assertEqual((first.text, second.text), ("Таймер на 5 минут.", "Таймер на 5 минут."))
        self.assertEqual(timers.calls, [("start", 300, None)])

    async def test_shadow_classifies_and_executes_nothing(self):
        timers = FakeTimers()
        r = router(timers=timers)
        verdict = r.shadow("поставь таймер на пять минут", "pre")
        self.assertEqual((verdict.kind, verdict.intent, verdict.source), ("none", "timer.set", "shadow"))
        self.assertEqual(timers.calls, [])
        r.shadow("поставь таймер")
        self.assertEqual(r.pending, {})

    async def test_an_emergency_is_answered_at_once_and_never_reaches_the_model(self):
        r = router()
        for text in ("Мне плохо, вызови скорую", "позвони в полицию", "у нас пожар"):
            for phase in ("pre", None):
                got = await r.route("st", text, phase=phase)
                self.assertEqual((got.kind, got.intent, got.listen), ("reply", "refuse.emergency", False), text)
                self.assertIn("сто двенадцать", got.text)

    async def test_clock_and_date_are_local(self):
        r = router()
        self.assertEqual((await r.route("st", "который час")).text, "Сейчас 17 часов 5 минут.")
        self.assertEqual((await r.route("st", "какое сегодня число")).text, "Сегодня понедельник, 16 ноября.")
        on_the_hour = router(now=lambda: datetime(2026, 11, 16, 9, 0))
        self.assertEqual((await on_the_hour.route("st", "сколько времени")).text, "Сейчас ровно 9 часов.")

    async def test_unaddressed_caption_is_silent(self):
        got = await router().route("st", "Субтитры сделал DimaTorzok")
        self.assertEqual((got.kind, got.text, got.intent), ("silent", None, "unaddressed.caption"))

    async def test_repeat_uses_the_last_spoken_reply_of_the_station(self):
        r = router()
        self.assertEqual((await r.route("st", "повтори")).kind, "none")   # nothing said yet: the old path
        r.note_reply("st", "Сейчас 17 часов 5 минут.")
        self.assertEqual((await r.route("st", "повтори")).text, "Сейчас 17 часов 5 минут.")
        self.assertEqual((await r.route("other", "повтори")).kind, "none")

    async def test_timers_alarms_and_the_off_state(self):
        timers = FakeTimers()
        r = router(timers=timers)
        self.assertEqual((await r.route("st", "поставь таймер на пять минут")).text, "Таймер на 5 минут.")
        self.assertEqual((await r.route("st", "напомни через десять минут выключить плиту")).text, "Напомню через 10 минут.")
        self.assertEqual((await r.route("st", "заведи будильник на семь утра")).text, "Будильник на 07:00.")
        self.assertEqual((await r.route("st", "отмени таймер")).text, "Таймер отменён.")
        self.assertEqual((await r.route("st", "сколько осталось")).text, "Осталось 4 минуты 30 секунд.")
        self.assertEqual(timers.calls[0], ("start", 300, None))
        self.assertEqual(timers.calls[1], ("start", 600, "выключить плиту"))
        self.assertEqual(timers.calls[2], ("alarm", 7, 0, "morning", False))
        off = router()
        self.assertEqual((await off.route("st", "поставь таймер на пять минут")).text, intent_route.TIMERS_OFF)

    async def test_the_confirmation_says_the_duration_in_the_accusative(self):
        r = router(timers=FakeTimers(), monotonic=lambda: 100.0)
        for text, want in (("поставь таймер на минуту", "Таймер на 1 минуту."),
                           ("напомни через двадцать одну минуту выключить плиту", "Напомню через 21 минуту."),
                           ("поставь таймер на час тридцать минут", "Таймер на 1 час 30 минут."),
                           ("поставь таймер на двадцать две минуты", "Таймер на 22 минуты.")):
            self.assertEqual((await r.route("st", text)).text, want, text)

    async def test_home_assistant_executors_need_a_home_assistant_binding(self):
        ha = FakeHa()
        r = router(ha=ha)
        got = await r.route("st", "Наставь кондиционер на втором этаже. Обогрев тридцать градусов.")
        self.assertEqual((got.kind, got.text), ("reply", "Готово."))
        self.assertEqual(ha.calls, [("ha.climate", {"room": "upstairs", "mode": "heat", "temperature": 30})])
        self.assertEqual((await r.route("st", "люстру белый цвет")).text, "Готово.")
        self.assertEqual(ha.calls[-1], ("ha.light_color", {"color": "white", "room": None}))
        self.assertEqual((await router().route("st", "кондиционер 23")).kind, "none")

    async def test_a_home_assistant_command_carries_the_speaker_that_heard_it(self):
        ha = FakeHa()
        r = router(ha=ha)
        await r.route("M00ZRW3008RAJG", "сделай свет красным")
        await r.route("M10DST310HXNPK", "сделай теплее")
        self.assertEqual(ha.stations, ["M00ZRW3008RAJG", "M10DST310HXNPK"])

    async def test_degrees_are_the_ac_and_never_the_volume(self):
        ha = FakeHa()
        r = router(ha=ha)
        self.assertEqual((await r.route("quasar", "прибавь два градуса", phase="pre", via_quasar=True)).kind, "none")
        got = await r.route("st", "прибавь два градуса")
        self.assertEqual((got.kind, got.text), ("reply", "Готово."))
        self.assertEqual(ha.calls, [("ha.climate_delta", {"room": None, "direction": 1, "delta": {"amount": 2, "unit": "градус"}})])
        self.assertEqual((await r.route("quasar", "прибавь на тридцать", phase="pre", via_quasar=True)).kind, "none")

    async def test_quasar_owned_decisions_are_not_executed_here(self):
        r = router()
        for text in ("громче", "стоп", "выключи свет на кухне", "громкость на пять"):
            with self.subTest(text=text):
                self.assertEqual((await r.route("st", text)).kind, "none")

    async def test_volume_and_dialog_end_are_for_the_overlay_only(self):
        r = router()
        up = await r.route("quasar", "прибавь громкость на десять процентов", phase="pre", via_quasar=True)
        self.assertEqual((up.kind, up.volume), ("volume", {"deltaLevels": 1.0}))
        down = await r.route("quasar", "убавь звук на два", phase="pre", via_quasar=True)
        self.assertEqual(down.volume, {"deltaLevels": -2.0})
        self.assertEqual((await r.route("st", "прибавь громкость на два")).kind, "none")   # the adapter cannot change volume

    async def test_volume_unit_follows_the_args_contract(self):
        r = router()
        for text, want in (("громкость на пять", {"target": 0.5}), ("громкость на максимум", {"target": 1.0}),
                           ("громкость 50%", {"target": 0.5}), ("громкость на десять процентов", {"target": 0.1}),
                           ("громче", {"steps": 1.0}), ("тише", {"steps": -1.0}), ("громковато", {"steps": -1.0})):
            with self.subTest(text=text):
                self.assertEqual((await r.route("quasar", text, via_quasar=True)).volume, want)
        # a bare number off the 0..10 scale is no level and no percent: nothing is guessed
        self.assertEqual((await r.route("quasar", "громкость 50", via_quasar=True)).volume, None)

    def test_volume_from_accepts_exactly_one_unit_in_range(self):
        base = router().classify("громче")

        def volume(**args):
            return intent_route.volume_from(dataclasses.replace(base, action={"executor": "speaker.volume", "args": args}))
        self.assertEqual(volume(level=0), {"target": 0.0})
        self.assertEqual(volume(level=10), {"target": 1.0})
        self.assertEqual(volume(percent=10), {"target": 0.1})
        self.assertEqual(volume(percent=100), {"target": 1.0})
        self.assertEqual(volume(step=-1), {"steps": -1.0})
        for bad in ({}, {"level": 11}, {"level": -1}, {"percent": 101}, {"level": 5, "percent": 50}, {"level": 5, "step": 1},
                    {"level": "5"}, {"level": True}):
            with self.subTest(args=bad):
                self.assertIsNone(volume(**bad))

    async def test_a_guessed_reading_never_acts(self):
        r = router(ha=FakeHa())
        self.assertEqual(r.classify("Ключи гирлянду.").tier, "confirm")                    # typo-repaired reading
        self.assertEqual((await r.route("st", "Ключи гирлянду.")).kind, "none")           # asked of the owner by the quasar layer

    async def test_compound_runs_every_clause_or_none(self):
        timers = FakeTimers()
        r = router(timers=timers)
        got = await r.route("st", "поставь таймер на пять минут и поставь таймер на минуту")
        self.assertEqual((got.intent, got.text), ("compound", "Таймер на 5 минут. Таймер на 1 минуту."))

    async def test_a_compound_with_a_clause_the_adapter_cannot_run_changes_nothing(self):
        for text in ("поставь таймер на пять минут и выключи свет",     # one clause is the quasar layer's
                     "поставь таймер на пять минут и сделай теплее"):    # Home Assistant is not bound
            timers = FakeTimers()
            mixed = await router(timers=timers).route("st", text)
            self.assertEqual(mixed.kind, "none", text)
            self.assertEqual(timers.calls, [], text)                    # nothing started before the answer was none
        timers = FakeTimers()
        self.assertEqual((await router(timers=timers, ha=FakeHa()).route("st", "поставь таймер на минуту и повтори")).kind, "none")
        self.assertEqual(timers.calls, [])                              # «повтори» has nothing to repeat yet

    async def test_an_executor_error_fails_open(self):
        class Broken:
            async def start(self, *args):
                raise OSError("disk")
        got = await router(timers=Broken()).route("st", "поставь таймер на пять минут")
        self.assertEqual(got.kind, "none")

    async def test_weather_and_music_go_through_the_skills(self):
        skills = AsyncMock()
        skills.weather.return_value = "Ясно, десять градусов."
        skills.music.return_value = "Включаю."
        r = router(skills=skills)
        self.assertEqual((await r.route("st", "какая погода")).text, "Ясно, десять градусов.")
        self.assertEqual((await r.route("st", "включи музыку")).text, "Включаю.")
        skills.weather.assert_awaited_once_with("now")

    async def test_a_short_reply_to_the_brains_own_question_is_not_a_reprompt(self):
        clock = [100.0]
        r = router(monotonic=lambda: clock[0])
        r.note_reply("st", "Рассказать подробнее?", brain=True)
        for text in ("да", "нет", "про собак", "ещё одну"):
            self.assertEqual((await r.route("st", text)).kind, "none", text)      # the brain continues its own conversation
        self.assertEqual(r.last_reprompt, {})                                       # and no «не расслышал» cooldown was spent
        self.assertEqual((await r.route("st", "который час")).intent, "time.now")   # a real intent still answers
        r.note_reply("st", "Сейчас 17 часов 5 минут.")
        self.assertEqual((await r.route("st", "да")).intent, "asr.garbage_short")  # after a statement the reprompt is back
        self.assertEqual((await r.route("st", "нет")).kind, "none")                 # the reprompt is said once a minute

    async def test_only_a_question_the_brain_asked_and_only_for_a_while_suppresses_the_catch_alls(self):
        clock = [100.0]
        r = router(monotonic=lambda: clock[0])
        for line in ("Да?", "На сколько поставить таймер?", "Какую громкость поставить?"):   # the grammar's own questions
            r.note_reply("st", line)
            self.assertEqual((await r.route("st", "да")).intent, "asr.garbage_short", line)
            clock[0] += intent_route.REPROMPT_COOLDOWN_S + 1
        r.note_reply("st", "Рассказать подробнее?", brain=True)
        clock[0] += intent_route.BRAIN_QUESTION_WINDOW_S + 1                            # nobody answered it for a while
        self.assertEqual((await r.route("st", "да")).intent, "asr.garbage_short")
        r.note_reply("st", "Рассказать подробнее?", brain=True)
        r.note_reply("st", "Да?")                                                        # a grammar line replaces the brain's question
        clock[0] += intent_route.REPROMPT_COOLDOWN_S + 1
        self.assertEqual((await r.route("st", "да")).intent, "asr.garbage_short")

    async def test_the_shared_none_route_is_never_stamped(self):
        counter = itertools.count(0, 3_000_000)
        with patch.object(intent_route.time, "perf_counter_ns", lambda: next(counter)):
            got = await router().route("st", "что такое фотосинтез")
        self.assertEqual(got.kind, "none")
        self.assertGreater(got.ms, 0)
        self.assertEqual(intent_route.NONE.ms, 0)

    def test_volume_delta_takes_a_direction_and_exactly_one_amount(self):
        base = router().classify("громче")

        def delta(**args):
            return intent_route.volume_from(dataclasses.replace(base, action={"executor": "speaker.volume_delta", "args": args}))
        self.assertEqual(delta(direction=1, levels=2, percent=None), {"deltaLevels": 2.0})
        self.assertEqual(delta(direction=-1, levels=None, percent=20), {"deltaLevels": -2.0})
        for bad in ({"direction": 1}, {"direction": 0, "levels": 2}, {"direction": 1, "levels": 2, "percent": 20},
                    {"direction": 1, "levels": 11}, {"direction": 1, "levels": 0}, {"direction": 1, "percent": 101},
                    {"direction": 1, "levels": True}, {"direction": 1, "delta": {"amount": 30, "unit": ""}}):
            with self.subTest(args=bad):
                self.assertIsNone(delta(**bad))


class AdapterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": self.dir.name + "/adapter.sqlite3", "AISIS_MEMORY": "0",
                    "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": "test-only-token", "AISIS_HA_ACTIONS": "off"}

    async def adapter(self, **extra):
        adapter = Adapter({**self.env, **extra})
        adapter.turn = AsyncMock(return_value="ответ мозга")
        adapter.start = AsyncMock()
        adapter.close = AsyncMock()
        return adapter

    async def spoken(self, adapter, text):
        ws = AsyncMock()
        ws.closed = False
        await adapter._process_turn(ws, {"text": text, "metadata": {"stationId": "st1"}})
        return ws.send_json.await_args.args[0]["data"]["text"]

    async def test_grammar_answers_before_the_brain_and_the_brain_keeps_the_rest(self):
        adapter = await self.adapter()
        self.assertEqual(await self.spoken(adapter, "Алиса. Который час?"), intent_route.clock_text(adapter.intents.now()))
        self.assertIn(await self.spoken(adapter, "Алиса"), ("Слушаю.", "Да?", "Я здесь."))
        self.assertEqual(await self.spoken(adapter, "что такое фотосинтез"), "ответ мозга")
        self.assertEqual(adapter.turn.await_count, 1)
        self.assertEqual(await self.spoken(adapter, "Продолжение следует..."), "")
        self.assertEqual(await self.spoken(adapter, "два плюс два"), "4")
        self.assertEqual(adapter.turn.await_count, 1)

    async def test_repeat_says_the_last_reply_again(self):
        adapter = await self.adapter()
        first = await self.spoken(adapter, "который час")
        self.assertEqual(await self.spoken(adapter, "повтори"), first)

    async def test_a_rejected_turn_still_gets_its_error_reply(self):
        adapter = await self.adapter()
        for data in ({"text": "привет", "metadata": {"stationId": "other"}},                       # station not allowed
                     {"text": "привет", "metadata": {}},                                             # no station
                     {"text": "привет", "metadata": "oops"},                                         # metadata that is no mapping
                     {"text": "привет", "isExternalEvent": True, "metadata": {"stationId": "st1"}}):
            ws = AsyncMock()
            ws.closed = False
            await adapter._process_turn(ws, data)
            self.assertEqual(ws.send_json.await_args.args[0]["data"]["text"], ERROR_REPLY, data)
        adapter.turn.assert_not_awaited()
        self.assertEqual(adapter.intents.last_reply, {})      # nothing was said to a station that was never accepted

    async def test_only_the_brains_own_question_is_marked_as_such(self):
        adapter = await self.adapter()
        adapter.turn = AsyncMock(side_effect=["Рассказать подробнее?", "про собак — да, расскажу"])
        self.assertEqual(await self.spoken(adapter, "расскажи про дракона и принцессу и что было дальше у них"), "Рассказать подробнее?")
        self.assertEqual(await self.spoken(adapter, "да"), "про собак — да, расскажу")     # the brain, not «Не расслышал»
        self.assertEqual(adapter.turn.await_count, 2)
        self.assertEqual(await self.spoken(adapter, "Громкость."), "Какую громкость поставить?")   # the grammar's question
        self.assertEqual(await self.spoken(adapter, "голи со спортом"), "Не расслышал. Повтори, пожалуйста.")
        self.assertEqual(adapter.turn.await_count, 2)
        self.assertEqual(adapter.intents.brain_asked, {})

    async def test_an_emergency_never_goes_to_the_brain(self):
        adapter = await self.adapter()
        adapter.skills = AsyncMock()
        self.assertIn("сто двенадцать", await self.spoken(adapter, "Мне плохо, вызови скорую"))
        adapter.turn.assert_not_awaited()
        adapter.skills.answer.assert_not_awaited()

    async def test_a_grammar_question_keeps_the_microphone_open(self):
        adapter = await self.adapter()
        ws = AsyncMock()
        ws.closed = False
        await adapter._process_turn(ws, {"text": "Громкость.", "metadata": {"stationId": "st1"}})
        data = ws.send_json.await_args.args[0]["data"]
        self.assertEqual((data["text"], data["shouldListen"]), ("Какую громкость поставить?", True))
        ws2 = AsyncMock()
        ws2.closed = False
        await adapter._process_turn(ws2, {"text": "который час", "metadata": {"stationId": "st1"}})
        self.assertFalse(ws2.send_json.await_args.args[0]["data"]["shouldListen"])

    async def test_off_and_shadow_leave_the_old_path_alone(self):
        for mode in ("off", "shadow"):
            adapter = await self.adapter(AISIS_INTENT_GRAMMAR=mode)
            self.assertEqual(await self.spoken(adapter, "который час"), "ответ мозга", mode)
            self.assertEqual(await self.spoken(adapter, "два плюс два"), "4", mode)
        self.assertIsNone((await self.adapter(AISIS_INTENT_GRAMMAR="off")).intents)

    async def test_shadow_decides_but_never_acts(self):
        adapter = await self.adapter(AISIS_INTENT_GRAMMAR="shadow", AISIS_TIMER_TTS_ENTITY="tts.x", AISIS_TIMER_PLAYER="media_player.y",
                                     AISIS_TIMERS_DB=self.dir.name + "/timers.json")
        adapter.intents.ha = AsyncMock()
        with self.assertLogs("aisis.yandex", "INFO") as logs:
            self.assertEqual(await self.spoken(adapter, "поставь таймер на пять минут"), "ответ мозга")
            self.assertEqual(await self.spoken(adapter, "сделай теплее"), "ответ мозга")
        self.assertEqual(adapter.timers.remaining(), [])
        adapter.intents.ha.run.assert_not_awaited()
        self.assertEqual(adapter.intents.stats, {})
        self.assertIn("intent_shadow intent=timer.set tier=execute", "\n".join(logs.output))
        await adapter.timers.close()

    async def test_a_missing_bundle_fails_open(self):
        adapter = await self.adapter(AISIS_INTENT_BUNDLE="/nonexistent/ru.json")
        self.assertIsNone(adapter.intents)
        self.assertEqual(await self.spoken(adapter, "который час"), "ответ мозга")

    async def test_decide_endpoint_for_the_overlay(self):
        adapter = await self.adapter()
        async with TestClient(TestServer(adapter.app())) as client:
            headers = {"Authorization": "Bearer test-only-token"}

            async def ask(text):
                response = await client.post("/intent/decide", json={"text": text, "dialogActive": False}, headers=headers)
                self.assertEqual(response.status, 200)
                return await response.json()

            self.assertEqual((await ask("Алиса, привет"))["route"], "reply")
            self.assertEqual((await ask("Субтитры сделал DimaTorzok"))["route"], "silent")
            self.assertEqual((await ask("убавь звук на два"))["volume"], {"deltaLevels": -2.0})
            self.assertEqual((await ask("не включай свет"))["text"], "Хорошо, не буду.")
            for text in ("стоп", "голи со спортом", "кондиционер 23", "что такое фотосинтез"):
                self.assertEqual((await ask(text))["route"], "none", text)    # the quasar layer's own code, or the brain
            shadow = await (await client.post("/intent/decide", json={"text": "Алиса, привет", "shadow": True}, headers=headers)).json()
            self.assertEqual((shadow["route"], shadow["text"], shadow["intent"]), ("none", None, "smalltalk.greeting"))
            self.assertEqual((await client.post("/intent/decide", json={"text": ""}, headers=headers)).status, 400)
            self.assertEqual((await client.post("/intent/decide", json={"text": "привет"})).status, 401)

    async def test_asr_correct_skips_the_model_for_phrases_the_grammar_knows(self):
        adapter = await self.adapter()
        adapter._asr_corrector = lambda: self.fail("the corrector must not be consulted")
        async with TestClient(TestServer(adapter.app())) as client:
            response = await client.post("/asr/correct", json={"text": "который час"}, headers={"Authorization": "Bearer test-only-token"})
            data = await response.json()
        self.assertEqual((data["mode"], data["status"], data["ms"]), ("ignore", "grammar", 0))

    async def test_healthz_reports_the_routes(self):
        adapter = await self.adapter()
        await self.spoken(adapter, "который час")
        async with TestClient(TestServer(adapter.app())) as client:
            data = await (await client.get("/healthz")).json()
        self.assertEqual(data["intent_grammar"], {"mode": "on", "loaded": True, "routes": {"grammar": 1}})

    async def test_deterministic_route_stays_inside_the_latency_budget(self):
        """Audit §7.4: transcript -> decision under 300 ms at the 95th percentile, over every pinned example phrase
        (the adapter's websocket turn path and the quasar overlay's /intent/decide), no model, no Home Assistant."""
        adapter = await self.adapter()
        adapter.skills = AsyncMock()
        adapter.skills.answer.return_value = None
        adapter.intents.skills = None
        phrases = [example.say for intent in adapter.intents.grammar.intents for example in intent.examples]
        self.assertGreater(len(phrases), 100)
        turn, hook = [], []
        async with TestClient(TestServer(adapter.app())) as client:
            headers = {"Authorization": "Bearer test-only-token"}
            for text in phrases:
                started = time.perf_counter()
                await self.spoken(adapter, text)
                turn.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                await client.post("/intent/decide", json={"text": text}, headers=headers)
                hook.append((time.perf_counter() - started) * 1000)
        for name, samples in (("adapter turn", turn), ("overlay hook", hook)):
            p95 = sorted(samples)[int(0.95 * len(samples))]
            self.assertLess(p95, 300, f"{name} p95 {p95:.1f} ms")

    async def test_timers_are_built_only_with_an_announcement_target(self):
        self.assertIsNone((await self.adapter()).timers)
        adapter = await self.adapter(AISIS_TIMER_TTS_ENTITY="tts.x", AISIS_TIMER_PLAYER="media_player.y",
                                     AISIS_TIMERS_DB=self.dir.name + "/timers.json")
        self.assertIsNotNone(adapter.timers)
        self.assertIn("Таймер на 5 минут", await self.spoken(adapter, "поставь таймер на пять минут"))
        await adapter.timers.close()


if __name__ == "__main__":
    unittest.main()
