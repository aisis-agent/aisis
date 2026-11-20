"""How the correction engine's evidence reads in diagnostics (#182): the trace's `correction` record, the second opinion stored for a
clip, the Telegram caption and the session and /grammar pages.

Every hypothesis, percentage and dialog line below is a FIXTURE shaped like the documented contract (trace.correction and
voiceclips.hypotheses_of), not a measurement. The clip row is the real one of the owner's clip tg#30662 (id, text, trigger)."""
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import clipfeed
import session_pages
import trace_view
import voiceclips

CORRECTION = {
    "trigger": "contradiction", "reason": "noop", "decision": "ask", "heard": "включи свет",
    "nbest": [{"text": "выключи свет", "percent": 94, "source": "beam", "engine": "gigaam-v3-ctc", "fit": 0.91},
              {"text": "включи свет", "percent": 5, "source": "primary", "engine": "yandex-spotter", "fit": None},
              {"text": "включи свет в студии", "percent": 1, "source": "beam", "engine": "gigaam-v3-ctc", "fit": 0.4}],
    "nbest_ms": 120, "cue": [{"kind": "noop", "supports": "выключи свет"}],
    "dialog": {"state": "awaiting_confirm", "ask": "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?", "answer": "да",
               "outcome": "accepted", "retries": 0},
}

EVIDENCE = {"source": "spotter", "primary_text": "включи свет", "ms": 140,
            "nbest": {"engine": "gigaam-v3-ctc", "ms": 120, "hypotheses": [
                {"text": "выключи свет", "percent": 94, "posterior": 0.94, "fit": 0.91, "source": "beam", "engine": "gigaam-v3-ctc",
                 "logprob": -0.1, "words": []},
                {"text": "включи свет", "percent": 5, "posterior": 0.05, "fit": 0.5, "source": "rescore", "engine": "gigaam-v3-ctc",
                 "logprob": -3.0, "words": []}]}}

# the real row of the owner's clip: spotter text, no Whisper, closed by the spotter
OWNER_CLIP = {"id": "20261119T142227-210b52", "ts": time.time() - 120, "dur": 2.0, "transcript": "включи свет", "status": "sent",
              "reason": None, "trigger": voiceclips.SPOTTER_TRIGGER, "peak": 91, "rms": 8, "voiced_ms": 1500, "silence_ms": 400,
              "asr_ms": None, "engine": "yandex-spotter", "dialog_id": None, "station": "M00ZRW3008RAJG"}
SPOTTER_TRACE = {"v": 1, "via": "hook", "station": "M00ZRW3008RAJG", "text": "включи свет", "layer": "none"}


class CorrectionLines(unittest.TestCase):
    def lines(self, correction):
        return trace_view.ranked_lines({"layer": "grammar", "correction": correction}, compact=True)[1:]

    def test_the_decision_and_trigger_lead_in_russian_and_the_alternatives_follow_with_percentages(self):
        got = self.lines(CORRECTION)
        self.assertEqual(got[0], (1, "🔎 уточнение: подозрение на противоположную команду"))
        self.assertIn((1, "варианты: «выключи свет» 94% · «включи свет» 5%"), got)
        self.assertIn((2, "ещё варианты: «включи свет в студии» 1%"), got)

    def test_the_question_the_answer_and_the_outcome_are_one_rank_one_line(self):
        got = dict((text, rank) for rank, text in self.lines(CORRECTION))
        line = "❓ «Мне послышалось «включи свет». Вы имели в виду «выключи свет»?» → «да»: принято"
        self.assertEqual(got.get(line), 1)

    def test_cues_are_rank_two_and_the_second_opinion_time_is_rank_four(self):
        got = self.lines(CORRECTION)
        self.assertIn((2, "улика: команда ничего не меняет (в пользу «выключи свет»)"), got)
        self.assertIn((4, "⏱ второе мнение: 120 мс"), got)

    def test_a_choice_outcome_names_the_chosen_phrase_and_an_open_question_says_it_waits(self):
        choice = dict(CORRECTION, decision="ask-choice", trigger="low-margin",
                      dialog={"state": "awaiting_choice", "ask": "Включить или выключить?", "answer": "выключить", "outcome": "chose:выключи свет"})
        self.assertIn((1, "❓ «Включить или выключить?» → «выключить»: выбрано «выключи свет»"), self.lines(choice))
        open_ = dict(CORRECTION, dialog={"state": "awaiting_confirm", "ask": "Вы имели в виду «выключи свет»?"})
        self.assertIn((1, "❓ «Вы имели в виду «выключи свет»?» · ждём «да» или «нет»"), self.lines(open_))

    def test_a_finished_dialog_never_says_it_is_still_waiting(self):
        got = " ".join(text for _, text in self.lines(CORRECTION))     # the producer keeps the state it last had: awaiting_confirm
        self.assertIn("принято", got)
        self.assertNotIn("ждём", got)

    def test_a_denied_answer_is_still_waiting_for_the_repeat(self):
        denied = dict(CORRECTION, dialog={"state": "awaiting_repeat", "ask": "Повторите, пожалуйста.", "answer": "нет",
                                          "outcome": "denied", "retries": 1})
        self.assertIn((1, "❓ «Повторите, пожалуйста.» → «нет»: отклонено · ждём повтор фразы"), self.lines(denied))

    def test_a_shadow_question_is_labelled_as_never_asked(self):
        shadow = dict(CORRECTION, shadow=True, dialog={"state": "awaiting_confirm", "ask": "Вы имели в виду «выключи свет»?",
                                                       "answer": None, "outcome": None, "retries": 0})
        got = self.lines(shadow)
        self.assertIn((1, "❓ вопрос не задан (теневой режим): «Вы имели в виду «выключи свет»?»"), got)
        self.assertNotIn("ждём", " ".join(text for _, text in got))

    def test_a_decision_without_a_question_is_a_low_rank_note(self):
        got = self.lines({"decision": "none", "reason": "agree", "nbest": CORRECTION["nbest"][:1]})
        self.assertEqual(got[0], (3, "🔎 без вопроса: оба распознавания согласны"))

    def test_every_reason_the_engine_and_the_core_emit_has_a_russian_wording(self):
        for reason in ("agree", "vetoed", "long", "no-evidence", "below-floor", "same-action", "no-action", "no-wording"):
            with self.subTest(reason=reason):
                self.assertNotIn(reason, self.lines({"decision": "none", "reason": reason})[0][1])

    def test_percentages_are_diagnostics_only_the_asked_question_has_none(self):
        question = CORRECTION["dialog"]["ask"]
        self.assertNotRegex(question, r"\d+\s*%")

    def test_junk_never_raises(self):
        for junk in (None, 5, "x", [], [1, 2], {}, {"nbest": "x"}, {"nbest": [None, 3, {"text": None, "percent": "n"}]},
                     {"cue": {"kind": 1}}, {"cue": [None, {"kind": None, "supports": 7}]}, {"dialog": "x"}, {"dialog": {"ask": 1, "outcome": 2}},
                     {"decision": ["a"], "trigger": {}, "nbest_ms": "slow"}, {"nbest": [{"text": "a", "percent": float("nan")}]}):
            with self.subTest(junk=junk):
                trace_view.ranked_lines({"layer": "grammar", "correction": junk})
                trace_view.lines({"correction": junk}, compact=True)

    def test_a_posterior_too_large_to_scale_is_dropped_not_raised(self):
        for huge in (1e308, -1e308, float("inf")):
            with self.subTest(huge=huge):
                self.assertIsNone(trace_view.percent({"posterior": huge}))
        self.assertEqual(trace_view.percent({"posterior": 1e308, "percent": 7}), 7)
        self.assertEqual(trace_view.percent({"posterior": 0.94}), 94)

    def test_unknown_words_pass_through_instead_of_vanishing(self):
        got = self.lines({"decision": "ask", "trigger": "brand-new-trigger"})
        self.assertEqual(got[0], (1, "🔎 уточнение: brand-new-trigger"))

    def test_a_record_without_a_correction_is_unchanged(self):
        self.assertEqual(trace_view.lines({"layer": "grammar"}), [trace_view.route_line({"layer": "grammar"})])


class EvidenceLines(unittest.TestCase):
    def test_the_second_opinion_is_labelled_with_its_engine(self):
        got = trace_view.evidence_lines(EVIDENCE, [SPOTTER_TRACE])
        self.assertEqual(got[0], (1, "🔎 второе мнение (GigaAM): «выключи свет» 94% · «включи свет» 5%"))
        self.assertIn((2, "основное распознавание (споттер колонки): «включи свет»"), got)
        self.assertIn((4, "⏱ второе мнение: 140 мс"), got)       # nbest.ms 120 is the scoring alone; the evidence's own ms is the whole time

    def test_the_reply_clip_keeps_its_own_evidence_when_the_trace_alternatives_belong_to_the_question(self):
        question_trace = {"correction": dict(CORRECTION, heard="включи свет")}      # a reply trace inherits the question's record
        self.assertTrue(trace_view.evidence_lines(dict(EVIDENCE, primary_text="да"), [question_trace]))
        # the owner repeats the phrase to keep it: the same words, other hypotheses of its own
        repeat = dict(EVIDENCE, primary_text="Включи свет.")
        self.assertTrue(trace_view.evidence_lines(repeat, [{"correction": CORRECTION}]))

    def test_the_trace_of_the_question_already_carries_the_alternatives_so_they_are_not_repeated(self):
        asked = dict(CORRECTION, dialog={"state": "awaiting_confirm", "ask": "Вы имели в виду «выключи свет»?", "answer": None,
                                         "outcome": None, "retries": 0})
        self.assertEqual(trace_view.evidence_lines(EVIDENCE, [{"correction": asked}]), [])
        stored = dict(EVIDENCE, primary_text="Включи свет.")      # the gateway keeps the primary as it said it, the hook's `heard` is normalised
        self.assertEqual(trace_view.evidence_lines(stored, [{"correction": asked}]), [])
        expired = dict(CORRECTION, dialog={"state": "awaiting_confirm", "ask": "?", "answer": None, "outcome": "timeout", "retries": 0})
        self.assertTrue(trace_view.evidence_lines(EVIDENCE, [{"correction": expired}]))      # the unrelated next utterance

    def test_a_correction_without_alternatives_does_not_hide_the_evidence(self):
        self.assertTrue(trace_view.evidence_lines(EVIDENCE, [{"correction": {"decision": "none", "reason": "no-evidence"}}]))

    def test_no_evidence_and_junk_give_nothing(self):
        for junk in (None, {}, {"nbest": None}, {"nbest": {"hypotheses": "x"}}, {"nbest": {"hypotheses": [None]}}, 7):
            with self.subTest(junk=junk):
                self.assertEqual(trace_view.evidence_lines(junk, []), [])


class OwnerClipCaption(unittest.TestCase):
    """tg#30662: the spotter heard «включи свет»; the second opinion (fixture) says «выключи свет»."""

    def caption(self, evidence=EVIDENCE, traces=(SPOTTER_TRACE,)):
        clip = dict(OWNER_CLIP)
        diag = clipfeed.trace_caption_lines(clip, list(traces)) + clipfeed.evidence_caption_lines(evidence, list(traces))
        return clipfeed.build_caption(clip, "распознано самой колонкой (споттер команд); текст не от Whisper", 1, diag=diag,
                                      link="🔗 http://dash.test:8781/session/clip/" + clip["id"])

    def test_the_caption_shows_the_alternatives_with_the_engine_that_said_them(self):
        text = self.caption()
        print("\n--- caption of the fixture shaped like tg#30662 ---\n" + text + "\n---")
        self.assertIn("«включи свет»", text.splitlines()[0])
        self.assertIn("🔎 второе мнение (GigaAM): «выключи свет» 94% · «включи свет» 5%", text)
        self.assertIn("id 20261119T142227-210b52", text)
        self.assertLessEqual(len(text), 1024)

    def test_a_tight_caption_keeps_the_alternatives_before_the_stage_times_and_the_footer(self):
        clip = dict(OWNER_CLIP)
        diag = clipfeed.evidence_caption_lines(EVIDENCE, []) + [(4, "⏱ " + "x" * 200)]
        text = clipfeed.build_caption(clip, "r", 1, diag=diag, link="🔗 l", limit=260)
        self.assertIn("второе мнение (GigaAM)", text)
        self.assertNotIn("xxxx", text)

    def test_a_trace_with_a_correction_and_the_same_evidence_shows_the_variants_once(self):
        trace = dict(SPOTTER_TRACE, correction=dict(CORRECTION, dialog={"state": "awaiting_confirm", "ask": "Вы имели в виду «выключи свет»?",
                                                                          "answer": None, "outcome": None, "retries": 0}))
        text = self.caption(traces=[trace])
        self.assertEqual(sum(l.startswith("варианты:") for l in text.splitlines()), 1)
        self.assertNotIn("второе мнение (GigaAM)", text)
        self.assertIn("🔎 уточнение: подозрение на противоположную команду", text)

    def test_a_clip_without_evidence_is_posted_as_before(self):
        text = self.caption(evidence=None)
        self.assertNotIn("🔎", text)


class EvidenceLookup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = self.tmp + "/d.db"
        sqlite3.connect(self.db).close()

    def test_post_context_adds_the_stored_second_opinion(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        for ddl in ("CREATE TABLE voice_clips (id TEXT, ts REAL, status TEXT, dialog_id TEXT, transcript TEXT)",
                    "CREATE TABLE dialog_turns (dialog_id TEXT)"):
            conn.execute(ddl)
        clip = dict(OWNER_CLIP)
        with mock.patch.object(voiceclips, "hypotheses_of", create=True, return_value=EVIDENCE) as lookup, \
                mock.patch.object(clipfeed, "window_end", return_value=clip["ts"] + 5), \
                mock.patch.object(clipfeed, "load_dialog", return_value=None), \
                mock.patch.object(clipfeed, "activation_of", return_value=None), \
                mock.patch.object(clipfeed.dialogstore, "traces_between", return_value=[]):
            _, diag, _ = clipfeed.post_context(conn, clip, self.db)
        lookup.assert_called_once_with(self.db, clip["id"])
        self.assertTrue(any("второе мнение (GigaAM)" in line for _, line in diag))

    def test_a_failing_lookup_never_keeps_the_clip_from_the_owner(self):
        with mock.patch.object(voiceclips, "hypotheses_of", create=True, side_effect=RuntimeError("boom")):
            self.assertEqual(clipfeed.evidence_of(self.db, OWNER_CLIP), None)


class DashboardAttachesEvidence(unittest.TestCase):
    def test_the_session_of_a_spotter_clip_shows_the_stored_second_opinion(self):
        import dashboard
        import dialogstore
        with tempfile.TemporaryDirectory() as tmp:
            db = tmp + "/dialogs.sqlite3"
            dialogstore.init_db(db).close()
            with mock.patch.dict("os.environ", {"AISIS_VOICE_ARCHIVE": tmp + "/archive"}), mock.patch.object(dialogstore, "DB_PATH", db):
                voiceclips.save(db, b"\x00\x01" * 8000, 16000, {"ts": time.time() - 7200, "status": "sent", "transcript": "включи свет",
                                                               "engine": "yandex-spotter", "trigger": voiceclips.SPOTTER_TRIGGER})
                conn = voiceclips.connect(db)
                clip_id = conn.execute("SELECT id FROM voice_clips").fetchone()[0]
                conn.close()
                with mock.patch.object(voiceclips, "hypotheses_of", create=True, return_value=EVIDENCE) as lookup:
                    status, body = dashboard.session_html(clip_id=clip_id)
        self.assertEqual(status, 200)
        lookup.assert_called_once_with(db, clip_id)
        self.assertIn("🔎 второе мнение (GigaAM): «выключи свет» 94% · «включи свет» 5%", body)


GRAMMAR = SimpleNamespace(
    version="t", intents=[
        SimpleNamespace(id="light.off", tier="execute", priority=10, phase=None, action={"executor": "ha"}, labels=(), reply=None,
                        pattern_text=["выключи свет"], examples=[], contexts=()),
        SimpleNamespace(id="repair.accept", tier="execute", priority=900, phase=None, action={"executor": "repair.accept"}, labels=(),
                        reply=None, pattern_text=["да"], examples=[SimpleNamespace(say="да")], contexts=("repair.confirm",)),
        SimpleNamespace(id="repair.cancel", tier="execute", priority=900, phase=None, action={"executor": "repair.cancel"}, labels=(),
                        reply=None, pattern_text=["отмена"], examples=[], contexts=("repair.confirm", "repair.repeat"))],
    contexts={"repair.confirm": SimpleNamespace(id="repair.confirm", values=("alt", "heard"), sample={"alt": ("выключи свет",)}),
              "repair.repeat": SimpleNamespace(id="repair.repeat", values=(), sample={})})


class GrammarPage(unittest.TestCase):
    def render(self, grammar):
        with mock.patch.object(session_pages, "grammar", return_value=grammar), mock.patch.object(session_pages, "definitions", return_value={}):
            return session_pages.render_intents()

    def test_contexts_group_their_intents_with_anchors(self):
        page = self.render(GRAMMAR)
        self.assertIn("id='context-repair.confirm'", page)
        self.assertIn("id='context-repair.repeat'", page)
        confirm = page.split("id='context-repair.confirm'")[1].split("</details>")[0]
        self.assertIn("href='/grammar#intent-repair.accept'", confirm)
        self.assertIn("href='/grammar#intent-repair.cancel'", confirm)
        self.assertNotIn("light.off", confirm)
        repeat = page.split("id='context-repair.repeat'")[1].split("</details>")[0]
        self.assertIn("repair.cancel", repeat)
        self.assertNotIn("repair.accept", repeat)

    def test_an_intent_card_names_its_contexts_and_keeps_its_anchor(self):
        page = self.render(GRAMMAR)
        self.assertIn("id='intent-repair.accept'", page)
        self.assertIn("repair.confirm", page.split("id='intent-repair.accept'")[1].split("</details>")[0])

    def test_the_committed_bundle_shows_every_declared_context_with_its_members(self):
        real = session_pages.grammar()
        page = session_pages.render_intents()
        for context_id in real.contexts:
            with self.subTest(context=context_id):
                card = page.split(f"id='context-{context_id}'")[1].split("</details>")[0]
                for intent in (i for i in real.intents if context_id in i.contexts):
                    self.assertIn(f"#intent-{intent.id}", card)

    def test_a_bundle_without_contexts_renders_the_page_as_before(self):
        plain = SimpleNamespace(version="t", intents=[GRAMMAR.intents[0]])      # no `contexts` attribute at all
        page = self.render(plain)
        self.assertIn("id='intent-light.off'", page)
        self.assertNotIn("context-", page)

    def test_a_context_anchor_opens_like_an_intent_anchor(self):
        self.assertIn("DETAILS", session_pages.SCRIPT)


class SessionPage(unittest.TestCase):
    def render(self, traces, evidence):
        clip = dict(OWNER_CLIP, gain=1, hypotheses=evidence)
        return session_pages.render_session(None, [clip], traces, key=clip["id"])

    def test_the_session_shows_the_lines_the_full_table_and_the_dialog_transcript(self):
        trace = dict(SPOTTER_TRACE, id=1, ts=OWNER_CLIP["ts"] + 1, correction=CORRECTION)
        page = self.render([trace], EVIDENCE)
        for needle in ("🔎 уточнение: подозрение на противоположную команду", "варианты: «выключи свет» 94% · «включи свет» 5%",
                       "gigaam-v3-ctc", "yandex-spotter", "0.91", "120 мс", "Мне послышалось «включи свет». Вы имели в виду «выключи свет»?",
                       "ответ: да", "итог диалога: принято"):
            self.assertIn(needle, page)

    def test_a_spotter_clip_without_a_correction_shows_the_second_opinion_and_its_table(self):
        page = self.render([], EVIDENCE)
        self.assertIn("🔎 второе мнение (GigaAM): «выключи свет» 94% · «включи свет» 5%", page)
        self.assertIn("<th>fit</th>", page)
        self.assertIn("rescore", page)

    def test_junk_evidence_and_correction_do_not_break_the_page(self):
        trace = dict(SPOTTER_TRACE, id=1, ts=OWNER_CLIP["ts"] + 1, correction={"nbest": [None, 3], "dialog": "x", "cue": 7})
        page = self.render([trace], {"nbest": {"hypotheses": [None, {"text": 5}]}})
        self.assertIn("включи свет", page)

    def test_a_correction_html_is_escaped(self):
        trace = dict(SPOTTER_TRACE, id=1, ts=OWNER_CLIP["ts"] + 1,
                     correction={"decision": "ask", "dialog": {"ask": "<script>x</script>", "answer": "<b>"}})
        page = self.render([trace], None)
        self.assertNotIn("<script>x</script>", page)

    def test_a_shadow_question_and_a_finished_dialog_read_truthfully_on_the_page(self):
        shadow = dict(CORRECTION, shadow=True, dialog={"state": "awaiting_confirm", "ask": "Вы имели в виду «выключи свет»?",
                                                       "answer": None, "outcome": None, "retries": 0})
        page = self.render([dict(SPOTTER_TRACE, id=1, ts=OWNER_CLIP["ts"] + 1, correction=shadow)], None)
        self.assertIn("теневой режим", page)
        self.assertNotIn("ждём", page)
        done = self.render([dict(SPOTTER_TRACE, id=1, ts=OWNER_CLIP["ts"] + 1, correction=CORRECTION)], None)
        self.assertIn("итог диалога: принято", done)
        self.assertNotIn("ждём", done)

    def test_the_stored_second_opinion_shows_its_whole_time(self):
        page = self.render([], EVIDENCE)
        self.assertIn("140 мс", page)
        self.assertNotIn("120 мс", page)

    def test_a_denied_answer_still_waits_for_the_repeat_on_the_page(self):
        denied = dict(CORRECTION, dialog={"state": "awaiting_repeat", "ask": "Повторите, пожалуйста.", "answer": "нет",
                                          "outcome": "denied", "retries": 1})
        page = self.render([dict(SPOTTER_TRACE, id=1, ts=OWNER_CLIP["ts"] + 1, correction=denied)], None)
        self.assertIn("итог диалога: отклонено", page)
        self.assertIn("состояние: ждём повтор фразы", page)


if __name__ == "__main__":
    unittest.main()
