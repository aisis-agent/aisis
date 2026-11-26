"""Replay of the clip 20261119T212142-d20a57 («выключи свет», 21:21:42) through an ISOLATED stack: a real adapter instance, the real Home Assistant
client and a fake Home Assistant that behaves like the real one did (the light obeys in 0.8 s, the answer takes longer than every caller waits).
Nothing here talks to the live adapter or the live Home Assistant.

What the clip showed: the phrase is the quasar fast-intent layer's (the adapter's hook declines it: «не обработано адаптером»); the old proxy took
the 2 s timeout for a restart, queued the call and the speaker announced a reboot. The commands the ADAPTER owns (brightness, colour, climate) get
the verdict of the confirmed call: the reply is chosen from the device state, and it names no cause nobody observed. When Home Assistant is gone
nothing is sent and nothing waits for its return.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

import ha_actions
import ha_client
import ha_fakes
from adapter import Adapter
from test_ha_actions import AREA_ENTITIES, PLAYER_AREAS, STATES, LIVING

CLIP_TEXT = "выключи свет"


class ClipReplay(unittest.IsolatedAsyncioTestCase):
    states = [ha_fakes.entity("light.studiia", "on")] + STATES + [
        ha_fakes.entity(entity_id, "playing") for entity_id in PLAYER_AREAS]

    async def asyncSetUp(self):
        self.ha = ha_fakes.FakeHomeAssistant(self.states)
        self.ha.require_auth = True
        self.ha.long_lived.add("llat")
        self.addCleanup(self.ha.close)
        self.ha.areas = {**PLAYER_AREAS, **{entity: area for area, ids in AREA_ENTITIES.items() for entity in ids}}
        self.ha.areas["light.studiia"] = "Студия"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = ha_client.Store(Path(tmp.name) / "connection.json")
        store.save(ha_client.Connection(self.ha.url, "long_lived", "llat"))
        self.adapter = Adapter({"AISIS_ALLOWED_STATIONS": LIVING, "AISIS_MEMORY": "0", "AISIS_CHATGPT_COMMAND": "[]",
                                "AISIS_ADAPTER_TOKEN": "test-only-token", "AISIS_STATE_DB": tmp.name + "/adapter.sqlite3",
                                "AISIS_DIALOGS_DB": tmp.name + "/dialogs.sqlite3"})
        self.adapter.ha_client = ha_client.HaClient(store, discover_fn=lambda **_: [])
        self.adapter.turn = AsyncMock(return_value="ответ мозга")
        self.adapter.skills = AsyncMock()
        self.adapter.skills.answer.return_value = None
        self.adapter.intents.skills = None
        self.addAsyncCleanup(self.adapter.flush_traces)
        self.addAsyncCleanup(self.adapter.close, None)

    async def speak(self, text):
        ws = AsyncMock()
        ws.closed = False
        await self.adapter._process_turn(ws, {"text": text, "metadata": {"stationId": LIVING}})
        return ws.send_json.await_args.args[0]["data"]["text"]

    def service_calls(self):
        return [r for r in self.ha.requests if r[0] == "WS"]

    async def test_the_adapters_hook_leaves_the_phrase_to_the_quasar_layer(self):
        async with TestClient(TestServer(self.adapter.app())) as client:
            answer = await client.post("/intent/decide", json={"text": CLIP_TEXT}, headers={"Authorization": "Bearer test-only-token"})
            body = await answer.json()
        self.assertEqual(answer.status, 200)
        self.assertEqual(body["route"], "none")                  # nothing for the adapter to do before the quasar layer: it answers itself

    async def test_an_adapter_command_against_a_slow_home_assistant_is_confirmed_from_the_state(self):
        self.ha.script("light.turn_on", state_after=0.8, respond_after=4.0)
        text = await self.speak("яркость пятьдесят")
        self.assertEqual(text, "Яркость 50 процентов.")
        self.assertEqual(self.ha.states["light.living_lamp"]["state"], "on")
        self.assertEqual(len(self.service_calls()), 2)                       # both dimmable lamps of the room; the plug cannot dim; each sent once
        await self.adapter.flush_traces()

    async def test_the_adapters_trace_keeps_the_verdict_and_the_reason_of_the_reply(self):
        self.ha.script("light.turn_on", state_after=0.8, respond_after=4.0)
        await self.speak("яркость пятьдесят")
        await self.adapter.flush_traces()
        conn = sqlite3.connect(self.adapter.dialogs_db)
        try:
            (raw,) = conn.execute("SELECT trace FROM utterance_traces ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        trace = json.loads(raw)
        self.assertEqual(trace["reply_why"], "ha_verified")
        call = trace["calls"][0]
        self.assertEqual((call["service"], call["verify"]["result"], call["status"]), ("light.turn_on", "ok", 200))

    async def test_a_home_assistant_that_does_not_answer_is_not_a_reboot(self):
        self.ha.script("light.turn_on", state_after=None, respond_after=None)
        text = await self.speak("яркость пятьдесят")
        self.assertIn(text, (ha_actions.REPLY_NOT_CONFIRMED, ha_actions.REPLY_UNVERIFIED))
        self.assertNotIn("перезагруж", text)

    async def test_a_refused_connection_is_named_and_nothing_waits_for_its_return(self):
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        await self.speak("яркость пятьдесят")                                # warm the adapter's registry and area caches
        calls = len(self.service_calls())
        self.ha.stop_listening()
        text = await self.speak("яркость сорок")
        self.assertEqual(text, ha_actions.REPLY_HA_DOWN)
        self.assertNotIn("перезагруж", text)
        self.ha.start_listening()
        self.adapter.ha_client.store.set_url(self.ha.url)
        self.assertEqual(len(self.service_calls()), calls)                   # the refused command is not replayed when Home Assistant returns

    async def test_a_mode_and_its_setpoint_are_sent_once_and_in_order(self):
        """set_hvac_mode must reach Home Assistant before the set_temperature that depends on it."""
        actions = ha_actions.HaActions(self.adapter._ha_get_json, self.adapter._ha_post_json)
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        await actions.run("ha.light_brightness", {"percent": 50, "room": None}, LIVING)      # warm the registry and state caches
        text = await actions.run("ha.climate", {"room": None, "mode": "heat", "temperature": 30}, LIVING)
        climate = [(r[1].removeprefix("/api/services/"), r[2]) for r in self.service_calls() if "/climate/" in r[1]]
        self.assertEqual(climate, [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"}),
                                   ("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 30})])
        self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
