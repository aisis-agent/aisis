"""The adapter stores one utterance trace per call: layer, grammar decision with parameters, calls, stages, errors (utterance_trace.py)."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import dialogstore
import ha_actions
import jev
from adapter import Adapter
from test_ha_actions import FakeHa, LIVING

STATION = "st1"


class Scripted:
    def __init__(self, decision):
        self.decision, self.stats = decision, {}

    async def assess(self, turn):
        answers = {"utterance": jev.ChoiceAnswer(self.decision.utterance, self.decision.confidence),
                   "tier": jev.ChoiceAnswer("fast", 0.9), "effort": jev.ChoiceAnswer("none", 0.9),
                   "route": jev.ChoiceAnswer("escalate", 0.8)}
        return jev.Verdict(self.decision, "ok", answers, 250, 0.00002)


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = self.dir.name + "/dialogs.sqlite3"
        self.env = {"AISIS_ALLOWED_STATIONS": STATION, "AISIS_STATE_DB": self.dir.name + "/adapter.sqlite3", "AISIS_MEMORY": "0",
                    "AISIS_DIALOGS_DB": self.db, "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": "test-only-token",
                    "AISIS_HA_ACTIONS": "off"}

    async def adapter(self, **extra):
        adapter = Adapter({**self.env, **extra})
        self.addAsyncCleanup(adapter.flush_traces)
        adapter.turn = AsyncMock(return_value="ответ мозга")
        adapter.skills = AsyncMock()
        adapter.skills.answer.return_value = None
        adapter.intents.skills = None
        return adapter

    async def speak(self, adapter, text, station=STATION):
        ws = AsyncMock()
        ws.closed = False
        await adapter._process_turn(ws, {"text": text, "metadata": {"stationId": station}})
        await adapter.flush_traces()
        return ws.send_json.await_args.args[0]["data"]

    def traces(self):
        conn = sqlite3.connect(self.db)
        try:
            return [json.loads(row[0]) for row in conn.execute("SELECT trace FROM utterance_traces ORDER BY id")]
        finally:
            conn.close()

    async def test_a_grammar_answer_records_intent_tier_band_confidence_and_stages(self):
        adapter = await self.adapter()
        await self.speak(adapter, "который час")
        (trace,) = self.traces()
        self.assertEqual((trace["via"], trace["layer"], trace["station"], trace["text"]), ("turn", "grammar", STATION, "который час"))
        self.assertEqual((trace["intent"], trace["tier"], trace["executor"], trace["conf"], trace["fuzzy"]),
                         ("time.now", "query", "clock.say", 1.0, False))
        self.assertIsInstance(trace["priority"], int)
        self.assertTrue(trace["reply"].startswith("Сейчас"))
        self.assertIn("grammar", trace["stages_ms"])
        self.assertIn("total", trace["stages_ms"])
        self.assertNotIn("errors", trace)

    async def test_slots_and_arguments_of_the_decision_are_kept(self):
        adapter = await self.adapter()
        await self.speak(adapter, "поставь кондиционер на 24")
        (trace,) = self.traces()
        self.assertEqual(trace["intent"], "climate.set")
        self.assertEqual(trace["slots"], {"temp": 24})
        self.assertEqual(trace["args"]["temperature"], 24)

    async def test_a_clarification_is_flagged_with_the_question_and_the_open_microphone(self):
        adapter = await self.adapter()
        data = await self.speak(adapter, "Громкость.")
        (trace,) = self.traces()
        self.assertTrue(data["shouldListen"])
        self.assertEqual((trace["intent"], trace["tier"], trace["asked"], trace["listen"]), ("volume.slotless", "clarify", "clarify", True))
        self.assertEqual(trace["reply"], "Какую громкость поставить?")

    async def test_the_answer_joined_to_a_clarification_records_the_joined_phrase(self):
        adapter = await self.adapter()
        await self.speak(adapter, "Громкость.")
        await self.speak(adapter, "семь")
        second = self.traces()[1]
        self.assertEqual(second["text"], "семь")
        self.assertEqual(second["joined"], "Громкость. семь")

    async def test_a_reprompt_names_itself(self):
        adapter = await self.adapter()
        await self.speak(adapter, "голи со спортом")
        (trace,) = self.traces()
        self.assertEqual((trace["intent"], trace["tier"], trace["asked"]), ("asr.garbage_short", "reprompt", "reprompt"))

    async def test_a_decision_the_adapter_does_not_run_says_why_and_the_brain_answers(self):
        adapter = await self.adapter(AISIS_HA_ACTIONS="off")
        await self.speak(adapter, "громче")
        trace = self.traces()[0]
        self.assertEqual(trace["layer"], "brain")
        self.assertIn("quasar", trace["declined"])

    async def test_arithmetic_is_a_grammar_skill_and_the_brain_reply_is_a_brain_layer(self):
        adapter = await self.adapter()
        await self.speak(adapter, "два плюс два")
        await self.speak(adapter, "что такое фотосинтез")
        math, brain = self.traces()
        self.assertEqual((math["layer"], math["intent"], math["reply"]), ("skill", "skill.math", "4"))
        self.assertEqual((brain["layer"], brain["reply"]), ("brain", "ответ мозга"))
        self.assertNotIn("intent", brain)

    async def test_the_brain_record_has_provider_model_attempts_and_stage_timing(self):
        adapter = await self.adapter(AISIS_CHATGPT_COMMAND='["brain"]', AISIS_CLAUDE_MODEL="claude-test")
        del adapter.turn
        answer = {"text": "Фотосинтез — это ...", "conversation_id": "c1", "provider": "claude", "credential_slot": "slot-a",
                  "attempts": [{"slot": "slot-a", "result": "complete", "seconds": 1.2}]}
        with patch("adapter.run_command", AsyncMock(return_value=answer)):
            await self.speak(adapter, "что такое фотосинтез")
        (trace,) = self.traces()
        self.assertEqual(trace["layer"], "brain")
        self.assertEqual((trace["brain"]["provider"], trace["brain"]["slot"], trace["brain"]["model"]), ("claude", "slot-a", "claude-test"))
        self.assertEqual(trace["brain"]["attempts"], [{"slot": "slot-a", "result": "complete", "s": 1.2}])
        self.assertIn("brain", trace["stages_ms"])
        self.assertNotIn("Фотосинтез", json.dumps(trace["brain"], ensure_ascii=False))

    async def test_the_brain_record_keeps_the_routers_key_states_and_drops_anything_malformed(self):
        adapter = await self.adapter(AISIS_CHATGPT_COMMAND='["brain"]')
        del adapter.turn
        answer = {"text": "Ответ", "conversation_id": "c1", "provider": "claude", "credential_slot": "calendar",
                  "attempts": [{"slot": "finance", "result": "cooldown"}, {"slot": "calendar", "result": "complete", "seconds": 1.5}],
                  "keys": [{"slot": "finance", "ok": False, "until": 1796083200.0}, {"slot": "calendar", "ok": True, "until": None},
                           {"slot": "report", "ok": True, "until": "soon", "secret": "sk-ant-not-copied"}, "junk", {"slot": 5, "ok": True}]}
        with patch("adapter.run_command", AsyncMock(return_value=answer)):
            await self.speak(adapter, "что такое фотосинтез")
        (trace,) = self.traces()
        self.assertEqual(trace["brain"]["keys"], [{"slot": "finance", "ok": False, "until": 1796083200.0},
                                                  {"slot": "calendar", "ok": True, "until": None}, {"slot": "report", "ok": True, "until": None}])
        self.assertNotIn("sk-ant", json.dumps(trace["brain"]))

    async def test_a_router_without_key_states_leaves_the_record_as_before(self):
        adapter = await self.adapter(AISIS_CHATGPT_COMMAND='["brain"]')
        del adapter.turn
        with patch("adapter.run_command", AsyncMock(return_value={"text": "Ответ", "conversation_id": "c1", "provider": "claude"})):
            await self.speak(adapter, "что такое фотосинтез")
        (trace,) = self.traces()
        self.assertNotIn("keys", trace["brain"])

    async def test_a_brain_failure_is_an_error_layer_with_the_reason(self):
        adapter = await self.adapter()
        adapter.turn = AsyncMock(side_effect=RuntimeError("boom"))
        data = await self.speak(adapter, "что такое фотосинтез")
        (trace,) = self.traces()
        self.assertIn("Не удалось", data["text"])
        self.assertEqual((trace["layer"], trace["errors"]), ("error", ["RuntimeError"]))

    async def test_home_assistant_calls_carry_service_entity_data_status_and_room(self):
        adapter = await self.adapter(AISIS_ALLOWED_STATIONS=LIVING)
        fake = FakeHa()
        adapter.intents.ha = ha_actions.HaActions(fake.get, fake.post)
        # the speaker is in the living room: its player sits in that HA area, so «поставь кондиционер на 24» needs no room word
        data = await self.speak(adapter, "поставь кондиционер на 24", station=LIVING)
        trace = self.traces()[-1]
        self.assertEqual(trace["layer"], "grammar")
        calls = trace["calls"]
        self.assertEqual([c["service"] for c in calls], ["climate.set_temperature", "climate.read_back"])
        self.assertEqual((calls[0]["entity"], calls[0]["data"], calls[0]["status"], calls[0]["ok"]),
                         ("climate.kondei_1", {"temperature": 24}, 200, True))
        self.assertEqual((calls[1]["entity"], calls[1]["ok"], calls[1]["state"]), ("climate.kondei_1", True, "cool"), "the state read back after the command")
        self.assertEqual(trace["room"], "Гостиная")
        self.assertTrue(data["text"])

    async def test_a_home_assistant_outage_is_recorded_as_a_failed_call(self):
        adapter = await self.adapter(AISIS_ALLOWED_STATIONS=LIVING)
        fake = FakeHa(service_status=500)
        adapter.intents.ha = ha_actions.HaActions(fake.get, fake.post)
        await self.speak(adapter, "поставь кондиционер на 24", station=LIVING)
        trace = self.traces()[-1]
        self.assertEqual((trace["calls"][0]["ok"], trace["calls"][0]["status"]), (False, 500))

    async def test_a_timer_start_is_a_recorded_call(self):
        adapter = await self.adapter()
        started = []

        class Timers:
            async def start(self, seconds, note):
                started.append(seconds)
                return {"seconds": seconds}

            def remaining(self):
                return []

        adapter.intents.timers = Timers()
        await self.speak(adapter, "поставь таймер на пять минут")
        (trace,) = self.traces()
        self.assertEqual(started, [300])
        self.assertEqual((trace["calls"][0]["kind"], trace["calls"][0]["executor"], trace["calls"][0]["ok"]), ("timer", "timer.start", True))

    async def test_weather_cli_is_a_recorded_call_and_its_failure_an_error(self):
        adapter = await self.adapter(AISIS_WEATHER_COMMAND='["/nonexistent/weather"]')
        import voice_skills
        adapter.skills = voice_skills.Skills(adapter.env)
        adapter.intents.skills = adapter.skills
        await self.speak(adapter, "погода завтра")
        (trace,) = self.traces()
        self.assertEqual((trace["intent"], trace["calls"][0]["kind"], trace["calls"][0]["ok"]), ("skill.weather", "weather-cli", False))
        self.assertEqual(trace["errors"], ["weather-cli: launch failed"])

    async def test_jev_on_records_the_verdict_and_that_it_acted(self):
        adapter = await self.adapter(AISIS_JEV="on", AISIS_JEV_REPROMPT="on")
        adapter.jev = Scripted(jev.RouteDecision("deterministic", "jev_garbled", utterance="garbled", confidence=0.99))
        await self.speak(adapter, "ляля тополя ляля ляля ляля")
        (trace,) = self.traces()
        self.assertEqual((trace["layer"], trace["intent"], trace["asked"]), ("jev", "jev.garbled", "reprompt"))
        self.assertEqual((trace["jev"]["mode"], trace["jev"]["acted"], trace["jev"]["decision"], trace["jev"]["utterance"]),
                         ("on", True, "jev_garbled", "garbled 0.99"))
        self.assertIn("jev", trace["stages_ms"])

    async def test_the_quasar_hook_stores_a_hook_trace_without_a_station(self):
        adapter = await self.adapter()
        async with TestClient(TestServer(adapter.app())) as client:
            hook = await client.post("/intent/decide", json={"text": "который час"}, headers={"Authorization": "Bearer test-only-token"})
            self.assertEqual(hook.status, 200)
            shadow = await client.post("/intent/decide", json={"text": "Алиса, привет", "shadow": True},
                                       headers={"Authorization": "Bearer test-only-token"})
            self.assertEqual(shadow.status, 200)
            await adapter.flush_traces()
        hooked, shadowed = self.traces()
        self.assertEqual((hooked["via"], hooked["layer"], hooked["intent"], hooked["route"]), ("hook", "grammar", "time.now", "reply"))
        self.assertNotIn("station", hooked)
        expected = adapter.intents.classify("Алиса, привет", "pre")
        self.assertIsNotNone(expected)
        self.assertEqual((shadowed["via"], shadowed["layer"], shadowed["intent"]), ("hook-shadow", "none", expected.intent))

    async def test_a_turn_without_speech_and_a_rejected_station_store_nothing(self):
        adapter = await self.adapter()
        ws = AsyncMock()
        ws.closed = False
        await adapter._process_turn(ws, {"text": "  ", "metadata": {"stationId": STATION}})
        await adapter._process_turn(ws, {"text": "привет", "metadata": {"stationId": "other"}})
        await adapter.flush_traces()
        self.assertEqual(self.traces() if self.has_table() else [], [])

    def has_table(self):
        conn = sqlite3.connect(self.db)
        try:
            return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='utterance_traces'").fetchone())
        finally:
            conn.close()

    async def test_off_switch_and_a_broken_store_never_cost_the_reply(self):
        off = await self.adapter(AISIS_UTTERANCE_TRACE="off")
        self.assertTrue((await self.speak(off, "который час"))["text"])
        self.assertFalse(self.has_table())
        broken = await self.adapter(AISIS_DIALOGS_DB=self.dir.name + "/missing-dir/dialogs.sqlite3")
        with self.assertLogs("aisis.yandex", "WARNING") as logs:
            self.assertTrue((await self.speak(broken, "который час"))["text"])
        self.assertTrue(any("utterance_trace_store_failed" in line for line in logs.output))

    async def test_traces_land_on_the_dialog_that_was_running(self):
        adapter = await self.adapter()
        await self.speak(adapter, "который час")
        conn = dialogstore.init_db(self.db)
        trace_ts = conn.execute("SELECT ts FROM utterance_traces").fetchone()[0]
        conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,ts_end,status) VALUES('d-1',?,'voice','docker:x',?,?,'serverFinished')",
                     (STATION, trace_ts - 2, trace_ts + 4))
        conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,ts_end,status) VALUES('d-0',?,'voice','docker:x',?,?,'serverFinished')",
                     (STATION, trace_ts - 600, trace_ts - 590))
        conn.commit()
        dialog = dialogstore.get_dialog(conn, "d-1")
        other = dialogstore.get_dialog(conn, "d-0")
        conn.close()
        self.assertEqual([t["intent"] for t in dialog["traces"]], ["time.now"])
        self.assertEqual(other["traces"], [])

    async def test_a_brain_answer_with_odd_metadata_is_still_delivered(self):
        adapter = await self.adapter(AISIS_CHATGPT_COMMAND='["brain"]')
        del adapter.turn
        with patch("adapter.run_command", AsyncMock(return_value={"text": "VALID REPLY", "conversation_id": "c1", "attempts": 3})):
            data = await self.speak(adapter, "что такое фотосинтез")
        self.assertEqual(data["text"], "VALID REPLY")
        (trace,) = self.traces()
        self.assertEqual(trace["layer"], "brain")
        self.assertNotIn("attempts", trace["brain"])

    async def test_a_socket_that_breaks_while_sending_still_leaves_the_record(self):
        for failure in (ConnectionResetError, asyncio.CancelledError):
            with self.subTest(failure=failure.__name__):
                adapter = await self.adapter()
                ws = AsyncMock()
                ws.closed = False
                ws.send_json.side_effect = failure()
                before = len(self.traces()) if self.has_table() else 0
                with self.assertRaises(failure):
                    await adapter._process_turn(ws, {"text": "который час", "metadata": {"stationId": STATION}})
                await adapter.flush_traces()
                trace = self.traces()[before]
                self.assertEqual((trace["layer"], trace["intent"]), ("grammar", "time.now"))
                self.assertEqual(trace["errors"], ["reply not delivered: " + failure.__name__])


class HaCacheMarkTests(unittest.IsolatedAsyncioTestCase):
    """hacli serves its old cache with `X-Aisis-Cache: stale` while Home Assistant is down; the adapter hands that to the executors as `_stale`."""

    async def test_a_stale_entity_is_marked_and_a_live_one_is_not(self):
        from aiohttp import ClientSession, web
        from types import SimpleNamespace

        async def reply(request):
            return web.json_response({"entity_id": "climate.x"}, headers={"X-Aisis-Cache": request.match_info["kind"]})

        app = web.Application()
        app.router.add_get("/api/states/{kind}", reply)
        async with TestServer(app) as server, ClientSession() as session:
            host = SimpleNamespace(intent_ha_url=str(server.make_url("")).rstrip("/"), _hacli=lambda: session)
            self.assertEqual(await Adapter._ha_get_json(host, "/api/states/stale"), {"entity_id": "climate.x", "_stale": True})
            self.assertEqual(await Adapter._ha_get_json(host, "/api/states/live"), {"entity_id": "climate.x"})



if __name__ == "__main__":
    unittest.main()
