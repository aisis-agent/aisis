"""Replay of the clip 20261119T212142-d20a57 («выключи свет», 21:21:42) through an ISOLATED stack: a real adapter instance, a real ha-cli
handler and a fake Home Assistant that behaves like the real one did (the light obeys in 0.8 s, the HTTP answer takes longer than
every caller waits). Nothing here talks to the live adapter, the live ha-cli or the live Home Assistant.

What the clip showed: the phrase is the quasar fast-intent layer's (the adapter's hook declines it: «не обработано адаптером»), the
quasar called ha-cli, ha-cli took the 2 s timeout for a restart and queued the call, the quasar announced a reboot. Here the same call
is answered by the state of the light, and the commands the ADAPTER owns (brightness, colour, climate) get the same treatment: the
reply is chosen from the verified state, and it names no cause that nobody observed.
"""
import asyncio
import json
import time
import unittest
import urllib.request
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from test_hacli_verify import Stack     # first: it points ha-cli's files at a scratch directory before `hacli` is imported

import ha_actions
import ha_fakes
import ha_action_log
import hacli
from adapter import Adapter
from test_ha_actions import AREA_ENTITIES, PLAYER_AREAS, STATES, LIVING

CLIP_TEXT = "выключи свет"


class ClipReplay(Stack, unittest.IsolatedAsyncioTestCase):
    states = [ha_fakes.entity("light.studiia", "on")] + STATES + [
        ha_fakes.entity(entity_id, "playing") for entity_id in PLAYER_AREAS]

    async def asyncSetUp(self):
        self.ha.areas = {**PLAYER_AREAS, **{entity: area for area, ids in AREA_ENTITIES.items() for entity in ids}}
        self.ha.areas["light.studiia"] = "Студия"
        self.dir_env = {"AISIS_ALLOWED_STATIONS": LIVING, "AISIS_MEMORY": "0", "AISIS_CHATGPT_COMMAND": "[]",
                        "AISIS_ADAPTER_TOKEN": "test-only-token", "AISIS_HACLI_URL": f"http://127.0.0.1:{self.server.server_port}"}
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.adapter = Adapter({**self.dir_env, "AISIS_STATE_DB": tmp.name + "/adapter.sqlite3", "AISIS_DIALOGS_DB": tmp.name + "/dialogs.sqlite3"})
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

    async def test_the_adapters_hook_leaves_the_phrase_to_the_quasar_layer(self):
        async with TestClient(TestServer(self.adapter.app())) as client:
            answer = await client.post("/intent/decide", json={"text": CLIP_TEXT}, headers={"Authorization": "Bearer test-only-token"})
            body = await answer.json()
        self.assertEqual(answer.status, 200)
        self.assertEqual(body["route"], "none")                  # nothing for the adapter to do before the quasar layer: it answers itself

    async def test_the_quasars_call_for_the_clip_is_answered_by_the_state_of_the_light(self):
        self.ha.script("light.turn_off", state_after=0.8, respond_after=4.0)
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/api/services/light/turn_off",
                                         data=json.dumps({"entity_id": "light.studiia"}).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        def quasar_fetch():                                                  # what the quasar's haFetch does, off the event loop
            with urllib.request.urlopen(request, timeout=3.0) as response:    # the quasar's FETCH_TIMEOUT_MS
                return response.status, response.headers.get("X-Aisis-Queue"), response.headers.get("X-Aisis-Verify")

        started = time.monotonic()
        status, queued, verify = await asyncio.get_running_loop().run_in_executor(None, quasar_fetch)
        self.assertEqual((status, queued, verify), (200, None, "ok"))
        self.assertLess(time.monotonic() - started, 2.4)                    # the state said so; Home Assistant itself took 4 s
        self.assertEqual(self.ha.states["light.studiia"]["state"], "off")
        line = ha_action_log.action_line(self.action())
        self.assertRegex(line, r"^HA light\.turn_off light\.studiia → проверено: light\.studiia выкл за \d\.\d с$")

    async def test_an_adapter_command_against_a_slow_home_assistant_is_confirmed_from_the_state(self):
        self.ha.script("light.turn_on", state_after=0.8, respond_after=4.0)
        text = await self.speak("яркость пятьдесят")
        self.assertEqual(text, "Яркость 50 процентов.")
        self.assertEqual(self.ha.states["light.living_lamp"]["state"], "on")
        self.assertEqual(hacli._queue, [])
        verified = [r for r in self.records("action") if r["verify"]["result"] == "ok"]
        self.assertEqual(len(verified), 2)                                   # both dimmable lamps of the room; the plug cannot dim
        await self.adapter.flush_traces()

    async def test_the_adapters_trace_keeps_the_verdict_and_the_reason_of_the_reply(self):
        import sqlite3
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
        self.assertTrue(call["aid"])
        self.assertEqual(call["aid"], self.records("action")[0]["id"])      # the trace and the ha-cli journal name the same action

    async def test_a_home_assistant_that_does_not_answer_is_not_a_reboot(self):
        self.fast(1.2)
        self.ha.hang_reads = True
        self.ha.script("light.turn_on", state_after=None, respond_after=None)
        self.ha.hang_reads = False                                           # the registry reads work; the device calls hang
        self.ha.script("light.turn_on", state_after=None, respond_after=None)
        text = await self.speak("яркость пятьдесят")
        self.assertIn(text, (ha_actions.REPLY_NOT_CONFIRMED, ha_actions.REPLY_UNVERIFIED))
        self.assertNotIn("перезагруж", text)
        self.assertEqual(hacli._queue, [])

    async def test_a_refused_connection_is_named_with_its_evidence(self):
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        await self.speak("яркость пятьдесят")                                # warm the adapter's registry and area caches
        self.ha.stop_listening()
        text = await self.speak("яркость сорок")
        self.assertEqual(text, ha_actions.REPLY_QUEUED)
        self.assertNotIn("перезагруж", text)
        self.assertGreaterEqual(len(hacli._queue), 1)

    async def test_a_mode_and_its_setpoint_queued_during_an_outage_replay_in_order(self):
        """set_hvac_mode must reach Home Assistant before the set_temperature that depends on it, as they were sent."""
        actions = ha_actions.HaActions(self.adapter._ha_get_json, self.adapter._ha_post_json)
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        await actions.run("ha.light_brightness", {"percent": 50, "room": None}, LIVING)      # warm the registry and state caches
        self.ha.stop_listening()
        text = await actions.run("ha.climate", {"room": None, "mode": "heat", "temperature": 30}, LIVING)
        self.assertEqual(text, ha_actions.REPLY_QUEUED)
        self.assertEqual([(path.removeprefix("/api/services/"), body.get("hvac_mode", body.get("temperature"))) for path, body, _ in hacli._queue],
                         [("climate/set_hvac_mode", "heat"), ("climate/set_temperature", 30)])
        self.ha.start_listening()
        await asyncio.get_running_loop().run_in_executor(None, hacli.probe_once)
        self.assertEqual(hacli._queue, [])
        climate = [(path.removeprefix("/api/services/"), body) for path, body in self.ha.calls("/api/services/climate/")]
        self.assertEqual(climate, [("climate/set_hvac_mode", {"entity_id": "climate.kondei_1", "hvac_mode": "heat"}),
                                   ("climate/set_temperature", {"entity_id": "climate.kondei_1", "temperature": 30})])
        self.assertEqual([r["service"] for r in self.records("replay")], ["climate.set_hvac_mode", "climate.set_temperature"])


if __name__ == "__main__":
    unittest.main()
