"""extra_dict.py (ticket #268): the loader and its validation, the context rules of the matcher, the mid-sentence names that must stay people, the
wake-word variants at the head, the mode spelling and the stt-cli confusables file.

The real transcripts behind the tests: «Олеса, включи свет.» (clip 20261120T235150-330d2a, after a sentence), the owner's «Олеса Алекса Олеся -> Алиса»
(tg#31621), «Алиса, урок из 5.» (clip 20261109T010646-564b98, the owner's verdict «Алиса, громкость 5»).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import extra_dict
import wake_word


def document(*entries):
    return {"version": 1, "locale": "ru", "entries": list(entries)}


def entry(**changes):
    base = {"id": "wake-test", "kind": "wake", "canonical": "Алиса", "context": "address", "variants": [{"text": "олеса"}],
            "evidence": {"source": "owner", "note": "test"}}
    base.update(changes)
    return base


class CommittedFile(unittest.TestCase):
    def test_the_shipped_dictionary_loads_and_is_not_empty(self):
        dictionary = extra_dict.load()
        self.assertEqual(dictionary.version, 1)
        self.assertTrue(dictionary.of_kind("wake") and dictionary.of_kind("phrase"))

    def test_the_owners_three_names_are_wake_variants(self):
        variants = {variant.text for item in extra_dict.load().of_kind("wake") for variant in item.variants}
        self.assertTrue({"олеса", "олеся", "алекса"} <= variants)

    def test_no_wake_variant_repeats_the_grammar_lexicon(self):
        """The stored «Толиса», «Тариса», «Малис», «Лисы» are already in lexicon.wake: repeating them would be two sources of truth."""
        lexicon = wake_word.lexicon()
        for item in extra_dict.load().of_kind("wake"):
            for variant in item.variants:
                self.assertNotIn(variant.text, lexicon)

    def test_the_committed_json_carries_no_path_or_secret_shaped_text(self):
        text = extra_dict.DATA.read_text(encoding="utf-8")
        for marker in ("/home/", "token", "password", "secret", "http://", "https://"):
            self.assertNotIn(marker, text.lower(), marker)


class Validation(unittest.TestCase):
    def rejects(self, raw, fragment):
        with self.assertRaises(extra_dict.ExtraDictError) as caught:
            extra_dict.parse(raw, "test.json")
        self.assertIn("test.json", str(caught.exception))
        self.assertIn(fragment, str(caught.exception))

    def test_a_minimal_document_loads(self):
        self.assertEqual(len(extra_dict.parse(document(entry())).entries), 1)

    def test_the_top_level_is_checked(self):
        self.rejects([], "top level")
        self.rejects({"version": 2, "locale": "ru", "entries": [entry()]}, "version")
        self.rejects({"version": 1, "locale": "en", "entries": [entry()]}, "locale")
        self.rejects({"version": 1, "locale": "ru"}, "missing keys")
        self.rejects({"version": 1, "locale": "ru", "entries": [entry()], "extra": 1}, "unknown keys")
        self.rejects({"version": 1.0, "locale": "ru", "entries": [entry()]}, "version")
        self.rejects({"version": True, "locale": "ru", "entries": [entry()]}, "version")
        self.rejects(document(), "entries")

    def test_an_entry_names_what_is_wrong(self):
        self.rejects(document(entry(id="Wake Test")), "slug")
        self.rejects(document(entry(kind="name")), "kind")
        self.rejects(document(entry(context="middle")), "context")
        self.rejects(document(entry(kind="phrase", canonical="громкость 5", context="address")), "context")
        self.rejects(document(entry(canonical="")), "canonical")
        self.rejects(document(entry(canonical="Алина")), "lexicon.wake")
        self.rejects(document(entry(evidence={"source": "guess"})), "evidence.source")
        self.rejects(document(entry(evidence="owner")), "evidence")
        self.rejects(document(entry(unknown=1)), "unknown keys")

    def test_variants_are_checked(self):
        self.rejects(document(entry(variants=[])), "variants")
        self.rejects(document(entry(variants=["олеса"])), "variant is an object")
        self.rejects(document(entry(variants=[{"text": "ол"}])), "letters")
        self.rejects(document(entry(variants=[{"text": "олеса", "n": -1}])), "non-negative")
        self.rejects(document(entry(variants=[{"text": "олеса", "n": True}])), "non-negative")
        self.rejects(document(entry(variants=[{"text": "олеса"}, {"text": "Олеса"}])), "twice")
        self.rejects(document(entry(kind="phrase", context="anywhere", canonical="громкость", variants=[{"text": "громкость"}])), "equals the canonical")
        self.rejects(document(entry(variants=[{"text": "алиса"}])), "lexicon.wake")
        self.rejects(document(entry(variants=[{"text": "малис"}])), "lexicon.wake")

    def test_a_variant_cannot_belong_to_two_entries_or_be_another_canonical(self):
        self.rejects(document(entry(), entry(id="wake-other")), "already claimed")
        self.rejects(document(entry(), entry(id="phrase-chain", kind="phrase", context="anywhere", canonical="олеса", variants=[{"text": "урок из 5"}])),
                     "canonical of entry")
        self.rejects(document(entry(), entry()), "used twice")

    def test_the_activation_word_is_no_variant_of_any_kind_nor_part_of_one(self):
        """A command or phrase variant that is, or holds, a name of lexicon.wake would eat the activation word in `on` mode («Алиса включи свет» -> «громкость свет»)."""
        for kind in ("command", "phrase"):
            for text in ("яндекс", "алиса", "брачо", "алиса включи", "включи яндекс", "ну алиса включи свет"):
                self.rejects(document(entry(kind=kind, context="anywhere", canonical="громкость", variants=[{"text": text}], evidence={"source": "owner"})), "lexicon.wake")
        self.rejects(document(entry(variants=[{"text": "алиса олеса"}])), "lexicon.wake")

    def test_evidence_must_carry_a_count_unless_the_owner_named_it(self):
        self.rejects(document(entry(evidence={"source": "observed"}, variants=[{"text": "олеса"}])), "n >= 1")
        self.rejects(document(entry(evidence={"source": "verdict"}, variants=[{"text": "олеса"}])), "n >= 1")
        extra_dict.parse(document(entry(evidence={"source": "observed"}, variants=[{"text": "олеса", "n": 1}])))   # a wake variant is bound by its context
        extra_dict.parse(document(entry(evidence={"source": "owner"}, variants=[{"text": "олеса"}])))

    def test_a_command_or_phrase_seen_once_is_not_enough(self):
        phrase = dict(kind="phrase", context="anywhere", canonical="громкость 5", evidence={"source": "observed"})
        self.rejects(document(entry(**phrase, variants=[{"text": "урок из 5", "n": 1}])), "n >= 2")
        extra_dict.parse(document(entry(**phrase, variants=[{"text": "урок из 5", "n": 2}])))

    def test_files_that_cannot_be_read_name_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with self.assertRaises(extra_dict.ExtraDictError) as caught:
                extra_dict.load(missing)
            self.assertIn("missing.json", str(caught.exception))
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(extra_dict.ExtraDictError) as caught:
                extra_dict.load(broken)
            self.assertIn("broken.json", str(caught.exception))

    def test_a_service_runs_without_a_dictionary_it_cannot_load(self):
        original = extra_dict.DATA
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("[]", encoding="utf-8")
            extra_dict.DATA = bad
            try:
                with self.assertLogs("aisis.extra_dict", level="ERROR") as logs:
                    self.assertIsNone(extra_dict.load_or_none())
            finally:
                extra_dict.DATA = original
        self.assertIn("extra_dict_unusable", logs.output[0])


class WakeVariants(unittest.TestCase):
    dictionary = extra_dict.load()

    def fixed(self, text):
        return self.dictionary.correct(text).text

    def split(self, text):
        found = wake_word.split(self.fixed(text))
        return found.clean, found.head, found.only

    def test_the_variants_at_the_head_are_the_name(self):
        for text, clean in (("Олеса, включи свет.", "Включи свет."), ("олеся, громче", "Громче"), ("Алекса, стоп!", "Стоп!"),
                            ("Эй, Олеса, включи музыку", "Включи музыку"), ("Алекса. Который час?", "Который час?"),
                            ("Олеса: пауза", "Пауза"), ("Олеса — тише", "Тише")):
            with self.subTest(text=text):
                got = self.split(text)
                self.assertEqual((got[0], got[2]), (clean, False))
                self.assertTrue(got[1].endswith("Алиса"))

    def test_a_variant_that_is_the_whole_segment_is_an_activation(self):
        for text in ("Олеса.", "Олеся", "Алекса?", "Олеса, Олеся."):
            with self.subTest(text=text):
                clean, head, only = self.split(text)
                self.assertEqual((clean, only), ("", True))

    def test_the_address_may_be_an_imperative_of_the_grammar_without_a_comma(self):
        self.assertEqual(self.split("Алекса включи свет")[0], "Включи свет")
        self.assertEqual(self.split("Олеся поставь таймер на пять минут")[0], "Поставь таймер на пять минут")

    def test_a_run_of_lead_ins_is_left_to_the_name_cut_like_wake_word_leaves_it(self):
        """wake_word.split takes one lead-in in front of the name: a correction behind two of them would leave the name uncut."""
        for text in ("Ну, эй, Олеса, включи свет", "Эй, ну, Алекса, включи свет"):
            self.assertEqual(self.fixed(text), text)
        self.assertEqual(self.split("Ну, Олеса, включи свет")[0], "Включи свет")
        for text in ("Эй, Олеса, включи свет", "Слушай, Алекса, стоп"):
            fixed = self.fixed(text)
            self.assertTrue(wake_word.split(fixed).head, text)   # whatever is corrected is cut by the split

    def test_a_name_in_the_middle_of_a_sentence_stays_a_name(self):
        for text in ("Мы с Алексой ходили в кино", "Скажи Алексе, что я приду", "Привет, Олеся", "Позвони Олесе, пожалуйста",
                     "Это Алекса, моя знакомая", "Включи свет, Олеса", "Где Олеса?", "Передай привет Алексе и Олесе",
                     "А вот это наша колонка. Олеса, включи свет."):
            with self.subTest(text=text):
                self.assertEqual(self.fixed(text), text)

    def test_a_name_at_the_head_without_the_address_stays_a_name(self):
        for text in ("Алекса пошёл домой", "Олеся любит чай", "Алекса купил молоко и хлеб", "Олеса и Алекса дома"):
            with self.subTest(text=text):
                self.assertEqual(self.fixed(text), text)

    def test_other_forms_and_neighbours_are_not_variants(self):
        for text in ("Алексей, включи свет", "Алексу позови", "Алеся, привет", "Алиса, включи свет", "Олег, стоп", ""):
            with self.subTest(text=text):
                self.assertEqual(self.fixed(text), text)

    def test_the_text_without_a_hit_is_returned_as_it_is(self):
        text = "Включи свет в студии."
        result = self.dictionary.correct(text)
        self.assertIs(result.text, text)
        self.assertEqual(result.hits, ())

    def test_a_hit_says_where_and_what(self):
        result = self.dictionary.correct("Эй, Олеса, включи свет.")
        self.assertEqual(result.text, "Эй, Алиса, включи свет.")
        hit, = result.hits
        self.assertEqual((hit.entry, hit.kind, hit.variant, hit.before, hit.after), ("wake-owner-names", "wake", "олеса", "Олеса", "Алиса"))
        self.assertEqual("Эй, Олеса, включи свет."[hit.start:hit.end], "Олеса")

    def test_a_standalone_variant_needs_the_whole_segment(self):
        dictionary = extra_dict.parse(document(entry(context="standalone", variants=[{"text": "олеса"}])))
        self.assertEqual(dictionary.correct("Олеса.").text, "Алиса.")
        self.assertEqual(dictionary.correct("Олеса, включи свет").text, "Олеса, включи свет")

    def test_a_start_variant_is_cut_whatever_follows(self):
        dictionary = extra_dict.parse(document(entry(context="start", variants=[{"text": "олеса"}])))
        self.assertEqual(dictionary.correct("Олеса включи свет").text, "Алиса включи свет")
        self.assertEqual(dictionary.correct("Привет олеса").text, "Привет олеса")

    def test_a_multi_word_variant_matches_across_blanks_only(self):
        dictionary = extra_dict.parse(document(entry(context="start", variants=[{"text": "алл иса"}])))
        self.assertEqual(dictionary.correct("Алл иса, включи свет").text, "Алиса, включи свет")
        self.assertEqual(dictionary.correct("Алл, иса, включи свет").text, "Алл, иса, включи свет")


class Phrases(unittest.TestCase):
    dictionary = extra_dict.load()

    def test_the_owners_verdict_phrase_is_corrected_with_the_case_kept(self):
        self.assertEqual(self.dictionary.correct("Алиса, урок из 5.").text, "Алиса, громкость 5.")
        self.assertEqual(self.dictionary.correct("урок из 5").text, "громкость 5")
        self.assertEqual(self.dictionary.correct("Урок из 5").text, "Громкость 5")

    def test_whole_words_only(self):
        self.assertEqual(self.dictionary.correct("мурок из 5").text, "мурок из 5")
        self.assertEqual(self.dictionary.correct("урок из 50").text, "урок из 50")
        self.assertEqual(self.dictionary.correct("урок из 5 пожалуйста").text, "урок из 5 пожалуйста")

    def test_a_phrase_and_a_wake_variant_in_one_text(self):
        result = self.dictionary.correct("Олеса, урок из 5")
        self.assertEqual(result.text, "Алиса, громкость 5")
        self.assertEqual({hit.kind for hit in result.hits}, {"wake", "phrase"})

    def test_a_standalone_phrase_needs_the_whole_request_after_the_name(self):
        dictionary = extra_dict.parse(document(entry(kind="phrase", context="standalone", canonical="громкость пять",
                                                     variants=[{"text": "кромкость пять"}], evidence={"source": "owner"})))
        self.assertEqual(dictionary.correct("Кромкость пять.").text, "Громкость пять.")
        self.assertEqual(dictionary.correct("Алиса, кромкость пять.").text, "Алиса, громкость пять.")
        self.assertEqual(dictionary.correct("Эй, Алиса, кромкость пять").text, "Эй, Алиса, громкость пять")
        self.assertEqual(dictionary.correct("Скажи кромкость пять").text, "Скажи кромкость пять")
        self.assertEqual(dictionary.correct("Кромкость пять песен").text, "Кромкость пять песен")

    def test_the_shipped_phrase_is_not_a_sentence_about_lessons(self):
        for text in ("Урок из 5 песен длится час", "Это урок из 5 пунктов", "Сколько стоит урок из 5 занятий"):
            self.assertEqual(self.dictionary.correct(text).text, text)

    def test_overlapping_variants_resolve_to_the_longest(self):
        dictionary = extra_dict.parse(document(
            entry(id="phrase-short", kind="phrase", context="anywhere", canonical="свет", variants=[{"text": "свед"}], evidence={"source": "owner"}),
            entry(id="phrase-long", kind="phrase", context="anywhere", canonical="свет в спальне", variants=[{"text": "свед в спальне"}], evidence={"source": "owner"})))
        self.assertEqual(dictionary.correct("включи свед в спальне").text, "включи свет в спальне")


class Mode(unittest.TestCase):
    def test_the_default_is_shadow_and_an_unknown_value_never_switches_on(self):
        for raw, mode in ((None, "shadow"), ("shadow", "shadow"), ("on", "on"), ("1", "on"), ("enforce", "on"), ("ON", "on"), ("off", "off"), ("0", "off"),
                          ("nonsense", "shadow"), ("", "shadow"), (" on ", "on")):
            self.assertEqual(extra_dict.parse_mode(raw), mode, raw)


class ConfusablesFile(unittest.TestCase):
    dictionary = extra_dict.load()

    def test_only_command_and_phrase_entries_are_exported_as_phonetic_sets(self):
        document_ = extra_dict.confusables_document(self.dictionary)
        self.assertEqual([s["id"] for s in document_["sets"]], ["extra-phrase-volume-five"])
        group_canonical, group_variants = document_["sets"][0]["groups"]
        self.assertEqual(document_["sets"][0]["kind"], "phonetic")
        self.assertEqual(group_canonical["forms"], ["громкость 5"])
        self.assertEqual(group_variants["forms"], ["урок из 5"])

    def test_a_dictionary_with_no_command_or_phrase_has_no_file(self):
        only_wake = extra_dict.parse(document(entry()))
        self.assertIsNone(extra_dict.confusables_document(only_wake))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(extra_dict.write_confusables(only_wake, tmp))
            self.assertEqual(os.listdir(tmp), [])

    def test_the_file_is_written_whole_and_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = extra_dict.write_confusables(self.dictionary, Path(tmp) / "nested")
            second = extra_dict.write_confusables(self.dictionary, Path(tmp) / "nested")
            self.assertEqual(first, second)
            self.assertEqual(json.loads(first.read_text(encoding="utf-8")), extra_dict.confusables_document(self.dictionary))
            self.assertEqual(os.listdir(first.parent), [first.name])   # no temp file is left behind

    def test_a_failed_write_leaves_no_temp_file_and_keeps_the_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = extra_dict.write_confusables(self.dictionary, tmp)
            installed = first.read_bytes()
            original = json.dump
            json.dump = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
            try:
                with self.assertRaises(OSError):
                    extra_dict.write_confusables(self.dictionary, tmp)
            finally:
                json.dump = original
            self.assertEqual(os.listdir(tmp), [first.name])
            self.assertEqual(first.read_bytes(), installed)

    def test_the_cache_directory_follows_xdg(self):
        previous = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = "/tmp/xdg-test"
        try:
            self.assertEqual(extra_dict.cache_dir(), Path("/tmp/xdg-test/aisis/extra-dict"))
        finally:
            if previous is None:
                del os.environ["XDG_CACHE_HOME"]
            else:
                os.environ["XDG_CACHE_HOME"] = previous


try:
    import stt_cli.hypotheses as real_core
except ImportError:
    real_core = None


@unittest.skipIf(real_core is None, "the stt-cli core is not installed")
class RealCore(unittest.TestCase):
    """The file this module writes is accepted by the installed core and extends its sets (CI installs the pinned wheel)."""

    def test_the_core_loads_the_extra_file_and_offers_the_canonical_as_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = extra_dict.write_confusables(extra_dict.load(), tmp)
            extended = real_core.load(extra=[path])
        base = real_core.load()
        self.assertEqual([c.text for c in base.candidates("Алиса, урок из 5.")], [])
        self.assertEqual([c.text for c in extended.candidates("Алиса, урок из 5.")], ["Алиса, громкость 5."])
        self.assertEqual([c.text for c in base.candidates("включи свет")], [c.text for c in extended.candidates("включи свет")])

    def test_the_core_rejects_nothing_the_loader_accepts(self):
        raw = document(entry(kind="command", context="anywhere", canonical="громкость", variants=[{"text": "кромкость"}, {"text": "громкасть"}],
                             evidence={"source": "owner"}))
        dictionary = extra_dict.parse(raw)
        with tempfile.TemporaryDirectory() as tmp:
            path = extra_dict.write_confusables(dictionary, tmp)
            real_core.load(extra=[path])


if __name__ == "__main__":
    unittest.main()
