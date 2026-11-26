"""reply_guard: what the brain's text may say. A reply that only asks to repeat is no reply: the adapter replaces it with the clarifier's line (#307)."""
import unittest

import reply_guard


class TidyTests(unittest.TestCase):
    def test_the_not_heard_tail_and_an_option_list_are_cut(self):
        self.assertEqual(reply_guard.tidy("Урок из пяти чего: минут, упражнений, слов? Не расслышал, уточни."), "Урок из пяти чего?")

    def test_a_plain_answer_is_left_alone(self):
        self.assertEqual(reply_guard.tidy("  Столица Австралии — Канберра. "), "Столица Австралии — Канберра.")

    def test_nothing_left_is_empty_not_a_canned_repeat_request(self):
        self.assertEqual(reply_guard.tidy("Не расслышал, уточни."), "")
        self.assertEqual(reply_guard.tidy("   "), "")


class UnheardTests(unittest.TestCase):
    def test_a_reply_that_only_asks_to_repeat_or_says_nothing_was_understood_is_unheard(self):
        for reply in ("", "Повтори?", "Повторите, пожалуйста.", "Не расслышал. Повтори, пожалуйста.", "Что?", "Что именно?", "Непонятно.",
                      "Не понял.", "Не расслышала, уточни.", "Извини, не расслышал.", "Ещё раз?", "Уточни."):
            self.assertTrue(reply_guard.is_unheard(reply), reply)

    def test_an_answer_or_a_real_question_is_not(self):
        for reply in ("Канберра.", "Рассказать подробнее?", "Включить свет на кухне?", "Повтори за мной: привет.", "Молчу.",
                      "Это я пока не умею."):
            self.assertFalse(reply_guard.is_unheard(reply), reply)


if __name__ == "__main__":
    unittest.main()
