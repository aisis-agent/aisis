"""The extra dictionary (extra_dict.py, ticket #268) in the STT gateway and in the correction engine: AISIS_EXTRA_DICT=shadow (the default) logs
`extra_dict_would_correct` and changes nothing, `on` corrects, `off` is silent; the engine's confusables take the dictionary's command and
phrase entries only in `on`.

Real inputs: «Олеса, включи свет.» (clip 20261120T235150-330d2a, tg#31621: the owner's «Олеса Алекса Олеся -> Алиса»).
"""
import json
import os
import tempfile
import pathlib
import types
import unittest
from unittest import mock

import correction
import extra_dict
import hypotheses_fakes
import stt_wyoming
import test_stt_wake
from test_stt_wake import speech, silence

try:
    import stt_cli.hypotheses as real_core
except ImportError:
    real_core = None


class Utt:
    """What distrust() reads of an utterance."""
    voiced_ms = 1500
    contrast = 9.0
    duration_ms = 2400
    trailing_ms = 800


DICTIONARY = extra_dict.load()


DYNAMIC_ENTRY = {"id": "dyn-a", "kind": "phrase", "canonical": "люстра", "context": "anywhere", "variants": [{"text": "люстара", "n": 3}], "evidence": {"source": "observed"}}


def install_layer(*entries):
    """A dynamic layer at the place the services look for it (XDG_CACHE_HOME)."""
    layer = extra_dict.dynamic_path()
    layer.parent.mkdir(parents=True, exist_ok=True)
    layer.write_text(json.dumps({"version": 1, "locale": "ru", "entries": list(entries), "generated": {"at": 1, "vocabulary": "x", "min_support": 3},
                                 "vocabulary": {"verbs": [], "names": []}}, ensure_ascii=False), encoding="utf-8")


class Judge(unittest.TestCase):
    def test_off_and_no_dictionary_are_the_plain_split(self):
        for extra, mode in ((None, "on"), (DICTIONARY, "off")):
            rejected, named = stt_wyoming.judge_transcript("Олеса, включи свет.", Utt, extra, mode)
            self.assertEqual((rejected, named.clean, named.head), (None, "Олеса, включи свет.", ""))

    def test_shadow_changes_nothing_and_says_what_it_would_have_done(self):
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            rejected, named = stt_wyoming.judge_transcript("Олеса, включи свет.", Utt, DICTIONARY, "shadow")
        self.assertEqual((rejected, named.clean, named.head, named.only), (None, "Олеса, включи свет.", "", False))
        line, = logs.output
        self.assertIn("extra_dict_would_correct mode=shadow layer=static kinds=wake entries=wake-owner-names", line)
        self.assertIn("before='Олеса, включи свет.' after='Алиса, включи свет.' activation=False", line)

    def test_on_cuts_the_variant_like_the_name(self):
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            rejected, named = stt_wyoming.judge_transcript("Олеса, включи свет.", Utt, DICTIONARY, "on")
        self.assertEqual((rejected, named.clean, named.head), (None, "Включи свет.", "Алиса"))
        self.assertIn("extra_dict_corrected mode=on", logs.output[0])

    def test_on_makes_a_bare_variant_an_activation(self):
        rejected, named = stt_wyoming.judge_transcript("Олеса.", Utt, DICTIONARY, "on")
        self.assertEqual((rejected, named.only, named.clean), (stt_wyoming.WAKE_ONLY, True, ""))

    def test_shadow_of_a_bare_variant_keeps_it_a_request_as_before(self):
        with self.assertLogs("aisis.stt", level="INFO"):
            rejected, named = stt_wyoming.judge_transcript("Олеса.", Utt, DICTIONARY, "shadow")
        self.assertEqual((rejected, named.only, named.clean), (None, False, "Олеса."))

    def test_a_name_in_a_sentence_is_never_touched_in_any_mode(self):
        for mode in ("shadow", "on"):
            with self.subTest(mode=mode):
                rejected, named = stt_wyoming.judge_transcript("Мы с Алексой ходили в кино.", Utt, DICTIONARY, mode)
                self.assertEqual((rejected, named.clean, named.head), (None, "Мы с Алексой ходили в кино.", ""))

    def test_a_phrase_is_corrected_only_in_on(self):
        with self.assertLogs("aisis.stt", level="INFO"):
            self.assertEqual(stt_wyoming.judge_transcript("Алиса, урок из 5.", Utt, DICTIONARY, "shadow")[1].clean, "Урок из 5.")
            self.assertEqual(stt_wyoming.judge_transcript("Алиса, урок из 5.", Utt, DICTIONARY, "on")[1].clean, "Громкость 5.")

    def test_a_rejected_transcript_is_never_corrected(self):
        rejected, named = stt_wyoming.judge_transcript("Продолжение следует...", Utt, DICTIONARY, "on")
        self.assertEqual((rejected, named), ("hallucination", None))


class Tuning(unittest.TestCase):
    def test_the_default_is_shadow_and_unknown_never_switches_on(self):
        for raw, mode in ((None, "shadow"), ("on", "on"), ("1", "on"), ("off", "off"), ("0", "off"), ("nonsense", "shadow")):
            self.assertEqual(stt_wyoming.Tuning.from_env({} if raw is None else {"AISIS_EXTRA_DICT": raw}).extra_dict, mode, raw)


class Sessions(unittest.TestCase):
    """The gateway end to end: the websocket final carries the text downstream."""
    def setUp(self):
        test_stt_wake.GatewayReplay.setUp(self)
        cache = tempfile.TemporaryDirectory()   # the runtime layer is looked for under XDG_CACHE_HOME: never the real one
        self.addCleanup(cache.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.cache = pathlib.Path(cache.name)

    def test_the_runtime_layer_corrects_in_on_and_is_only_logged_in_shadow(self):
        install_layer(DYNAMIC_ENTRY)
        self.env["AISIS_EXTRA_DICT"] = "on"
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            messages, _, _ = self.heard("Алиса, включи люстара")
        self.assertEqual(messages, [{"text": "Включи люстра", "end_of_utt": True}])
        self.assertIn("extra_dict_corrected mode=on layer=dynamic kinds=phrase entries=dyn-a", " ".join(logs.output))
        self.env["AISIS_EXTRA_DICT"] = "shadow"
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            messages, _, _ = self.heard("Алиса, включи люстара")
        self.assertEqual(messages, [{"text": "Включи люстара", "end_of_utt": True}])
        self.assertIn("extra_dict_would_correct mode=shadow layer=dynamic", " ".join(logs.output))

    def test_a_layer_that_cannot_be_read_leaves_the_static_dictionary(self):
        (self.cache / "aisis" / "extra-dict").mkdir(parents=True)
        extra_dict.dynamic_path().write_text("{not json", encoding="utf-8")
        self.env["AISIS_EXTRA_DICT"] = "on"
        with self.assertLogs("aisis.extra_dict", level="WARNING"):
            messages, _, _ = self.heard("Олеса, включи свет.")
        self.assertEqual(messages, [{"text": "Включи свет.", "end_of_utt": True}])
    run_session = test_stt_wake.GatewayReplay.run_session
    rows = test_stt_wake.GatewayReplay.rows

    def heard(self, text):
        return self.run_session([speech(1500) + silence(900)], [text])

    def test_the_default_sends_what_was_heard_and_logs_the_would_be_correction(self):
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            messages, _, rows = self.heard("Олеса, включи свет.")
        self.assertEqual(messages, [{"text": "Олеса, включи свет.", "end_of_utt": True}])
        self.assertEqual(rows, [("sent", None, "Олеса, включи свет.")])
        self.assertEqual(len([line for line in logs.output if "extra_dict_would_correct" in line]), 1)

    def test_on_sends_the_request_without_the_name_and_records_the_cut(self):
        self.env["AISIS_EXTRA_DICT"] = "on"
        messages, _, rows = self.heard("Олеса, включи свет.")
        self.assertEqual(messages, [{"text": "Включи свет.", "end_of_utt": True}])
        self.assertEqual(rows, [("sent", "wake_cut:Алиса", "Включи свет.")])

    def test_on_ends_the_turn_silently_for_a_bare_variant(self):
        self.env["AISIS_EXTRA_DICT"] = "on"
        messages, _, rows = self.run_session([speech(400) + silence(900)], ["Олеся."])
        self.assertEqual(messages, [{"text": "", "end_of_utt": True, "rejected": "wake_word"}])
        self.assertEqual(rows[0][0], "activation")

    def test_off_is_silent(self):
        self.env["AISIS_EXTRA_DICT"] = "off"
        with self.assertLogs("aisis.stt", level="INFO") as logs:   # the gateway's own stt_final line keeps the block from being silent
            messages, _, _ = self.heard("Олеса, включи свет.")
        self.assertEqual(messages, [{"text": "Олеса, включи свет.", "end_of_utt": True}])
        self.assertEqual([line for line in logs.output if "extra_dict" in line], [])

    def test_a_dictionary_that_cannot_load_does_not_stop_the_gateway(self):
        self.env["AISIS_EXTRA_DICT"] = "on"
        original = extra_dict.DATA
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("[]")
            extra_dict.DATA = pathlib.Path(bad)
            try:
                with self.assertLogs("aisis.extra_dict", level="ERROR"):
                    messages, _, rows = self.heard("Олеса, включи свет.")
            finally:
                extra_dict.DATA = original
        self.assertEqual(messages, [{"text": "Олеса, включи свет.", "end_of_utt": True}])
        self.assertEqual(rows, [("sent", None, "Олеса, включи свет.")])


class Engine(unittest.TestCase):
    """correction.Correction: the dictionary's phrase entries as extra confusables, only in `on`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("XDG_CACHE_HOME", None) if self.previous is None else os.environ.__setitem__("XDG_CACHE_HOME", self.previous))

    def build(self, mode, core=None):
        return correction.Correction(lambda text: None, lambda text, context: None, None, mode="shadow", core=core or real_core, extra_dict_mode=mode)

    @unittest.skipIf(real_core is None, "the stt-cli core is not installed")
    def test_a_layer_that_appears_while_running_is_taken_up_and_one_that_goes_is_dropped(self):
        engine = self.build("on")
        self.assertEqual([c.text for c in engine.confusables.candidates("включи люстара")], ["выключи люстара"])
        install_layer(DYNAMIC_ENTRY)
        engine._extra._recheck = 0.0   # do not wait out the 30 s between two looks at the file
        self.assertEqual(sorted(c.text for c in engine.confusables.candidates("включи люстара")), ["включи люстра", "выключи люстара"])
        extra_dict.dynamic_path().unlink()
        self.assertEqual([c.text for c in engine.confusables.candidates("включи люстара")], ["выключи люстара"])

    @unittest.skipIf(real_core is None, "the stt-cli core is not installed")
    def test_shadow_names_the_layer_that_added_the_candidate(self):
        install_layer(DYNAMIC_ENTRY)
        engine = self.build("shadow")
        with self.assertLogs("aisis.correction", level="INFO") as logs:
            engine._shadow_extra("включи люстара")
        self.assertIn("extra_dict_would_correct mode=shadow layer=dynamic,static kind=confusable", logs.output[0])

    @unittest.skipIf(real_core is None, "the stt-cli core is not installed")
    def test_off_is_the_cores_own_sets(self):
        engine = self.build("off")
        self.assertIsNone(engine.shadow_confusables)
        self.assertEqual(engine.confusables.candidates("Алиса, урок из 5."), [])
        self.assertEqual(os.listdir(self.tmp.name), [])   # no file is written when the dictionary is off

    @unittest.skipIf(real_core is None, "the stt-cli core is not installed")
    def test_on_offers_the_canonical_as_a_candidate(self):
        engine = self.build("on")
        self.assertIsNone(engine.shadow_confusables)
        self.assertEqual([c.text for c in engine.confusables.candidates("Алиса, урок из 5.")], ["Алиса, громкость 5."])
        self.assertEqual([c.text for c in engine.confusables.candidates("включи свет")], ["выключи свет"])   # the core's own sets are all still there

    @unittest.skipIf(real_core is None, "the stt-cli core is not installed")
    def test_shadow_decides_with_its_own_sets_and_logs_what_the_extra_ones_add(self):
        engine = self.build("shadow")
        self.assertEqual(engine.confusables.candidates("Алиса, урок из 5."), [])
        with self.assertLogs("aisis.correction", level="INFO") as logs:
            engine._shadow_extra("Алиса, урок из 5.")
            engine._shadow_extra("включи свет")
        line, = logs.output
        self.assertIn("extra_dict_would_correct mode=shadow layer=static kind=confusable before='Алиса, урок из 5.' after=['Алиса, громкость 5.']", line)

    def test_a_core_that_refuses_the_extra_file_leaves_the_engine_on_its_own_sets(self):
        fake = types.SimpleNamespace(Policy=lambda: object(), load=lambda: hypotheses_fakes.FakeConfusables())   # no `extra` parameter: a TypeError
        with self.assertLogs("aisis.correction", level="WARNING") as logs:
            engine = self.build("on", fake)
        self.assertIsInstance(engine.confusables, hypotheses_fakes.FakeConfusables)
        self.assertIsNone(engine.shadow_confusables)
        self.assertIn("extra_dict_confusables_unavailable class=TypeError", logs.output[0])

    def test_an_invalid_dictionary_is_logged_with_the_marker_and_the_reason_and_the_engine_keeps_its_own_sets(self):
        original = extra_dict.DATA
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("[]")
        extra_dict.DATA = pathlib.Path(bad)
        try:
            with self.assertLogs("aisis.correction", level="ERROR") as logs:
                engine = self.build("on", types.SimpleNamespace(Policy=lambda: object(), load=lambda **kwargs: hypotheses_fakes.FakeConfusables()))
        finally:
            extra_dict.DATA = original
        self.assertIsInstance(engine.confusables, hypotheses_fakes.FakeConfusables)
        self.assertIn("extra_dict_unusable error=", logs.output[0])
        self.assertIn("the top level must be an object", logs.output[0])


if __name__ == "__main__":
    unittest.main()
