"""hint_echo.py: a transcript that is only the recogniser's own prompt lists read back (ticket #258).

The real transcripts behind the tests: clip 20261120T133321-8c9f82 (owner verdict «шум», tg#31304 / tg#31315) was «Комнаты, гирлянда, люстра.»
and clip 20261117T222122-e32da8 was «Кондей 2, гирлянда, люстра.». The prompt is the one `refresh-whisper-prompt.sh` installed on 2026-11-20.
"""
import os
import tempfile
import unittest

import hint_echo
import voice_vocab

PROMPT = ("Включи свет на кухне. Выключи люстру. Поставь таймер на десять минут. Разбуди меня в семь тридцать. "
          "Кондиционер: обогрев, охлаждение, температура двадцать три градуса. Комнаты: гостиная, кухня, спальня, студия, угол, второй этаж, "
          "прихожая. Устройства: глобус, ультра лампа, кондей 2, гирлянда, люстра, студия. Исполнители: Eminem, Drummatix.")
HINT = hint_echo.Hint.of([PROMPT])


class ListEntries(unittest.TestCase):
    def test_only_the_prompt_lists_are_entries(self):
        self.assertEqual(HINT.labels, frozenset({"комнаты", "устройства", "исполнители"}))
        self.assertEqual(HINT.items, frozenset({"гостиная", "кухня", "спальня", "студия", "угол", "второй этаж", "прихожая", "глобус", "ультра лампа",
                                                "кондей 2", "гирлянда", "люстра", "eminem", "drummatix"}))

    def test_the_labels_come_from_the_prompt_builder(self):
        """A list the builder adds is an entry without a change here."""
        built = voice_vocab.whisper_prompt(voice_vocab.Catalog(areas=["кухня", "спальня"], entities=["люстра"], artists=["Eminem"]))
        hint = hint_echo.Hint.of([built])
        self.assertEqual(hint.labels, {hint_echo.fold(label) for label, _ in voice_vocab.PROMPT_LISTS})
        self.assertTrue({"кухня", "люстра", "eminem"} <= hint.items)

    def test_example_commands_and_climate_words_are_not_entries(self):
        for phrase in ("Включи свет на кухне", "Выключи люстру", "Кондиционер", "обогрев", "температура двадцать три градуса"):
            self.assertNotIn(hint_echo.fold(phrase), HINT.items | HINT.labels, phrase)

    def test_two_prompts_are_united(self):
        merged = hint_echo.Hint.of([PROMPT, "Комнаты: терраса, баня."])
        self.assertTrue({"терраса", "баня", "кухня"} <= merged.items)

    def test_no_prompt_no_entries(self):
        self.assertEqual(hint_echo.Hint.of(["", ""]), hint_echo.Hint())

    def test_an_item_with_a_full_stop_stays_whole(self):
        """The artist «Dr. Dre» and the device «лампа 2.0» must not cut their list short (the rest of it would never be an entry)."""
        hint = hint_echo.Hint.of(["Комнаты: кухня. Устройства: лампа 2.0, гирлянда, люстра. Исполнители: Dr. Dre, Eminem."])
        self.assertTrue({"кухня", "лампа 2", "гирлянда", "люстра", "dr", "dre", "eminem"} <= hint.items)
        self.assertTrue(hint_echo.is_echo("Устройства: лампа 2.0, гирлянда, люстра.", hint))
        self.assertTrue(hint_echo.is_echo("Исполнители: Dr. Dre, Eminem.", hint))


class Verdict(unittest.TestCase):
    def test_the_two_stored_echoes_match(self):
        self.assertTrue(hint_echo.is_echo("Комнаты, гирлянда, люстра.", HINT))   # tg#31304: the owner's «шум»
        self.assertTrue(hint_echo.is_echo("Кондей 2, гирлянда, люстра.", HINT))

    def test_a_label_with_one_item_matches(self):
        self.assertTrue(hint_echo.is_echo("Устройства: люстра.", HINT))
        self.assertTrue(hint_echo.is_echo("комнаты. кухня", HINT))

    def test_three_items_without_a_label_match(self):
        self.assertTrue(hint_echo.is_echo("Спальня, студия, угол.", HINT))
        self.assertTrue(hint_echo.is_echo("ультра лампа; кондей 2; гирлянда", HINT))

    def test_capitals_and_a_yo_fold(self):
        self.assertTrue(hint_echo.is_echo("ВТОРОЙ ЭТАЖ, Прихожая, Угол", HINT))
        yo = hint_echo.Hint.of(["Комнаты: приёмная, гостиная, студия."])
        self.assertTrue(hint_echo.is_echo("Комнаты, приемная, гостиная", yo))   # the recogniser writes «е», the prompt «ё»

    def test_two_items_without_a_label_are_a_request(self):
        """«кухня, студия» answers «где?»: two rooms are not the prompt read back."""
        self.assertFalse(hint_echo.is_echo("Кухня, студия.", HINT))

    def test_a_label_or_an_item_alone_is_a_request(self):
        for text in ("Комнаты.", "Люстра.", "Студия", "Eminem"):
            self.assertFalse(hint_echo.is_echo(text, HINT), text)

    def test_example_commands_are_requests(self):
        """The recogniser's own example commands read back are still what the owner says: the spotter transcribes «включи свет» word for word."""
        for text in ("Включи свет на кухне.", "Выключи люстру.", "Включи свет.", "Выключи свет", "Поставь таймер на десять минут.",
                     "Включи свет на кухне. Выключи люстру."):
            self.assertFalse(hint_echo.is_echo(text, HINT), text)

    def test_a_climate_request_is_a_request(self):
        for text in ("Кондиционер, обогрев", "Кондиционер на втором этаже.", "Обогрев, охлаждение"):
            self.assertFalse(hint_echo.is_echo(text, HINT), text)

    def test_one_foreign_piece_makes_it_a_request(self):
        for text in ("Комнаты, гирлянда, диван.", "Включи, гирлянда, люстра.", "Гирлянда, люстра, и свет", "Тогда, гирлянда, люстра, студия"):
            self.assertFalse(hint_echo.is_echo(text, HINT), text)

    def test_an_inflected_item_is_not_an_entry(self):
        self.assertFalse(hint_echo.is_echo("Комнаты, гирлянду, люстру.", HINT))

    def test_empty_and_punctuation_only_are_not_an_echo(self):
        for text in ("", " ", "...", ",,"):
            self.assertFalse(hint_echo.is_echo(text, HINT), repr(text))

    def test_without_a_prompt_nothing_is_an_echo(self):
        self.assertFalse(hint_echo.is_echo("Комнаты, гирлянда, люстра.", hint_echo.Hint()))


class Mode(unittest.TestCase):
    def test_shadow_is_the_default(self):
        self.assertEqual(hint_echo.DEFAULT_MODE, "shadow")


class PromptFileReading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "whisper-prompt.env")

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_reads_the_installed_env_file(self):
        self.write(f'WHISPER_INITIAL_PROMPT="{PROMPT}"\n')
        self.assertEqual(hint_echo.PromptFile(self.path).read(), PROMPT)

    def test_unescapes_what_the_installer_escaped(self):
        self.write('WHISPER_INITIAL_PROMPT="Исполнители: Say \\"Hi\\", A\\\\B."\n')
        self.assertEqual(hint_echo.PromptFile(self.path).read(), 'Исполнители: Say "Hi", A\\B.')

    def test_a_missing_file_is_no_prompt(self):
        self.assertEqual(hint_echo.PromptFile(self.path).read(), "")

    def test_a_file_without_the_variable_is_no_prompt(self):
        self.write("SOMETHING_ELSE=1\n")
        self.assertEqual(hint_echo.PromptFile(self.path).read(), "")

    def test_an_unreadable_encoding_is_no_prompt(self):
        with open(self.path, "wb") as handle:
            handle.write(b'WHISPER_INITIAL_PROMPT="\xff\xfe"\n')
        self.assertEqual(hint_echo.PromptFile(self.path).read(), "")

    def test_a_rewritten_file_is_read_again(self):
        self.write('WHISPER_INITIAL_PROMPT="Комнаты: кухня."\n')
        reader = hint_echo.PromptFile(self.path)
        self.assertEqual(reader.read(), "Комнаты: кухня.")
        self.write('WHISPER_INITIAL_PROMPT="Комнаты: кухня, спальня."\n')
        self.assertEqual(reader.read(), "Комнаты: кухня, спальня.")

    def test_a_removed_file_forgets_the_prompt(self):
        self.write('WHISPER_INITIAL_PROMPT="Комнаты: кухня."\n')
        reader = hint_echo.PromptFile(self.path)
        reader.read()
        os.remove(self.path)
        self.assertEqual(reader.read(), "")


if __name__ == "__main__":
    unittest.main()
