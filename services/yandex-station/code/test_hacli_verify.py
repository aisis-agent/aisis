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
                      mock.patch.object(hacli, "LATE_VERIFY_TOTAL_S", 0.5),
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

    def fast_late(self, total):
        """The background look at an unconfirmed call lasts this long after the call was sent."""
        patch = mock.patch.object(hacli, "LATE_VERIFY_TOTAL_S", total)
        patch.start()
        self.addCleanup(patch.stop)

    def join_calls(self):
        """A call thread that outlived its caller writes its end to the journal: release what hangs, let it, before the patches are undone."""
        self.ha.stopping.set()
        for thread in threading.enumerate():
            if thread.name in ("ha-call", "ha-late"):
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
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "ha_down_in_flight"))     # a 503 after the request was sent is ambiguous
        self.assertEqual(hacli._queue, [])
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

    def test_an_outage_seen_only_by_the_state_reads_is_observed_too(self):
        """The call hangs, then Home Assistant goes away: two polls in a row that find nobody listening are the evidence."""
        self.ha.script("light.turn_off", state_after=None, respond_after=None)
        real = hacli.read_states
        reads = {"n": 0}

        def reads_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:                                     # the state before the call
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", reads_then_refused):
            status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "ha_down_in_flight"))     # the call may have run: reported, never queued
        self.assertEqual(hacli._queue, [])

    def test_a_call_home_assistant_answered_is_not_queued_when_it_goes_away_afterwards(self):
        """A restart right after an accepted toggle must not make the speaker say «недоступен» and replay the toggle."""
        self.fast()
        self.ha.script("light.turn_off", state_after=None, respond_after=0.0)
        real = hacli.read_states
        reads = {"n": 0}

        def reads_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", reads_then_refused):
            status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertNotEqual(status, 202)
        self.assertNotIn("x-aisis-queue", headers)
        self.assertEqual(hacli._queue, [])

    def test_a_successful_read_between_two_failed_ones_breaks_the_outage_streak_of_an_already_matching_call(self):
        """refused, matching 200, refused is not two failures in a row: nothing is queued."""
        self.fast()
        self.ha.script("light.turn_on", state_after=None, respond_after=None)
        real = hacli.read_states
        reads = {"n": 0}

        def alternating(entities, timeout):
            reads["n"] += 1
            if reads["n"] % 2 == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", alternating):
            status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia"})
        self.assertNotEqual(status, 202)
        self.assertNotIn("x-aisis-queue", headers)
        self.assertEqual(hacli._queue, [])

    def test_a_call_that_timed_out_does_not_break_the_outage_streak_of_the_state_reads(self):
        """A timeout is no answer: two refused state reads around it still say Home Assistant is gone."""
        self.fast()
        self.ha.script("light.turn_off", state_after=None, respond_after=30.0)
        created, real_call, real = [], hacli._Call, hacli.read_states
        reads = {"n": 0}

        def recording_call(*args):
            created.append(real_call(*args))
            return created[-1]

        def refused_with_the_timeout_in_between(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            if reads["n"] == 3:
                created[0].reply = hacli.Reply(0, b"", "timeout", 900)
                created[0].done.set()
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "_Call", recording_call), mock.patch.object(hacli, "read_states", refused_with_the_timeout_in_between):
            status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "ha_down_in_flight"))     # Home Assistant is gone, but the call timed out after it was sent
        self.assertEqual(hacli._queue, [])

    def test_an_unanswered_toggle_is_never_queued_when_only_the_state_reads_fail(self):
        """The toggle may have run while its answer is delayed: replaying it on recovery would undo it."""
        self.fast()
        self.ha.script("switch.toggle", state_after=None, respond_after=30.0)
        real = hacli.read_states
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", first_then_refused):
            status, _, headers, _ = self.post("/api/services/switch/toggle", {"entity_id": "switch.plug"})
        self.assertNotEqual(status, 202)
        self.assertNotIn("x-aisis-queue", headers)
        self.assertEqual((headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), ("unknown", "ha_down_in_flight"))
        self.assertEqual(hacli._queue, [])

    def test_an_unanswered_relative_step_is_never_queued_when_only_the_state_reads_fail(self):
        """brightness_step_pct may have run while its answer is lost: replaying it on recovery would step the lamp twice."""
        self.fast()
        self.ha.script("light.turn_on", state_after=None, respond_after=30.0)
        real = hacli.read_states
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", first_then_refused):
            status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "brightness_step_pct": 10})
        self.assertNotEqual(status, 202)
        self.assertNotIn("x-aisis-queue", headers)
        self.assertEqual((headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), ("unknown", "ha_down_in_flight"))
        self.assertEqual(hacli._queue, [])

    def test_nothing_is_queued_for_a_call_that_may_have_run_whatever_it_is(self):
        self.fast()
        self.ha.script("light.turn_on", state_after=None, respond_after=30.0)
        real = hacli.read_states
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", first_then_refused):
            status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "brightness_pct": 40})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "ha_down_in_flight"))
        self.assertEqual(hacli._queue, [])

    def test_a_toggle_answered_between_the_outage_and_the_answer_is_not_queued(self):
        """The call thread may publish Home Assistant's 200 after the outage was seen and before the answer is built."""
        self.fast()
        self.ha.script("switch.toggle", state_after=None, respond_after=30.0)
        real, real_answer = hacli.read_states, hacli.ServiceCall._answer
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        def answered_first(service_call):
            service_call.call.reply = hacli.Reply(200, b"[]", None, 5)
            service_call.call.done.set()
            return real_answer(service_call)

        with mock.patch.object(hacli, "read_states", first_then_refused), mock.patch.object(hacli.ServiceCall, "_answer", answered_first):
            status, _, headers, _ = self.post("/api/services/switch/toggle", {"entity_id": "switch.plug"})
        self.assertNotEqual(status, 202)
        self.assertNotIn("x-aisis-queue", headers)
        self.assertEqual((headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), ("unknown", "ha_down_after_answer"))
        self.assertEqual(hacli._queue, [])

    def _toggle_with_reply_published_before_the_answer(self, reply):
        self.fast()
        self.ha.script("switch.toggle", state_after=None, respond_after=30.0)
        real, real_answer = hacli.read_states, hacli.ServiceCall._answer
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        def published_first(service_call):
            service_call.call.reply = reply
            service_call.call.done.set()
            return real_answer(service_call)

        with mock.patch.object(hacli, "read_states", first_then_refused), mock.patch.object(hacli.ServiceCall, "_answer", published_first):
            return self.post("/api/services/switch/toggle", {"entity_id": "switch.plug"})

    def test_a_toggle_whose_call_ended_in_a_timeout_or_reset_is_not_queued(self):
        """A timeout or a reset after the request was sent says nothing about whether the toggle ran."""
        for error in ("timeout", "reset"):
            with self.subTest(error=error):
                hacli._queue.clear()
                status, _, headers, _ = self._toggle_with_reply_published_before_the_answer(hacli.Reply(0, b"", error, 5))
                self.assertNotEqual(status, 202)
                self.assertNotIn("x-aisis-queue", headers)
                self.assertEqual((headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), ("unknown", "ha_down_in_flight"))
                self.assertEqual(hacli._queue, [])

    def test_a_toggle_whose_call_was_refused_is_queued(self):
        """Connection refused: the request never reached Home Assistant, so sending it again is safe."""
        status, _, headers, _ = self._toggle_with_reply_published_before_the_answer(hacli.Reply(0, b"", "refused", 1))
        self.assertEqual((status, headers["x-aisis-queue"]), (202, "1"))
        self.assertEqual(len(hacli._queue), 1)

    def test_nine_explicit_lights_are_queued_when_the_connection_was_refused(self):
        """More entities than are polled one by one: nothing to look at, and the call never connected, so it waits for the return."""
        lights = [f"light.l{i}" for i in range(hacli.ha_verify.MAX_ENTITIES + 1)]
        self.ha.stop_listening()
        status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": lights})
        self.assertEqual((status, headers["x-aisis-down"]), (202, "refused"))
        self.assertEqual(len(hacli._queue), 1)

    def test_a_pulse_switch_whose_answer_is_lost_is_never_queued(self):
        """Whatever the service: a call that may have run is not sent again (a pulse, a timer, a step all repeat their effect)."""
        self.fast()
        self.ha.script("switch.turn_on", state_after=None, respond_after=30.0)
        real = hacli.read_states
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", first_then_refused):
            status, _, headers, _ = self.post("/api/services/switch/turn_on", {"entity_id": "switch.plug"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "ha_down_in_flight"))
        self.assertEqual(hacli._queue, [])

    def test_a_toggle_that_ran_and_then_met_a_503_restarting_gateway_is_not_queued(self):
        """The 503 body of a proxy says «restarting»: the toggle was sent before that, so replaying it would undo it."""
        status, _, headers, _ = self._toggle_with_reply_published_before_the_answer(hacli.Reply(503, b"upstream restarting", "starting", 5))
        self.assertNotEqual(status, 202)
        self.assertEqual((headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), ("unknown", "ha_down_in_flight"))
        self.assertEqual(hacli._queue, [])

    def test_a_toggle_whose_call_ended_in_a_gateway_error_is_not_queued(self):
        """A proxy can fail after Home Assistant ran the toggle: 502 and 503 do not prove it was never sent."""
        for code in (502, 503):
            with self.subTest(code=code):
                hacli._queue.clear()
                status, _, headers, _ = self._toggle_with_reply_published_before_the_answer(hacli.Reply(code, b"", None, 5))
                self.assertNotEqual(status, 202)
                self.assertEqual((headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), ("unknown", "ha_down_in_flight"))
                self.assertEqual(hacli._queue, [])

    def test_a_flash_is_never_queued_when_only_the_state_reads_fail(self):
        """The flash may have happened while its answer is lost: replaying it would flash again."""
        self.fast()
        self.ha.script("light.turn_on", state_after=None, respond_after=30.0)
        real = hacli.read_states
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", first_then_refused):
            status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "flash": "short"})
        self.assertNotEqual(status, 202)
        self.assertEqual(headers["x-aisis-verify-reason"], "ha_down_in_flight")
        self.assertEqual(hacli._queue, [])

    def test_the_queue_is_sent_only_after_a_200_answer(self):
        hacli._queue.append(("/api/services/light/turn_off", {"entity_id": "light.studiia"}, "q1"))
        with mock.patch.object(hacli, "ha_call", return_value=hacli.Reply(500, b"", "", 1)), \
                mock.patch.object(hacli, "_replay") as replay:
            hacli.probe_once()
        replay.assert_not_called()
        self.assertEqual(len(hacli._queue), 1)


class NotOursToJudge(Stack):
    def test_a_rejection_that_arrives_while_the_device_already_is_in_the_state_is_still_a_rejection(self):
        self.ha.script("light.turn_on", state_after=None, respond_after=0.15, status=400)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "color_name": "nonsense"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (400, "rejected"))

    def test_a_rejection_that_arrives_during_the_already_in_state_wait_wins_over_the_matching_state(self):
        self.ha.script("light.turn_on", state_after=None, respond_after=0.35, status=400)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "color_name": "nonsense"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (400, "rejected"))

    def test_a_rejection_published_while_the_state_is_read_is_still_a_rejection(self):
        self.ha.script("light.turn_on", state_after=None, respond_after=None)
        created, real_call, real_check = [], hacli._Call, hacli._check

        def recording_call(*args):
            created.append(real_call(*args))
            return created[-1]

        def check_after_the_refusal(*args):
            created[0].reply = hacli.Reply(400, b'{"message": "bad color"}', None, 5)
            created[0].done.set()
            return real_check(*args)

        with mock.patch.object(hacli, "_Call", recording_call), mock.patch.object(hacli, "_check", check_after_the_refusal):
            status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "color_name": "nonsense"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (400, "rejected"))

    def test_a_late_rejection_of_a_call_the_state_cannot_judge_is_still_a_rejection(self):
        self.ha.script("light.turn_on", state_after=None, respond_after=0.9, status=400)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "color_name": "nonsense"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (400, "rejected"))

    def test_an_already_matching_state_without_any_answer_is_unknown_not_verified(self):
        self.fast()
        self.ha.script("light.turn_on", state_after=None, respond_after=None)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "color_name": "nonsense"})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (504, "unknown", "already_in_state_unanswered"))
        late = self.wait_for("late_verify", 3.0)[0]
        self.assertEqual((late["verify"]["result"], late["verify"]["reason"]), ("unknown", "already_in_state_unanswered"))

    def test_a_200_published_between_the_watch_and_the_answer_is_not_called_unanswered(self):
        self.fast()
        self.ha.script("light.turn_on", state_after=None, respond_after=None)
        real_answer = hacli.ServiceCall._answer

        def published_first(service_call):
            service_call.call.reply = hacli.Reply(200, b"[]", None, 5)
            service_call.call.done.set()
            return real_answer(service_call)

        with mock.patch.object(hacli.ServiceCall, "_answer", published_first):
            status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.studiia", "color_name": "nonsense"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (200, "already_in_state"))

    def test_a_call_with_nothing_to_check_is_answered_inside_the_budget_when_home_assistant_hangs(self):
        self.ha.script("tts.speak", state_after=None, respond_after=None)
        self.ha.hang_reads = True
        status, _, headers, elapsed = self.post("/api/services/tts/speak", {"entity_id": "tts.cloud", "message": "привет"})
        self.assertEqual(status, 504)
        self.assertLess(elapsed, hacli.CALL_BUDGET_S + 0.4)
        self.assertEqual([r for r in self.ha.requests if r[0] == "GET" and r[1].startswith("/api/states/")], [])   # no read before a call with nothing to check


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

    def test_the_query_string_of_a_service_call_reaches_home_assistant(self):
        self.ha.script("light.turn_off", state_after=0.0, respond_after=0.0)
        status, _, _, _ = self.post("/api/services/light/turn_off?x=1", {"entity_id": "light.studiia"})
        self.assertEqual(status, 200)
        self.assertIn("/api/services/light/turn_off?x=1", [path for path, _ in self.ha.calls()])


class ClimateUnit(Stack):
    """A climate unit (an IR air conditioner) that takes seconds: 2026-11-15 and 2026-11-20 the state showed the OLD mode (heat) while
    Home Assistant's call still ran and the new one (cool) a second later, after the caller had given up."""
    states = [ha_fakes.entity("climate.ac", "off", temperature=24)]

    def set_state(self, state):
        self.ha.states["climate.ac"]["state"] = state

    def test_a_mismatch_seen_before_home_assistant_answered_is_not_a_failure_and_is_looked_at_later(self):
        self.fast_late(5.0)
        self.ha.script("climate.set_hvac_mode", state_after=2.7, respond_after=3.5, effect=lambda body, states: self.set_state("cool"))
        old_mode = threading.Timer(1.8, self.set_state, ("heat",))
        old_mode.daemon = True
        old_mode.start()
        status, _, headers, _ = self.post("/api/services/climate/set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (504, "unknown", "not_yet_confirmed"))
        action = self.action()
        self.assertEqual((action["service"], action["entities"], action["data"]), ("climate.set_hvac_mode", ["climate.ac"], {"hvac_mode": "cool"}))
        self.assertEqual((action["before"], action["call"]["error"], action["reply"]["status"]), ({"climate.ac": "off 24°"}, "timeout", 504))
        late = self.wait_for("late_verify", 6.0)[0]
        self.assertEqual((late["id"], late["service"], late["entities"]), (action["id"], "climate.set_hvac_mode", ["climate.ac"]))
        self.assertEqual((late["verify"]["result"], late["verify"]["after"]), ("ok", {"climate.ac": "cool 24°"}))
        self.assertEqual(late["climate"], {"climate.ac": {"hvac_mode": "cool", "temperature": 24}})
        self.assertGreater(late["ms"], 2600)
        self.assertIn("позже:", ha_action_log.action_line(late))
        self.assertEqual(late["ts"], action["ts"])                       # the record belongs to the utterance that made the call
        self.assertGreater(late["done_ts"], late["ts"])

    def test_a_mismatch_after_home_assistant_answered_ok_is_a_failure(self):
        self.ha.script("climate.set_hvac_mode", state_after=None, respond_after=0.0)
        status, _, headers, _ = self.post("/api/services/climate/set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (502, "failed", "state_mismatch"))
        self.assertEqual(self.records("late_verify"), [])

    def test_a_busy_moment_drops_the_late_look_instead_of_queueing_it(self):
        self.fast_late(1.0)
        self.fast()
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        self.ha.script("climate.set_hvac_mode", state_after=None, respond_after=None)
        with mock.patch.object(hacli, "_late_slots", slots):
            status, _, headers, _ = self.post("/api/services/climate/set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "not_yet_confirmed"))
        time.sleep(0.5)
        self.assertEqual(self.records("late_verify"), [])

    def test_a_generic_call_on_a_climate_unit_is_judged_like_a_climate_call(self):
        """`homeassistant.turn_off` on climate.ac: the unit goes off just after the budget while the call still runs."""
        self.fast()
        self.fast_late(3.0)
        self.set_state("heat")
        self.ha.script("homeassistant.turn_off", state_after=1.3, respond_after=None, effect=lambda body, states: self.set_state("off"))
        status, _, headers, _ = self.post("/api/services/homeassistant/turn_off", {"entity_id": "climate.ac"})
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "not_yet_confirmed"))
        late = self.wait_for("late_verify", 4.0)[0]
        self.assertEqual((late["verify"]["result"], late["climate"]["climate.ac"]["hvac_mode"]), ("ok", "off"))

    def test_a_read_that_started_before_the_answer_ended_does_not_make_a_mismatch_final(self):
        call = hacli.ServiceCall("/api/services/climate/set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
        mismatch = hacli.ha_verify.Verdict("failed", "state_mismatch", {"climate.ac": "heat"}, {"climate.ac": "heat"})
        answer = hacli.Reply(200, b"[]", None, 50, done_at=100.0)
        self.assertFalse(call._mismatch_is_final(mismatch, answer, 99.9))       # the read began, then the answer ended
        self.assertTrue(call._mismatch_is_final(mismatch, answer, 100.1))
        self.assertFalse(call._mismatch_is_final(mismatch, hacli.Reply(0, b"", "timeout", 2300, done_at=100.0), 101.0))
        self.assertTrue(call._mismatch_is_final(hacli.ha_verify.Verdict("failed", "state_mismatch", {}, {"light.a": "on"}), answer, 99.9))
        mixed = hacli.ha_verify.Verdict("failed", "state_mismatch", {}, {"climate.ac": "heat", "light.a": "on"})
        self.assertTrue(call._mismatch_is_final(mixed, hacli.Reply(0, b"", "timeout", 2300), 101.0))      # a lamp is no climate unit

    def test_a_lamp_that_did_not_obey_is_a_failure_even_when_a_climate_unit_is_in_the_call(self):
        self.fast()
        self.fast_late(1.0)
        self.ha.states["light.a"] = ha_fakes.entity("light.a", "on")
        self.ha.script("homeassistant.turn_off", state_after=None, respond_after=None)
        status, _, headers, _ = self.post("/api/services/homeassistant/turn_off", {"entity_id": ["climate.ac", "light.a"]})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-reason"]), (502, "failed", "state_mismatch"))

    def test_the_late_look_goes_on_after_the_answer_until_the_unit_has_the_new_state(self):
        """Response at 2.7 s, state at 3.6 s: a look at 3.0 s sees the old state after the answer, and that is not the end."""
        self.fast_late(6.0)
        with mock.patch.object(hacli, "CALL_TIMEOUT_S", 5.0):
            self.ha.script("climate.set_hvac_mode", state_after=3.6, respond_after=2.7, effect=lambda body, states: self.set_state("cool"))
            status, _, headers, _ = self.post("/api/services/climate/set_hvac_mode", {"entity_id": "climate.ac", "hvac_mode": "cool"})
            self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "not_yet_confirmed"))
            late = self.wait_for("late_verify", 8.0)[0]
        self.assertEqual((late["verify"]["result"], late["verify"]["after"]), ("ok", {"climate.ac": "cool 24°"}))

    def test_a_get_through_the_proxy_keeps_its_query(self):
        path = "/api/history/period/2026-11-20T10:00:00+00:00?filter_entity_id=climate.ac&minimal_response"
        self.get(path)
        self.assertIn(("GET", path, None), self.ha.requests)


class TimedSiren(Stack):
    states = [ha_fakes.entity("siren.alarm", "off")]

    def test_a_timed_siren_whose_answer_is_lost_is_never_queued(self):
        """The siren may have sounded already: replaying it on recovery would sound it again after its duration elapsed."""
        self.fast()
        self.ha.script("siren.turn_on", state_after=None, respond_after=30.0)
        real = hacli.read_states
        reads = {"n": 0}

        def first_then_refused(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "refused", 1)]

        with mock.patch.object(hacli, "read_states", first_then_refused):
            status, _, headers, _ = self.post("/api/services/siren/turn_on", {"entity_id": "siren.alarm", "duration": 1})
        self.assertNotEqual(status, 202)
        self.assertEqual(headers["x-aisis-verify-reason"], "ha_down_in_flight")
        self.assertEqual(hacli._queue, [])


class LateLook(Stack):
    """State reads fail until the budget is over; the late look then reads the device against what Home Assistant answered."""

    def unreadable_until_the_budget_is_over(self):
        self.fast(1.0)
        real, t0, reads = hacli.read_states, time.monotonic(), {"n": 0}

        def read(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1 or time.monotonic() - t0 > 1.3:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "timeout", 1)]

        return mock.patch.object(hacli, "read_states", read)

    def late_record(self, case):
        self.fast_late(3.5)
        self.ha.script("light.turn_off", state_after=2.0 if case == "obeys" else None,
                       respond_after=None if case == "silent" else 0.3)
        with self.unreadable_until_the_budget_is_over():
            status, _, headers, _ = self.post("/api/services/light/turn_off", {"entity_id": "light.studiia"})
            self.assertEqual((status, headers["x-aisis-verify"]), (504, "unknown"))
            return self.wait_for("late_verify", 5.0)[0]

    def test_a_mismatch_seen_by_the_late_look_is_waited_out_until_the_device_obeys(self):
        late = self.late_record("obeys")           # answered at 0.3 s, light off at 2.0 s: the looks of 1.3-2.0 s see «on»
        self.assertEqual((late["verify"]["result"], late["verify"]["after"]), ("ok", {"light.studiia": "off"}))

    def test_a_mismatch_after_an_ok_answer_is_a_failure_at_the_deadline(self):
        late = self.late_record("answered_but_unchanged")
        self.assertEqual((late["verify"]["result"], late["verify"]["reason"]), ("failed", "state_mismatch"))

    def test_a_mismatch_without_an_answer_stays_unknown_at_the_deadline(self):
        late = self.late_record("silent")
        self.assertEqual((late["verify"]["result"], late["verify"]["reason"]), ("unknown", "not_yet_confirmed"))


class PartialVerification(Stack):
    states = [ha_fakes.entity("light.rgb", "on", supported_color_modes=["rgb"], color_mode="rgb")]

    def test_an_rgb_only_lamp_asked_for_a_colour_temperature_is_partial_all_the_way_to_the_adapter(self):
        """on is confirmed, the colour temperature never can be: the header reaches HaReply and the adapter does not call it verified."""
        import ha_actions
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.rgb", "color_temp_kelvin": 2700})
        self.assertEqual((status, headers["x-aisis-verify"], headers["x-aisis-verify-unchecked"]), (200, "ok", "color_temp_kelvin"))
        self.assertEqual(self.action()["verify"]["unchecked"], ["color_temp_kelvin"])
        reply = ha_actions.HaReply.from_response(status, None, {k.title(): v for k, v in headers.items()})
        self.assertEqual(reply.verify["unchecked"], ["color_temp_kelvin"])
        self.assertIn("проверено частично", ha_action_log.verdict_line(reply.verify, ha=reply.ha))

    def test_a_queued_call_keeps_its_partial_verification_when_replayed(self):
        hacli._queue.append(("/api/services/light/turn_on", {"entity_id": "light.rgb", "color_temp_kelvin": 2700}, "q1"))
        self.ha.script("light.turn_on", state_after=0.0, respond_after=0.0)
        hacli.probe_once()
        replay = self.records("replay")[0]
        self.assertEqual((replay["verify"]["result"], replay["verify"]["unchecked"]), ("ok", ["color_temp_kelvin"]))

    def test_a_delayed_503_after_a_partial_match_is_an_outage_but_the_call_is_not_queued(self):
        """The 503 came after the call was sent: whether it ran is unknown, so it is reported, not queued."""
        self.ha.script("light.turn_on", state_after=None, respond_after=0.4, status=503)
        body = {"entity_id": "light.rgb", "color_temp_kelvin": 2700}
        with mock.patch.object(hacli, "_probe", return_value=hacli.Reply(0, b"", "refused", 1)):
            status, _, headers, _ = self.post("/api/services/light/turn_on", body)
        self.assertEqual((status, headers["x-aisis-verify-reason"]), (504, "ha_down_in_flight"))
        self.assertEqual(hacli._queue, [])

    def test_a_late_look_that_recovers_keeps_what_the_state_could_not_confirm(self):
        """State reads fail until the budget is over; then the lamp reads ok, partially: the late record says so."""
        self.fast(1.0)
        self.fast_late(3.0)
        self.ha.states["light.rgb"]["state"] = "off"
        self.ha.script("light.turn_on", state_after=0.0, respond_after=None,
                       effect=lambda body, states: states["light.rgb"].update(state="on"))
        real, t0, reads = hacli.read_states, time.monotonic(), {"n": 0}

        def unreadable_until_the_budget_is_over(entities, timeout):
            reads["n"] += 1
            if reads["n"] == 1 or time.monotonic() - t0 > 1.3:
                return real(entities, timeout)
            return {entity: None for entity in entities}, [hacli.Reply(0, b"", "timeout", 1)]

        with mock.patch.object(hacli, "read_states", unreadable_until_the_budget_is_over):
            status, _, headers, _ = self.post("/api/services/light/turn_on", {"entity_id": "light.rgb", "color_temp_kelvin": 2700})
            self.assertEqual((status, headers["x-aisis-verify"]), (504, "unknown"))
            late = self.wait_for("late_verify", 4.0)[0]
        self.assertEqual((late["verify"]["result"], late["verify"]["unchecked"]), ("ok", ["color_temp_kelvin"]))
        self.assertIn("проверено частично", ha_action_log.action_line(late))


if __name__ == "__main__":
    unittest.main()
