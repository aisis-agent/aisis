"""Grammar contexts: intents that match only while a dialog context is active, per-turn values from the caller, and the
emergency intent that no context can suppress. Mini grammars here; the shipped bundle's context rows are in test_correction_grammar."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from intent_grammar import ActiveContext, GrammarError, cli, compile_bundle, load, loads

CTX = """
version: 1
locale: ru
lexicon:
  wake: [алиса]
  lead_in: [а, ну, так]
  negation: [не]
  connectors: [и]
  emergency: [=скорую, помогите]
contexts:
  - id: repair.confirm
    values: [alt, heard]
    sample: {alt: ["выключи свет", "выключи"], heard: ["включи свет", "включи"]}
  - id: repair.repeat
intents:
  - id: refuse.emergency
    tier: canned
    phase: pre
    priority: 1000
    patterns: ["{emergency} ...", "... {emergency}"]
    reply: {kind: canned, say: ["Звони сто двенадцать."]}
    examples: ["помогите", "вызови скорую"]
  - id: media.stop
    tier: execute
    patterns: ["(стоп|стой)"]
    action: {executor: media.key, args: {key: stop}}
    examples: [стоп]
  - id: light.on
    tier: execute
    patterns: ["включи свет"]
    action: {executor: ha.light, args: {state: "on"}}
    examples: ["включи свет"]
  - id: repair.accept
    tier: execute
    context: repair.confirm
    patterns: ["(да|ага|угу|конечно)", "{@alt}"]
    action: {executor: repair.accept}
    examples: ["да", "выключи свет", "выключи"]
    negatives: ["включи свет"]
  - id: repair.keep
    tier: execute
    context: repair.confirm
    patterns: ["{@heard}"]
    action: {executor: repair.keep}
    examples: ["включи свет", "включи"]
  - id: repair.deny
    tier: execute
    context: [repair.confirm, repair.repeat]
    allow_negation: true
    patterns: ["(нет|не то|не так)"]
    action: {executor: repair.deny}
    examples: ["нет", "не то"]
  - id: repair.cancel
    tier: execute
    context: [repair.confirm, repair.repeat]
    priority: 950
    allow_negation: true
    patterns: ["(отмена|забудь|стоп|не надо ничего)"]
    action: {executor: repair.cancel}
    examples: ["отмена", "стоп", "не надо ничего"]
"""


def confirm(**values):
    return ActiveContext("repair.confirm", {"alt": ("выключи свет", "выключи"), "heard": ("включи свет", "включи"), **values})


class ContextMatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = loads(CTX)

    def test_a_context_intent_never_matches_without_its_context(self):
        for text in ("да", "угу", "выключи свет", "нет", "отмена"):
            decision = self.g.decide(text)
            self.assertFalse(decision is not None and decision.intent.startswith("repair."), text)
        self.assertIsNone(self.g.decide("да"))

    def test_a_context_intent_matches_inside_its_context(self):
        self.assertEqual(self.g.decide("да", context=confirm()).intent, "repair.accept")
        self.assertEqual(self.g.decide("Алиса, ну да", context=confirm()).intent, "repair.accept")
        self.assertEqual(self.g.decide("нет", context=confirm()).intent, "repair.deny")
        self.assertEqual(self.g.decide("не то", context=confirm()).intent, "repair.deny")

    def test_a_context_id_alone_is_a_context_without_values(self):
        self.assertEqual(self.g.decide("да", context="repair.confirm").intent, "repair.accept")
        self.assertIsNone(self.g.decide("выключи свет", context="repair.confirm"))   # {@alt} has no phrases to match

    def test_an_intent_of_another_context_does_not_match(self):
        self.assertEqual(self.g.decide("нет", context=ActiveContext("repair.repeat")).intent, "repair.deny")
        self.assertIsNone(self.g.decide("да", context=ActiveContext("repair.repeat")))
        self.assertIsNone(self.g.decide("угу", context=ActiveContext("repair.repeat")))

    def test_a_context_intent_outranks_a_normal_intent_on_the_same_words(self):
        self.assertEqual(self.g.decide("стоп").intent, "media.stop")
        self.assertEqual(self.g.decide("стоп", context=confirm()).intent, "repair.cancel")

    def test_normal_intents_still_answer_inside_a_context(self):
        self.assertEqual(self.g.decide("включи свет", context=ActiveContext("repair.repeat")).intent, "light.on")

    def test_the_default_priority_of_a_context_intent_is_900_and_an_explicit_one_is_kept(self):
        priority = {i.id: i.priority for i in self.g.intents}
        self.assertEqual((priority["repair.accept"], priority["repair.cancel"]), (900, 950))

    def test_values_are_phrases_matched_by_stem_and_whole_utterance(self):
        self.assertEqual(self.g.decide("выключить свет", context=confirm()).intent, "repair.accept")
        self.assertEqual(self.g.decide("выключи", context=confirm()).intent, "repair.accept")
        self.assertEqual(self.g.decide("включи", context=confirm()).intent, "repair.keep")
        self.assertIsNone(self.g.decide("выключи свет в спальне", context=confirm()))
        self.assertIsNone(self.g.decide("выключи чайник", context=confirm()))

    def test_values_come_from_the_caller_per_turn(self):
        other = confirm(alt=("выключи телевизор",))
        self.assertEqual(self.g.decide("выключи телевизор", context=other).intent, "repair.accept")
        self.assertIsNone(self.g.decide("выключи свет", context=other))

    def test_a_context_never_forms_a_compound(self):
        self.assertIsNone(self.g.decide("да и стоп", context=confirm()))
        self.assertEqual(self.g.decide("включи свет и стоп", context=confirm()).intent, "compound")   # normal clauses only

    def test_emergency_outranks_every_context_intent_in_every_context(self):
        for context in (confirm(), ActiveContext("repair.repeat"), "repair.confirm"):
            for phrase in ("помогите", "вызови скорую", "скорую"):
                self.assertEqual(self.g.decide(phrase, context=context).intent, "refuse.emergency", (context, phrase))

    def test_a_context_intent_matching_the_emergency_words_still_loses(self):
        g = loads(CTX.replace('patterns: ["(отмена|забудь|стоп|не надо ничего)"]', 'patterns: ["(отмена|помогите)"]'))
        self.assertEqual(g.decide("помогите", context=confirm()).intent, "refuse.emergency")

    def test_context_argument_does_not_change_intents_without_a_context(self):
        for text in ("включи свет", "стой", "привет", "абракадабра", "включи свет и стой"):
            plain = self.g.decide(text)
            inside = self.g.decide(text, context=ActiveContext("repair.repeat"))
            self.assertEqual(plain and (plain.intent, plain.slots, plain.action), inside and (inside.intent, inside.slots, inside.action), text)

    def test_candidates_list_context_intents_only_in_their_context(self):
        self.assertEqual([c.intent for c in self.g.candidates("да")], [])
        self.assertEqual([c.intent for c in self.g.candidates("да", context=confirm())], ["repair.accept"])

    def test_pinned_examples_and_negatives_run_inside_their_contexts(self):
        self.assertEqual(self.g.self_check(), [])
        broken = loads(CTX.replace('examples: ["да", "выключи свет", "выключи"]', 'examples: ["да", "выключи свет", "включи свет"]'))
        self.assertTrue(any("repair.accept" in line and "включи свет" in line for line in broken.self_check()))

    def test_the_context_declaration_is_kept(self):
        self.assertEqual([c.id for c in self.g.contexts.values()], ["repair.confirm", "repair.repeat"])
        self.assertEqual(self.g.contexts["repair.confirm"].values, ("alt", "heard"))
        self.assertEqual(self.g.contexts["repair.confirm"].sample["alt"], ("выключи свет", "выключи"))


class ContextSchemaTests(unittest.TestCase):
    def fails(self, text, needle):
        with self.assertRaises(GrammarError) as raised:
            loads(text)
        self.assertIn(needle, str(raised.exception))

    def test_an_intent_in_an_undeclared_context_is_rejected(self):
        self.fails(CTX.replace("context: repair.confirm\n    patterns: [\"{@heard}\"]", "context: repair.nope\n    patterns: [\"{@heard}\"]"), "repair.nope")

    def test_a_context_intent_may_not_reach_the_emergency_priority(self):
        self.fails(CTX.replace("priority: 950", "priority: 1000"), "1000")
        self.fails(CTX.replace("priority: 950", "priority: 1200"), "1000")

    def test_a_value_reference_needs_the_value_in_every_context_of_the_intent(self):
        self.fails(CTX.replace('patterns: ["(нет|не то|не так)"]', 'patterns: ["(нет|не то|не так) {@alt}?"]'), "alt")

    def test_a_value_reference_outside_a_context_intent_is_rejected(self):
        self.fails(CTX.replace('patterns: ["включи свет"]', 'patterns: ["включи {@alt}"]'), "alt")

    def test_context_declaration_errors(self):
        self.fails(CTX.replace("  - id: repair.repeat\n", "  - id: repair.confirm\n"), "duplicate context")
        self.fails(CTX.replace("contexts:\n  - id: repair.confirm", "contexts:\n  - nope: 1\n  - id: repair.confirm"), "context")
        self.fails(CTX.replace("values: [alt, heard]", "values: [alt, 5]"), "values")
        self.fails(CTX.replace('sample: {alt: ["выключи свет", "выключи"], heard: ["включи свет", "включи"]}', 'sample: {zzz: ["x"]}'), "zzz")

    def test_an_example_of_a_context_intent_that_does_not_decide_to_it_fails_the_check(self):
        g = loads(CTX.replace('examples: ["отмена", "стоп", "не надо ничего"]', 'examples: ["отмена", "привет"]'))
        self.assertTrue(any("repair.cancel" in line and "привет" in line for line in g.self_check()))


class ContextCliAndBundleTests(unittest.TestCase):
    def run_cli(self, *argv):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "g.yaml"
            path.write_text(CTX, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--grammar", str(path), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_match_decides_inside_a_context_with_its_sample_or_given_values(self):
        code, out, _ = self.run_cli("match", "--context", "repair.confirm", "выключи свет")
        self.assertEqual((code, json.loads(out)["decision"]["intent"]), (0, "repair.accept"))
        code, out, _ = self.run_cli("match", "--context", "repair.confirm", "--value", "alt=выключи телевизор", "выключи свет")
        self.assertIsNone(json.loads(out)["decision"])
        code, out, _ = self.run_cli("match", "да")
        self.assertIsNone(json.loads(out)["decision"])

    def test_match_rejects_a_bad_value_and_an_unknown_context(self):
        self.assertEqual(self.run_cli("match", "--context", "repair.confirm", "--value", "oops", "да")[0], 2)
        self.assertEqual(self.run_cli("match", "--value", "alt=x", "да")[0], 2)
        self.assertEqual(self.run_cli("match", "--context", "nope", "да")[0], 2)

    def test_check_reads_the_context_intents_in_their_contexts(self):
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("2 contexts", out)

    def test_the_compiled_bundle_keeps_contexts(self):
        bundle = json.loads(compile_bundle(CTX))
        self.assertEqual([c["id"] for c in bundle["data"]["contexts"]], ["repair.confirm", "repair.repeat"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "g.json"
            path.write_text(compile_bundle(CTX), encoding="utf-8")
            self.assertEqual(load(path).decide("да", context=confirm()).intent, "repair.accept")


if __name__ == "__main__":
    unittest.main()
