"""The dashboard's session page (/session/<dialog>, /session/clip/<clip>) and intents page (/grammar): what they show and that they open."""
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import dashboard
import dialogstore
import session_pages
import trace_view
import voiceclips

NOW = time.time() - 7200     # the archive links clips only once they are 30 s old by the real clock
DIALOG = "ab58437a-07c6-433a-a202-9ccc90402c6f"


def clip(clip_id, ts, status="sent", transcript="", **extra):
    row = {"id": clip_id, "ts": ts, "dur": 2.4, "voiced_ms": 1900, "silence_ms": 800, "peak": 120, "rms": 40, "asr_ms": 554, "final_ms": 900,
           "engine": "faster-whisper large-v3-turbo (wyoming)", "trigger": "vad", "status": status, "transcript": transcript, "reason": None,
           "station": "M00ZRW3008RAJG", "floor_rms": 10, "contrast": 2.5, "gain": 14, "truth": None}
    row.update(extra)
    return row


def dialog(**extra):
    base = {"id": DIALOG, "station": "M00ZRW3008RAJG", "ts": NOW, "status": "serverFinished", "dur": 12.0, "err": "", "raw": ["16:03:37 [INFO] x"],
            "turns": [{"role": "event", "kind": "intent", "t": "16:03:37", "text": "Напомню", "res": "Скажи, когда напомнить"},
                      {"role": "assistant", "kind": "speech", "t": "16:03:38", "text": "Скажи, когда напомнить."}]}
    base.update(extra)
    return base


TRACE = {"id": 7, "ts": NOW + 9.5, "via": "turn", "station": "M00ZRW3008RAJG", "text": "Напомню, выключить грелку через десять минут.",
         "layer": "grammar", "intent": "reminder.help", "tier": "canned", "priority": 3, "conf": 1.0, "fuzzy": False, "pattern": 0,
         "reply": "Скажи, когда напомнить.", "listen": True, "asked": "clarify", "stages_ms": {"grammar": 1, "route": 3, "total": 5}}


class Rendering(unittest.TestCase):
    def test_a_session_shows_activation_request_diagnostics_and_the_intent_definition_link(self):
        wake = clip("20261117T160328-c20733", NOW + 1, "activation", "Малис.", reason="wake")
        request = clip("20261117T160337-f7efcc", NOW + 9, "sent", "Напомню, выключить грелку через десять минут.", reason="wake_cut:Алиса")
        page = session_pages.render_session(dialog(), [wake, request], [TRACE], key=DIALOG)
        for needle in ("🔔 Активация", "Малис.", "Напомню, выключить грелку через десять минут.", "faster-whisper large-v3-turbo",
                       "ASR 554 мс", "×14", "reminder.help", "уточнение", "Скажи, когда напомнить.", "всего 5 мс",
                       "/api/voice/clip/20261117T160337-f7efcc", f"id {DIALOG}", "Журнал квазара", "wake_cut:Алиса"):
            self.assertIn(needle, page)
        self.assertIn("<a href='/grammar#intent-reminder.help'>reminder.help</a>", page)
        self.assertEqual(page.count("Малис."), 1, "the activation is not listed again as a request")

    def test_an_id_that_is_not_a_grammar_intent_is_text_not_a_link(self):
        page = session_pages.render_session(dialog(), [clip("20261117T160337-f7efcc", NOW + 9, "sent", "два плюс два")],
                                            [{**TRACE, "layer": "skill", "intent": "skill.zzz.unknown"}])
        self.assertIn("skill.zzz.unknown", page)
        self.assertNotIn("#intent-skill.zzz.unknown", page)

    def test_a_phrase_nobody_traced_says_so_and_orphan_calls_are_listed(self):
        page = session_pages.render_session(dialog(), [clip("20261117T160337-f7efcc", NOW + 9, "sent", "что-то")],
                                            [{**TRACE, "id": 9, "ts": NOW + 400, "text": "совсем другое"}])
        self.assertIn("Вызовы адаптера без записи", page)
        page = session_pages.render_session(dialog(), [clip("20261117T160337-f7efcc", NOW + 9, "sent", "что-то")], [])
        self.assertIn("Запись адаптера об этой фразе не найдена", page)

    def test_user_text_is_escaped(self):
        page = session_pages.render_session(dialog(), [clip("20261117T160337-f7efcc", NOW + 9, "sent", "<script>alert(1)</script>")],
                                            [{**TRACE, "text": "<script>alert(1)</script>", "reply": "<img src=x onerror=1>"}])
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;", page)

    def test_a_rejected_clip_without_a_dialog_still_has_a_page(self):
        page = session_pages.render_session(None, [clip("20261117T162401-e1d17f", NOW, "gate", "", reason="short")], [], key="x")
        self.assertIn("отброшено (gate): short", page)
        self.assertIn("диалога нет", page)

    def test_an_unknown_session_is_a_404_page(self):
        page = session_pages.render_session(None, [], [], key="nope")
        self.assertIn("Сессия не найдена", page)

    def test_a_dialog_without_archived_audio_still_shows_what_was_heard_and_the_calls_text(self):
        page = session_pages.render_session(dialog(heard=["включи свет"]), [], [{**TRACE, "text": "включи свет"}])
        self.assertIn("Распознано (записи в архиве нет)", page)
        self.assertIn("«включи свет»", page)

    def test_the_intents_page_has_an_anchor_and_the_yaml_block_for_every_intent_and_a_github_line_link(self):
        page = session_pages.render_intents()
        grammar = session_pages.grammar()
        self.assertGreater(len(grammar.intents), 40)
        for intent in grammar.intents:
            self.assertIn(f"id='intent-{intent.id}'", page, intent.id)
        definitions = session_pages.definitions()
        self.assertEqual(set(definitions), {i.id for i in grammar.intents}, "every intent of the bundle has its YAML block")
        self.assertRegex(page, r"https://github\.com/alex-mextner/aisis/blob/main/services/yandex-station/code/intent_grammar/grammar/ru\.yaml#L\d+")
        self.assertNotIn("Example(", page)
        timer = definitions["timer.set"]
        self.assertTrue(timer["source"].startswith("  - id: timer.set"))
        self.assertIn(f"строки {timer['line']}–{timer['end']}", page)


class Ownership(unittest.TestCase):
    def test_a_call_belongs_to_the_same_dialog_whether_one_or_all_are_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "d.sqlite3")
            conn = dialogstore.init_db(db)
            for ident, start, end in (("d1", 100, 110), ("d2", 112, 125)):
                conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,ts_end,status) VALUES(?,?,?,?,?,?,?)",
                             (ident, "s", "voice", "docker:x", start, end, "serverFinished"))
            conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,ts_end,status) VALUES('brain-1','s','text','router',113,113,'complete')")
            conn.commit()
            for ts in (103, 111, 113):
                dialogstore.add_trace(db, {**TRACE, "ts": ts, "text": "t%d" % ts})
            page = {d["id"]: [t["text"] for t in d["traces"]] for d in dialogstore.page(conn)["dialogs"]}
            alone = {i: [t["text"] for t in dialogstore.get_dialog(conn, i)["traces"]] for i in ("d1", "d2")}
            conn.close()
        self.assertEqual(alone, {"d1": ["t103", "t111"], "d2": ["t113"]})
        self.assertEqual({i: page[i] for i in ("d1", "d2")}, alone)
        self.assertEqual(page["brain-1"], [])

    def test_rejected_noise_never_shows_the_neighbouring_commands_diagnostics(self):
        noise = clip("20261117T160327-aaaaaa", NOW, "gate", "", reason="short")
        command = clip("20261117T160337-bbbbbb", NOW + 8, "sent", "поставь кондиционер на 24")
        trace = {**TRACE, "ts": NOW + 9, "text": "поставь кондиционер на 24", "intent": "climate.set"}
        page = session_pages.render_session(dialog(), [noise, command], [trace])

        self.assertEqual(trace_view.traces_of_clip(noise, [trace]), [])
        self.assertEqual([t["id"] for t in trace_view.traces_of_clip(command, [trace])], [7])
        spotter = clip("20261117T160338-cccccc", NOW + 8, "sent", "другое", trigger="spotter")
        self.assertEqual(trace_view.traces_of_clip(spotter, [trace]), [])


class Serving(unittest.TestCase):
    """Through the dashboard's real request handler: status, content type and the rendered intent link."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        patch = mock.patch.object(dialogstore, "DB_PATH", self.db)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"AISIS_VOICE_ARCHIVE": os.path.join(self.tmp.name, "archive")})
        env.start()
        self.addCleanup(env.stop)
        conn = dialogstore.init_db(self.db)
        conn.execute("INSERT INTO dialogs(id,station,kind,src,ts_start,ts_end,status,heard,answered) VALUES(?,?,?,?,?,?,?,?,?)",
                     (DIALOG, "M00ZRW3008RAJG", "voice", "docker:x", NOW - 60, NOW - 40, "serverFinished", '["Напомню"]', '["Скажи"]'))
        conn.commit()
        dialogstore.add_trace(self.db, {**TRACE, "ts": NOW - 50, "text": "Напомню"})
        voiceclips.save(self.db, b"\x00\x01" * 8000, 16000, {"ts": NOW - 52, "status": "sent", "transcript": "Напомню", "engine": "e", "trigger": "vad"})
        voiceclips.save(self.db, b"\x00\x01" * 8000, 16000, {"ts": NOW - 5, "status": "gate", "reason": "short", "engine": "e", "trigger": "vad"})
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as response:
                return response.status, response.headers.get_content_type(), response.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get_content_type(), err.read().decode()

    def clip_ids(self):
        conn = voiceclips.connect(self.db)
        try:
            return [r[0] for r in conn.execute("SELECT id FROM voice_clips ORDER BY ts")]
        finally:
            conn.close()

    def test_the_session_by_dialog_id_opens_with_the_trace_and_the_intent_link(self):
        status, kind, body = self.get("/session/" + DIALOG)
        self.assertEqual((status, kind), (200, "text/html"))
        self.assertIn("<a href='/grammar#intent-reminder.help'>reminder.help</a>", body)
        self.assertIn("Напомню", body)

    def test_the_session_by_clip_id_resolves_the_dialog_and_a_dialogless_clip_has_its_own_page(self):
        sent, gate = self.clip_ids()
        status, _, body = self.get("/session/clip/" + sent)
        self.assertEqual(status, 200)
        self.assertIn(f"id {DIALOG}", body)
        status, _, body = self.get("/session/clip/" + gate)
        self.assertEqual(status, 200)
        self.assertIn("отброшено (gate): short", body)

    def test_unknown_sessions_are_404_and_the_spa_does_not_swallow_the_routes(self):
        self.assertEqual(self.get("/session/nothing-here")[0], 404)
        self.assertEqual(self.get("/session/clip/20261117T000000-aaaaaa")[0], 404)
        self.assertEqual(self.get("/session/clip/..%2F..%2Fetc")[0], 404)
        self.assertFalse(dashboard.is_spa_route("/grammar"))
        self.assertFalse(dashboard.is_spa_route("/session/" + DIALOG))

    def test_the_intents_page_opens_and_carries_the_anchor_sessions_link_to(self):
        status, kind, body = self.get("/grammar")
        self.assertEqual((status, kind), (200, "text/html"))
        self.assertIn("id='intent-reminder.help'", body)

    def test_the_events_api_carries_the_traces_of_each_dialog(self):
        dialogs = dashboard.events_json(10)["dialogs"]
        mine = next(d for d in dialogs if d["id"] == DIALOG)
        self.assertEqual([t["intent"] for t in mine["traces"]], ["reminder.help"])


if __name__ == "__main__":
    unittest.main()
