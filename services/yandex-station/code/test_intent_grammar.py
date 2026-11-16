"""Tests of the intent grammar engine: normaliser, numerals, durations/clock, patterns, decisions, back-test."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from intent_grammar import GrammarError, backtest, cli, loads
from intent_grammar.grammar import load
from intent_grammar.normalize import Canon, edit_distance_le1, normalise, phonetic_key, repair_typo, stem, tokenize
from intent_grammar.numerals import parse_number, parse_ordinal
from intent_grammar.timewords import duration_ru, parse_clock, parse_duration

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
    patterns: ["сколько _ будет"]
  - id: rel.up
    tier: execute
    slots: {d: {type: delta}}
    patterns: ["прибавь громкость {d}"]
backtest:
  brain_labels: [CHITCHAT]
  unaddressed: {HALLUCINATION: drop}
  holdout_percent: 20
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
        self.assertIsNone(self.g.decide("сколько будет _"[:-2] + "два"))   # calc.a and calc.b both read it
        self.assertEqual({c.intent for c in self.g.candidates("сколько будет два")}, {"calc.a"})

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
        self.fails("version: 1\nintents: [{id: a, tier: canned, patterns: [x]}]", "canned reply")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: [x]}, {id: a, tier: execute, patterns: [y]}]", "duplicate")
        self.fails("version: 1\nintents: [{id: a, tier: execute, patterns: [x], slots: {v: {type: nope}}}]", "unknown type")
        self.fails("{", "valid YAML")


class ShippedGrammarTests(unittest.TestCase):
    def test_pinned_examples_hold_and_nothing_conflicts(self):
        grammar = load(SHIPPED)
        self.assertEqual(grammar.self_check(), [])
        self.assertEqual(cli._conflicts(grammar), [])


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

    def tearDown(self):
        self.tmp.cleanup()

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
        first = backtest.split_of("Включи свет.", 20, self.grammar)
        self.assertEqual(first, backtest.split_of("включи  свет", 20, self.grammar))
        sides = {backtest.split_of(f"фраза номер {i}", 20, self.grammar) for i in range(200)}
        self.assertEqual(sides, {"dev", "holdout"})
        share = sum(backtest.split_of(f"фраза номер {i}", 20, self.grammar) == "holdout" for i in range(2000)) / 2000
        self.assertTrue(0.15 < share < 0.25)

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
        self.assertTrue(any("regressed" in line for line in after["gate"]["failures"]))
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
            rc = cli.main(["--grammar", str(SHIPPED), "backtest", "--corpus", str(self.dir), "--no-text"])
        self.assertEqual(rc, 0)
        self.assertIn("real/all", out.getvalue())
        saved = json.loads((self.dir / "reports" / "latest.json").read_text())
        self.assertTrue(saved["dev_misses"])
        self.assertTrue(all("text" not in miss for miss in saved["dev_misses"]))
        self.assertEqual(len(list((self.dir / "reports").glob("backtest-*.json"))), 1)

    def test_cli_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["backtest", "--corpus", str(self.dir / "missing")]), 2)
            self.assertEqual(cli.main(["--grammar", str(self.dir / "nope.yaml"), "check"]), 2)
            self.assertEqual(cli.main(["check"]), 0)
            self.assertEqual(cli.main(["backtest", "--corpus", str(self.dir), "--gate", "--no-latest",
                                       "--out", str(self.dir / "x.json")]), 0)


if __name__ == "__main__":
    unittest.main()
