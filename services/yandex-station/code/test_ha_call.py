"""ha_call: a service call confirmed by Home Assistant's own events, and the replays of the incidents that shaped the old proxy's verification.

2026-11-19 21:21 «выключи свет»: the light was off 0.85 s after the command and Home Assistant's answer took longer than the caller waits; the old
2 s limit treated that as «Home Assistant is down», queued the call and announced a reboot. 2026-11-15 21:33: Home Assistant did not answer, the old
proxy waited 5 s, the quasar layer had aborted at 3 s and told the owner the command failed while the proxy replayed it later. 2026-11-15 and
2026-11-20: an air conditioner takes seconds, the old mode shows while the call still runs. Against the fake Home Assistant (REST + WebSocket).
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_call  # noqa: E402
import ha_client  # noqa: E402
import ha_fakes  # noqa: E402

QUASAR_FETCH_TIMEOUT_S = 3.0   # FETCH_TIMEOUT_MS in the quasar layer's ha-intents.ts


class Stack(unittest.TestCase):
    states = [ha_fakes.entity("light.studiia", "on"), ha_fakes.entity("light.kitchen", "on"), ha_fakes.entity("switch.plug", "on")]

    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant(self.states)
        self.ha.require_auth = True
        self.ha.long_lived.add("llat")
        self.addCleanup(self.ha.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        store = ha_client.Store(Path(self.dir.name) / "connection.json")
        store.save(ha_client.Connection(self.ha.url, "long_lived", "llat"))
        self.client = ha_client.HaClient(store, discover_fn=lambda **_: [])

    def call(self, domain, service, data, budget=ha_call.CALL_BUDGET_S):
        started = time.monotonic()
        result = ha_call.service_call(self.client, domain, service, data, budget)
        return result, time.monotonic() - started


class TheClipOfNovember19(Stack):
    """«выключи свет»: the light obeys within a second, Home Assistant answers after 4 s (longer than any caller waits)."""

    def test_a_slow_answer_of_an_obeyed_command_is_a_verified_success(self):
        self.ha.script("light.turn_off", state_after=0.8, respond_after=4.0)
        result, elapsed = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (200, "ok", "state_matched"))
        self.assertEqual(result.verify["after"], {"light.studiia": "off"})
        self.assertLess(elapsed, 2.0)                              # answered when Home Assistant's event said so, not when its answer came
        self.assertTrue(700 <= result.verify["ms"] <= 1800, result.verify["ms"])
        self.assertIsNone(result.down)                             # not an outage: nothing is announced, nothing is kept for later
        self.assertEqual(self.ha.calls(), [])                      # sent once, over the WebSocket, never again

    def test_the_call_reached_home_assistant_exactly_once(self):
        self.ha.script("light.turn_off", state_after=0.8, respond_after=4.0)
        self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual([r for r in self.ha.requests if r[0] == "WS"], [("WS", "/api/services/light/turn_off", {"entity_id": "light.studiia"})])


class ObeyedButUnanswered(Stack):
    def test_the_event_confirms_even_when_home_assistant_never_answers(self):
        self.ha.script("light.turn_off", state_after=0.0, respond_after=None)
        result, elapsed = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify["result"]), (200, "ok"))
        self.assertLess(elapsed, 0.8)

    def test_a_state_that_was_already_right_is_confirmed_by_the_answer(self):
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        result, _ = self.call("light", "turn_on", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify["reason"]), (200, "already_in_state"))

    def test_a_state_that_was_already_right_without_any_answer_is_unknown(self):
        self.ha.script("light.turn_on", state_after=0.0, respond_after=None)
        result, elapsed = self.call("light", "turn_on", {"entity_id": "light.studiia"}, budget=0.8)
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (504, "unknown", "already_in_state_unanswered"))
        self.assertLess(elapsed, 1.3)

    def test_a_toggle_is_judged_against_the_state_before(self):
        self.ha.script("switch.toggle", state_after=0.1, respond_after=0.0)
        result, _ = self.call("switch", "toggle", {"entity_id": "switch.plug"})
        self.assertEqual((result.status, result.verify["after"]), (200, {"switch.plug": "off"}))

    def test_brightness(self):
        self.ha.script("light.turn_on", state_after=0.1, respond_after=0.0)
        result, _ = self.call("light", "turn_on", {"entity_id": "light.kitchen", "brightness_pct": 40})
        self.assertEqual((result.status, result.verify["after"]), (200, {"light.kitchen": "on 40%"}))

    def test_every_lamp_of_the_call_must_obey(self):
        self.ha.script("light.turn_off", state_after=0.1, respond_after=0.0)
        result, _ = self.call("light", "turn_off", {"entity_id": ["light.studiia", "light.kitchen"]})
        self.assertEqual((result.status, sorted(result.verify["after"])), (200, ["light.kitchen", "light.studiia"]))

    def test_all_lights_are_checked_through_one_template_after_the_answer(self):
        self.ha.states["light.c"] = ha_fakes.entity("light.c", "unavailable")      # a dead lamp is not a failure of the command
        self.ha.script("light.turn_off", state_after=0.1, respond_after=0.3)
        result, _ = self.call("light", "turn_off", {"entity_id": "all"})
        self.assertEqual((result.status, result.verify["result"]), (200, "ok"))

    def test_a_partial_confirmation_names_what_the_state_cannot_say(self):
        self.ha.states["light.rgb"] = ha_fakes.entity("light.rgb", "on", supported_color_modes=["rgb"], color_mode="rgb")
        self.ha.script("light.turn_on", state_after=0.1, respond_after=0.0)
        result, _ = self.call("light", "turn_on", {"entity_id": "light.rgb", "color_temp_kelvin": 4000})
        self.assertEqual((result.status, result.verify["result"]), (200, "ok"))
        self.assertTrue(result.verify["unchecked"])


class DeviceDidNotObey(Stack):
    def test_acknowledged_by_home_assistant_but_unchanged_is_a_failure_when_the_budget_ends(self):
        self.ha.script("light.turn_off", state_after=None, respond_after=0.0)
        result, elapsed = self.call("light", "turn_off", {"entity_id": "light.studiia"}, budget=1.0)
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (502, "failed", "state_mismatch"))
        self.assertEqual((result.ha, result.verify["wrong"]), ("200", {"light.studiia": "on"}))
        self.assertTrue(0.9 <= elapsed < 1.6, elapsed)             # it listened until the budget ended: a lamp may lag behind the answer

    def test_the_device_went_unavailable(self):
        self.ha.script("light.turn_off", state_after=0.0, respond_after=0.0,
                       effect=lambda body, states: states["light.studiia"].update(state="unavailable"))
        result, _ = self.call("light", "turn_off", {"entity_id": "light.studiia"}, budget=0.8)
        self.assertEqual((result.status, result.verify["reason"]), (502, "device_unavailable"))

    def test_a_lamp_that_never_obeys_and_is_never_answered_is_a_failure_at_the_end_of_the_budget(self):
        self.ha.script("light.turn_off", state_after=None, respond_after=None)
        result, elapsed = self.call("light", "turn_off", {"entity_id": "light.studiia"}, budget=0.8)
        self.assertEqual((result.status, result.verify["reason"]), (502, "state_mismatch"))
        self.assertLess(elapsed, 1.3)

    def test_a_rejection_is_passed_through(self):
        self.ha.script("light.turn_off", state_after=None, respond_after=0.0, status=400)
        result, _ = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify), (400, {"result": "skipped", "reason": "rejected"}))

    def test_a_rejection_wins_when_the_device_already_is_in_the_state(self):
        self.ha.script("light.turn_on", state_after=None, respond_after=0.0, status=400)
        result, _ = self.call("light", "turn_on", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify), (400, {"result": "skipped", "reason": "rejected"}))   # «on» is no proof the call worked


class AnAirConditionerTakesSeconds(Stack):
    """2026-11-15 and 2026-11-20: the state shows the OLD mode while Home Assistant's call is still running."""
    states = [ha_fakes.entity("climate.ac", "off", temperature=24)]

    def test_a_mismatch_before_home_assistant_answered_is_not_a_failure(self):
        self.ha.script("climate.set_hvac_mode", state_after=3.0, respond_after=4.0)
        result, elapsed = self.call("climate", "set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (504, "unknown", "not_yet_confirmed"))
        self.assertLess(elapsed, QUASAR_FETCH_TIMEOUT_S)

    def test_the_unit_that_answers_in_time_is_confirmed(self):
        self.ha.script("climate.set_hvac_mode", state_after=1.0, respond_after=1.5)
        result, _ = self.call("climate", "set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
        self.assertEqual((result.status, result.verify["after"]), (200, {"climate.ac": "cool 24°"}))

    def test_a_mismatch_after_home_assistant_answered_ok_is_a_failure(self):
        self.ha.script("climate.set_hvac_mode", state_after=None, respond_after=0.2)
        result, _ = self.call("climate", "set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"}, budget=0.8)
        self.assertEqual((result.status, result.verify["result"]), (502, "failed"))

    def test_a_lamp_in_the_call_that_did_not_obey_is_a_failure_even_with_a_climate_unit(self):
        self.ha.states["light.a"] = ha_fakes.entity("light.a", "on")
        self.ha.script("light.turn_off", state_after=None, respond_after=None)
        result, _ = self.call("light", "turn_off", {"entity_id": "light.a"}, budget=0.6)
        self.assertEqual(result.verify["result"], "failed")


class HomeAssistantDoesNotAnswer(Stack):
    def test_silence_is_not_a_reboot(self):
        self.ha.hang_reads = True
        self.ha.hang_ws = True
        self.ha.script("light.turn_off", state_after=0.0, respond_after=None)
        result, elapsed = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (504, "unknown", "ha_no_answer"))
        self.assertIsNone(result.down)                              # no cause is claimed for a few seconds of silence
        self.assertLess(elapsed, QUASAR_FETCH_TIMEOUT_S)            # answered inside the quasar layer's 3 s fetch timeout
        self.assertLess(ha_call.CALL_BUDGET_S, QUASAR_FETCH_TIMEOUT_S)

    def test_the_budget_fits_the_callers_timeout_when_only_the_answer_hangs(self):
        self.ha.script("climate.set_hvac_mode", state_after=None, respond_after=None)
        self.ha.states["climate.ac"] = ha_fakes.entity("climate.ac", "off")
        result, elapsed = self.call("climate", "set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
        self.assertEqual(result.status, 504)
        self.assertLess(elapsed, QUASAR_FETCH_TIMEOUT_S)

    def test_a_call_without_a_state_effect_that_times_out_is_unknown_too(self):
        self.ha.script("tts.speak", state_after=None, respond_after=None)
        result, elapsed = self.call("tts", "speak", {"entity_id": "tts.cloud", "message": "hi"}, budget=1.0)
        self.assertEqual((result.status, result.verify["reason"]), (504, "no_answer_unverifiable"))
        self.assertLess(elapsed, 1.6)

    def test_a_call_without_a_state_effect_is_answered_as_home_assistant_did(self):
        result, _ = self.call("tts", "speak", {"entity_id": "tts.cloud", "message": "hi"})
        self.assertEqual((result.status, result.verify["reason"]), (200, "not_verifiable"))

    def test_a_rejected_call_without_a_state_effect(self):
        self.ha.script("tts.speak", state_after=None, respond_after=0.0, status=400)
        result, _ = self.call("tts", "speak", {"entity_id": "tts.cloud", "message": "hi"})
        self.assertEqual((result.status, result.verify["reason"]), (400, "rejected"))


class HomeAssistantIsGone(Stack):
    """The proxy queued such a call and replayed it when Home Assistant came back. Nothing is sent now, nothing is kept."""

    def test_a_refused_connection_sends_nothing_and_names_its_evidence(self):
        self.ha.stop_listening()
        result, elapsed = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.down, result.verify["reason"]), (0, "refused", "ha_down_observed"))
        self.assertLess(elapsed, 1.0)

    def test_nothing_is_replayed_when_home_assistant_returns(self):
        self.ha.stop_listening()
        self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.ha.start_listening()
        self.client.store.set_url(self.ha.url) if self.client.store.load().url != self.ha.url else None
        time.sleep(0.4)
        self.assertEqual([r for r in self.ha.requests if r[0] in ("WS", "POST") and "/api/services" in r[1]], [])
        self.assertEqual(self.ha.states["light.studiia"]["state"], "on")

    def test_a_call_without_a_state_effect_is_not_queued_either(self):
        self.ha.stop_listening()
        result, _ = self.call("tts", "speak", {"entity_id": "tts.cloud", "message": "hi"})
        self.assertEqual((result.status, result.down), (0, "refused"))

    def test_home_assistant_that_is_starting_is_down_evidence(self):
        self.ha.starting = True
        result, _ = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.down), (0, "starting"))

    def test_a_connection_lost_in_flight_is_unknown_and_not_sent_again(self):
        self.ha.script("light.turn_off", state_after=None, respond_after=None, on_receive=lambda ha: ha.drop_sockets())
        result, _ = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify["reason"]), (504, "ha_down_in_flight"))
        self.assertEqual(len([r for r in self.ha.requests if r[0] == "WS"]), 1)

    def test_a_connection_lost_after_the_device_obeyed_is_judged_from_the_state(self):
        """The call ran and the WebSocket died before either the event or the answer arrived: the device is the witness, not the silence."""
        def obey_then_the_socket_dies(ha):
            ha.states["light.studiia"]["state"] = "off"      # the state first: the drop wakes the caller, whose recovery read must see it
            ha.drop_sockets()
        self.ha.script("light.turn_off", state_after=None, respond_after=None, on_receive=obey_then_the_socket_dies)
        result, _ = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (200, "ok", "state_matched"))
        self.assertEqual(len([r for r in self.ha.requests if r[0] == "WS"]), 1)   # and still sent once

    def test_a_connection_lost_before_the_call_was_sent_never_confirms_it(self):
        """Another controller already put the light where the call wants it, and the socket died while subscribing: this call was never sent."""
        from unittest import mock
        import ha_ws
        real_send = ha_ws.Session.send

        def send(session, type_, **fields):
            if type_ == "subscribe_trigger":
                self.ha.states["light.studiia"]["state"] = "off"
                raise ha_ws.WsError("lost", "reset")
            return real_send(session, type_, **fields)
        with mock.patch.object(ha_ws.Session, "send", send):
            result, _ = self.call("light", "turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (504, "unknown", "ha_down_in_flight"))
        self.assertEqual([r for r in self.ha.requests if r[0] == "WS"], [])

    def test_a_state_that_could_not_be_read_after_the_loss_is_not_taken_from_before_the_call(self):
        """Two lamps: one was already off and cannot be read any more, the other obeyed. Only fresh readings count; the first has none."""
        def obey_and_lose_the_other(ha):
            ha.states["light.studiia"]["state"] = "off"
            del ha.states["light.kitchen"]
            ha.drop_sockets()
        self.ha.script("light.turn_off", state_after=None, respond_after=None, on_receive=obey_and_lose_the_other)
        self.ha.states["light.kitchen"]["state"] = "off"
        result, _ = self.call("light", "turn_off", {"entity_id": ["light.studiia", "light.kitchen"]})
        self.assertEqual((result.status, result.verify["result"], result.verify["reason"]), (504, "unknown", "ha_down_in_flight"))
        self.assertEqual(len([r for r in self.ha.requests if r[0] == "WS"]), 1)


class TheBudget(Stack):
    def test_the_budget_stays_below_the_callers_timeouts(self):
        self.assertLess(ha_call.CALL_BUDGET_S, QUASAR_FETCH_TIMEOUT_S)
        self.assertLess(ha_call.BEFORE_READ_S + ha_call.CONNECT_S + ha_call.READ_S, QUASAR_FETCH_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
