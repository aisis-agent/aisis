"""The verification verdict is visible where the owner looks: the utterance trace, the clip post in Telegram and the session page (owner tg#30861);
Home Assistant's own logbook line sits beside it."""
import time
import unittest
from unittest import mock

import clipfeed
import session_pages
import trace_view

NOW = time.time() - 7200
CLIP = {"id": "20261119T212142-d20a57", "ts": NOW + 1.0, "dur": 2.0, "transcript": "выключи свет", "status": "sent", "reason": None,
        "trigger": "spotter", "peak": 189, "rms": 19, "dialog_id": "80649d63-df9a-4e48-b796-52bdd30e0bb4", "station": "st1"}
HOOK_TRACE = {"id": 1, "ts": NOW + 0.2, "via": "hook", "text": "выключи свет", "layer": "none", "stages_ms": {"route": 5, "total": 6}}
LOGBOOK_LINE = (2, "Home Assistant: Студия → выключен (21:21:43)")


class TraceLines(unittest.TestCase):
    def test_a_verified_call_shows_the_state_it_found_not_only_the_http_status(self):
        call = {"kind": "ha", "service": "light.turn_on", "entity": "light.lamp", "data": {"brightness_pct": 50}, "status": 200, "ok": True, "ms": 1200,
                "verify": {"result": "ok", "reason": "state_matched", "ms": 840, "after": {"light.lamp": "on 50%"}}, "aid": "ab12", "ha": "timeout"}
        self.assertEqual(trace_view.call_line(call), "HA light.turn_on light.lamp {'brightness_pct': 50} → проверено: light.lamp вкл 50% за 0.8 с "
                                                      "(ответ HA: таймаут) · 1200 мс")

    def test_a_call_without_a_verdict_keeps_the_old_line(self):
        call = {"kind": "ha", "service": "climate.set_temperature", "entity": "climate.ac", "status": 200, "ms": 10}
        self.assertEqual(trace_view.call_line(call), "HA climate.set_temperature climate.ac → 200 · 10 мс")

    def test_an_outage_call_says_it_was_not_sent(self):
        call = {"kind": "ha", "service": "light.turn_off", "entity": "light.lamp", "status": 0, "ms": 3, "ha": "refused", "down": "refused",
                "verify": {"result": "unknown", "reason": "ha_down_observed", "ms": 260}}
        line = trace_view.call_line(call)
        self.assertIn("Home Assistant был недоступен, команда не отправлена (соединение отклонено)", line)
        self.assertNotIn("очеред", line)

    def test_the_reason_of_the_reply_is_a_line_of_the_trace(self):
        lines = trace_view.lines({"layer": "grammar", "intent": "light.brightness", "reply": "Яркость 50 процентов.", "reply_why": "ha_verified"})
        self.assertIn("💬 причина ответа: состояние устройства проверено и совпало", lines)
        lines = trace_view.lines({"layer": "grammar", "reply_why": "ha_unverified"})
        self.assertTrue(any("состояние не прочитано" in line for line in lines))

    def test_an_unknown_reason_code_is_shown_as_is(self):
        self.assertIn("💬 причина ответа: new_code", trace_view.lines({"layer": "grammar", "reply_why": "new_code"}))


class ClipPost(unittest.TestCase):
    def post_lines(self, traces, logbook=(LOGBOOK_LINE,)):
        with mock.patch.object(clipfeed.dialogstore, "traces_between", return_value=traces), \
                mock.patch.object(clipfeed.ha_client, "default"), \
                mock.patch.object(clipfeed.ha_logbook, "lines", return_value=list(logbook)) as lines:
            out = [line for _, line in clipfeed.processing_lines(mock.Mock(), dict(CLIP), CLIP["ts"] + 5)]
        self.lines = lines
        return out

    def test_the_clip_shows_home_assistants_own_record_of_the_phrase(self):
        lines = self.post_lines([HOOK_TRACE])
        self.assertIn("🧭 грамматика не знает эту фразу → дальше решал квазар (быстрый интент или ИИ)", lines)
        self.assertIn(LOGBOOK_LINE[1], lines)
        start, end = self.lines.call_args.args[1:]
        self.assertAlmostEqual(start, CLIP["ts"] - trace_view.MATCH_BEFORE_S)
        self.assertAlmostEqual(end, CLIP["ts"] + 5 + trace_view.MATCH_AFTER_S)

    def test_a_noise_clip_never_borrows_a_neighbours_ha_actions(self):
        with mock.patch.object(clipfeed.dialogstore, "traces_between", return_value=[HOOK_TRACE]), \
                mock.patch.object(clipfeed.ha_logbook, "lines", return_value=[LOGBOOK_LINE]) as lines:
            out = clipfeed.processing_lines(mock.Mock(), {**CLIP, "status": "gate", "transcript": ""}, CLIP["ts"] + 5)
        self.assertEqual(out, [])
        lines.assert_not_called()

    def test_an_unreadable_logbook_never_keeps_the_clip_from_the_owner(self):
        with mock.patch.object(clipfeed.dialogstore, "traces_between", return_value=[HOOK_TRACE]), \
                mock.patch.object(clipfeed.ha_client, "default"), \
                mock.patch.object(clipfeed.ha_logbook, "lines", side_effect=PermissionError("denied")):
            out = [line for _, line in clipfeed.processing_lines(mock.Mock(), dict(CLIP), CLIP["ts"] + 5)]
        self.assertTrue(any("диагностика обработки недоступна" in line for line in out))


class SessionPage(unittest.TestCase):
    def test_the_session_page_lists_home_assistants_record_of_the_phrase(self):
        page = session_pages.render_session(None, [CLIP], [HOOK_TRACE], key=CLIP["id"], ha_lines=lambda start, end: [LOGBOOK_LINE])
        self.assertIn("<h2>Home Assistant</h2>", page)
        self.assertIn("Home Assistant: Студия → выключен (21:21:43)", page)

    def test_no_ha_card_without_entries(self):
        self.assertNotIn("<h2>Home Assistant</h2>", session_pages.render_session(None, [CLIP], [HOOK_TRACE], key=CLIP["id"]))
        self.assertNotIn("<h2>Home Assistant</h2>", session_pages.render_session(None, [CLIP], [HOOK_TRACE], key=CLIP["id"], ha_lines=lambda s, e: []))

    def test_each_phrase_asks_for_its_own_window(self):
        second = {**CLIP, "id": "20261119T212150-bbbbbb", "ts": NOW + 9.0, "transcript": "включи свет"}
        windows = []
        session_pages.render_session(None, [CLIP, second], [], key="x", ha_lines=lambda start, end: windows.append((start, end)) or [])
        self.assertEqual(windows, [(CLIP["ts"] - trace_view.MATCH_BEFORE_S, second["ts"]), (second["ts"] - trace_view.MATCH_BEFORE_S, second["ts"] + 45)])


if __name__ == "__main__":
    unittest.main()
