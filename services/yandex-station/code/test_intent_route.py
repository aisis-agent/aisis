"""The adapter's grammar route: classification, execution, the /intent/decide and /asr/correct endpoints."""
import asyncio
import dataclasses
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

import intent_route
from adapter import Adapter
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


class FakeHa:
    def __init__(self):
        self.calls = []

    async def run(self, executor, args):
        self.calls.append((executor, args))
        return "Готово."


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
        decide = intent_route.measured_route(self.router, mode="combined")
        row = lambda text, quasar: Row("r", text, "LIGHT_CMD", "real", "dev", quasar)
        self.assertEqual(decide(row("который час", True)).intent, "time.now")
        self.assertEqual(decide(row("выключи ночник", True)).intent, "quasar.handled")
        self.assertEqual(decide(row("голи со спортом", False)).intent, "asr.garbage_short")
        self.assertIsNone(decide(row("что такое фотосинтез", False)))
        baseline = intent_route.measured_route(self.router, mode="quasar")
        self.assertEqual(baseline(row("выключи ночник", True)).intent, "quasar.handled")
        self.assertIsNone(baseline(row("который час", False)))


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

    async def test_the_same_timer_or_home_assistant_action_runs_once_in_five_seconds(self):
        timers, ha = FakeTimers(), FakeHa()
        clock = [100.0]
        r = router(timers=timers, ha=ha, monotonic=lambda: clock[0])
        first = await r.route("quasar", "поставь таймер на пять минут")
        clock[0] += 2.0
        second = await r.route("st", "поставь таймер на пять минут")
        self.assertEqual((first.text, second.text), ("Таймер на 5 минут.", "Таймер на 5 минут."))
        self.assertEqual(timers.calls, [("start", 300, None)])
        await r.route("st", "сделай теплее")
        await r.route("quasar", "сделай теплее")
        self.assertEqual(len(ha.calls), 1)
        clock[0] += 6.0
        await r.route("st", "поставь таймер на пять минут")
        self.assertEqual(len(timers.calls), 2)

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

    async def test_home_assistant_executors_need_a_home_assistant_binding(self):
        ha = FakeHa()
        r = router(ha=ha)
        got = await r.route("st", "Наставь кондиционер на втором этаже. Обогрев тридцать градусов.")
        self.assertEqual((got.kind, got.text), ("reply", "Готово."))
        self.assertEqual(ha.calls, [("ha.climate", {"room": "upstairs", "mode": "heat", "temperature": 30})])
        self.assertEqual((await r.route("st", "люстру белый цвет")).text, "Готово.")
        self.assertEqual(ha.calls[-1], ("ha.light_color", {"color": "white", "room": None}))
        self.assertEqual((await router().route("st", "кондиционер 23")).kind, "none")

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
        self.assertEqual((got.intent, got.text), ("compound", "Таймер на 5 минут. Таймер на 1 минута."))
        mixed = await r.route("st", "поставь таймер на пять минут и выключи свет")   # one clause is the quasar layer's
        self.assertEqual(mixed.kind, "none")

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
