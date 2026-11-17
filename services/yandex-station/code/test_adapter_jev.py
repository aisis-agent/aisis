"""Jev in the adapter: acts only on a confident decision of a class the owner switched on, never on an emergency phrase,
shadow never changes the reply, a slow or broken Jev costs nothing."""
import asyncio
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

import intent_route
import jev
from adapter import Adapter

GARBLED_PHRASE = "ляля тополя ляля ляля ляля"
COMMAND_PHRASE = "закажи мне пиццу на вечер с доставкой"
EMERGENCIES = ("Мне плохо, вызови скорую", "Позвони в полицию")


def decision(reason, utterance, confidence=0.99, alias=None, effort="none"):
    kind = "model" if utterance == "talk" else "deterministic"
    return jev.RouteDecision(kind, reason, model_alias=alias, effort=effort, utterance=utterance, confidence=confidence)


class Scripted:
    """A Jev provider answering a fixed decision: confident (`ok`), below the threshold, or an abstention."""

    def __init__(self, scripted=None, reason="ok", raises=None):
        self.scripted, self.reason, self.raises, self.calls = scripted, reason, raises, []
        self.stats = {"calls": 0}

    async def assess(self, turn):
        self.calls.append(turn.text)
        if self.raises is not None:
            raise self.raises
        if self.scripted is None:
            return jev.Verdict(None, "breaker_open")
        answers = {"utterance": jev.ChoiceAnswer(self.scripted.utterance, self.scripted.confidence),
                   "tier": jev.ChoiceAnswer("fast", 0.9), "effort": jev.ChoiceAnswer("none", 0.9),
                   "route": jev.ChoiceAnswer("escalate", 0.8)}
        return jev.Verdict(self.scripted, self.reason, answers, 250, 0.0000257)


class AdapterJevTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": self.dir.name + "/adapter.sqlite3", "AISIS_MEMORY": "0",
                    "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": "test-only-token", "AISIS_HA_ACTIONS": "off"}

    async def adapter(self, mode="on", scripted=None, refuse="on", reprompt="on", **extra):
        adapter = Adapter({**self.env, "AISIS_JEV": mode, "AISIS_JEV_REFUSE": refuse, "AISIS_JEV_REPROMPT": reprompt, **extra})
        adapter.turn = AsyncMock(return_value="ответ мозга")
        adapter.skills = AsyncMock()
        adapter.skills.answer.return_value = None
        if adapter.intents is not None:
            adapter.intents.skills = None
        adapter.jev = scripted
        return adapter

    async def sent(self, adapter, text, station="st1"):
        ws = AsyncMock()
        ws.closed = False
        await adapter._process_turn(ws, {"text": text, "metadata": {"stationId": station}})
        return ws.send_json.await_args.args[0]["data"]

    async def spoken(self, adapter, text, station="st1"):
        return (await self.sent(adapter, text, station))["text"]

    async def test_off_by_default_without_a_key_and_nothing_changes(self):
        adapter = await self.adapter(mode="off")
        self.assertIsNone(adapter.jev)
        self.assertEqual(await self.spoken(adapter, GARBLED_PHRASE), "ответ мозга")
        keyless = Adapter({**self.env, "AISIS_JEV": "on"})
        self.assertIsNone(keyless.jev)
        self.assertEqual(keyless.jev_settings, jev.JevSettings(0.95, False, False))

    async def test_a_confident_garbled_phrase_is_reprompted_once_a_minute_and_keeps_the_microphone_open(self):
        scripted = Scripted(decision("jev_garbled", "garbled"))
        adapter = await self.adapter(scripted=scripted)
        first = await self.sent(adapter, GARBLED_PHRASE)
        self.assertEqual((first["text"], first["shouldListen"]), (intent_route.REPROMPT_TEXT, True))
        adapter.turn.assert_not_awaited()
        second = await self.sent(adapter, GARBLED_PHRASE)
        self.assertEqual((second["text"], second["shouldListen"]), ("ответ мозга", False))   # the cooldown: the brain this time

    async def test_an_unimplemented_command_gets_the_plain_refusal_and_the_microphone_closes(self):
        adapter = await self.adapter(scripted=Scripted(decision("jev_command_unsupported", "command")))
        sent = await self.sent(adapter, COMMAND_PHRASE)
        self.assertEqual((sent["text"], sent["shouldListen"]), (intent_route.UNSUPPORTED_TEXT, False))
        adapter.turn.assert_not_awaited()

    async def test_a_short_answer_to_a_brain_question_is_not_reprompted_by_jev(self):
        scripted = Scripted(decision("jev_garbled", "garbled"))
        adapter = await self.adapter(scripted=scripted)
        adapter.intents.brain_asked["st1"] = adapter.intents.monotonic()   # the brain's last line to this station was a question
        self.assertEqual(await self.spoken(adapter, "про собак"), "ответ мозга")
        adapter.turn.assert_awaited()

    async def test_a_numeric_emergency_number_never_reaches_jev_with_the_grammar_off(self):
        scripted = Scripted(decision("jev_command_unsupported", "command"))
        adapter = await self.adapter(scripted=scripted, AISIS_INTENT_GRAMMAR="off")
        for phrase in ("позвони 194", "набери 192", "позвони 193"):
            with self.subTest(phrase):
                self.assertEqual(await self.spoken(adapter, phrase), "ответ мозга")
        self.assertEqual(scripted.calls, [])   # the guard stopped them before the call

    async def test_refusal_and_reprompt_are_separate_switches(self):
        cases = (("on", "off", "jev_command_unsupported", "command", COMMAND_PHRASE, intent_route.UNSUPPORTED_TEXT),
                 ("on", "off", "jev_garbled", "garbled", GARBLED_PHRASE, "ответ мозга"),
                 ("off", "on", "jev_garbled", "garbled", GARBLED_PHRASE, intent_route.REPROMPT_TEXT),
                 ("off", "on", "jev_command_unsupported", "command", COMMAND_PHRASE, "ответ мозга"))
        for refuse, reprompt, reason, utterance, phrase, want in cases:
            with self.subTest(refuse=refuse, reprompt=reprompt, utterance=utterance):
                scripted = Scripted(decision(reason, utterance))
                adapter = await self.adapter(scripted=scripted, refuse=refuse, reprompt=reprompt)
                self.assertEqual(await self.spoken(adapter, phrase), want)

    async def test_on_with_both_switches_off_is_advisory_and_does_not_wait_for_jev(self):
        scripted = Scripted(decision("jev_command_unsupported", "command"))
        adapter = await self.adapter(scripted=scripted, refuse="off", reprompt="off")
        with self.assertLogs("aisis.yandex", level="INFO") as logs:
            self.assertEqual(await self.spoken(adapter, COMMAND_PHRASE), "ответ мозга")
            await asyncio.gather(*adapter.jev_pending)
        self.assertTrue(any("jev_shadow utterance=command" in line for line in logs.output))
        health = await self.health(adapter)
        self.assertEqual((health["mode"], health["acting"]), ("on", False))

    async def test_a_decision_below_the_threshold_acts_on_nothing_but_is_logged(self):
        scripted = Scripted(decision("jev_command_unsupported", "command", confidence=0.9), reason="below_threshold")
        adapter = await self.adapter(scripted=scripted)
        with self.assertLogs("aisis.yandex", level="INFO") as logs:
            self.assertEqual(await self.spoken(adapter, COMMAND_PHRASE), "ответ мозга")
        line = next(line for line in logs.output if "jev_route" in line)
        self.assertIn("conf=0.90 ", line)
        self.assertIn("decision=jev_command_unsupported outcome=below_threshold", line)

    async def test_talk_abstention_and_not_addressed_go_to_the_brain(self):
        for scripted in (Scripted(decision("jev_talk", "talk", alias="fast", effort="none")), Scripted(None),
                         Scripted(decision("jev_not_addressed", "not_addressed"))):
            adapter = await self.adapter(scripted=scripted)
            self.assertEqual(await self.spoken(adapter, "расскажи мне про динозавров подробно"), "ответ мозга")
            self.assertEqual(scripted.calls, ["расскажи мне про динозавров подробно"])

    async def test_an_emergency_never_gets_a_jev_text(self):
        """Even a Jev that says «command» at 0.99 with refusal and reprompt on: the guard keeps the turn from Jev altogether."""
        for phrase in EMERGENCIES:
            for utterance, reason in (("command", "jev_command_unsupported"), ("garbled", "jev_garbled")):
                with self.subTest(phrase=phrase, utterance=utterance):
                    scripted = Scripted(decision(reason, utterance))
                    adapter = await self.adapter(scripted=scripted, **{"AISIS_INTENT_GRAMMAR": "off"})
                    self.assertIsNone(adapter.intents)             # the grammar's own emergency answer is out of the picture
                    with self.assertLogs("aisis.yandex", level="INFO") as logs:
                        reply = await self.spoken(adapter, phrase)
                    self.assertEqual(reply, "ответ мозга")
                    self.assertNotIn(reply, (intent_route.UNSUPPORTED_TEXT, intent_route.REPROMPT_TEXT))
                    self.assertEqual(scripted.calls, [])
                    self.assertTrue(any("guard=emergency" in line for line in logs.output))

    async def test_the_grammar_answers_an_emergency_before_jev_and_with_no_refusal_text(self):
        for phrase in EMERGENCIES:
            scripted = Scripted(decision("jev_command_unsupported", "command"))
            adapter = await self.adapter(scripted=scripted)
            reply = await self.spoken(adapter, phrase)
            self.assertNotEqual(reply, intent_route.UNSUPPORTED_TEXT)
            self.assertNotEqual(reply, "ответ мозга")
            self.assertEqual(scripted.calls, [])

    async def test_in_shadow_an_emergency_is_decided_and_logged_with_the_guard(self):
        scripted = Scripted(decision("jev_command_unsupported", "command", confidence=0.92))
        adapter = await self.adapter(mode="shadow", scripted=scripted, **{"AISIS_INTENT_GRAMMAR": "off"})
        with self.assertLogs("aisis.yandex", level="INFO") as logs:
            self.assertEqual(await self.spoken(adapter, EMERGENCIES[0]), "ответ мозга")
            await asyncio.gather(*adapter.jev_pending)
        self.assertTrue(any("jev_shadow utterance=command" in line and "guard=emergency" in line for line in logs.output))

    async def test_a_jev_bug_hands_the_turn_to_the_brain(self):
        adapter = await self.adapter(scripted=Scripted(raises=ZeroDivisionError("bug")))
        with self.assertLogs("aisis.yandex", level="WARNING") as logs:
            self.assertEqual(await self.spoken(adapter, COMMAND_PHRASE), "ответ мозга")
        self.assertTrue(any("jev_route_failed class=ZeroDivisionError" in line for line in logs.output))

    async def test_grammar_answers_never_reach_jev(self):
        scripted = Scripted(decision("jev_garbled", "garbled"))
        adapter = await self.adapter(scripted=scripted)
        await self.spoken(adapter, "который час")
        await self.spoken(adapter, "два плюс два")
        self.assertEqual(scripted.calls, [])

    async def test_shadow_logs_the_turn_hash_latency_and_cost_and_never_the_text_or_changes_the_reply(self):
        scripted = Scripted(decision("jev_garbled", "garbled"))
        adapter = await self.adapter(mode="shadow", scripted=scripted)
        with self.assertLogs("aisis.yandex", level="INFO") as logs:
            sent = await self.sent(adapter, GARBLED_PHRASE)
            await asyncio.gather(*adapter.jev_pending)
        self.assertEqual((sent["text"], sent["shouldListen"]), ("ответ мозга", False))
        self.assertEqual(scripted.calls, [GARBLED_PHRASE])
        line = next(line for line in logs.output if "jev_shadow" in line)
        self.assertIn("jev_shadow utterance=garbled tier=fast effort=none conf=0.99", line)
        self.assertIn(f"turn={jev.turn_hash(GARBLED_PHRASE)}", line)
        self.assertIn("ms=250 cost=0.0000257", line)
        self.assertIn("outcome=ok", line)
        self.assertFalse(any("ляля" in line for line in logs.output))       # the transcript is never logged

    async def test_a_slow_jev_costs_the_budget_not_the_turn(self):
        async def slow_post(url, headers, body):
            await asyncio.sleep(5)
        provider = jev.JevRouteProvider(jev.JevClient(slow_post, "test-only-key", timeout=0.05))
        adapter = await self.adapter(scripted=provider)
        started = time.perf_counter()
        with self.assertLogs("aisis.jev", level="WARNING"):
            self.assertEqual(await self.spoken(adapter, "расскажи мне про динозавров подробно"), "ответ мозга")
        self.assertLess(time.perf_counter() - started, 0.5)

    async def health(self, adapter):
        async with TestClient(TestServer(adapter.app())) as client:
            return (await (await client.get("/healthz")).json())["jev"]

    async def test_healthz_reports_the_mode_the_switches_and_the_threshold(self):
        adapter = await self.adapter(scripted=jev.JevRouteProvider(jev.JevClient(AsyncMock(), "test-only-key")),
                                     refuse="on", reprompt="off", AISIS_JEV_THRESHOLD="0.9")
        data = await self.health(adapter)
        self.assertEqual((data["mode"], data["active"], data["acting"]), ("on", True, True))
        self.assertEqual((data["threshold"], data["refuse"], data["reprompt"]), (0.9, True, False))
        self.assertIn("calls", data["stats"])

    async def test_an_invalid_threshold_falls_back_to_the_default_and_is_logged(self):
        with self.assertLogs("aisis.jev", level="WARNING") as logs:
            adapter = await self.adapter(AISIS_JEV_THRESHOLD="0.2")
        self.assertEqual(adapter.jev_settings.threshold, jev.CONFIDENT)
        self.assertIn("name=AISIS_JEV_THRESHOLD", logs.output[0])


if __name__ == "__main__":
    unittest.main()
