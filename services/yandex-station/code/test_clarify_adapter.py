"""The proactive clarification (#307) through the adapter's own turn path: a phrase nobody could read is answered with the
likely actions drawn from the live Home Assistant, as a short question; a «да» does it with one standard service call; a bare
repeat request is never spoken. The incident is the recorded one (traces 125/126, 2026-11-26): «Ставьте их на паузу.» with the
television playing."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

import ha_client
import ha_fakes
from adapter import Adapter
from test_correction import Clock, FakeCore

HEADERS = {"Authorization": "Bearer test-only-token"}
TV = "media_player.televizor"


def home(tv_state="playing", with_tv=True):
    states = [ha_fakes.entity("light.studio_lamp", "on", friendly_name="Лампа в студии"),
              ha_fakes.entity("climate.upstairs", "off", friendly_name="Кондиционер наверху")]
    if with_tv:
        states.append(ha_fakes.entity(TV, tv_state, friendly_name="Телевизор"))
    return states


class ClarifyCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.fake = ha_fakes.FakeHomeAssistant(home())
        self.addCleanup(self.fake.close)
        self.env = {"AISIS_ALLOWED_STATIONS": "st1", "AISIS_STATE_DB": self.dir.name + "/adapter.sqlite3", "AISIS_MEMORY": "0",
                    "AISIS_UTTERANCE_TRACE": "off", "AISIS_CHATGPT_COMMAND": "[]", "AISIS_ADAPTER_TOKEN": "test-only-token"}
        self.store = ha_client.Store(Path(self.dir.name) / "connection.json")
        self.store.save(ha_client.Connection(self.fake.url, "long_lived", "llat"))
        patcher = mock.patch("correction.import_core", return_value=FakeCore())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def adapter(self, brain="Повтори?", **extra):
        adapter = Adapter({**self.env, **extra})
        adapter.ha_client = ha_client.HaClient(self.store, discover_fn=lambda **_: [])
        adapter.turn = AsyncMock(return_value=brain)
        adapter.start = AsyncMock()
        adapter.close = AsyncMock()
        self.clock = Clock()
        adapter.intents.monotonic = self.clock
        return adapter

    async def spoken(self, adapter, text):
        ws = AsyncMock()
        ws.closed = False
        data = {"text": text, "metadata": {"stationId": "st1"}}
        await adapter._process_turn(ws, data)
        reply = ws.send_json.await_args.args[0]["data"]
        return reply["text"], reply["shouldListen"]

    def paused_calls(self):
        return [(path, body) for kind, path, body in self.fake.requests if kind == "WS" and path.endswith("/media_pause")]


class IncidentTests(ClarifyCase):
    async def test_the_misheard_pause_asks_about_the_playing_tv_and_a_yes_pauses_it(self):
        adapter = await self.adapter()
        said, listen = await self.spoken(adapter, "Ставьте их на паузу.")
        self.assertEqual(said, "Поставить телевизор на паузу?")
        self.assertTrue(listen)                        # the microphone stays open for the answer
        adapter.turn.assert_not_awaited()              # nothing waited two seconds for the brain
        self.assertEqual(self.paused_calls(), [])      # a guess runs nothing before the yes

        said, _ = await self.spoken(adapter, "да")
        self.assertEqual(self.paused_calls(), [("/api/services/media_player/media_pause", {"entity_id": TV})])
        self.assertEqual(self.fake.states[TV]["state"], "paused")
        self.assertIn("пауз", said.lower())
        self.assertNotIn("повтор", said.lower())

    async def test_a_no_offers_what_the_home_can_do_instead_of_asking_to_repeat(self):
        adapter = await self.adapter()
        await self.spoken(adapter, "Ставьте их на паузу.")
        said, listen = await self.spoken(adapter, "нет")
        self.assertEqual(self.paused_calls(), [])
        self.assertNotIn("повтор", said.lower())
        self.assertIn("свет", said.lower())            # the kinds of action this home has, from its catalog
        self.assertTrue(listen)

    async def test_a_paused_tv_is_offered_to_continue_not_to_pause(self):
        self.fake.states[TV]["state"] = "paused"
        adapter = await self.adapter()
        said, _ = await self.spoken(adapter, "продолжите просмотр телика")
        self.assertEqual(said, "Продолжить телевизор?")

    async def test_without_a_tv_in_the_catalog_no_question_names_one(self):
        self.fake.states.pop(TV)
        adapter = await self.adapter()
        said, _ = await self.spoken(adapter, "Ставьте их на паузу.")
        self.assertNotIn("телевизор", said.lower())
        self.assertNotIn("повтор", said.lower())


class NothingToOfferTests(ClarifyCase):
    async def test_the_brain_asking_to_repeat_becomes_what_was_heard_and_what_can_be_done(self):
        adapter = await self.adapter(brain="Повтори?")
        said, _ = await self.spoken(adapter, "мумба юмба тарабарщина")
        self.assertIn("мумба юмба тарабарщина", said)
        for kind in ("свет", "телевизор", "кондиционер"):   # the live catalog's kinds
            self.assertIn(kind, said)
        self.assertNotIn("повтор", said.lower())

    async def test_a_brain_answer_that_is_an_answer_is_kept(self):
        adapter = await self.adapter(brain="Столица Австралии — Канберра.")
        self.assertEqual(await self.spoken(adapter, "столица австралии"), ("Столица Австралии — Канберра.", False))

    async def test_a_turn_without_speech_says_so_and_what_can_be_done(self):
        adapter = await self.adapter()
        said, _ = await self.spoken(adapter, "")
        self.assertIn("ничего", said.lower())
        self.assertIn("свет", said)
        self.assertNotIn("повтор", said.lower())

    async def test_an_unreachable_home_still_gets_a_line_that_names_what_was_heard(self):
        self.fake.stop_listening()
        adapter = await self.adapter(brain="Не расслышал, уточни.")
        said, _ = await self.spoken(adapter, "тарабарщина")
        self.assertIn("тарабарщина", said)
        self.assertNotIn("повтор", said.lower())
        self.assertNotIn("уточни", said.lower())

    async def test_with_the_grammar_in_shadow_no_question_is_opened_nobody_could_answer(self):
        adapter = await self.adapter(AISIS_INTENT_GRAMMAR="shadow")
        said, listen = await self.spoken(adapter, "Ставьте их на паузу.")
        self.assertEqual(said, "Прозвучало «Ставьте их на паузу», но что сделать, не ясно.")
        self.assertFalse(listen)
        self.assertEqual(adapter.intents.correction, None)       # no dialog engine was even built
        self.assertEqual([r for r in self.fake.requests if r[0] == "GET" and r[1].startswith("/api/states")], [])      # and Home Assistant was not read for it

    async def test_an_emergency_phrase_is_never_turned_into_a_question(self):
        adapter = await self.adapter()
        said, _ = await self.spoken(adapter, "вызови скорую")
        self.assertIn("двенадцать", said)              # the grammar's own emergency answer, before any clarifier


if __name__ == "__main__":
    unittest.main()
