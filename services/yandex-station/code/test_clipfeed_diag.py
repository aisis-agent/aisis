"""Every clip post says how the utterance was processed and always carries the session link (owner tg#30180)."""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import clipfeed
import cliptruth
import dialogstore
import session_links
import voiceclips

BASE = "http://dash.test:8781"
DIALOG = "ab58437a-07c6-433a-a202-9ccc90402c6f"


def clip_row(**extra):
    row = {"id": "20261117T160337-f7efcc", "ts": time.time() - 120, "dur": 2.42, "transcript": "Напомню, выключить грелку через десять минут.",
           "status": "sent", "reason": None, "trigger": "vad", "peak": 3000, "rms": 400, "voiced_ms": 1900, "silence_ms": 800, "asr_ms": 554,
           "engine": "faster-whisper large-v3-turbo (wyoming)", "dialog_id": DIALOG, "station": "M00ZRW3008RAJG"}
    row.update(extra)
    return row


TRACE = {"v": 1, "via": "turn", "station": "M00ZRW3008RAJG", "text": "Напомню, выключить грелку через десять минут.", "layer": "grammar",
         "intent": "reminder.help", "tier": "canned", "priority": 3, "conf": 1.0, "fuzzy": False, "slots": {"note": "выключить грелку"},
         "reply": "Скажи, когда напомнить.", "listen": True, "asked": "clarify",
         "stages_ms": {"grammar": 1, "route": 3, "total": 5}}


class Caption(unittest.TestCase):
    def test_the_post_carries_the_route_parameters_activation_asr_and_the_session_link(self):
        clip = clip_row(activation={"ts": time.time() - 130, "transcript": "Малис."})
        diag = clipfeed.trace_caption_lines(clip, [{**TRACE, "ts": clip["ts"] + 1}])
        caption = clipfeed.build_caption(clip, "💬 ответ: Скажи, когда напомнить.", 14, diag=diag, link="🔗 %s/session/%s" % (BASE, DIALOG))
        for needle in ("«Напомню, выключить грелку через десять минут.»", "🧭 грамматика → reminder.help · canned · p3 · 1.00", "слоты: note=выключить грелку",
                       "❓ уточнение: «Скажи, когда напомнить.»", "🔔 активация", "«Малис.»", "ASR 554 мс", "усилено ×14", "хвост тишины 800 мс",
                       "🔗 %s/session/%s" % (BASE, DIALOG), "id 20261117T160337-f7efcc"):
            self.assertIn(needle, caption)
        self.assertLessEqual(len(caption), clipfeed.CAPTION_CHARS)
        self.assertTrue(caption.endswith("id 20261117T160337-f7efcc"))

    def test_when_the_space_runs_out_the_link_and_the_answer_stay_and_the_stage_times_go_first(self):
        clip = clip_row()
        busy = {**TRACE, "calls": [{"kind": "ha", "service": "climate.set_temperature", "entity": "climate.kondei_%d" % n,
                                    "data": {"temperature": 24}, "status": 200, "ms": 10} for n in range(8)],
                "errors": ["weather-cli: timeout 8 s"], "jev": {"mode": "on", "utterance": "garbled 0.99", "decision": "jev_garbled", "ms": 250}}
        diag = clipfeed.trace_caption_lines(clip, [busy])
        link = "🔗 %s/session/%s" % (BASE, DIALOG)
        caption = clipfeed.build_caption(clip, "💬 ответ: " + "я" * 400, 1, diag=diag, link=link)
        self.assertLessEqual(len(caption), clipfeed.CAPTION_CHARS)
        self.assertIn(link, caption)
        self.assertIn("id 20261117T160337-f7efcc", caption)
        self.assertIn("🧭 грамматика → reminder.help", caption)
        self.assertIn("⚠ weather-cli: timeout 8 s", caption)
        self.assertNotIn("⏱", caption)
        self.assertEqual(caption.count("\n"), len(caption.split("\n")) - 1)
        self.assertEqual(caption.split("\n")[-2], link)
        self.assertNotIn("Реплай", caption, "the footer is the first optional line to give way")

    def test_a_phrase_without_an_adapter_record_says_why_but_a_noise_clip_does_not(self):
        sent = clipfeed.trace_caption_lines(clip_row(), [])
        self.assertEqual([line for _, line in sent], ["🧭 записи вызова адаптера нет и диалога квазара нет: возможно, фраза не дошла до разбора"])
        self.assertEqual(clipfeed.trace_caption_lines(clip_row(status="gate", transcript=""), []), [])
        self.assertEqual(clipfeed.trace_caption_lines(clip_row(trigger=voiceclips.SPOTTER_TRIGGER), []), [])

    def test_a_phrase_the_quasar_answered_itself_names_the_quasar_fast_intent(self):
        dialog = {"status": "serverFinished", "brain": [], "actions": [
            {"ts": "16:03:38", "at": clip_row()["ts"] + 1, "kind": "запрос", "text": "Напомню, выключить грелку через десять минут.", "res": "Скажи, когда напомнить. (listen=false)"}]}
        sent = clipfeed.trace_caption_lines(clip_row(), [], dialog)
        self.assertEqual([line for _, line in sent], ["🧭 записи вызова адаптера нет: фраза обработана быстрым слоем quasar "
                                                      "(быстрый интент → «Скажи, когда напомнить.»)"])

    def test_a_spotter_clip_with_a_server_side_answer_in_its_dialog_is_explained_by_it(self):
        spotter = clip_row(trigger=voiceclips.SPOTTER_TRIGGER)
        dialog = {"status": "serverFinished", "brain": [], "actions": [
            {"ts": "a", "at": spotter["ts"] + 1, "kind": "запрос", "text": spotter["transcript"], "res": "Включаю"}]}
        self.assertIn("быстрый интент → «Включаю»", clipfeed.trace_caption_lines(spotter, [], dialog)[0][1])

    def test_the_same_phrase_said_again_is_answered_by_its_own_clip_window(self):
        first = clip_row()
        text = first["transcript"]
        dialog = {"status": "serverFinished", "brain": [], "actions": [
            {"ts": "a", "at": first["ts"] + 1, "kind": "запрос", "text": text, "res": "null"},
            {"ts": "b", "at": first["ts"] + 30, "kind": "запрос", "text": text, "res": "Готово"}]}
        sent = clipfeed.trace_caption_lines(first, [], dialog, first["ts"] + 25)
        self.assertNotIn("квазар", sent[0][1].split("адаптера нет")[1].split("и ответа")[0])
        self.assertIn("ответа квазара в диалоге нет", sent[0][1])

    def test_the_hook_and_the_turn_of_one_phrase_are_both_shown_and_named(self):
        brain = {**TRACE, "layer": "brain", "intent": None, "tier": None, "asked": None, "brain": {"provider": "claude", "slot": "a"}}
        diag = clipfeed.trace_caption_lines(clip_row(), [{**TRACE, "via": "hook", "layer": "none", "intent": None, "tier": None, "asked": None},
                                                         brain])
        text = "\n".join(line for _, line in diag)
        self.assertIn("🧭 хук квазара: грамматика не знает эту фразу → ответ: ИИ (текстовый запрос к адаптеру)", text)
        self.assertIn("🧭 текстовый запрос к адаптеру: ИИ → claude a", text)
        self.assertNotIn("не обработано адаптером", text)

    def test_a_handed_on_phrase_with_no_witness_of_the_answer_says_the_quasar_decided(self):
        diag = clipfeed.trace_caption_lines(clip_row(), [{**TRACE, "via": "hook", "layer": "none", "intent": None, "tier": None, "asked": None}])
        self.assertEqual(diag[0][1], "🧭 грамматика не знает эту фразу → дальше решал квазар (быстрый интент или ИИ)")

    def test_activation_line_for_a_separate_wake_segment_and_for_a_cut_head(self):
        separate = clipfeed.activation_line(clip_row(activation={"ts": 100.0, "transcript": "Алиса"}, ts=102.5))
        self.assertIn("«Алиса»", separate)
        self.assertIn("запрос через 2.5 с", separate)
        cut = clipfeed.activation_line(clip_row(reason="wake_cut:Алиса"))
        self.assertIn("«Алиса»", cut)
        self.assertIn("вырезано", cut)
        self.assertIsNone(clipfeed.activation_line(clip_row()))

    def test_with_an_activation_there_is_no_warning_that_the_first_phrase_lacks_a_wake_word(self):
        clip = clip_row(first_in_dialog=True, transcript="включи свет")
        self.assertIn("«Алиса» могла исказиться", "\n".join(clipfeed.endpoint_hints(clip)))
        clip["activation"] = {"ts": 1.0, "transcript": "Алиса"}
        self.assertNotIn("«Алиса» могла исказиться", "\n".join(clipfeed.endpoint_hints(clip)))

    def test_without_a_known_address_the_link_line_says_so_and_keeps_the_path(self):
        self.assertEqual(clipfeed.link_line(clip_row(), BASE), "🔗 %s/session/%s" % (BASE, DIALOG))
        self.assertEqual(clipfeed.link_line(clip_row(dialog_id=None), BASE), "🔗 %s/session/clip/20261117T160337-f7efcc" % BASE)
        self.assertIn("адрес дашборда не определён, путь /session/", clipfeed.link_line(clip_row(), None))


class FeedPosts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        env = mock.patch.dict(os.environ, {"AISIS_VOICE_ARCHIVE": os.path.join(self.tmp.name, "archive"), "AISIS_CONFIG_DIR": self.tmp.name,
                                           "AISIS_DASHBOARD_URL": BASE})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(setattr, session_links, "_cache", None)
        session_links._cache = None
        logbook = mock.patch.object(clipfeed.ha_logbook, "lines", return_value=[])
        logbook.start()        # the live Home Assistant of this host must not add «Home Assistant» lines to the captions asserted here
        self.addCleanup(logbook.stop)
        self.now = time.time()
        conn = dialogstore.init_db(self.db)
        conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,ts_end,status,heard,answered,actions) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (DIALOG, "M00ZRW3008RAJG", "voice", "docker:x", self.now - 130, self.now - 100, "serverFinished", "[]", "[]",
                      '[{"ts":"%s","at":%s,"kind":"запрос","text":"Напомню, выключить грелку через десять минут.","res":"Скажи, когда напомнить. (listen=false)"}]'
                      % (time.strftime("%H:%M:%S", time.localtime(self.now - 119)), self.now - 119)))
        conn.commit()
        conn.close()
        self.sent = []

    def archive(self, ts, **meta):
        saved = voiceclips.save(self.db, b"\x00\x01" * 16000, 16000, {"ts": ts, "engine": "faster-whisper large-v3-turbo (wyoming)", "trigger": "vad",
                                                                      "peak": 3000, "rms": 400, "voiced_ms": 1900, "silence_ms": 800, "asr_ms": 554, **meta})
        self.assertIsNotNone(saved)
        return saved

    def send(self, settings, caption, audio):
        self.sent.append(caption)
        return 30000 + len(self.sent)

    def feed(self):
        conn = cliptruth.connect(self.db)
        try:
            conn.execute("INSERT OR REPLACE INTO clip_feed_state(k, v) VALUES('since', ?)", (str(self.now - 1000),))
            conn.commit()
        finally:
            conn.close()
        return clipfeed.Feed(self.db, send=self.send, now=lambda: self.now)

    def ledger(self):
        conn = cliptruth.connect(self.db)
        try:
            return {r["clip_id"]: (r["state"], r["why"]) for r in conn.execute("SELECT clip_id, state, why FROM clip_feed")}
        finally:
            conn.close()

    def test_a_wake_word_and_its_request_are_one_post_with_one_session_link(self):
        wake = self.archive(self.now - 125, status="activation", transcript="Малис.", reason="wake")
        request = self.archive(self.now - 119, status="sent", transcript="Напомню, выключить грелку через десять минут.")
        dialogstore.add_trace(self.db, {**TRACE, "ts": self.now - 118})
        self.assertEqual(self.feed().tick(), 1)
        (caption,) = self.sent
        self.assertEqual(self.ledger()[wake], ("skipped", "activation"))
        self.assertEqual(self.ledger()[request][0], "sent")
        for needle in ("🔔 активация", "«Малис.»", "🧭 грамматика → reminder.help", "❓ уточнение", "🔗 %s/session/%s" % (BASE, DIALOG), "ASR "):
            self.assertIn(needle, caption)
        self.assertEqual(caption.count("/session/"), 1)

    def test_a_noise_clip_without_a_dialog_links_the_clip_page(self):
        noise = self.archive(self.now - 10, status="gate", reason="short")
        self.assertEqual(self.feed().tick(), 1)
        self.assertIn("🔗 %s/session/clip/%s" % (BASE, noise), self.sent[0])
        self.assertIn("🚫 отброшено как не-речь: short", self.sent[0])

    def test_a_broken_diagnostics_lookup_never_keeps_the_clip_from_the_owner(self):
        self.archive(self.now - 119, status="sent", transcript="Напомню, выключить грелку через десять минут.")
        with mock.patch.object(dialogstore, "traces_between", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(self.feed().tick(), 1)
        self.assertIn("⚠ диагностика обработки недоступна (OperationalError)", self.sent[0])
        self.assertIn("/session/", self.sent[0])

    def test_replaying_an_archived_clip_posts_the_same_diagnostics_and_leaves_the_ledger_alone(self):
        request = self.archive(self.now - 119, status="sent", transcript="Напомню, выключить грелку через десять минут.")
        dialogstore.add_trace(self.db, {**TRACE, "ts": self.now - 118})
        voiceclips.link_dialogs(self.db)
        with mock.patch.object(clipfeed, "send_clip", return_value=("tg", 31000, "")) as send, \
                mock.patch.object(clipfeed.clipbot, "BotService"):
            self.assertEqual(clipfeed.send_replay(self.db, request), 0)
        kwargs = send.call_args.kwargs
        self.assertTrue(kwargs["test"])
        self.assertIn("reminder.help", "\n".join(line for _, line in kwargs["diag"]))
        self.assertEqual(kwargs["link"], "🔗 %s/session/%s" % (BASE, DIALOG))
        self.assertEqual(self.ledger(), {})
        self.assertEqual(clipfeed.send_replay(self.db, "20261117T000000-aaaaaa"), 1)


if __name__ == "__main__":
    unittest.main()
