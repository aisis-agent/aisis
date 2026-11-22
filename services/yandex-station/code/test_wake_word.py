"""The activation word: wake_word.split at the head of a transcript (pure, no recognizer, no audio) and the clip feed's rule for it."""
import os
import tempfile
import unittest
from unittest import mock

import bench_asr_archive as bench
import clipfeed
import voiceclips
import wake_word


class WakeWordSplitTests(unittest.TestCase):
    def split(self, text):
        found = wake_word.split(text)
        return found.clean, found.head, found.only

    def test_a_bare_name_is_an_activation_not_content(self):
        # what Whisper really wrote for the station's «Алиса» (voice archive 2026-11)
        for text in ("Алиса.", "Алиса?", "Малис.", "Тариса.", "Лисы.", "алиса", "Алиса, Алиса.", "Яндекс.",
                     "Малисан.", "Алису", "Лиса."):   # not in the lexicon: a sound-alike counts when it is all that was said
            with self.subTest(text=text):
                clean, head, only = self.split(text)
                self.assertEqual((clean, only), ("", True))
                self.assertTrue(head)

    def test_the_name_is_cut_from_the_head_of_a_request(self):
        for text, want, head in (
                ("Алиса, какая сейчас погода?", "Какая сейчас погода?", "Алиса"),
                ("Алиса. Громкость пять.", "Громкость пять.", "Алиса"),
                ("алиса включи свет", "Включи свет", "алиса"),
                ("Толиса в эфирционерном трамотаже на нагреве.", "В эфирционерном трамотаже на нагреве.", "Толиса"),
                ("Алиса, Алиса, стоп!", "Стоп!", "Алиса, Алиса"),
                ("Эй, Алиса, включи музыку", "Включи музыку", "Эй, Алиса"),
                ("Яндекс, который час?", "Который час?", "Яндекс"),
                ("Алиса - включи свет", "Включи свет", "Алиса")):
            with self.subTest(text=text):
                self.assertEqual(self.split(text), (want, head, False))

    def test_everything_else_is_left_alone(self):
        for text in ("Включи свет.", "Здравствуйте, Алиса.", "Алина, привет", "Расскажи про Алису в стране чудес",
                     "Лиса бежит по лесу", "Какая погода?", "Напомню, выключить грелку через десять минут.", ""):
            with self.subTest(text=text):
                self.assertEqual(self.split(text), (text, "", False))

    def test_a_sound_alike_is_never_cut_from_a_request(self):
        """«полиса» is two edits from «алиса»: after the name, or first, it is the request's own word."""
        self.assertEqual(self.split("Алиса, полиса нет, что делать?"), ("Полиса нет, что делать?", "Алиса", False))
        self.assertEqual(self.split("Полиса нет"), ("Полиса нет", "", False))
        self.assertEqual(self.split("Малисан включи свет"), ("Малисан включи свет", "", False))   # add it to the lexicon to cut it

    def test_a_minus_sign_is_content(self):
        self.assertEqual(self.split("Алиса, -5 громкость"), ("-5 громкость", "Алиса", False))
        self.assertEqual(self.split("Алиса 5 громкость"), ("5 громкость", "Алиса", False))
        for minus in ("—", "–", "−"):   # the grammar reads these as a minus too (normalize.fold): a negative level is no level
            self.assertEqual(self.split(f"Алиса, {minus}10 громкость"), (f"{minus}10 громкость", "Алиса", False))

    def test_only_a_repeat_of_the_name_is_cut_after_it(self):
        """«Яндекс» and «лисы» after the name are the request's own subject."""
        self.assertEqual(self.split("Алиса, Яндекс — это чья компания?"), ("Яндекс — это чья компания?", "Алиса", False))
        self.assertEqual(self.split("Алиса, лисы опасны для человека?"), ("Лисы опасны для человека?", "Алиса", False))
        self.assertEqual(self.split("Алиса, алиса, стоп"), ("Стоп", "Алиса, алиса", False))

    def test_a_real_word_of_the_lexicon_is_cut_only_when_it_is_addressed(self):
        """«лисы» and «алисы» are in lexicon.wake and are ordinary words too (ticket #282): «Лисы живут в лесу» keeps its first word."""
        for text in ("Лисы живут в лесу", "Алисы нет дома", "Лисы — хищные животные", "Алисы нет дома, она ушла.", "лисы опасны для человека?"):
            with self.subTest(text=text):
                self.assertEqual(self.split(text), (text, "", False))
        for text, want, head in (("Лисы, включи свет", "Включи свет", "Лисы"), ("Лисы включи свет", "Включи свет", "Лисы"),
                                 ("Алисы, какая погода?", "Какая погода?", "Алисы"), ("Эй, лисы, стоп!", "Стоп!", "Эй, лисы"),
                                 ("Алисы: включи музыку", "Включи музыку", "Алисы")):
            with self.subTest(text=text):
                self.assertEqual(self.split(text), (want, head, False))
        self.assertEqual(self.split("Лисы."), ("", "Лисы", True))   # the stored activation: the whole segment
        self.assertEqual(self.split("Алисы!"), ("", "Алисы", True))

    def test_a_comparative_or_pause_right_behind_a_real_word_is_a_request(self):
        for text, want in (("Алисы громче", "Громче"), ("Лисы тише", "Тише"), ("Алисы пауза", "Пауза")):
            with self.subTest(text=text):
                self.assertEqual(self.split(text)[0], want)

    def test_the_verbs_come_from_the_grammar_and_a_floor_when_it_cannot_be_read(self):
        verbs = wake_word.command_verbs()
        self.assertTrue({"включи", "запусти", "громче"} <= verbs)
        wake_word.command_verbs.cache_clear()
        try:
            with mock.patch.object(wake_word, "BUNDLE", "/nonexistent/ru.json"), self.assertLogs("aisis.wake_word", level="WARNING"):
                self.assertEqual(wake_word.command_verbs(), wake_word.VERB_FLOOR | frozenset(wake_word.VOICE_WORDS))
        finally:
            wake_word.command_verbs.cache_clear()

    def test_the_lexicon_is_the_grammars_one(self):
        """One list of names: the intent grammar's lexicon. A word it knows is cut here too."""
        import json
        with open(os.path.join(os.path.dirname(wake_word.__file__), "intent_grammar", "grammar", "ru.json"), encoding="utf-8") as bundle:
            lexicon = json.load(bundle)["data"]["lexicon"]["wake"]
        for word in lexicon:
            with self.subTest(word=word):
                self.assertTrue(wake_word.split(word + " включи свет").head, word)


class BenchSkipsActivations(unittest.TestCase):
    """An activation clip (the name alone) is no utterance: no engine is benchmarked on it, it counts as neither sent nor gated."""

    def test_an_activation_clip_is_not_benchmarked_nor_counted_as_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "dialogs.sqlite3")
            conn = bench.connect(db)
            for clip_id, status, transcript in (("a1", voiceclips.ACTIVATION, "Малис."), ("s1", "sent", "Включи свет"), ("g1", "gate", None)):
                conn.execute("INSERT INTO voice_clips(id, ts, path, status, transcript) VALUES(?,?,?,?,?)",
                             (clip_id, 1.0, clip_id + ".opus", status, transcript))
            conn.commit()
            self.assertEqual(sorted(row["id"] for row in bench.pending(conn, "engine", 10)), ["g1", "s1"])
            for clip_id, text in (("a1", "Алиса."), ("s1", "Включи свет"), ("g1", "")):
                conn.execute("INSERT INTO asr_bench VALUES(?,?,?,?,?,?,?,?,?,?)", (clip_id, "engine", text, 100, 1.0, int(not text), 0, 0, None, 1.0))
            conn.commit()
            conn.close()
            engine = bench.summary(db)["engines"]["engine"]
            self.assertEqual((engine["n"], engine["gated_clips"], engine["text_on_gated"], engine["live_clips"], engine["wer_live"]),
                             (2, 1, 0, 1, 0.0))


class ActivationIsNoPost(unittest.TestCase):
    """tg#30182: «Алиса» was a feed item of its own. An activation clip is part of the session of the request that follows."""

    def feed(self):
        return clipfeed.Feed(":memory:")

    def clip(self, status, transcript):
        return {"id": "20261117T160328-c20733", "ts": 1794927808.8, "status": status, "transcript": transcript}

    def test_an_activation_clip_is_never_posted_on_its_own(self):
        for settings in ({**clipfeed.DEFAULTS, "include_rejected": True}, {**clipfeed.DEFAULTS, "include_rejected": False}):
            self.assertEqual(self.feed().skip_reason(settings, self.clip(voiceclips.ACTIVATION, "Малис.")), "activation")

    def test_other_clips_are_judged_as_before(self):
        settings = {**clipfeed.DEFAULTS, "include_rejected": True}
        self.assertIsNone(self.feed().skip_reason(settings, self.clip("sent", "Включи свет.")))
        self.assertIsNone(self.feed().skip_reason(settings, self.clip("gate", "")))      # gated noise still shows, as before
        self.assertEqual(self.feed().skip_reason({**settings, "include_rejected": False}, self.clip("gate", "")), "rejected")


if __name__ == "__main__":
    unittest.main()
