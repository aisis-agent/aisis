"""Tests of the intent grammar engine: normaliser, numerals, durations/clock, patterns, decisions, back-test."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import intent_grammar.grammar as engine
from intent_grammar import GrammarError, backtest, cli, loads
from intent_grammar.grammar import load, render_reply
from intent_grammar.normalize import Canon, edit_distance_le1, normalise, phonetic_key, repair_typo, stem, tokenize
from intent_grammar.numerals import parse_number, parse_ordinal
from intent_grammar.timewords import duration_ru, parse_clock, parse_duration, plural

SHIPPED = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.yaml"


def words(text):
    return text.split()


class NormaliserTests(unittest.TestCase):
    def test_stem_joins_inflections_and_keeps_short_words(self):
        self.assertEqual(stem("кухне"), stem("кухню"))
        self.assertEqual(stem("включи"), stem("включить"))
        self.assertEqual(stem("громкость"), stem("громкости"))
        self.assertEqual(stem("минут"), stem("минуту"))
        self.assertEqual(stem("стой"), "стой")   # not «сто» (a hundred)
        self.assertEqual(stem("на"), "на")

    def test_tokenize_folds_and_expands_percent(self):
        tokens, spans = tokenize("Громкость 50%, поставь-ка!")
        self.assertEqual(tokens, ["громкость", "50", "процентов", "поставь"])
        self.assertEqual(spans[0], (0, 9))
        self.assertEqual(tokenize("ёлка 7,5")[0], ["елка", "7.5"])

    def test_wake_words_and_fillers_are_cut_and_recorded(self):
        canon = Canon(fillers=frozenset({"пожалуйста"}), lead_in=frozenset({"алиса", "ну"}), wake=frozenset({"алиса"}))
        got = normalise("Ну, Алиса, включи свет, пожалуйста", canon)
        self.assertEqual(got.tokens, ["включи", "свет"])
        self.assertTrue(got.woke)
        self.assertEqual(got.spans[0], (11, 17))
        alone = normalise("Алиса?", canon)
        self.assertEqual(alone.tokens, ["алиса"])
        self.assertTrue(alone.woke)

    def test_negation_and_question_flags(self):
        canon = Canon(negation=frozenset({"не"}), question_words=frozenset({"какой"}))
        self.assertTrue(normalise("не включай свет", canon).negated)
        self.assertTrue(normalise("какой свет", canon).question)
        self.assertTrue(normalise("свет включен?", canon).question)

    def test_edit_distance_and_phonetics(self):
        self.assertTrue(edit_distance_le1("громкость", "громксть"))
        self.assertTrue(edit_distance_le1("стоп", "сотп"))
        self.assertFalse(edit_distance_le1("стоп", "пауза"))
        self.assertEqual(phonetic_key("кандиционер"), phonetic_key("кондиционер"))

    def test_typo_repair_abstains_when_two_words_are_close(self):
        self.assertEqual(repair_typo("включии", ("включи", "выключи")), "включи")
        self.assertEqual(repair_typo("вкл", ("включи",)), "вкл")                      # too short to guess
        self.assertEqual(repair_typo("погромчо", ("погромче", "погромчу")), "погромчо")  # two candidates: abstain

    def test_signed_numbers_stay_signed(self):
        self.assertEqual(tokenize("громкость -5")[0], ["громкость", "-5"])
        self.assertEqual(tokenize("громкость −5")[0], ["громкость", "-5"])     # the real minus sign
        self.assertEqual(tokenize("пять-десять 5-10")[0], ["пять-десять", "5", "10"])
        self.assertEqual(parse_number(["-5"]), [])


class NumeralTests(unittest.TestCase):
    def best(self, text):
        readings = parse_number(words(text))
        return readings[0] if readings else None

    def test_cardinals_in_all_forms(self):
        self.assertEqual(self.best("пять"), (1, 5))
        self.assertEqual(self.best("двадцать три"), (2, 23))
        self.assertEqual(self.best("сто двадцать пять"), (3, 125))
        self.assertEqual(self.best("двух"), (1, 2))
        self.assertEqual(self.best("пятидесяти"), (1, 50))
        self.assertEqual(self.best("две тысячи пятьсот"), (3, 2500))
        self.assertEqual(self.best("тысяча"), (1, 1000))
        self.assertEqual(self.best("ноль"), (1, 0))

    def test_halves_digits_decimals(self):
        self.assertEqual(self.best("полтора"), (1, 1.5))
        self.assertEqual(self.best("пять с половиной"), (3, 5.5))
        self.assertEqual(self.best("50"), (1, 50))
        self.assertEqual(self.best("7.5"), (1, 7.5))
        self.assertEqual(self.best("5 тысяч"), (2, 5000))

    def test_every_prefix_reading_is_offered_longest_first(self):
        self.assertEqual(parse_number(words("двадцать три")), [(2, 23), (1, 20)])
        self.assertEqual(parse_number(words("два тридцать")), [(1, 2)])   # a clock, not 32
        self.assertEqual(parse_number(words("кухня")), [])

    def test_ordinals(self):
        self.assertEqual(parse_ordinal(words("седьмого")), [(1, 7)])
        self.assertEqual(parse_ordinal(words("двадцать первый")), [(2, 21)])
        self.assertEqual(parse_ordinal(words("пять")), [])

    def test_oversized_digit_runs_are_not_numbers(self):
        self.assertEqual(parse_number(["9" * 400]), [])
        self.assertEqual(parse_number(["9" * 400 + ".5"]), [])
        self.assertEqual(parse_number(["123456789"]), [(1, 123456789)])
        self.assertEqual(parse_clock(["9" * 5000 + ":30"]), [])


class TimeTests(unittest.TestCase):
    def duration(self, text):
        readings = parse_duration(words(text))
        return readings[0] if readings else None

    def test_durations(self):
        self.assertEqual(self.duration("пять минут"), (2, 300))
        self.assertEqual(self.duration("минуту"), (1, 60))
        self.assertEqual(self.duration("полчаса"), (1, 1800))
        self.assertEqual(self.duration("пол часа"), (2, 1800))
        self.assertEqual(self.duration("полтора часа"), (2, 5400))
        self.assertEqual(self.duration("час и пять минут"), (4, 3900))
        self.assertEqual(self.duration("две минуты тридцать секунд"), (4, 150))
        self.assertEqual(self.duration("1.5 часа"), (2, 5400))
        self.assertIsNone(self.duration("минут"))     # a bare plural is not a quantity
        self.assertIsNone(self.duration("кухня"))

    def test_duration_words(self):
        self.assertEqual(duration_ru(300), "5 минут")
        self.assertEqual(duration_ru(5400), "1 час 30 минут")
        self.assertEqual(duration_ru(90), "1 минута 30 секунд")
        self.assertEqual(duration_ru(21 * 60), "21 минута")

    def clock(self, text):
        readings = parse_clock(words(text))
        return (readings[0][0], readings[0][1]["text"], readings[0][1]["ambiguous"]) if readings else None

    def test_clock_forms(self):
        self.assertEqual(self.clock("7:30"), (1, "07:30", False))
        self.assertEqual(self.clock("семь тридцать"), (2, "07:30", True))
        self.assertEqual(self.clock("семь ноль пять"), (3, "07:05", True))
        self.assertEqual(self.clock("семь утра"), (2, "07:00", False))
        self.assertEqual(self.clock("семь вечера"), (2, "19:00", False))
        self.assertEqual(self.clock("двенадцать ночи"), (2, "00:00", False))
        self.assertEqual(self.clock("двадцать три сорок"), (3, "23:40", False))

    def test_spoken_idioms(self):
        self.assertEqual(self.clock("половина седьмого"), (2, "06:30", True))
        self.assertEqual(self.clock("пол седьмого вечера"), (3, "18:30", False))
        self.assertEqual(self.clock("без пятнадцати восемь"), (3, "07:45", True))
        self.assertEqual(self.clock("без четверти восемь"), (3, "07:45", True))
        self.assertEqual(self.clock("десять минут седьмого"), (3, "06:10", True))
        self.assertEqual(self.clock("четверть шестого"), (2, "05:15", True))

    def test_idioms_resolve_the_day_part_of_the_referenced_hour(self):
        self.assertEqual(self.clock("половина двенадцатого дня"), (3, "11:30", False))
        self.assertEqual(self.clock("без пятнадцати двенадцать дня"), (4, "11:45", False))
        self.assertEqual(self.clock("четверть двенадцатого дня"), (3, "11:15", False))
        self.assertEqual(self.clock("десять минут двенадцатого дня"), (4, "11:10", False))
        self.assertEqual(self.clock("половина двенадцатого утра"), (3, "11:30", False))
        self.assertEqual(self.clock("половина первого дня"), (3, "12:30", False))
        self.assertEqual(self.clock("половина двенадцатого ночи"), (3, "23:30", False))
        self.assertEqual(self.clock("без четверти двенадцать ночи"), (4, "23:45", False))
        self.assertEqual(self.clock("половина первого ночи"), (3, "00:30", False))
        self.assertEqual(self.clock("без пятнадцати восемь вечера"), (4, "19:45", False))

    def test_explicit_24_hour_values_survive_a_day_part(self):
        self.assertEqual(self.clock("23:30 ночи"), (2, "23:30", False))
        self.assertEqual(self.clock("двадцать три тридцать ночи"), (4, "23:30", False))
        self.assertEqual(self.clock("7:30 вечера"), (2, "19:30", False))
        self.assertIsNone(self.clock("15:00 ночи"))        # incompatible: rejected, not shifted
        self.assertIsNone(self.clock("15:00 утра"))


MINI = """
version: 1
lexicon:
  wake: [алиса]
  lead_in: [ну]
  fillers: [пожалуйста]
  negation: [не]
  question_words: [какой, что]
  connectors: [и, потом]
  rooms: {kitchen: [кухн*], bedroom: [спальн*]}
  on_verb: [включи, зажги]
  off_verb: [выключи, погаси]
intents:
  - id: light.on
    tier: execute
    slots: {room: {type: rooms}}
    patterns: ["{on_verb} свет (на|в)? {room}?"]
    action: {executor: ha.light, args: {state: "on", room: "$room"}}
    labels: [LIGHT]
    examples: ["включи свет", {say: "включи свет на кухне", slots: {room: kitchen}}]
    negatives: ["не включай свет"]
  - id: light.off
    tier: execute
    slots: {room: {type: rooms}}
    patterns: ["{off_verb} свет (на|в)? {room}?"]
    labels: [LIGHT]
    examples: ["выключи свет в спальне"]
  - id: vol.set
    tier: execute
    slots: {value: {type: percent, range: [0, 100], required: true, ask: "Какую громкость?"}}
    patterns: ["громкост* (на)? {value}?"]
    labels: [VOLUME]
    examples: [{say: "громкость на пять", slots: {value: 5}}]
    negatives: ["громкость 150"]
  - id: timer.set
    tier: execute
    slots: {duration: {type: duration}}
    patterns: ["(поставь|заведи) таймер на {duration}"]
    reply: {kind: template, say: "Таймер на {duration_text}."}
    labels: [TIMER]
    examples: [{say: "поставь таймер на пять минут", slots: {duration: 300}}]
  - id: dialog.thanks
    tier: canned
    patterns: ["спасибо"]
    reply: {kind: canned, say: ["Пожалуйста."]}
  - id: wake.only
    tier: canned
    patterns: ["{wake}"]
    reply: {kind: canned, say: ["Слушаю."]}
  - id: calc.a
    tier: query
    patterns: ["сколько будет _"]
  - id: calc.b
    tier: query
    patterns: ["сколько _ два"]
  - id: rel.up
    tier: execute
    slots: {d: {type: delta}}
    patterns: ["прибавь громкость {d}"]
backtest:
  brain_labels: [CHITCHAT]
  unaddressed: {HALLUCINATION: drop}
  holdout_percent: 20
"""

# Two intents read «стоп и хватит» equally; each half is also read by one intent of its own.
TIE = """
version: 1
lexicon: {connectors: [и]}
intents:
  - {id: stop.a, tier: execute, patterns: ["стоп и хватит"], labels: [A]}
  - {id: stop.b, tier: execute, patterns: ["стоп и хватит"], labels: [B]}
  - {id: stop.one, tier: execute, patterns: ["стоп"]}
  - {id: enough, tier: execute, patterns: ["хватит"]}
"""

TEMPLATE = """
version: 1
intents:
  - id: volume.query
    tier: query
    patterns: ["какая громкость"]
    reply: {kind: template, say: "Громкость {level}."}
"""

FOLDING = """
version: 1
lexicon:
  names: [Алиса, Ёжик]
intents:
  - {id: again, tier: execute, patterns: ["ещё раз"]}
  - {id: name, tier: canned, patterns: ["{names}"], reply: {kind: canned, say: ["Да."]}}
"""


class GrammarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = loads(MINI)

    def test_pinned_examples_hold(self):
        self.assertEqual(self.g.self_check(), [])

    def test_slots_and_resolved_action(self):
        d = self.g.decide("Алиса, включи свет на кухне пожалуйста")
        self.assertEqual((d.intent, d.slots, d.confidence), ("light.on", {"room": "kitchen"}, 1.0))
        self.assertEqual(d.action, {"executor": "ha.light", "args": {"state": "on", "room": "kitchen"}})

    def test_inflected_room_is_matched_through_the_stem(self):
        self.assertEqual(self.g.decide("выключи свет в спальню").slots, {"room": "bedroom"})

    def test_acting_intents_are_guarded_against_negation_by_default(self):
        self.assertIsNone(self.g.decide("не включай свет"))
        self.assertIsNone(self.g.decide("не выключай свет в спальне"))

    def test_whole_utterance_or_abstain(self):
        self.assertIsNone(self.g.decide("включи свет и расскажи анекдот про кота"))
        self.assertIsNone(self.g.decide("что такое фотосинтез"))
        self.assertIsNone(self.g.decide(""))

    def test_slot_range_rejects_the_reading(self):
        self.assertEqual(self.g.decide("громкость пятьдесят процентов").slots, {"value": 50})
        self.assertIsNone(self.g.decide("громкость 150"))

    def test_missing_required_slot_asks(self):
        d = self.g.decide("громкость")
        self.assertEqual((d.tier, d.ask), ("clarify", "Какую громкость?"))
        self.assertEqual(self.g.say(d), "Какую громкость?")

    def test_duration_slot_and_template_reply(self):
        d = self.g.decide("поставь таймер на полтора часа")
        self.assertEqual(d.slots, {"duration": 5400})
        self.assertEqual(self.g.say(d), "Таймер на 1 час 30 минут.")

    def test_relative_delta_slot(self):
        self.assertEqual(self.g.decide("прибавь громкость на десять процентов").slots["d"], {"amount": 10, "unit": "процент"})
        self.assertEqual(self.g.decide("прибавь громкость на два").slots["d"], {"amount": 2, "unit": ""})

    def test_canned_and_wake_only(self):
        d = self.g.decide("Алиса")
        self.assertEqual((d.intent, self.g.say(d)), ("wake.only", "Слушаю."))
        self.assertEqual(self.g.say(self.g.decide("спасибо")), "Пожалуйста.")

    def test_two_equal_readings_abstain(self):
        self.assertIsNone(self.g.decide("сколько будет два"))                 # calc.a and calc.b read it equally well
        self.assertEqual({c.intent for c in self.g.candidates("сколько будет два")}, {"calc.a", "calc.b"})
        self.assertEqual(self.g.decide("сколько будет три").intent, "calc.a")

    def test_a_tied_utterance_is_not_split_into_a_compound(self):
        g = loads(TIE)
        self.assertEqual({c.intent for c in g.candidates("стоп и хватит")}, {"stop.a", "stop.b"})
        self.assertIsNone(g.decide("стоп и хватит"))
        self.assertEqual(g.decide("стоп").intent, "stop.one")

    def test_fuzzy_pass_degrades_acting_intents_to_confirm(self):
        g = loads(MINI.replace('patterns: ["{on_verb} свет (на|в)? {room}?"]', 'patterns: ["{on_verb} ~люстру (на|в)? {room}?"]'))
        d = g.decide("включи люстуру на кухне")
        self.assertEqual((d.intent, d.tier, d.fuzzy, d.confidence), ("light.on", "confirm", True, 0.8))
        self.assertEqual(g.decide("включи люстру").tier, "execute")

    def test_compound_command(self):
        d = self.g.decide("выключи свет в спальне и включи свет на кухне")
        self.assertEqual(d.intent, "compound")
        self.assertEqual([(p.intent, p.slots) for p in d.parts],
                         [("light.off", {"room": "bedroom"}), ("light.on", {"room": "kitchen"})])
        self.assertIsNone(self.g.decide("выключи свет и расскажи анекдот"))
        self.assertIsNone(self.g.decide("выключи свет и не включай свет"))

    def test_priority_beats_specificity(self):
        text = MINI.replace("  - id: dialog.thanks", """  - id: thanks.vip
    tier: canned
    priority: 5
    patterns: ["спасибо"]
    reply: {kind: canned, say: ["Всегда рада."]}
  - id: dialog.thanks""")
        self.assertEqual(loads(text).decide("спасибо").intent, "thanks.vip")

    def test_compound_tier_is_the_most_active_part(self):
        d = self.g.decide("спасибо и выключи свет")
        self.assertEqual((d.intent, d.tier), ("compound", "execute"))
        fuzzy = loads(MINI.replace('patterns: ["{on_verb} свет (на|в)? {room}?"]', 'patterns: ["{on_verb} ~люстру (на|в)? {room}?"]'))
        d = fuzzy.decide("выключи свет и включи люстуру")
        self.assertEqual((d.intent, d.tier, d.fuzzy), ("compound", "confirm", True))

    def test_question_mark_applies_to_every_clause(self):
        g = loads(MINI.replace("  - id: light.on\n    tier: execute\n", "  - id: light.on\n    tier: execute\n    guards: [not_question]\n"))
        self.assertEqual(g.decide("громкость на пять и включи свет").intent, "compound")
        self.assertIsNone(g.decide("громкость на пять и включи свет?"))

    def test_unresolved_reply_placeholder_is_not_spoken(self):
        g = loads(TEMPLATE)
        d = g.decide("какая громкость")
        self.assertIsNone(g.say(d))
        self.assertIsNone(render_reply(d))
        self.assertEqual(g.say(d, values={"level": 7}), "Громкость 7.")
        self.assertEqual(render_reply(d, values={"level": 3}), "Громкость 3.")
        self.assertEqual(self.g.say(self.g.decide("поставь таймер на пять минут")), "Таймер на 5 минут.")

    def test_pattern_and_lexicon_literals_are_folded_like_utterances(self):
        g = loads(FOLDING)
        self.assertEqual(g.decide("Ещё раз").intent, "again")
        self.assertEqual(g.decide("ещё раз").intent, "again")
        self.assertEqual(g.decide("алиса").intent, "name")
        self.assertEqual(g.decide("Ёжик").intent, "name")


EXTENSION_INTENTS = '''  - id: early.hello
    tier: canned
    phase: pre
    patterns: ["привет"]
    reply: {kind: canned, say: ["Здравствуй."]}
  - id: chatter.short
    tier: reprompt
    priority: -10
    max_tokens: 2
    guards: [not_question, "not_has:chat_markers"]
    patterns: ["..."]
    reply: {kind: canned, say: ["Не расслышал."]}
'''
MINI_EXT = MINI.replace("  rooms:", "  chat_markers: [расскажи, хочу]\n  rooms:", 1).replace("backtest:", EXTENSION_INTENTS + "backtest:", 1)


class EngineExtensionTests(unittest.TestCase):
    """Phases, priority bands, token caps, lexicon-set guards and the repaired-typo flag."""

    @classmethod
    def setUpClass(cls):
        cls.g = loads(MINI_EXT)

    def test_phase_selects_the_intents_that_may_answer_early(self):
        self.assertEqual(self.g.decide("привет", phase="pre").intent, "early.hello")
        self.assertIsNone(self.g.decide("включи свет", phase="pre"))
        self.assertEqual(self.g.decide("включи свет", phase="post").intent, "light.on")
        self.assertEqual(self.g.decide("привет", phase="post").intent, "chatter.short")   # the early intent is out of this phase

    def test_priority_bands_separate_real_intents_from_catch_alls(self):
        self.assertIsNone(self.g.decide("абракадабра ляля", min_priority=0))
        self.assertEqual(self.g.decide("абракадабра ляля", max_priority=-1).intent, "chatter.short")
        self.assertEqual(self.g.decide("включи свет", min_priority=0).intent, "light.on")

    def test_token_cap_and_set_guards_keep_the_catch_all_narrow(self):
        self.assertIsNone(self.g.decide("абракадабра ляля тополя"))               # three tokens: over max_tokens
        self.assertIsNone(self.g.decide("хочу ляля"))                             # a chat marker is in the utterance
        self.assertIsNone(self.g.decide("что ляля"))                              # a question
        self.assertEqual(self.g.decide("ляля").intent, "chatter.short")

    def test_unknown_set_guard_is_rejected(self):
        with self.assertRaises(GrammarError):
            loads(MINI_EXT.replace('"not_has:chat_markers"', '"not_has:nope"'))

    def test_typo_repair_makes_the_reading_fuzzy(self):
        g = loads(MINI_EXT.replace("  fillers: [пожалуйста]", "  fillers: [пожалуйста]\n  typo_vocabulary: [включи, выключи]"))
        exact = g.decide("включи свет")
        self.assertEqual((exact.tier, exact.fuzzy), ("execute", False))
        repaired = g.decide("включии свет")
        self.assertEqual((repaired.intent, repaired.tier, repaired.fuzzy, repaired.confidence), ("light.on", "confirm", True, 0.8))

    def test_core_text_gives_back_the_raw_words_without_wake_and_punctuation(self):
        got = self.g.normalise("Алиса, 12+12.")
        self.assertEqual(got.core_text(), "12+12")
        self.assertEqual(self.g.normalise("...").core_text(), "")

    def test_plural_forms(self):
        self.assertEqual([plural(n, "минута", "минуты", "минут") for n in (1, 2, 5, 11, 21, 22, 25)],
                         ["минута", "минуты", "минут", "минут", "минута", "минуты", "минут"])


class SchemaErrorTests(unittest.TestCase):
    def fails(self, text, needle):
        with self.assertRaises(GrammarError) as raised:
            loads(text)
        self.assertIn(needle, str(raised.exception))

    def test_rejects_malformed_grammars(self):
        self.fails("version: 2\nintents: []", "version")
        self.fails("version: 1\nintents: []", "no intents")
        self.fails("version: 1\nbogus: 1\nintents: []", "unknown top-level")
        self.fails("version: 1\nintents: [{id: a, tier: nope, patterns: [x]}]", "tier")
        self.fails("version: 1\nintents: [{id: a, tier: execute}]", "pattern")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: ['(x']}]", "missing ')'")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: ['{nope}']}]", "neither a lexicon set")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: [x], guards: [zzz]}]", "guard")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: [x], phase: sideways}]", "phase")
        self.fails("version: 1\nintents: [{id: a, tier: canned, patterns: [x]}]", "canned reply")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: [x]}, {id: a, tier: execute, patterns: [y]}]", "duplicate")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: [x], slots: {v: {type: nope}}}]", "unknown type")
        self.fails("{", "valid YAML")


class ShippedGrammarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grammar = load(SHIPPED)

    def test_pinned_examples_hold_and_nothing_conflicts(self):
        self.assertEqual(self.grammar.self_check(), [])
        self.assertEqual(cli._conflicts(self.grammar), [])

    def intent(self, text, **band):
        decision = self.grammar.decide(text, **band)
        return decision and decision.intent

    def test_hallucinated_captions_are_dropped_not_answered(self):
        for text in ("Продолжение следует...", "Субтитры сделал DimaTorzok", "Редактор субтитров А.Семкин", "Спасибо за просмотр!",
                     "Подписывайтесь на канал", "Thanks for watching"):
            with self.subTest(text=text):
                self.assertEqual(self.grammar.decide(text).tier, "drop")

    def test_negation_is_a_noop_never_the_opposite_action(self):
        for text in ("не включай свет", "не выключай музыку", "не делай громче", "не надо включать свет"):
            with self.subTest(text=text):
                self.assertEqual(self.intent(text), "negation.noop")
        self.assertEqual(self.intent("не надо"), "dialog.dismiss")                  # a plain «no thanks» still ends the dialog

    def test_spoken_numerals_in_every_case_reach_the_slots(self):
        self.assertEqual(self.grammar.decide("громкость на двадцать пять процентов").slots, {"percent": 25})
        self.assertIsNone(self.grammar.decide("громкость пятнадцать", min_priority=0))   # a level is 0..10, never a guessed percent
        self.assertEqual(self.grammar.decide("громкость десять").slots, {"level": 10})
        self.assertEqual(self.grammar.decide("поставь таймер на две минуты тридцать секунд").slots, {"duration": 150})
        self.assertEqual(self.grammar.decide("таймер на полтора часа").slots, {"duration": 5400})
        self.assertEqual(self.grammar.decide("убавь громкость на пять процентов").slots, {"percent": 5})
        self.assertEqual(self.grammar.decide("кондиционер в спальне на охлаждение двадцать два градуса").slots,
                         {"room": "bedroom", "mode": "cool", "temp": 22})

    def test_clock_alarms(self):
        for text, want in (("поставь будильник на семь утра", "07:00"), ("разбуди меня в половине седьмого", "06:30"),
                           ("заведи будильник на 7:30", "07:30"), ("будильник на без пятнадцати восемь", "07:45")):
            with self.subTest(text=text):
                self.assertEqual(self.grammar.decide(text).slots["time"]["text"], want)

    def test_compound_commands(self):
        decision = self.grammar.decide("включи свет и выключи чайник")
        self.assertEqual((decision.intent, [part.intent for part in decision.parts]), ("compound", ["light.on", "device.off"]))
        self.assertEqual([part.slots.get("device") for part in decision.parts], [None, "kettle"])
        self.assertIsNone(self.grammar.decide("включи свет и расскажи анекдот про кота и собаку"))

    def test_questions_the_brain_must_answer_stay_with_the_brain(self):
        for text in ("что такое фотосинтез", "кто лучше собаки или кошки", "расскажи мне про динозавров", "почему небо голубое",
                     "мне скучно", "как приготовить борщ"):
            with self.subTest(text=text):
                self.assertIsNone(self.grammar.decide(text))

    def test_the_real_intents_do_not_depend_on_the_catch_alls(self):
        self.assertIsNone(self.intent("голи со спортом", min_priority=0))
        self.assertEqual(self.intent("голи со спортом", max_priority=-1), "asr.garbage_short")


class ShippedBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load(SHIPPED)

    def args(self, text):
        decision = self.g.decide(text)
        self.assertIsNotNone(decision, text)
        return decision.action["args"]

    def assertNoRealReading(self, text):
        """No real intent (priority >= 0) reads it. Phase 2's catch-alls (priority < 0: the «Не расслышал» reprompt, the
        «Какую громкость?» question) may still answer a short unreadable phrase; none of them acts."""
        self.assertIsNone(self.g.decide(text, min_priority=0), text)
        decision = self.g.decide(text)
        self.assertTrue(decision is None or decision.action is None, text)

    def test_volume_keeps_its_unit(self):
        self.assertEqual(self.args("громкость 10%"), {"percent": 10})
        self.assertEqual(self.args("громкость на десять процентов"), {"percent": 10})
        self.assertEqual(self.args("громкость 50 процентов"), {"percent": 50})
        self.assertEqual(self.args("50% громкость"), {"percent": 50})
        self.assertEqual(self.args("громкость на максимум"), {"level": 10})
        self.assertEqual(self.args("громкость на пять"), {"level": 5})
        self.assertEqual(self.args("10 громкость"), {"level": 10})
        for text in ("громкость 10%", "громкость 7", "громкость на максимум", "сделай звук 3 процента", "громче", "тише"):
            args = self.g.decide(text).action["args"]
            self.assertFalse({"level", "percent"} <= set(args), text)

    def test_a_bare_number_off_the_speaker_scale_is_not_a_level(self):
        for text in ("громкость 50", "громкость 150%", "громкость -5", "громкость 5" + "0" * 400):
            with self.subTest(text=text[:20]):
                self.assertNoRealReading(text)

    def test_past_tense_and_questions_do_not_act(self):
        for text in ("выключил свет?", "включил свет?", "Включил свет.", "выключил свет", "Включить свет?", "Выключить свет?",
                     "включил чайник", "Включить чайник?", "остановил музыку", "поставил паузу"):
            self.assertNoRealReading(text)
        self.assertEqual(self.g.decide("выключи свет").intent, "light.off")
        self.assertEqual(self.g.decide("включи свет").intent, "light.on")
        self.assertNoRealReading("Продолжить воспроизведение?")
        self.assertEqual(self.g.decide("продолжи воспроизведение").intent, "media.resume")

    def test_fragments_do_not_act(self):
        for text in ("Убери.", "Полный.", "Полная", "Полно", "полная громкость"):
            self.assertNoRealReading(text)
        # Phase 2 answers a bare «Поставь.» on purpose with the clarifying «Что именно?» (asr.dangling_verb): it asks, never acts
        dangling = self.g.decide("Поставь.")
        self.assertEqual((dangling.intent, dangling.tier, dangling.action), ("asr.dangling_verb", "clarify", None))
        self.assertEqual(self.g.decide("поставь музыку").intent, "music.generic")
        self.assertEqual(self.g.decide("убери громкость").intent, "volume.down")
        self.assertEqual(self.g.decide("громкость на полную").intent, "volume.max")
        self.assertEqual(self.g.decide("сделай на полную").intent, "volume.max")

    def test_too_loud_goes_down(self):
        self.assertEqual(self.g.decide("громковато").action["args"], {"step": -1})
        self.assertEqual(self.g.decide("громче").action["args"], {"step": 1})

    def test_gate_block_limits_a_first_report(self):
        gate = self.g.gate
        self.assertEqual((gate["max_false_triggers"], gate["max_wrong_actions"], gate["max_p95_us"]), (2, 6, 25000))
        self.assertTrue(gate["min_coverage"])

    def test_relative_volume_takes_a_plain_level_or_a_percent_and_nothing_else(self):
        for text in ("прибавь два градуса", "сделай громче на два градуса", "убавь на три градуса", "убавь на пять единиц",
                     "прибавь на тридцать", "убавь громкость на пятьдесят", "прибавь громкость на сто"):
            with self.subTest(text=text):
                decision = self.g.decide(text, phase="pre")
                self.assertTrue(decision is None or decision.intent not in ("volume.delta_up", "volume.delta_down"), text)
        self.assertEqual(self.g.decide("прибавь два градуса").intent, "climate.warmer")      # degrees belong to the AC
        self.assertEqual(self.g.decide("убавь на три градуса").intent, "climate.cooler")
        self.assertEqual(self.args("прибавь громкость на десять"), {"direction": 1, "levels": 10, "percent": None})
        self.assertEqual(self.args("убавь звук на двадцать процентов"), {"direction": -1, "levels": None, "percent": 20})
        self.assertEqual(self.args("сделай громче на два"), {"direction": 1, "levels": 2, "percent": None})

    def test_refusals_leave_media_and_knowledge_requests_alone(self):
        for text in ("включи фильм Идиот", "поставь Сука любовь", "включи Тупой и ещё тупее", "включи матч Медведева",
                     "расскажи про трамплин", "прыжки с трамплина"):
            with self.subTest(text=text):
                self.assertIsNone(self.g.decide(text, phase="pre"), text)      # nothing answers before the quasar layer
        for text, intent in (("Алиса, путин пидор.", "refuse.insult"), ("ты тупая", "refuse.insult"),
                             ("Путин молодец", "refuse.politics"), ("Трамп молодец", "refuse.politics")):
            with self.subTest(text=text):
                self.assertEqual(self.g.decide(text, phase="pre").intent, intent)

    def test_hardware_commands_are_no_questions(self):
        for text in ("Поставить температуру 30?", "Сделать свет красным?", "Сделать теплее?", "Сделать холоднее?",
                     "Яркость пятьдесят?"):
            with self.subTest(text=text):
                self.assertNoRealReading(text)
        for text, intent in (("поставь температуру 30", "climate.set"), ("сделай свет красным", "light.color"),
                             ("сделай теплее", "climate.warmer"), ("яркость пятьдесят", "light.brightness")):
            self.assertEqual(self.g.decide(text).intent, intent, text)

    def test_chains_joined_by_any_connector_are_split_by_their_real_boundaries(self):
        clauses = ["включи свет", "выключи свет", "включи свет", "выключи свет", "включи свет"]
        for joiner in (" и ", " потом ", " а потом "):                 # «а потом» registers two overlapping cuts per boundary
            with self.subTest(joiner=joiner):
                decision = self.g.decide(joiner.join(clauses))
                self.assertEqual((decision.intent, len(decision.parts)), ("compound", 5))
        too_long = " а потом ".join(["включи свет"] * 9)
        decision = self.g.decide(too_long, min_priority=0)
        self.assertTrue(decision is None or decision.intent != "compound")

    def test_a_long_chain_of_clauses_is_answered_at_once(self):
        for connector_word in ("путин", "закрой"):
            text = " и ".join([connector_word] * 30) + " и что"
            for phase in (None, "pre"):
                started = time.perf_counter()
                self.g.decide(text, phase=phase)
                self.assertLess(time.perf_counter() - started, 0.5, (connector_word, phase))
        # below the clause cap the search itself must not repeat a failing start: 1.9^k paths otherwise
        with mock.patch.object(engine, "MAX_CLAUSES", 1000, create=True):
            text = " и ".join(["путин"] * 14) + " и что"
            started = time.perf_counter()
            self.g.decide(text)
            self.assertLess(time.perf_counter() - started, 1.0)


class BundleTests(unittest.TestCase):
    def test_committed_bundle_is_the_compiled_yaml(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["compile", "--check"]), 0)

    def test_bundle_loads_without_a_yaml_library(self):
        code = ("import sys; sys.modules['yaml'] = None; from intent_grammar import load; "
                f"g = load({str(SHIPPED.with_suffix('.json'))!r}); print(g.decide('громче').intent)")
        out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "volume.up"), out.stderr)
        self.assertEqual(load(SHIPPED.with_suffix(".json")).sha256, load(SHIPPED).sha256)


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.grammar = loads(MINI)
        real = [
            {"text": "Включи свет.", "truth": "LIGHT", "ts": 1},
            {"text": "Выключи свет в спальне", "truth": "LIGHT", "ts": 2},
            {"text": "Громкость на пять", "truth": "VOLUME", "ts": 3},
            {"text": "Кто лучше, собаки или кошки?", "truth": "CHITCHAT", "ts": 4},
            {"text": "Продолжение следует", "truth": "HALLUCINATION", "ts": 5},
            {"text": "Включи карусель", "truth": "LIGHT", "ts": 6},
        ]
        (self.dir / "real-voice-2026-11-labelled.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in real))
        synth = [{"text": "поставь таймер на пять минут", "expect": {"kind": "llm", "domain": "TIMER"}}]
        (self.dir / "intents-ru.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in synth))
        self.reports = self.dir.parent / (self.dir.name + "-reports")
        patcher = mock.patch.dict(os.environ, {backtest.ENV_REPORT_DIR: str(self.reports)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mini = self.dir / "mini.yaml"
        self.mini.write_text(MINI)

    def rows(self, grammar=None):
        return backtest.load_rows(self.dir, grammar or self.grammar)[0]

    def tearDown(self):
        self.tmp.cleanup()
        shutil.rmtree(self.reports, ignore_errors=True)

    def report(self, grammar=None, previous=None):
        grammar = grammar or self.grammar
        rows, manifest = backtest.load_rows(self.dir, grammar)
        return backtest.build_report(grammar, rows, manifest, previous=previous)

    def test_metrics(self):
        report = self.report()
        everything = report["splits"]["all/all"]
        self.assertEqual(everything["rows"], 7)
        self.assertEqual(everything["addressed"], 6)               # the hallucination is not addressed
        self.assertEqual(everything["answered_without_llm"], 4)    # включи свет, выключи, громкость, таймер
        self.assertEqual(everything["correct"], 4)
        self.assertEqual(everything["leaks_to_llm"], 1)            # the hallucination reached no drop rule
        self.assertEqual(everything["wrong_actions"], 0)
        self.assertEqual(report["per_truth"]["CHITCHAT"], {"n": 1, "answered": 0, "correct": 1})

    def test_split_is_stable_and_groups_repeated_phrases(self):
        first = backtest.split_of("Включи свет.", 20)
        self.assertEqual(first, backtest.split_of("включи  свет", 20))
        self.assertEqual(backtest.split_of("Ёлка, ВКЛЮЧИ!", 20), backtest.split_of("елка включи", 20))
        sides = {backtest.split_of(f"фраза номер {i}", 20) for i in range(200)}
        self.assertEqual(sides, {"dev", "holdout"})
        share = sum(backtest.split_of(f"фраза номер {i}", 20) == "holdout" for i in range(2000)) / 2000
        self.assertTrue(0.15 < share < 0.25)

    def test_split_ignores_the_grammar_vocabulary(self):
        extra = [{"text": f"плиз включи свет номер {i}", "truth": "LIGHT"} for i in range(80)]
        (self.dir / "more-labelled.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in extra))
        plain = {r.id: r.split for r in self.rows(loads(MINI))}
        wordy = {r.id: r.split for r in self.rows(loads(MINI.replace("fillers: [пожалуйста]", "fillers: [пожалуйста, плиз, номер]")))}
        self.assertEqual(plain, wordy)
        self.assertEqual({"dev", "holdout"}, set(plain.values()))
        self.assertEqual(self.report()["split"], {"version": backtest.SPLIT_VERSION, "holdout_percent": 20})

    def test_row_ids_do_not_depend_on_line_numbers(self):
        synth = self.dir / "intents-ru.jsonl"
        lines = synth.read_text().splitlines()
        before = {r.text: r.id for r in self.rows()}
        extra = json.dumps({"text": "включи свет на кухне", "expect": {"kind": "cmd", "domain": "LIGHT"}}, ensure_ascii=False)
        synth.write_text("\n".join([extra] + lines))
        after = {r.text: r.id for r in self.rows()}
        for text, row_id in before.items():
            self.assertEqual(after[text], row_id, text)

    def test_exact_duplicates_get_distinct_stable_ids(self):
        line = json.dumps({"text": "включи свет", "expect": {"kind": "cmd", "domain": "LIGHT"}}, ensure_ascii=False)
        synth = self.dir / "intents-ru.jsonl"
        synth.write_text("\n".join([line, line, line]))
        first = [r.id for r in self.rows() if r.source == "synthetic"]
        self.assertEqual(len(set(first)), 3)
        other = json.dumps({"text": "выключи свет", "expect": {"kind": "cmd", "domain": "LIGHT"}}, ensure_ascii=False)
        synth.write_text("\n".join([other, line, line, line]))
        second = [r.id for r in self.rows() if r.source == "synthetic" and r.text == "включи свет"]
        self.assertEqual(second, first)

    def test_held_out_texts_never_reach_the_report(self):
        report = self.report()
        holdout_ids = {i for i, row in report["rows"].items() if row["split"] == "holdout"}
        for miss in report["dev_misses"]:
            self.assertNotIn(miss["id"], holdout_ids)
        for miss in report["holdout_misses"]:
            self.assertNotIn("text", miss)
        blob = json.dumps(report, ensure_ascii=False)
        for row in backtest.load_rows(self.dir, self.grammar)[0]:
            if row.split == "holdout":
                self.assertNotIn(row.text, blob)

    def test_regression_is_found_and_fails_the_gate(self):
        before = self.report()
        worse = loads(MINI.replace("(на|в)? {room}?", "(на|в)? {room}").replace("громкост* (на)? {value}?", "громкост* (на)? {value}"))
        after = self.report(worse, previous=before)
        regressed = after["compare"]["regressed"]
        self.assertTrue(regressed)
        self.assertFalse(after["gate"]["ok"])
        self.assertTrue(any("got worse" in line for line in after["gate"]["failures"]))
        clean = self.report(previous=before)
        self.assertEqual(clean["compare"]["regressed"], [])
        self.assertTrue(clean["gate"]["ok"])

    def test_gate_rules_from_the_grammar(self):
        grammar = loads(MINI + "gate: {min_coverage: {all/all: 0.99}, max_p95_us: 0}\n")
        gate = self.report(grammar)["gate"]
        self.assertFalse(gate["ok"])
        self.assertTrue(any("coverage" in f for f in gate["failures"]))
        self.assertTrue(any("p95" in f for f in gate["failures"]))

    def test_cli_backtest_writes_a_report_and_latest(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["--grammar", str(self.mini), "backtest", "--corpus", str(self.dir), "--no-text"])
        self.assertEqual(rc, 0)
        self.assertIn("real/all", out.getvalue())
        saved = json.loads((self.reports / "latest.json").read_text())
        self.assertTrue(saved["dev_misses"])
        self.assertTrue(all("text" not in miss for miss in saved["dev_misses"]))
        self.assertEqual(len(list(self.reports.glob("backtest-*.json"))), 1)

    def test_cli_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["backtest", "--corpus", str(self.dir / "missing")]), 2)
            self.assertEqual(cli.main(["--grammar", str(self.dir / "nope.yaml"), "check"]), 2)
            self.assertEqual(cli.main(["check"]), 0)
            self.assertEqual(cli.main(["--grammar", str(self.mini), "backtest", "--corpus", str(self.dir), "--gate",
                                       "--init-baseline", "test start", "--no-latest", "--out", str(self.dir / "x.json")]), 0)
            gated = ["backtest", "--corpus", str(self.dir), "--gate", "--no-latest", "--out", str(self.dir / "x.json")]
            self.assertEqual(cli.main(gated), 1)       # the shipped grammar's coverage floors do not hold on this tiny corpus

    def backtest_cli(self, grammar_path, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["--grammar", str(grammar_path), "backtest", "--corpus", str(self.dir), "--no-text", *extra])

    def test_failed_gate_does_not_replace_the_baseline(self):
        worse = self.dir / "worse.yaml"
        worse.write_text(MINI.replace("(на|в)? {room}?", "(на|в)? {room}"))    # «включи свет» is no longer read
        reports = self.reports
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 1)             # no baseline yet: not a pass
        self.assertFalse((reports / "latest.json").exists())
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "first run of the combined route"), 0)
        baseline = (reports / "latest.json").read_text()
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "again"), 2)    # refused while a baseline exists
        reports_before = len(list(reports.glob("backtest-*.json")))
        self.assertEqual(self.backtest_cli(worse, "--gate"), 1)
        self.assertEqual((reports / "latest.json").read_text(), baseline)
        self.assertEqual(self.backtest_cli(worse, "--gate"), 1)                # an identical rerun fails again
        self.assertEqual((reports / "latest.json").read_text(), baseline)
        self.assertEqual(len(list(reports.glob("backtest-*.json"))) - reports_before, 2)   # failing runs are still archived
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 0)
        self.assertNotEqual((reports / "latest.json").read_text(), baseline)   # a passing run is promoted

    def corpus_rows(self):
        path = self.dir / "real-voice-2026-11-labelled.jsonl"
        return path, [json.loads(line) for line in path.read_text().splitlines()]

    @staticmethod
    def write_rows(path, rows):
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows))

    def test_the_owner_can_rebaseline_after_a_row_was_relabelled(self):
        latest = self.reports / "latest.json"
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "first run"), 0)
        path, rows = self.corpus_rows()
        rows[5]["truth"] = "CHITCHAT"      # «Включи карусель» had been labelled a light command by mistake
        self.write_rows(path, rows)
        baseline = latest.read_text()
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 1)         # relabelled: red until the owner decides
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 1)
        self.assertEqual(latest.read_text(), baseline)
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "row 6 was a chat question"), 0)
        block = json.loads(latest.read_text())["baseline"]
        self.assertEqual((block["init_reason"], block["rebaselined"]), ("row 6 was a chat question", {"lost_rows": 0, "relabelled": 1}))
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 0)         # the new baseline holds without the flag
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "again"), 2)   # nothing left to accept: refused

    def test_a_baseline_scored_by_an_older_rule_is_unusable_until_the_owner_restarts_it(self):
        latest = self.reports / "latest.json"
        older = self.report()
        older["schema"] = backtest.REPORT_SCHEMA - 1             # saved before rows were scored the way they are now
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(json.dumps(older))
        self.assertEqual(backtest.REPORT_SCHEMA, 3)
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 1)          # «another schema»: not silently compared
        self.assertEqual(json.loads(latest.read_text())["schema"], older["schema"])
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "scoring changed"), 0)   # nothing lost or relabelled
        block = json.loads(latest.read_text())
        self.assertEqual((block["schema"], block["baseline"]["init_reason"]), (backtest.REPORT_SCHEMA, "scoring changed"))
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 0)

    def test_a_rebaseline_accepts_lost_rows_but_never_a_regression(self):
        latest = self.reports / "latest.json"
        worse = self.dir / "worse.yaml"
        worse.write_text(MINI.replace("(на|в)? {room}?", "(на|в)? {room}"))    # «включи свет» is no longer read
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "first run"), 0)
        baseline = latest.read_text()
        path, rows = self.corpus_rows()
        self.write_rows(path, rows[:-1])                                       # the last row is removed from the corpus
        self.assertEqual(self.backtest_cli(self.mini, "--gate"), 1)
        self.assertEqual(self.backtest_cli(worse, "--gate", "--init-baseline", "dropped a row"), 1)   # a regression still fails
        self.assertEqual(latest.read_text(), baseline)
        self.assertEqual(self.backtest_cli(self.mini, "--gate", "--init-baseline", "dropped a row"), 0)
        block = json.loads(latest.read_text())["baseline"]
        self.assertEqual(block["rebaselined"], {"lost_rows": 1, "relabelled": 0})
        grammar = self.grammar
        rows_, manifest = backtest.load_rows(self.dir, grammar)
        no_baseline = backtest.build_report(grammar, rows_, manifest)
        self.assertFalse(backtest.accept_rebaseline(no_baseline, grammar, "nothing to compare"))   # only against a compatible baseline

    def test_the_replay_carries_how_the_quasar_layer_answered(self):
        replay = [{"text": "Громкость на пять", "handled": True, "via": "fast-clarify"},
                  {"text": "Включи свет.", "handled": True, "via": "media-key"},
                  {"text": "Выключи свет в спальне", "handled": True, "via": "fast-confirm"},
                  {"text": "Включи карусель", "handled": False, "via": None}]
        (self.dir / backtest.REPLAY_FILE).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in replay))
        by_text = {row.text: row for row in self.rows()}
        got = {text: (row.quasar, row.quasar_via, row.quasar_tier) for text, row in by_text.items()}
        self.assertEqual(got["Громкость на пять"], (True, "fast-clarify", "clarify"))
        self.assertEqual(got["Включи свет."], (True, "media-key", "execute"))
        self.assertEqual(got["Выключи свет в спальне"], (True, "fast-confirm", "confirm"))
        self.assertEqual(got["Включи карусель"], (False, None, "execute"))

    def test_a_failing_first_run_leaves_no_baseline(self):
        gated = self.dir / "gated.yaml"
        gated.write_text(MINI + "gate: {min_coverage: {all/all: 0.99}}\n")
        self.assertEqual(self.backtest_cli(gated, "--gate"), 1)
        self.assertFalse((self.reports / "latest.json").exists())
        self.assertEqual(len(list(self.reports.glob("backtest-*.json"))), 1)

    def test_out_on_a_fresh_corpus_still_writes_the_baseline(self):
        out = self.dir / "elsewhere" / "deep" / "result.json"
        self.assertEqual(self.backtest_cli(self.mini, "--out", str(out)), 0)
        self.assertTrue(out.exists())
        self.assertTrue((self.reports / "latest.json").exists())

    def test_baseline_is_replaced_atomically(self):
        latest = self.reports / "latest.json"
        latest.parent.mkdir()
        latest.write_text('{"old": true}')
        real_replace = os.replace
        seen = []

        def spy(src, dst):
            if Path(dst) == latest:
                seen.append(json.loads(latest.read_text()))      # a concurrent reader still sees a whole document
            real_replace(src, dst)

        with mock.patch.object(backtest.os, "replace", spy):
            backtest.save_report(self.report(), self.reports / "one.json", latest=latest)
        self.assertEqual(seen, [{"old": True}])
        self.assertEqual(json.loads(latest.read_text())["schema"], backtest.REPORT_SCHEMA)
        self.assertEqual(sorted(p.name for p in latest.parent.iterdir()), ["latest.json", "one.json"])

    def test_limits_fail_a_first_report_without_a_baseline(self):
        grammar = loads(MINI + "gate: {max_false_triggers: 0, max_wrong_actions: 0}\n")
        quiet = backtest.Row("a", "включи свет", "HALLUCINATION", "real", "dev")
        report = backtest.build_report(grammar, [quiet], [])
        self.assertFalse(report["gate"]["ok"])
        self.assertTrue(any("false triggers" in f for f in report["gate"]["failures"]))
        overreach = backtest.Row("b", "включи свет", "CHITCHAT", "real", "dev")
        report = backtest.build_report(grammar, [overreach], [])
        self.assertTrue(any("wrong actions" in f for f in report["gate"]["failures"]))
        relaxed = loads(MINI + "gate: {max_false_triggers: 1, max_wrong_actions: 1}\n")
        self.assertTrue(backtest.build_report(relaxed, [quiet], [])["gate"]["ok"])
        gated = self.dir / "gated.yaml"
        gated.write_text(MINI + "gate: {max_false_triggers: 0}\n")
        with (self.dir / "real-voice-2026-11-labelled.jsonl").open("a") as handle:
            handle.write("\n" + json.dumps({"text": "включи свет", "truth": "HALLUCINATION", "ts": 9}, ensure_ascii=False))
        self.assertEqual(self.backtest_cli(gated, "--gate"), 1)
        self.assertFalse((self.reports / "latest.json").exists())

    def test_coverage_floor_fails_when_its_split_is_missing_or_unmeasured(self):
        grammar = loads(MINI + "gate: {min_coverage: {real/holdout: 0.5}}\n")
        synthetic_only = [r for r in self.rows(grammar) if r.source == "synthetic"]
        report = backtest.build_report(grammar, synthetic_only, [])
        self.assertFalse(report["gate"]["ok"])
        self.assertTrue(any("real/holdout" in f for f in report["gate"]["failures"]), report["gate"])
        quiet = [backtest.Row("q", "Продолжение следует", "HALLUCINATION", "real", "holdout")]
        report = backtest.build_report(grammar, quiet, [])          # the split exists but nothing in it is addressed
        self.assertIsNone(report["splits"]["real/holdout"]["coverage"])
        self.assertTrue(any("real/holdout" in f for f in report["gate"]["failures"]), report["gate"])

    def hand(self, ident, text, truth="LIGHT", split="holdout"):
        return backtest.Row(ident, text, truth, "real", split)

    def test_adding_rows_cannot_fail_the_held_out_gate(self):
        handled = [self.hand("a", "включи свет"), self.hand("b", "выключи свет")]
        base = backtest.build_report(self.grammar, handled, [])
        grown = backtest.build_report(self.grammar, handled + [self.hand("c", "включи карусель")], [], previous=base)
        self.assertEqual(grown["compare"]["deltas"]["all/holdout"]["coverage"], 0.0)
        self.assertFalse(any("held-out" in f for f in grown["gate"]["failures"]), grown["gate"])

    def test_removing_rows_cannot_hide_a_drop(self):
        before = [self.hand("a", "включи свет"), self.hand("b", "громкость на пять", "VOLUME"), self.hand("c", "включи карусель")]
        base = backtest.build_report(self.grammar, before, [])
        report = backtest.build_report(self.grammar, before[1:], [], previous=base)       # the handled row «a» is deleted
        self.assertEqual(report["compare"]["lost_rows"], {"real": 1})
        self.assertTrue(any("gone from the corpus" in f for f in report["gate"]["failures"]), report["gate"])

    def test_a_row_that_gets_worse_fails_and_one_that_gets_better_does_not(self):
        worse = loads(MINI.replace("(на|в)? {room}?", "(на|в)? {room}"))    # «включи свет» is no longer read
        rows = [self.hand("a", "включи свет"), self.hand("b", "громкость на пять", "VOLUME")]
        base = backtest.build_report(self.grammar, rows, [])
        report = backtest.build_report(worse, rows, [], previous=base)
        self.assertEqual([r["id"] for r in report["compare"]["regressed"]], ["a"])
        self.assertTrue(any("got worse" in f for f in report["gate"]["failures"]))
        healed = backtest.build_report(self.grammar, rows, [], previous=report)
        self.assertEqual((healed["compare"]["regressed"], healed["compare"]["fixed"]), ([], 1))

    def test_turning_a_wrong_answer_into_an_abstention_is_not_a_regression_but_the_reverse_is(self):
        wrong = {"outcome": "wrong", "split": "holdout", "source": "real", "truth": "X", "intent": "a", "addressed": True, "completed": False}
        missed = dict(wrong, outcome="missed", intent=None)
        correct = dict(wrong, outcome="correct", completed=True)
        base = {"schema": backtest.REPORT_SCHEMA, "route": "combined", "split": {}, "splits": {}, "rows": {"r": wrong}}
        def compared(before_row, now_row):
            before = dict(base, rows={"r": before_row})
            now = {"schema": backtest.REPORT_SCHEMA, "route": "combined", "split": {}, "splits": {}, "rows": {"r": now_row}}
            return backtest.compare(now, before)["regressed"]
        self.assertEqual(compared(wrong, missed), [])
        self.assertEqual(len(compared(missed, wrong)), 1)
        self.assertEqual(len(compared(correct, missed)), 1)
        self.assertEqual(compared(missed, correct), [])

    def test_a_relabelled_row_and_an_incompatible_baseline_fail(self):
        rows = [self.hand("a", "включи свет")]
        base = backtest.build_report(self.grammar, rows, [], route="combined")
        relabelled = backtest.build_report(self.grammar, [self.hand("a", "включи свет", "VOLUME")], [], previous=base, route="combined")
        self.assertEqual(relabelled["compare"]["relabelled"], ["a"])
        self.assertTrue(any("changed their label" in f for f in relabelled["gate"]["failures"]))
        other_route = backtest.build_report(self.grammar, rows, [], previous=base, route="grammar")
        self.assertTrue(any("baseline unusable" in f for f in other_route["gate"]["failures"]))

    def test_gate_without_a_baseline_fails_when_one_is_required(self):
        rows = [self.hand("a", "включи свет")]
        self.assertTrue(backtest.build_report(self.grammar, rows, [])["gate"]["ok"])
        required = backtest.build_report(self.grammar, rows, [], require_baseline=True)
        self.assertTrue(any("no baseline" in f for f in required["gate"]["failures"]))

    def test_compound_wrong_actions_count_over_every_part(self):
        row = backtest.Row("t", "спасибо и включи свет", "CHITCHAT", "real", "dev")
        report = backtest.build_report(self.grammar, [row], [])
        self.assertEqual(report["splits"]["all/all"]["wrong_actions"], 1)

    EXPECT = """
version: 1
lexicon:
  on_verb: [включи]
  off_verb: [выключи]
intents:
  - id: volume.set
    tier: execute
    labels: [volume_partial]
    slots: {level: {type: number, range: [0, 10]}, percent: {type: number, range: [0, 100]}}
    patterns: ["громкость {level}", "громкость {percent} процент*"]
    action: {executor: speaker.volume, args: {level: "$level", percent: "$percent"}}
  - id: light.on
    tier: execute
    labels: [switch_partial]
    patterns: ["{on_verb} свет"]
    action: {executor: ha.light, args: {state: "on"}}
  - id: light.off
    tier: execute
    labels: [switch_partial]
    patterns: ["{off_verb} свет"]
    action: {executor: ha.light, args: {state: "off"}}
"""

    def outcomes(self, rows):
        return {o.row.id: o.outcome for o in backtest.evaluate(loads(self.EXPECT), rows)}

    def expected(self, ident, text, domain, **expect):
        return backtest.Row(ident, text, domain, "synthetic", "dev", expect={"kind": "cmd", "domain": domain, **expect})

    def test_corpus_expectations_are_compared(self):
        rows = [
            self.expected("ok", "громкость семь", "volume_partial", tier="execute", action="volume:7"),
            self.expected("level", "громкость семь", "volume_partial", tier="execute", action="volume:8"),
            self.expected("tier", "громкость семь", "volume_partial", tier="confirm", action="volume:7"),
            self.expected("on", "включи свет", "switch_partial", tier="execute", action="on:light.hall"),
            self.expected("state", "включи свет", "switch_partial", tier="execute", action="off:light.hall"),
            self.expected("brain", "включи свет", "switch_partial", tier="confirm", action="brain:включи свет"),
            self.expected("label-only", "включи свет", "switch_partial"),
        ]
        self.assertEqual(self.outcomes(rows), {"ok": "correct", "level": "wrong", "tier": "wrong", "on": "correct",
                                               "state": "wrong", "brain": "correct", "label-only": "correct"})
        report = backtest.build_report(loads(self.EXPECT), rows, [])
        self.assertEqual(report["splits"]["all/all"]["wrong_actions"], 3)

    def test_percent_is_not_a_level_in_the_corpus_comparison(self):
        percent = self.expected("p", "громкость 7 процентов", "volume_partial", tier="execute", action="volume:7")
        self.assertEqual(self.outcomes([percent]), {"p": "wrong"})


class HoldoutGateTests(unittest.TestCase):
    """The held-out split is tiny: row-by-row ratchet everywhere, and an absolute floor that switches on with n."""

    def split(self, addressed, answered, correct):
        return {"addressed": addressed, "answered_without_llm": answered, "correct": correct,
                "coverage": answered / addressed, "correct_rate": correct / addressed}

    def report(self, holdout):
        return {"splits": {"real/holdout": holdout}, "self_check_failures": [], "compare": None}

    RULE = {"splits": ["real/holdout"], "ci_min_addressed": 20, "floor_min_addressed": 60, "floor": {"coverage": 0.85, "correct": 0.80}}

    def test_today_s_n_is_not_gated_on_a_hand_picked_rate(self):
        self.assertEqual(backtest._holdout_failures(self.report(self.split(18, 15, 14)), self.RULE), [])

    def test_a_clearly_low_rate_fails_from_the_interval_test_before_the_point_floor(self):
        failures = backtest._holdout_failures(self.report(self.split(24, 15, 14)), self.RULE)
        self.assertEqual(len(failures), 2)
        self.assertIn("clearly below", failures[0])
        self.assertEqual(backtest._holdout_failures(self.report(self.split(24, 20, 19)), self.RULE), [])   # 0.83 coverage: inside the interval

    def test_the_point_floor_turns_on_with_enough_rows(self):
        failures = backtest._holdout_failures(self.report(self.split(60, 50, 46)), self.RULE)
        self.assertEqual(len(failures), 2)
        self.assertIn("< 0.85", failures[0])
        self.assertEqual(backtest._holdout_failures(self.report(self.split(60, 54, 50)), self.RULE), [])

    def test_a_missing_holdout_fails(self):
        self.assertTrue(backtest._holdout_failures({"splits": {}, "self_check_failures": [], "compare": None}, self.RULE))

    def test_the_legacy_floor_stays_visible(self):
        grammar = loads(MINI + "gate: {holdout: {legacy_floor: {split: real/holdout, coverage: 0.85}}}\n")
        status = backtest.legacy_floor_status({"splits": {"real/holdout": self.split(18, 15, 14)}}, grammar)
        self.assertEqual((status["met"], status["coverage"], status["addressed"]), (False, 15 / 18, 18))

    def test_the_goal_is_reported_with_an_interval_and_never_gated(self):
        grammar = loads(MINI + "gate: {goal: {split: real/all, coverage: 0.9, correct: 0.9}}\n")
        report = {"splits": {"real/all": self.split(79, 68, 65), "real/holdout": self.split(18, 15, 14)}}
        goal = backtest.goal_status(report, grammar)
        self.assertFalse(goal["all"]["coverage"]["met"])
        self.assertAlmostEqual(goal["all"]["coverage"]["gap"], 0.9 - 68 / 79, places=3)
        low, high = goal["holdout"]["coverage"]["ci95"]
        self.assertTrue(low < 15 / 18 < high and high - low > 0.3)


class EmergencySafetyTests(unittest.TestCase):
    """SAFETY: the emergency intent always wins and its must-pass set is part of the gate."""

    @classmethod
    def setUpClass(cls):
        cls.grammar = load(Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json")

    def test_every_safety_phrase_is_the_emergency_intent(self):
        self.assertEqual(backtest._safety_failures(self.grammar, self.grammar.gate["safety"]), [])
        self.assertGreater(len(self.grammar.gate["safety"]["phrases"]), 20)

    def test_it_wins_over_calls_compounds_and_every_other_band(self):
        for text in ("Мне плохо, вызови скорую.", "позвони в полицию", "включи свет и вызови скорую", "Алиса, мне плохо"):
            decision = self.grammar.decide(text)
            self.assertEqual((decision.intent, decision.tier), ("refuse.emergency", "canned"), text)

    def test_the_reply_gives_the_number_and_does_not_promise_a_call(self):
        reply = self.grammar.decide("вызови скорую").reply["say"][0]
        self.assertIn("не могу никому позвонить", reply)
        self.assertIn("сто двенадцать", reply)
        self.assertIn("сто девяносто четыре", reply)

    def test_ordinary_speech_is_left_alone(self):
        for text in ("мне плохо видно", "позвони маме", "помоги мне найти песню", "горит ли свет на кухне", "до скорой встречи"):
            decision = self.grammar.decide(text)
            self.assertTrue(decision is None or decision.intent != "refuse.emergency", text)

    def test_a_missing_safety_phrase_fails_the_gate(self):
        rule = {"intent": "refuse.emergency", "phrases": ["вызови скорую", "расскажи анекдот"]}
        failures = backtest._safety_failures(self.grammar, rule)
        self.assertEqual(len(failures), 1)
        self.assertIn("SAFETY", failures[0])


if __name__ == "__main__":
    unittest.main()
