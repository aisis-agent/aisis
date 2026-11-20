"""prepare_overlay: exact single-block patch of the quasar ha-intents, refused on anything else; the hand-over predicate against the adapter grammar."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

CODE_DIR = Path(__file__).resolve().parents[2] / "code"
PHRASES = json.loads((Path(__file__).resolve().parent / "phrases.json").read_text(encoding="utf-8"))


def overlay_patterns() -> list[re.Pattern[str]]:
    """The two phrase regexps of the patched TypeScript, with their ${CONSTANT} parts filled in (JS and Python read these alike)."""
    parts = dict(re.findall(r"^const (AC_[A-Z_]+) = '(.*)'$", overlay.AFTER, re.M))
    templates = re.findall(r"^const AC_[A-Z_]+_PHRASE = new RegExp\(`(.*)`\)$", overlay.AFTER, re.M)
    return [re.compile(re.sub(r"\$\{(AC_[A-Z_]+)\}", lambda found: parts[found.group(1)], template)) for template in templates]


def normalized(text: str) -> str:
    """fuzzy-ru normalize + tokenize for these phrases: lower case, ё as е, no sentence punctuation."""
    return " ".join(re.sub(r"[^a-zа-я0-9\s]", " ", text.lower().replace("ё", "е")).split())

def overlay_word_set(name: str) -> set[str]:
    body = re.search(rf"^const {name} = new Set\(\[(.*?)\]\)", overlay.AFTER, re.M | re.S).group(1)
    return set(re.findall(r"'([^']+)'", body))


def overlay_alternatives(name: str) -> list[str]:
    return re.search(rf"^const {name} = '\(\?:(.*)\)'$", overlay.AFTER, re.M).group(1).split("|")


def overlay_owns(phrase: str, patterns: list[re.Pattern[str]]) -> bool:
    """adapterOwnsAcPhrase of the patched TypeScript: fillers anywhere and edge words dropped, then a whole-phrase match."""
    fillers, edge = overlay_word_set("AC_FILLERS"), overlay_word_set("AC_EDGE_WORDS")
    words = [word for word in normalized(phrase).split() if word not in fillers]
    while words and words[0] in edge:
        words.pop(0)
    while words and words[-1] in edge:
        words.pop()
    return any(pattern.match(" ".join(words)) for pattern in patterns)

BASELINE = ("export function splitSentences() {}\n\n" + overlay.BEFORE +
            "  if (sentences.length > 1) {\n    return decideSentence(rawText)\n  }\n  return decideSentence(rawText)\n}\n")


class ApplyPatchTests(unittest.TestCase):
    def test_only_a_single_sentence_turn_is_checked_before_the_sentence_loop(self):
        patched = overlay.apply_patch(BASELINE)
        guard = "if (sentences.length <= 1) {"
        self.assertIn(guard, patched)
        self.assertLess(patched.index(guard), patched.index("if (sentences.length > 1)"))
        self.assertIn("return { tier: 'llm' }", patched)

    def test_only_turning_on_or_setting_a_climate_entity_that_the_adapter_parses_whole_is_handed_over(self):
        after = overlay.AFTER
        self.assertIn("(local.tier === 'execute' || local.tier === 'confirm')", after)  # not clarify, reprompt, query, answer
        self.assertIn("adapterOwnsAcPhrase(tokenize(normalize(rawText)))", after)  # the whole phrase has a shape of the adapter's climate.set rule
        self.assertIn("local.resolved.device.kind !== 'off'", after)  # the adapter has no AC-off rule
        self.assertIn("targets.some(entityId => entityId.startsWith('climate.'))", after)

    def test_the_patch_changes_nothing_else(self):
        patched = overlay.apply_patch(BASELINE)
        self.assertEqual(patched.replace(overlay.AFTER, overlay.BEFORE), BASELINE)

    def test_a_baseline_without_the_block_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patch("export function decideFastIntent() {}\n")

    def test_a_baseline_with_the_block_twice_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patch(BASELINE + overlay.BEFORE)

    def test_wrong_baseline_hash_is_refused_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "ha-intents.ts"
            baseline.write_text(BASELINE, encoding="utf-8")
            out = Path(tmp) / "out"
            with self.assertRaises(overlay.OverlayError):
                overlay.prepare(baseline, out)
            self.assertFalse(out.exists())

    def test_unchecked_prepare_writes_the_patched_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            baseline = Path(tmp) / "ha-intents.ts"
            baseline.write_text(BASELINE, encoding="utf-8")
            target = overlay.prepare(baseline, Path(tmp) / "out", check_hashes=False)
            self.assertEqual(target.read_text(encoding="utf-8"), overlay.apply_patch(BASELINE))


class HandOverMatchesAdapterTests(unittest.TestCase):
    """What the patched layer hands on must be what the adapter's grammar executes; the rest must stay with quasar."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(CODE_DIR))
        try:
            import intent_grammar
        finally:
            sys.path.remove(str(CODE_DIR))
        cls.grammar = intent_grammar.load(CODE_DIR / "intent_grammar" / "grammar" / "ru.json")
        cls.patterns = overlay_patterns()

    def test_both_phrase_shapes_are_found_in_the_patch(self):
        self.assertEqual(len(self.patterns), 2)

    def test_every_handed_over_phrase_matches_a_shape_and_is_executed_by_the_adapter_grammar(self):
        for phrase in PHRASES["handed_over"]:
            with self.subTest(phrase=phrase):
                self.assertTrue(overlay_owns(phrase, self.patterns), "the patched layer would keep it")
                decision = self.grammar.decide(phrase)
                self.assertIsNotNone(decision)
                self.assertEqual((decision.intent, decision.tier), ("climate.set", "execute"))

    def test_no_kept_phrase_matches_a_shape_and_the_adapter_grammar_does_not_execute_it_as_a_climate_command(self):
        for phrase in PHRASES["kept_by_quasar"]:
            with self.subTest(phrase=phrase):
                self.assertFalse(overlay_owns(phrase, self.patterns), "the patched layer would hand it on")
                decision = self.grammar.decide(phrase)
                self.assertFalse(decision is not None and decision.intent.startswith("climate."), decision)

    def test_the_words_the_patch_knows_are_the_words_of_the_grammar_lexicon(self):
        lexicon = json.loads((CODE_DIR / "intent_grammar" / "grammar" / "ru.json").read_text(encoding="utf-8"))["data"]["lexicon"]
        self.assertEqual(overlay_word_set("AC_FILLERS"), set(lexicon["fillers"]))
        self.assertEqual(overlay_word_set("AC_EDGE_WORDS"), set(lexicon["lead_in"]) | set(lexicon["wake"]))
        self.assertEqual(set(overlay_alternatives("AC_ON_VERB")), set(lexicon["on_verb"]))
        self.assertEqual(set(overlay_alternatives("AC_SET_VERB")), set(lexicon["set_verb"]))


if __name__ == "__main__":
    unittest.main()
