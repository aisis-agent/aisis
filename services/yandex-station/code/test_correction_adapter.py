"""The repair dialog through the adapter: the AISIS_CORRECTION switch, the /intent/decide fields (deviceId, rerun, noop, context), the speaker's own
turn path, and the question the brain is told about when a reply does not answer it."""
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

import dialog_context
import hypotheses_fakes as fakes
import intent_route
from adapter import Adapter
from test_correction import Clock, FakeCore, Store

HEADERS = {"Authorization": "Bearer test-only-token"}
NOTHING_READ_YES = "Прозвучало «да», но что сделать, не ясно: музыка?"   # the catch-all's line (clarify.py); the only kind of action this fixture has is the music skill


class AdapterCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": self.dir.name + "/adapter.sqlite3", "AISIS_MEMORY": "0",
                    "AISIS_UTTERANCE_TRACE": "off", "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": "test-only-token",
                    "AISIS_HA_ACTIONS": "off"}
        self.core = FakeCore()
        patcher = mock.patch("correction.import_core", return_value=self.core)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def adapter(self, **extra):
        adapter = Adapter({**self.env, **extra})
        adapter.turn = AsyncMock(return_value="ответ мозга")
        adapter.start = AsyncMock()
        adapter.close = AsyncMock()
        if adapter.intents is not None and adapter.intents.correction is not None:
            self.store, self.clock = Store(), Clock()
            adapter.intents.hypotheses = adapter.intents.correction.hypotheses = self.store
            adapter.intents.monotonic = self.clock
        return adapter

    async def spoken(self, adapter, text):
        ws = AsyncMock()
        ws.closed = False
        await adapter._process_turn(ws, {"text": text, "metadata": {"stationId": "st1"}})
        data = ws.send_json.await_args.args[0]["data"]
        return data["text"], data["shouldListen"]


class SwitchTests(AdapterCase):
    async def test_the_dialog_is_off_by_default_and_for_any_unknown_value(self):
        for extra in ({}, {"AISIS_CORRECTION": "off"}, {"AISIS_CORRECTION": "banana"}):
            adapter = await self.adapter(**extra)
            self.assertIsNone(adapter.intents.correction, extra)
        adapter = await self.adapter()
        async with TestClient(TestServer(adapter.app())) as client:
            data = await (await client.get("/healthz")).json()
        self.assertEqual(data["correction"], {"mode": "off", "active": False})

    async def test_on_and_shadow_build_the_engine(self):
        for mode in ("on", "shadow", "ON"):
            adapter = await self.adapter(AISIS_CORRECTION=mode)
            self.assertEqual(adapter.intents.correction.mode, mode.lower())
        async with TestClient(TestServer(adapter.app())) as client:
            data = await (await client.get("/healthz")).json()
        self.assertEqual(data["correction"], {"mode": "on", "active": True})

    async def test_a_core_that_is_not_installed_leaves_the_speaker_as_it_was(self):
        with mock.patch("correction.import_core", side_effect=ImportError("no stt_cli")):
            adapter = await self.adapter(AISIS_CORRECTION="on")
        self.assertIsNone(adapter.intents.correction)
        fixed = datetime(2026, 11, 20, 9, 30).astimezone()
        adapter.intents.now = lambda: fixed
        self.assertEqual(await self.spoken(adapter, "который час"), (intent_route.clock_text(fixed), False))

    async def test_off_changes_nothing_for_a_phrase_with_a_second_opinion(self):
        adapter = await self.adapter()
        self.assertEqual(await self.spoken(adapter, "расскажи про дракона"), ("ответ мозга", False))

    async def test_shadow_traces_the_verdict_and_leaves_the_answer_alone(self):
        adapter = await self.adapter(AISIS_CORRECTION="shadow", AISIS_UTTERANCE_TRACE="on")
        stored = []
        with mock.patch("adapter.dialogstore.add_trace", side_effect=lambda db, record: stored.append(record)):
            self.store.put("st1", "включи свет", ("выключи свет", 0.7), ("включи свет", 0.3))
            self.assertEqual(await self.spoken(adapter, "включи свет"), ("ответ мозга", False))     # the old path, untouched
            self.assertEqual(await self.spoken(adapter, "да"), (NOTHING_READ_YES, True))   # no context was opened
            await adapter.flush_traces()
        by_text = {record["text"]: record for record in stored}   # the traces are written by independent threads: no order
        record = by_text["включи свет"]["correction"]
        self.assertEqual((record["shadow"], record["decision"], record["trigger"]), (True, "ask", "confusable-disagreement"))
        self.assertNotIn("correction", by_text["да"])


class HookTests(AdapterCase):
    async def ask(self, client, **body):
        response = await client.post("/intent/decide", json=body, headers=HEADERS)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_a_noop_probe_opens_the_question_and_a_yes_reruns_the_alternative(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            noop = {"desired": "on", "label": "свет в студии", "targets": ["light.studio"]}
            first = await self.ask(client, text="включи свет", dialogActive=False, deviceId="st1")
            self.assertEqual((first["route"], "context" in first), ("none", False))
            asked = await self.ask(client, text="включи свет", dialogActive=False, deviceId="st1", noop=noop)
            self.assertEqual((asked["route"], asked["text"], asked["listen"], asked["context"]),
                             ("reply", "Свет в студии уже включён. Вы хотели выключить?", True, True))
            answer = await self.ask(client, text="да", dialogActive=False, deviceId="st1", rerun=True)
            self.assertEqual((answer["route"], answer["text"], "context" in answer), ("rerun", "выключи свет", False))

    async def test_without_rerun_support_the_owner_is_asked_to_say_it_again(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            await self.ask(client, text="включи свет", deviceId="st1", noop={"desired": "on", "label": "свет", "targets": []})
            answer = await self.ask(client, text="да", deviceId="st1")
            self.assertEqual((answer["route"], answer["text"], answer["listen"]), ("reply", "Скажите ещё раз: выключи свет.", True))

    async def test_a_second_opinion_opens_it_without_a_noop(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        self.store.put("st1", "включи свет", ("выключи свет", 0.7), ("включи свет", 0.3))
        async with TestClient(TestServer(adapter.app())) as client:
            asked = await self.ask(client, text="включи свет", deviceId="st1")
            self.assertEqual(asked["text"], "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?")

    async def test_malformed_extra_fields_are_ignored(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            for body in ({"noop": "yes"}, {"noop": ["a"]}, {"deviceId": 5}, {"deviceId": ""}, {"deviceId": "x" * 200}, {"rerun": "true"}):
                data = await self.ask(client, text="включи свет", **body)
                self.assertEqual(data["route"], "none", body)

    async def test_the_old_contract_is_untouched_when_the_dialog_is_off(self):
        adapter = await self.adapter()
        async with TestClient(TestServer(adapter.app())) as client:
            data = await self.ask(client, text="Алиса, привет", dialogActive=False)
            self.assertEqual(set(data), {"route", "text", "listen", "volume", "intent", "ms"})
            data = await self.ask(client, text="включи свет", noop={"desired": "on", "label": "свет", "targets": []}, deviceId="st1")
            self.assertEqual(data["route"], "none")

    async def test_cancel_over_the_hook_ends_the_overlays_dialog(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            await self.ask(client, text="включи свет", deviceId="st1", noop={"desired": "on", "label": "свет", "targets": []})
            data = await self.ask(client, text="отмена", deviceId="st1")
            self.assertEqual((data["route"], "context" in data), ("dialog_end", False))


class EvidenceFlowTests(AdapterCase):
    """The production wiring end to end: the evidence the STT gateway POSTs is what the dialog reads. The adapter is built the normal
    way (no store is assigned by hand); only the core's `same_text` is stood in for, so the suite needs no stt-cli install."""
    NBEST = {"engine": "gigaam-v3-ctc", "ms": 88, "hypotheses": [
        {"text": "выключи свет", "percent": 70, "posterior": 0.7, "fit": 0.9, "source": "beam", "engine": "gigaam-v3-ctc"},
        {"text": "включи свет", "percent": 30, "posterior": 0.3, "fit": 0.9, "source": "beam", "engine": "gigaam-v3-ctc"}]}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        modules = {name: types.ModuleType(name) for name in ("stt_cli", "stt_cli.hypotheses", "stt_cli.hypotheses.model")}
        modules["stt_cli.hypotheses.model"].same_text = fakes.same_text
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def adapter(self, **extra):
        adapter = Adapter({**self.env, **extra})
        adapter.turn = AsyncMock(return_value="ответ мозга")
        adapter.start = AsyncMock()
        adapter.close = AsyncMock()
        return adapter

    async def post_evidence(self, client, text="Включи свет.", **extra):
        body = {"station": None, "text": text, "source": "whisper", "nbest": self.NBEST, "ms": 91, **extra}
        response = await client.post("/asr/hypotheses", json=body, headers=HEADERS)
        self.assertEqual(response.status, 204)

    async def decide(self, client, **body):
        response = await client.post("/intent/decide", json={"deviceId": "st1", **body}, headers=HEADERS)
        return await response.json()

    async def test_the_engine_reads_the_gateways_store_not_a_private_one(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        self.assertIs(adapter.intents.hypotheses, adapter.hypotheses)
        self.assertIs(adapter.intents.correction.hypotheses, adapter.hypotheses)

    async def test_posted_evidence_opens_the_question_on_the_hook_and_a_yes_reruns_the_alternative(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            await self.post_evidence(client)
            asked = await self.decide(client, text="включи свет")
            self.assertEqual((asked["route"], asked["text"], asked["listen"], asked["context"]),
                             ("reply", "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?", True, True))
            answer = await self.decide(client, text="да", rerun=True)
            self.assertEqual((answer["route"], answer["text"]), ("rerun", "выключи свет"))

    async def test_posted_evidence_opens_the_question_on_the_speakers_own_turn(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            await self.post_evidence(client)
        self.assertEqual(await self.spoken(adapter, "включи свет"), ("Мне послышалось «включи свет». Вы имели в виду «выключи свет»?", True))
        adapter.turn.assert_not_awaited()

    async def test_posted_evidence_for_the_reply_vetoes_a_yes(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            await self.post_evidence(client)
            await self.decide(client, text="включи свет")
            await self.post_evidence(client, text="да", nbest={"engine": "gigaam-v3-ctc", "ms": 70, "hypotheses": [
                {"text": "вызови скорую", "percent": 80, "posterior": 0.8, "fit": 0.9, "source": "beam", "engine": "gigaam-v3-ctc"},
                {"text": "да", "percent": 20, "posterior": 0.2, "fit": 0.9, "source": "beam", "engine": "gigaam-v3-ctc"}]})
            answer = await self.decide(client, text="да", rerun=True)
            self.assertEqual((answer["route"], answer["text"]), ("reply", "Скажите «да» или «нет»."))

    async def test_agreeing_evidence_and_no_evidence_ask_nothing(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        async with TestClient(TestServer(adapter.app())) as client:
            self.assertEqual((await self.decide(client, text="включи свет"))["route"], "none")
            await self.post_evidence(client, nbest={**self.NBEST, "hypotheses": [{**self.NBEST["hypotheses"][1], "posterior": 0.9, "percent": 90},
                                                                              {**self.NBEST["hypotheses"][0], "posterior": 0.1, "percent": 10}]})
            self.assertEqual((await self.decide(client, text="включи свет"))["route"], "none")

    async def test_shadow_traces_the_posted_evidence_and_asks_nothing(self):
        adapter = await self.adapter(AISIS_CORRECTION="shadow", AISIS_UTTERANCE_TRACE="on")
        stored = []
        with mock.patch("adapter.dialogstore.add_trace", side_effect=lambda db, record: stored.append(record)):
            async with TestClient(TestServer(adapter.app())) as client:
                await self.post_evidence(client)
            self.assertEqual(await self.spoken(adapter, "включи свет"), ("ответ мозга", False))
            await adapter.flush_traces()
        record = stored[0]["correction"]
        self.assertEqual((record["shadow"], record["decision"], record["trigger"]), (True, "ask", "confusable-disagreement"))



class RealCoreEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """The gateway's payload as the real core writes it (`NBest.as_dict`) through the real store and the real engine."""

    async def test_what_the_gateway_posts_is_what_the_dialog_asks_about(self):
        try:
            from stt_cli.hypotheses import NBest
        except ImportError:
            self.skipTest("stt_cli.hypotheses is not installed (develop against the stt-cli worktree with PYTHONPATH)")
        with tempfile.TemporaryDirectory() as directory:
            adapter = Adapter({"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": directory + "/adapter.sqlite3", "AISIS_MEMORY": "0",
                               "AISIS_UTTERANCE_TRACE": "off", "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": "test-only-token",
                               "AISIS_HA_ACTIONS": "off", "AISIS_CORRECTION": "on"})
            nbest = NBest.from_logprobs([("выключи свет", -0.5), ("включи свет", -1.5)], engine="gigaam-v3-ctc", ms=88)
            async with TestClient(TestServer(adapter.app())) as client:
                body = {"station": None, "text": "Включи свет.", "source": "whisper", "nbest": nbest.as_dict(), "ms": 91}
                self.assertEqual((await client.post("/asr/hypotheses", json=body, headers=HEADERS)).status, 204)
                response = await client.post("/intent/decide", json={"text": "включи свет", "deviceId": "st1"}, headers=HEADERS)
                asked = await response.json()
        self.assertEqual((asked["route"], asked["text"]), ("reply", "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?"))


class TurnPathTests(AdapterCase):
    async def test_the_speakers_own_turn_asks_and_reads_the_answer(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        self.store.put("st1", "включи свет", ("выключи свет", 0.7), ("включи свет", 0.3))
        self.assertEqual(await self.spoken(adapter, "включи свет"), ("Мне послышалось «включи свет». Вы имели в виду «выключи свет»?", True))
        self.assertEqual(await self.spoken(adapter, "да"), ("Скажите ещё раз: выключи свет.", True))
        adapter.turn.assert_not_awaited()

    async def test_no_asks_to_repeat_and_cancel_is_silent(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        self.store.put("st1", "включи свет", ("выключи свет", 0.7), ("включи свет", 0.3))
        await self.spoken(adapter, "включи свет")
        self.assertEqual(await self.spoken(adapter, "нет"), ("Тогда что сделать: музыка?", True))
        self.assertEqual(await self.spoken(adapter, "отмена"), ("", False))
        adapter.turn.assert_not_awaited()

    async def test_a_context_less_yes_goes_the_old_way(self):
        for mode in ("off", "on"):
            adapter = await self.adapter(AISIS_CORRECTION=mode)
            self.assertEqual(await self.spoken(adapter, "да"), (NOTHING_READ_YES, True), mode)   # the catch-all, as before: what was heard, what can be asked for
            adapter.turn.assert_not_awaited()

    async def test_an_emergency_inside_the_dialog_gets_the_number(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        self.store.put("st1", "включи свет", ("выключи свет", 0.7), ("включи свет", 0.3))
        await self.spoken(adapter, "включи свет")
        reply, listen = await self.spoken(adapter, "мне плохо вызови скорую")
        self.assertIn("сто двенадцать", reply)
        adapter.turn.assert_not_awaited()

    async def test_a_timeout_makes_the_yes_a_plain_turn_again(self):
        adapter = await self.adapter(AISIS_CORRECTION="on")
        self.store.put("st1", "включи свет", ("выключи свет", 0.7), ("включи свет", 0.3))
        await self.spoken(adapter, "включи свет")
        self.clock.t += 31
        self.assertEqual(await self.spoken(adapter, "да"), (NOTHING_READ_YES, True))   # no dialog any more: the old path


class BrainQuestionTests(AdapterCase):
    """An unmatched reply reaches the brain with the question the speaker had just asked."""

    async def real_turn_adapter(self):
        adapter = Adapter({**self.env, "AISIS_CORRECTION": "on", "AISIS_CHATGPT_COMMAND": '["brain"]'})
        adapter.start, adapter.close = AsyncMock(), AsyncMock()
        self.store, self.clock = Store(), Clock()
        adapter.intents.hypotheses = adapter.intents.correction.hypotheses = self.store
        adapter.intents.monotonic = self.clock
        return adapter

    async def test_the_turn_text_carries_the_question(self):
        adapter = await self.real_turn_adapter()
        self.store.put("st1", "включи свет", ("выключи свет", 0.7), ("включи свет", 0.3))
        sent = []

        async def brain(command, payload, timeout):
            sent.append(payload["text"])
            return {"text": "про дракона так про дракона"}

        with mock.patch("adapter.run_command", brain):
            await self.spoken(adapter, "включи свет")
            reply, _listen = await self.spoken(adapter, "а расскажи лучше про дракона")
            self.assertEqual(reply, "про дракона так про дракона")
            await self.spoken(adapter, "а ещё про принцессу и что было дальше у них двоих")
        self.assertIn("[Только что был задан вопрос «Мне послышалось «включи свет». Вы имели в виду «выключи свет»?»; пользователь отвечает]\n"
                      "Пользователь: а расскажи лучше про дракона", sent[0].replace("\n\n", "\n"))
        self.assertNotIn("Только что был задан вопрос", sent[1])          # once

    async def test_the_block_is_sanitised_and_clipped(self):
        block = dialog_context.question_block("Мне послышалось «включи] [свет\n\x00». Вы имели в виду «" + "х" * 400 + "»?")
        self.assertTrue(block.startswith("[Только что был задан вопрос «"))
        self.assertTrue(block.endswith("»; пользователь отвечает]\n"))
        inner = block[len("[Только что был задан вопрос «"):-len("»; пользователь отвечает]\n")]
        self.assertNotIn("]", inner)
        self.assertNotIn("[", inner)
        self.assertNotIn("\n", inner)
        self.assertNotIn("\x00", inner)
        self.assertLessEqual(len(inner), dialog_context.QUESTION_CHARS)
        self.assertEqual(dialog_context.question_block(None), "")
        self.assertEqual(dialog_context.question_block(""), "")


if __name__ == "__main__":
    unittest.main()
