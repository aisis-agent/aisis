"""The verification verdict is visible where the owner looks: the utterance trace, the clip post in Telegram and the session page (owner tg#30861)."""
import io
import os
import tempfile
import time
import unittest
from unittest import mock

import clipfeed
import ha_action_log
import session_pages
import trace_view

NOW = time.time() - 7200
CLIP = {"id": "20261119T212142-d20a57", "ts": NOW + 1.0, "dur": 2.0, "transcript": "выключи свет", "status": "sent", "reason": None,
        "trigger": "spotter", "peak": 189, "rms": 19, "dialog_id": "80649d63-df9a-4e48-b796-52bdd30e0bb4", "station": "st1"}
HOOK_TRACE = {"id": 1, "ts": NOW + 0.2, "via": "hook", "text": "выключи свет", "layer": "none", "stages_ms": {"route": 5, "total": 6}}
ACTION = {"v": 1, "kind": "action", "ts": NOW + 0.4, "id": "9f3a1c20", "service": "light.turn_off", "entities": ["light.studiia"],
          "expect": "turn_off", "before": {"light.studiia": "on"}, "call": {"pending": True},
          "verify": {"result": "ok", "reason": "state_matched", "attempts": 3, "ms": 940, "after": {"light.studiia": "off"}},
          "reply": {"status": 200, "why": "verified_state"}, "ms": 960}


class Journal(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "ha-actions.jsonl")
        self.journal = ha_action_log.Journal(self.path, stream=io.StringIO())
        patch = mock.patch.object(ha_action_log, "JOURNAL_PATH", self.path)
        patch.start()
        self.addCleanup(patch.stop)


class TraceLines(unittest.TestCase):
    def test_a_verified_call_shows_the_state_it_found_not_only_the_http_status(self):
        call = {"kind": "ha", "service": "light.turn_on", "entity": "light.lamp", "data": {"brightness_pct": 50}, "status": 200, "ok": True, "ms": 1200,
                "verify": {"result": "ok", "reason": "state_matched", "ms": 840, "after": {"light.lamp": "on 50%"}}, "aid": "ab12", "ha": "timeout"}
        self.assertEqual(trace_view.call_line(call), "HA light.turn_on light.lamp {'brightness_pct': 50} → проверено: light.lamp вкл 50% за 0.8 с "
                                                      "(ответ HA: таймаут) · 1200 мс")

    def test_a_call_without_a_verdict_keeps_the_old_line(self):
        call = {"kind": "ha", "service": "climate.set_temperature", "entity": "climate.ac", "status": 200, "ms": 10}
        self.assertEqual(trace_view.call_line(call), "HA climate.set_temperature climate.ac → 200 · 10 мс")

    def test_the_reason_of_the_reply_is_a_line_of_the_trace(self):
        lines = trace_view.lines({"layer": "grammar", "intent": "light.brightness", "reply": "Яркость 50 процентов.", "reply_why": "ha_verified"})
        self.assertIn("💬 причина ответа: состояние устройства проверено и совпало", lines)
        lines = trace_view.lines({"layer": "grammar", "reply_why": "ha_unverified"})
        self.assertTrue(any("состояние не прочитано" in line for line in lines))

    def test_an_unknown_reason_code_is_shown_as_is(self):
        self.assertIn("💬 причина ответа: new_code", trace_view.lines({"layer": "grammar", "reply_why": "new_code"}))


class ClipPost(Journal):
    def post_lines(self, traces):
        with mock.patch.object(clipfeed.dialogstore, "traces_between", return_value=traces):
            return [line for _, line in clipfeed.processing_lines(mock.Mock(), dict(CLIP), CLIP["ts"] + 5)]

    def test_the_clip_the_quasar_layer_handled_shows_what_ha_cli_verified(self):
        self.journal.write(ACTION)
        lines = self.post_lines([HOOK_TRACE])
        self.assertIn("🧭 грамматика не знает эту фразу → дальше решал квазар (быстрый интент или ИИ)", lines)   # the hook declined: the quasar layer answered
        self.assertIn("🔧 HA light.turn_off light.studiia → проверено: light.studiia выкл за 0.9 с", lines)

    def test_a_call_the_adapter_traced_is_not_shown_twice(self):
        self.journal.write(ACTION)
        traced = {**HOOK_TRACE, "via": "turn", "layer": "grammar", "calls": [{"kind": "ha", "service": "light.turn_off", "aid": "9f3a1c20", "status": 200}]}
        lines = self.post_lines([traced])
        self.assertFalse([line for line in lines if "проверено: light.studiia" in line])

    def test_an_outage_that_was_observed_is_a_line_and_a_cause_that_was_not_is_not(self):
        self.journal.write({"kind": "ha_status", "ts": NOW + 0.5, "from": "up", "to": "down", "error": "refused"})
        self.journal.write({**ACTION, "id": "q1", "call": {"error": "refused", "ms": 3},
                            "verify": {"result": "unknown", "reason": "ha_down_observed", "ms": 260}, "reply": {"status": 202, "why": "queued_ha_down"}})
        text = "\n".join(self.post_lines([HOOK_TRACE]))
        self.assertIn("⚠ HA: up → down (соединение отклонено)", text)
        self.assertIn("HA недоступен (соединение отклонено)", text)
        self.assertIn("причина ответа ha-cli: HA недоступен: наблюдалось, команда в очереди", text)
        self.assertNotIn("перезагруж", text)

    def test_a_noise_clip_never_borrows_a_neighbours_ha_actions(self):
        self.journal.write(ACTION)
        with mock.patch.object(clipfeed.dialogstore, "traces_between", return_value=[HOOK_TRACE]):
            lines = clipfeed.processing_lines(mock.Mock(), {**CLIP, "status": "gate", "transcript": ""}, CLIP["ts"] + 5)
        self.assertEqual(lines, [])

    def test_an_unreadable_journal_never_keeps_the_clip_from_the_owner(self):
        with mock.patch.object(ha_action_log, "read", side_effect=PermissionError("denied")):
            lines = self.post_lines([HOOK_TRACE])
        self.assertTrue(any("диагностика обработки недоступна" in line for line in lines))


class SessionPage(unittest.TestCase):
    def test_the_session_page_lists_the_ha_cli_actions_of_the_phrase(self):
        page = session_pages.render_session(None, [CLIP], [HOOK_TRACE], key=CLIP["id"], ha_actions=[ACTION])
        self.assertIn("Home Assistant (ha-cli)", page)
        self.assertIn("HA light.turn_off light.studiia → проверено: light.studiia выкл за 0.9 с", page)

    def test_no_ha_card_when_ha_cli_did_nothing(self):
        self.assertNotIn("Home Assistant (ha-cli)", session_pages.render_session(None, [CLIP], [HOOK_TRACE], key=CLIP["id"]))

    def test_actions_of_another_time_are_not_attributed_to_the_phrase(self):
        far = {**ACTION, "ts": NOW + 600}
        self.assertNotIn("Home Assistant (ha-cli)", session_pages.render_session(None, [CLIP], [HOOK_TRACE], key=CLIP["id"], ha_actions=[far]))


if __name__ == "__main__":
    unittest.main()
