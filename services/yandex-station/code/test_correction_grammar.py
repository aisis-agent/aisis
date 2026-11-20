"""The shipped grammar's repair contexts (every standard reply in every context, the emergency intent above all of them) and the
dialog rows of the back-test."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intent_grammar import ActiveContext, backtest, load

BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
YAML = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.yaml"

CONFIRM = ActiveContext("repair.confirm", {"alt": ("выключи свет", "выключи"), "heard": ("включи свет", "включи")})
CHOICE = ActiveContext("repair.choice", {"first": ("включи свет", "включи"), "second": ("выключи свет", "выключи")})
REPEAT = ActiveContext("repair.repeat")

YES = ["да", "ага", "угу", "конечно", "именно", "верно", "точно", "так", "давай", "правильно", "Да, конечно", "да да", "ну да", "Алиса, да", "все верно"]
NO = ["нет", "не то", "не так", "неверно", "неа", "не надо", "нет, не то", "совсем не то", "нет нет"]
AGAIN = ["повтори", "повтори ещё раз", "ещё раз", "что?", "как", "повторите, пожалуйста"]
CANCEL = ["отмена", "забудь", "не надо ничего", "оставь", "стоп", "отбой", "ничего не надо", "ладно, забудь"]


class ShippedContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load(BUNDLE)

    def intent(self, text, context):
        decision = self.g.decide(text, context=context)
        return decision.intent if decision else None

    def action(self, text, context):
        decision = self.g.decide(text, context=context)
        return (decision.action["executor"], decision.action.get("args") or {}) if decision and decision.action else None

    def test_the_bundle_is_the_yaml(self):
        from intent_grammar import cli
        self.assertEqual(cli.main(["compile", "--check"]), 0)
        self.assertEqual(self.g.self_check(), [])

    def test_confirm_replies(self):
        for text in YES:
            self.assertEqual(self.action(text, CONFIRM), ("repair.accept", {}), text)
        for text in NO:
            self.assertEqual(self.action(text, CONFIRM), ("repair.deny", {}), text)
        for text in AGAIN:
            self.assertEqual(self.action(text, CONFIRM), ("repair.again", {}), text)
        for text in CANCEL:
            self.assertEqual(self.action(text, CONFIRM), ("repair.cancel", {}), text)

    def test_confirm_takes_the_proposed_alternative_and_the_heard_reading(self):
        for text in ("выключи", "Выключи свет", "выключить свет", "нет, выключи свет", "я хотел выключить"):
            self.assertEqual(self.action(text, CONFIRM), ("repair.accept", {}), text)
        for text in ("включи", "включи свет", "включить свет", "нет, включи свет", "я так и сказал"):
            self.assertEqual(self.action(text, CONFIRM), ("repair.keep", {}), text)

    def test_confirm_does_not_take_what_is_not_a_reply(self):
        for text in ("да и громче", "да, выключи свет в спальне", "выключи чайник", "не выключай свет", "расскажи анекдот", "который час",
                     "что такое фотосинтез", "повтори за мной привет", "не надо включать свет"):
            self.assertFalse((self.intent(text, CONFIRM) or "").startswith("repair."), text)

    def test_choice_replies(self):
        for text in ("включи", "включить", "включи свет", "первое", "я хотел включить"):
            self.assertEqual(self.action(text, CHOICE), ("repair.choose", {"pick": "first"}), text)
        for text in ("выключи", "выключить", "выключи свет", "второе", "я хотел выключить"):
            self.assertEqual(self.action(text, CHOICE), ("repair.choose", {"pick": "second"}), text)
        for text in NO:
            self.assertEqual(self.action(text, CHOICE), ("repair.deny", {}), text)
        for text in AGAIN:
            self.assertEqual(self.action(text, CHOICE), ("repair.again", {}), text)
        for text in CANCEL:
            self.assertEqual(self.action(text, CHOICE), ("repair.cancel", {}), text)

    def test_choice_does_not_read_yes_or_a_third_thing(self):
        for text in ("да", "угу", "выключи чайник", "включи свет и стоп"):
            self.assertFalse((self.intent(text, CHOICE) or "").startswith("repair.choose"), text)
        self.assertFalse((self.intent("да", CHOICE) or "").startswith("repair."))

    def test_repeat_has_no_reply_but_cancel(self):
        for text in CANCEL:
            self.assertEqual(self.action(text, REPEAT), ("repair.cancel", {}), text)
        for text in YES + NO + AGAIN + ["выключи свет", "включи свет", "выключи", "включи"]:
            self.assertFalse((self.intent(text, REPEAT) or "").startswith("repair."), text)

    def test_a_bare_yes_or_no_outside_a_context_is_nothing(self):
        for text in YES + NO + AGAIN + CANCEL + ["выключи", "включи"]:
            self.assertFalse((self.intent(text, None) or "").startswith("repair."), text)

    def test_values_of_another_turn_are_not_this_turns(self):
        other = ActiveContext("repair.confirm", {"alt": ("потише",), "heard": ("погромче",)})
        self.assertEqual(self.intent("потише", other), "repair.accept")
        self.assertEqual(self.intent("погромче", other), "repair.keep")
        self.assertFalse((self.intent("выключи свет", other) or "").startswith("repair."))

    def test_the_emergency_intent_wins_in_every_context_for_every_safety_phrase(self):
        phrases = self.g.gate["safety"]["phrases"]
        self.assertGreater(len(phrases), 20)
        for context in (CONFIRM, CHOICE, REPEAT, "repair.confirm"):
            for phrase in phrases:
                decision = self.g.decide(phrase, context=context)
                self.assertEqual(decision.intent if decision else None, "refuse.emergency", (context, phrase))
                decision = self.g.decide(phrase, min_priority=0, context=context)
                self.assertEqual(decision.intent if decision else None, "refuse.emergency", (context, phrase))

    def test_a_proposed_alternative_that_is_an_emergency_word_still_loses_to_the_emergency_intent(self):
        hostile = ActiveContext("repair.confirm", {"alt": ("помогите", "вызови скорую"), "heard": ("скорую",)})
        for phrase in ("помогите", "вызови скорую", "скорую"):
            self.assertEqual(self.intent(phrase, hostile), "refuse.emergency", phrase)

    def test_every_context_intent_stays_below_the_emergency_priority(self):
        context_intents = [i for i in self.g.intents if i.contexts]
        self.assertGreaterEqual(len(context_intents), 7)
        self.assertTrue(all(900 <= i.priority < 1000 for i in context_intents))
        self.assertEqual(max(i.priority for i in self.g.intents), 1000)


class DialogBacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = load(BUNDLE)

    def test_the_committed_fixture_loads_and_every_row_is_decided_as_labelled(self):
        rows = backtest.load_dialog_rows(None)
        kinds = {row.kind for row in rows}
        self.assertEqual(kinds, {"synthetic", "authored"})
        self.assertGreater(len([r for r in rows if r.kind == "synthetic"]), 100)
        self.assertGreater(len([r for r in rows if r.kind == "authored"]), 20)
        self.assertEqual({row.context for row in rows}, {"repair.confirm", "repair.choice", "repair.repeat"})
        result = backtest.dialog_metrics(self.g, rows)
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["rows"], len(rows))
        self.assertEqual(sum(kind["n"] for kind in result["kinds"].values()), len(rows))
        self.assertEqual(result["safety"]["failures"], [])
        self.assertEqual(result["safety"]["checked"], len(self.g.gate["safety"]["phrases"]) * 3)

    def test_every_standard_intent_has_dialog_rows(self):
        rows = backtest.load_dialog_rows(None)
        covered = {row.intent for row in rows}
        for intent in ("repair.accept", "repair.keep", "repair.deny", "repair.again", "repair.cancel", "repair.choose_first",
                       "repair.choose_second", "refuse.emergency"):
            self.assertIn(intent, covered)
        self.assertTrue(any(row.outside for row in rows))

    def test_a_wrong_label_is_a_failure_and_fails_the_gate(self):
        rows = backtest.load_dialog_rows(None)
        wrong = [backtest.DialogRow(**{**row.__dict__, "intent": "repair.deny"}) if row.text == "да" and row.context == "repair.confirm" and row.values is None else row
                 for row in rows]
        result = backtest.dialog_metrics(self.g, wrong)
        self.assertEqual([f["text"] for f in result["failures"]], ["да"])
        report = backtest.build_report(self.g, [backtest.Row("x", "включи свет", "LIGHT_CMD", "real", "dev")], [], dialog_rows=wrong)
        self.assertFalse(report["gate"]["ok"])
        self.assertTrue(any("dialog" in line for line in report["gate"]["failures"]), report["gate"]["failures"])

    def test_the_gate_wants_the_dialog_block_when_the_grammar_asks_for_it(self):
        row = backtest.Row("x", "включи свет", "LIGHT_CMD", "real", "dev")
        report = backtest.build_report(self.g, [row], [])
        self.assertTrue(any("dialog" in line for line in report["gate"]["failures"]), report["gate"]["failures"])
        full = backtest.build_report(self.g, [row], [], dialog_rows=backtest.load_dialog_rows(None))
        self.assertFalse(any("dialog" in line for line in full["gate"]["failures"]), full["gate"]["failures"])
        self.assertIn("dialog", full)

    def test_an_empty_dialog_block_proves_nothing_and_fails_the_gate(self):
        row = backtest.Row("x", "включи свет", "LIGHT_CMD", "real", "dev")
        report = backtest.build_report(self.g, [row], [], dialog_rows=[])
        self.assertTrue(any("no dialog rows" in line for line in report["gate"]["failures"]), report["gate"]["failures"])

    def test_a_missing_fixture_is_an_error_not_an_empty_dataset(self):
        with mock.patch.object(backtest, "DIALOG_FIXTURE", Path("/nonexistent/dialog-ru.jsonl")):
            with self.assertRaises(FileNotFoundError):
                backtest.load_dialog_rows(None)
            self.assertEqual(backtest.load_dialog_rows(None, fixture=False), [])

    def test_a_context_context_emergency_miss_fails_the_gate(self):
        broken = copy.deepcopy(self.g)
        broken.gate = {**broken.gate, "safety": {**broken.gate["safety"], "phrases": ["вызови скорую", "расскажи анекдот"]}}
        result = backtest.dialog_metrics(broken, [])
        self.assertEqual(len(result["safety"]["failures"]), 3)   # the non-emergency phrase, once per context
        self.assertIn("SAFETY", result["safety"]["failures"][0])

    def test_corpus_rows_with_a_context_become_dialog_rows_and_leave_the_normal_corpus(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            records = [{"text": "Да", "context": "repair.confirm", "truth": "repair.accept"},
                       {"text": "включи свет", "truth": "LIGHT_CMD"},
                       {"text": "нет", "context": "repair.confirm", "values": {"alt": ["потише"]}, "truth": "repair.deny", "ts": 5}]
            (directory / "real-voice-x-labelled.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
            rows, manifest = backtest.load_rows(directory, self.g)
            self.assertEqual([r.text for r in rows], ["включи свет"])
            self.assertEqual(manifest[0]["rows"], 1)
            dialog = [r for r in backtest.load_dialog_rows(directory) if r.kind == "real"]
            self.assertEqual([(r.text, r.context, r.intent) for r in dialog],
                             [("Да", "repair.confirm", "repair.accept"), ("нет", "repair.confirm", "repair.deny")])
            self.assertEqual(dialog[1].values, {"alt": ("потише",)})
            self.assertEqual(backtest.dialog_metrics(self.g, dialog)["failures"], [])

    def test_a_row_of_an_undeclared_context_is_a_failure_not_a_skip(self):
        row = backtest.DialogRow("x", "да", "repair.nope", None, "synthetic", "repair.accept")
        result = backtest.dialog_metrics(self.g, [row])
        self.assertEqual([f["got"] for f in result["failures"]], ["unknown context repair.nope"])

    def test_the_fixture_is_read_only_for_a_grammar_that_asks_for_dialog_rows(self):
        self.assertEqual(backtest.load_dialog_rows(None, fixture=False), [])
        self.assertIn("dialog", self.g.gate)

    def test_the_summary_names_the_dialog_numbers(self):
        report = backtest.build_report(self.g, [backtest.Row("x", "включи свет", "LIGHT_CMD", "real", "dev")], [],
                                       dialog_rows=backtest.load_dialog_rows(None))
        text = "\n".join(backtest.summary_lines(report))
        self.assertIn("dialog rows", text)
        self.assertIn("synthetic", text)
        self.assertIn("authored", text)


if __name__ == "__main__":
    unittest.main()
