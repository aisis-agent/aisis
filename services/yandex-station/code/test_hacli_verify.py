"""ha-cli verifies a service call against the device state and never claims a cause it did not observe.

2026-11-19 21:21:42: «выключи свет». The light was off 0.85 s after the command; Home Assistant's own answer took longer than
ha-cli's 2 s, ha-cli queued the call, answered 202, and the speaker said «Умный дом перезагружается, … через 25 секунд». Home
Assistant never restarted (its log is continuous since 04:33). These tests run ha-cli against a fake Home Assistant that misbehaves
the same ways: slow answer after an obeyed command, no answer at all, refused connection, «starting», an acknowledged call that did
nothing, a rejection.
"""
import http.client
import io
import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("AISIS_HACLI_HA_URL", "http://127.0.0.1:1")
os.environ["AISIS_HACLI_CACHE"] = os.path.join(_TMP.name, "cache.json")
os.environ["AISIS_HACLI_OUTAGES"] = os.path.join(_TMP.name, "outages.json")
os.environ["AISIS_HACLI_JOURNAL"] = os.path.join(_TMP.name, "journal.jsonl")

import ha_action_log  # noqa: E402
import ha_fakes  # noqa: E402
import hacli  # noqa: E402

QUASAR_FETCH_TIMEOUT_S = 3.0   # FETCH_TIMEOUT_MS in the quasar container's ha-intents.ts


class Stack(unittest.TestCase):
    """ha-cli's handler in front of a fake Home Assistant, with its own journal, queue, outage statistics and availability."""

    states = [ha_fakes.entity("light.studiia", "on"), ha_fakes.entity("light.kitchen", "on"),
              ha_fakes.entity("switch.plug", "on")]

    def setUp(self):
        self.ha = ha_fakes.FakeHomeAssistant(self.states)
        self.addCleanup(self.ha.close)
        self.bearer = secrets.token_hex(8)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.journal_path = os.path.join(tmp.name, "ha-actions.jsonl")
        self.stderr = io.StringIO()
        for patch in (mock.patch.object(hacli, "HA_URL", self.ha.url),
                      mock.patch.object(hacli, "HA_TOKEN", self.bearer),
                      mock.patch.object(hacli, "JOURNAL", ha_action_log.Journal(self.journal_path, stream=self.stderr)),
                      mock.patch.object(hacli, "OUTAGES_FILE", Path(tmp.name) / "outages.json"),
                      mock.patch.object(hacli, "CACHE_FILE", Path(tmp.name) / "cache.json"),
                      mock.patch.object(hacli, "_avail", hacli.Availability()),
                      mock.patch.object(hacli, "_measured_outages", []),
                      mock.patch.object(hacli, "_queue", [])):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), hacli.Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=lambda: self.server.serve_forever(0.02), daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.join_calls)

    def join_calls(self):
        """A call thread that outlived its caller writes its end to the journal: release what hangs, let it, before the patches are undone."""
        self.ha.stopping.set()
        for thread in threading.enumerate():
            if thread.name == "ha-call":
                thread.join(3)

    def fast(self, budget=1.0):
        """Shorter budgets for the scenarios whose point is what happens when the budget ends."""
        for name, value in (("CALL_BUDGET_S", budget), ("CALL_TIMEOUT_S", budget - 0.1), ("STATE_READ_S", 0.3), ("BEFORE_READ_S", 0.3)):
            patch = mock.patch.object(hacli, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def post(self, path, body, timeout=QUASAR_FETCH_TIMEOUT_S):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=timeout)
        started = time.monotonic()
        try:
            conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, raw, headers, time.monotonic() - started
        finally:
            conn.close()

    def get(self, path, timeout=QUASAR_FETCH_TIMEOUT_S):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=timeout)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read(), {k.lower(): v for k, v in response.getheaders()}
        finally:
            conn.close()

    def records(self, kind=None):
        out = ha_action_log.read(self.journal_path, 0, time.time() + 10)
        return [r for r in out if kind is None or r["kind"] == kind]

    def action(self):
        found = self.records("action")
        self.assertEqual(len(found), 1, found)
        return found[0]

    def wait_for(self, kind, timeout=4.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.records(kind):
                return self.records(kind)
            time.sleep(0.05)
        self.fail(f"no {kind} record in the journal")


class TheClipOfNovember19(Stack):
    """«выключи свет»: the light obeys within a second, Home Assistant answers after 4 s (longer than any caller waits)."""

    def test_slow_answer_of_an_obeyed_command_is_a_verified_success(self):
        self.ha.script("light.turn_off", state_after=0.8, respond_after=4.0)
        status, _, headers, elapsed = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-aisis-verify"], "ok")
        self.assertEqual(headers["x-aisis-verify-reason"], "state_matched")
        self.assertEqual(headers["x-aisis-verified-state"], "light.studiia=off")
        self.assertLess(elapsed, 2.4)                               # answered when the state said so (Home Assistant itself took 4 s)
        self.assertTrue(700 <= int(headers["x-aisis-verify-ms"]) <= 2000, headers["x-aisis-verify-ms"])
        self.assertEqual(hacli._queue, [])                          # not queued: nothing to replay, nothing announced
        self.assertNotIn("x-aisis-queue", headers)
        self.assertEqual(hacli._avail.state, "up")                  # not an outage
        self.assertEqual(hacli._measured_outages, [])

    def test_the_journal_says_what_happened_and_why(self):
        self.ha.script("light.turn_off", state_after=0.8, respond_after=4.0)
        _, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        record = self.action()
        self.assertEqual(record["id"], headers["x-aisis-action"])
        self.assertEqual((record["service"], record["entities"], record["expect"]), ("light.turn_off", ["light.studiia"], "turn_off"))
        self.assertEqual(record["before"], {"light.studiia": "on"})
        self.assertEqual(record["verify"]["result"], "ok")
        self.assertEqual(record["verify"]["after"], {"light.studiia": "off"})
        self.assertGreaterEqual(record["verify"]["attempts"], 2)
        self.assertEqual(record["reply"], {"status": 200, "why": "verified_state"})
        self.assertEqual(record["call"], {"pending": True})         # Home Assistant had not answered when the caller was
        late = self.wait_for("ha_response")[0]                       # ... and how its call ended is journalled all the same
        self.assertEqual((late["id"], late["call"]["error"]), (record["id"], "timeout"))
        self.assertIn("проверено: light.studiia выкл", ha_action_log.action_line(record))

    def test_the_journal_never_holds_the_bearer(self):
        self.ha.script("light.turn_off", state_after=0.0, respond_after=0.0)
        self.post("/api/services/light/turn_off", {"entity_id": "light.studiia", "access_token": self.bearer})
        with open(self.journal_path, encoding="utf-8") as fh:
            self.assertNotIn(self.bearer, fh.read())
        self.assertNotIn(self.bearer, self.stderr.getvalue())


class ObeyedButUnanswered(Stack):
    def test_state_is_checked_even_when_home_assistant_never_answers(self):
        self.ha.script("light.turn_off", state_after=0.0, respond_after=None)
        status, _, headers, elapsed = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify"]), (200, "ok"))
        self.assertLess(elapsed, 0.8)

    def test_already_in_the_expected_state(self):
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (200, "already_in_state"))

    def test_toggle_is_judged_against_the_state_before(self):
        self.ha.script("switch.toggle", state_after=0.1, respond_after=0.0)
        status, _, headers, _ = self.post("/api/services/switch/toggle", {"entity_id": "switch.plug"})
        self.assertEqual((status, headers["x-aisis-verified-state"]), (200, "switch.plug=off"))
        self.assertEqual(self.action()["before"], {"switch.plug": "on"})

    def test_brightness_and_other_domains(self):
        self.ha.script("light.turn_on", state_after=0.1, respond_after=0.0)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.kitchen", "brightness_pct": 40})
        self.assertEqual((status, headers["x-aisis-verified-state"]), (200, "light.kitchen=on 40%"))

    def test_all_lights_are_checked_through_one_template(self):
        self.ha.states["light.c"] = ha_fakes.entity("light.c", "unavailable")      # a dead lamp is not a failure of the command
        self.ha.script("light.turn_off", state_after=0.3, respond_after=None)
        status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "all"})
        self.assertEqual((status, headers["x-aisis-verify"]), (200, "ok"))
        self.assertEqual(self.action()["entities"], ["light.*"])


class DeviceDidNotObey(Stack):
    def test_acknowledged_by_home_assistant_but_unchanged(self):
        self.fast()
        self.ha.script("light.turn_off", state_after=None, respond_after=0.0)
        status, raw, headers, elapsed = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (502, "failed", "state_mismatch"))
        self.assertEqual(headers["x-aisis-ha-status"], "200")        # Home Assistant said yes; the device did not follow
        self.assertEqual(json.loads(raw)["verify"]["wrong"], {"light.studiia": "on"})
        self.assertGreaterEqual(elapsed, 0.9)                        # it looked until the budget ended
        record = self.action()
        self.assertEqual((record["reply"]["why"], record["verify"]["after"]), ("state_not_as_expected", {"light.studiia": "on"}))
        self.assertGreaterEqual(record["verify"]["attempts"], 3)
        self.assertIn("не подтверждено: light.studiia вкл", ha_action_log.action_line(record))
        self.assertEqual(hacli._queue, [])

    def test_the_device_went_unavailable(self):
        self.fast()
        self.ha.script("light.turn_off", state_after=0.0, respond_after=0.0,
                       effect=lambda body, states: states["light.studiia"].update(state="unavailable"))
        status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (502, "device_unavailable"))


class HomeAssistantDoesNotAnswer(Stack):
    def test_silence_is_not_a_reboot(self):
        self.fast(1.2)
        self.ha.hang_reads = True
        self.ha.script("light.turn_off", state_after=0.0, respond_after=None)
        status, raw, headers, elapsed = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (504, "unknown", "ha_no_answer"))
        self.assertNotIn("x-aisis-down", headers)
        self.assertNotIn("x-aisis-queue", headers)
        self.assertEqual(hacli._queue, [])                           # a reported failure is not replayed behind the speaker's back
        self.assertLess(elapsed, QUASAR_FETCH_TIMEOUT_S)
        self.assertNotIn("перезагруз", ha_action_log.action_line(self.action()))
        self.assertEqual(hacli._avail.state, "slow")                 # a few seconds of silence is slowness, not an outage
        self.assertEqual(json.loads(self.get("/health")[1])["ha_up"], True)

    def test_the_real_budget_fits_the_callers_timeout(self):
        self.ha.hang_reads = True
        self.ha.script("light.turn_off", state_after=0.0, respond_after=None)
        status, _, _, elapsed = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual(status, 504)
        self.assertLess(elapsed, QUASAR_FETCH_TIMEOUT_S)
        self.assertLess(hacli.CALL_BUDGET_S, QUASAR_FETCH_TIMEOUT_S)
        self.assertLess(hacli.CLIENT_TIMEOUT_S, QUASAR_FETCH_TIMEOUT_S)

    def test_a_call_without_a_state_effect_that_times_out_is_unknown_too(self):
        self.fast(1.0)
        self.ha.script("tts.speak", state_after=None, respond_after=None)
        status, _, headers, _ = self.post("/api/services/tts/speak", {"entity_id": "tts.cloud", "message": "hi"})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (504, "unknown", "no_answer_unverifiable"))
        self.assertEqual(hacli._queue, [])

    def test_a_single_slow_read_is_no_outage(self):
        with mock.patch.object(hacli, "CLIENT_TIMEOUT_S", 0.3):
            self.ha.hang_reads = True
            status, _, _ = self.get("/api/states/light.studiia")
        self.assertEqual(status, 404)                                # not in the (empty) cache: ha-cli does not invent a state
        self.assertEqual(hacli._avail.state, "slow")
        self.ha.hang_reads = False
        self.assertEqual(self.get("/api/states/light.studiia")[0], 200)
        self.assertEqual((hacli._avail.state, hacli._measured_outages), ("up", []))
        self.assertEqual([(r["from"], r["to"]) for r in self.records("ha_status")], [("unknown", "slow"), ("slow", "up")])


class HomeAssistantObservedGone(Stack):
    def test_refused_connection_is_an_observed_outage_and_the_call_waits(self):
        self.ha.stop_listening()
        status, raw, headers, elapsed = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual(status, 202)
        self.assertEqual((headers["x-aisis-down"], headers["x-aisis-queue"]), ("refused", "1"))
        self.assertGreater(int(headers["x-aisis-eta"]), 0)
        self.assertTrue(json.loads(raw)["queued"])
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(hacli._queue), 1)
        record = self.action()
        self.assertEqual((record["verify"]["reason"], record["reply"]["why"], record["call"]["error"]), ("ha_down_observed", "queued_ha_down", "refused"))
        self.assertIn("HA недоступен (соединение отклонено)", ha_action_log.action_line(record))
        self.assertEqual(hacli._avail.state, "down")
        self.assertEqual(json.loads(self.get("/health")[1])["status"], "down")

    def test_the_queued_call_is_sent_and_checked_when_home_assistant_returns(self):
        self.ha.stop_listening()
        self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.ha.start_listening()
        hacli.probe_once()
        self.assertEqual(hacli._queue, [])
        self.assertEqual(self.ha.states["light.studiia"]["state"], "off")
        replay = self.records("replay")[0]
        self.assertEqual((replay["service"], replay["call"]["status"], replay["verify"]["result"]), ("light.turn_off", 200, "ok"))
        statuses = [(r["from"], r["to"]) for r in self.records("ha_status")]
        self.assertEqual(statuses, [("unknown", "down"), ("down", "up")])
        self.assertEqual(len(hacli._measured_outages), 0)           # the start of this window was not observed (ha-cli never saw it up)

    def test_starting_is_down_on_both_looks(self):
        self.ha.starting = True
        status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-down"]), (202, "starting"))
        self.assertEqual(hacli._avail.last_error, "starting")

    def test_a_calls_reset_with_a_healthy_second_look_is_not_an_outage(self):
        """One failed call does not make an outage: the second look finds Home Assistant answering, so the state decides."""
        self.ha.script("light.turn_off", state_after=0.0, respond_after=0.0)
        real = hacli.ha_call
        flaky = {"left": 1}

        def reset_once(path, body=None, timeout=hacli.HA_TIMEOUT_S):
            if path.startswith("/api/services/") and flaky["left"]:
                flaky["left"] -= 1
                real(path, body, timeout)                           # it reached Home Assistant and the light went off ...
                return hacli.Reply(0, b"", "reset", 5)              # ... but the connection broke before the answer
            return real(path, body, timeout)

        with mock.patch.object(hacli, "ha_call", reset_once):
            status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify"]), (200, "ok"))
        self.assertEqual(hacli._queue, [])


class NotOursToJudge(Stack):
    def test_a_rejection_is_passed_through(self):
        self.ha.script("light.turn_on", state_after=None, respond_after=0.0, status=400)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "brightness_pct": 400})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (400, "rejected"))
        self.assertEqual(hacli._queue, [])

    def test_a_call_with_no_state_effect_answers_as_home_assistant_did(self):
        self.ha.script("tts.speak", state_after=None, respond_after=0.0)
        status, _, headers, _ = self.post("/api/services/tts/speak", {"entity_id": "tts.cloud", "message": "привет"})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (200, "skipped", "not_verifiable"))
        self.assertEqual(self.action()["data"], {"message": "привет"})

    def test_the_message_of_a_spoken_line_is_not_journalled_when_long(self):
        self.ha.script("tts.speak", state_after=None, respond_after=0.0)
        self.post("/api/services/tts/speak", {"entity_id": "tts.cloud", "message": "слово " * 40})
        self.assertEqual(self.action()["data"], {"message": "(240 chars)"})

    def test_calls_that_ask_for_data_are_not_verified_or_queued(self):
        self.ha.script("weather.get_forecasts", state_after=None, respond_after=0.0)
        status, _, headers, _ = self.post("/api/services/weather/get_forecasts?return_response", {"entity_id": "weather.home"})
        self.assertEqual(status, 200)
        self.assertNotIn("x-aisis-verify", headers)
        self.assertEqual(self.records("action"), [])


if __name__ == "__main__":
    unittest.main()
