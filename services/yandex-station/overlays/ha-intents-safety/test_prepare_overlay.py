"""prepare_overlay: exact single-block patches of the quasar fast-intent layer, refused on anything else."""
import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

TABLES = {
    "backend/ha-intents.ts": overlay.HA_INTENTS_PATCHES,
    "backend/turn-memory.ts": overlay.TURN_MEMORY_PATCHES,
    "backend/asr-correct.ts": overlay.ASR_CORRECT_PATCHES,
    "routers/uniproxy/input-handler.ts": overlay.INPUT_HANDLER_PATCHES,
}
IMAGE_END_HANDLER, END_HANDLER_AFTER = overlay.HA_INTENTS_ONE_OF[0][1][0]
CHAINED_END_HANDLER, CHAINED_END_HANDLER_AFTER = overlay.HA_INTENTS_ONE_OF[0][1][1]


def baseline(relative: str, end_handler: str = IMAGE_END_HANDLER) -> str:
    """A file made of the blocks the patch replaces, one after another: what the real file looks like to the patcher."""
    text = "\n".join(before for _, before, _ in TABLES[relative])
    return text + "\n" + end_handler if relative == "backend/ha-intents.ts" else text


BASELINE_DIR = os.environ.get("AISIS_QUASAR_BASELINE_DIR")


class ApplyPatchesTests(unittest.TestCase):
    def apply(self, relative: str, source: str) -> str:
        return overlay.apply_patches(source, TABLES[relative], overlay.HA_INTENTS_ONE_OF if relative == "backend/ha-intents.ts" else ())

    def test_every_block_is_replaced(self):
        for relative, patches in TABLES.items():
            patched = self.apply(relative, baseline(relative))
            for what, before, after in patches:
                self.assertIn(after, patched, what)
            self.assertNotEqual(patched, baseline(relative))

    def test_a_file_without_a_block_is_refused(self):
        for relative, patches in TABLES.items():
            for what, before, _ in patches:
                with self.subTest(file=relative, block=what), self.assertRaises(overlay.OverlayError):
                    self.apply(relative, baseline(relative).replace(before, "", 1))

    def test_a_file_with_a_block_twice_is_refused(self):
        for relative, patches in TABLES.items():
            for what, before, _ in patches:
                with self.subTest(file=relative, block=what), self.assertRaises(overlay.OverlayError):
                    self.apply(relative, baseline(relative) + "\n" + before)

    def test_an_already_patched_file_is_refused(self):
        for relative in TABLES:
            with self.subTest(file=relative), self.assertRaises(overlay.OverlayError):
                self.apply(relative, self.apply(relative, baseline(relative)))

    def test_overlapping_blocks_are_refused(self):
        patches = (("first", "alpha\nbeta\n", "x\n"), ("second", "beta\ngamma\n", "y\n"))
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches("alpha\nbeta\ngamma\n", patches)

    def test_anchors_are_looked_up_in_the_input_not_in_a_half_patched_copy(self):
        # the first block's result contains the second block's anchor: that must not make the second one ambiguous
        patches = (("first", "one\n", "two\n"), ("second", "two\n", "three\n"))
        self.assertEqual(overlay.apply_patches("one\ntwo\n", patches), "two\nthree\n")


class TerminatorHandlerTests(unittest.TestCase):
    """The handler that clears the open question on «ничего», «хватит» sits one block deeper after the correction-hook overlay."""

    def apply(self, source: str) -> str:
        return overlay.apply_patches(source, overlay.HA_INTENTS_PATCHES, overlay.HA_INTENTS_ONE_OF)

    def test_the_image_shape_is_patched(self):
        patched = self.apply(baseline("backend/ha-intents.ts"))
        self.assertIn(END_HANDLER_AFTER, patched)
        self.assertIsNone(re.search(r"\bpendingClarification\b", patched))

    def test_the_correction_hook_shape_is_patched(self):
        patched = self.apply(baseline("backend/ha-intents.ts", CHAINED_END_HANDLER))
        self.assertIn(CHAINED_END_HANDLER_AFTER, patched)
        self.assertIsNone(re.search(r"\bpendingClarification\b", patched))

    def test_both_shapes_or_none_are_refused(self):
        with self.assertRaises(overlay.OverlayError):
            self.apply(baseline("backend/ha-intents.ts") + "\n" + CHAINED_END_HANDLER)
        with self.assertRaises(overlay.OverlayError):
            self.apply(baseline("backend/ha-intents.ts", "// the handler is gone\n"))

    def test_the_two_shapes_differ_only_by_indentation(self):
        deeper = "".join("  " + line for line in IMAGE_END_HANDLER.splitlines(keepends=True))
        self.assertEqual(deeper, CHAINED_END_HANDLER)
        self.assertEqual("".join("  " + line for line in END_HANDLER_AFTER.splitlines(keepends=True)), CHAINED_END_HANDLER_AFTER)
        self.assertIn("setPending(null)", END_HANDLER_AFTER)


class WhatThePatchesDoTests(unittest.TestCase):
    def patched(self, relative: str) -> str:
        return overlay.apply_patches(baseline(relative), TABLES[relative],
                                     overlay.HA_INTENTS_ONE_OF if relative == "backend/ha-intents.ts" else ())

    def test_no_process_global_question_or_reprompt_state_is_left(self):
        patched = self.patched("backend/ha-intents.ts")
        self.assertIsNone(re.search(r"\bpendingClarification\b", patched))
        self.assertNotIn("lastRepromptAt", patched)

    def test_a_negated_sentence_is_no_match_for_whatever_acts(self):
        patched = self.patched("backend/ha-intents.ts")
        self.assertIn("if (hasNegation(words) && actsOnHome(decision)) return { tier: 'llm' }", patched)
        self.assertIn("hasNegation(tokenize(normalize(text)))", patched)  # a negated phrase is not offered to the corrector

    def test_a_yes_word_beside_a_negation_is_not_a_yes(self):
        patched = self.patched("backend/ha-intents.ts")
        self.assertIn("const refused = hasNegation(tokens) || tokens.some(token => NO_WORDS.includes(token))", patched)
        self.assertIn("if (!refused && tokens.some(token => YES_WORDS.includes(token))) return 'yes'", patched)

    def test_a_refusal_beats_the_slot_fill_and_does_not_replay_the_phrase(self):
        patched = self.patched("backend/ha-intents.ts")
        self.assertIn("const filled = answer === 'no' || hasNegation(tokens) ? null : pending.fill?.(", patched)
        self.assertIn("splitSentences(prior.phrase).find(sentence => !hasNegation(tokenize(sentence)))", patched)  # a follow-up repeats the sentence that ran
        self.assertIn("return pending.declineToBrain && !hasNegation(tokens)", patched)

    def test_a_dismissed_or_refused_question_is_no_corrector_context(self):
        patched = self.patched("backend/ha-intents.ts")
        self.assertEqual(patched.count("forgetAsked(currentStation())"), 4)  # terminator, dialog_end route, two refusals
        self.assertIn("forgetAsked", self.patched("backend/asr-correct.ts"))

    def test_the_turn_memory_is_keyed_by_station_and_bounded(self):
        patched = self.patched("backend/turn-memory.ts")
        self.assertIn("export const MAX_STATIONS = 32", patched)
        self.assertIn("export function stationKey", patched)
        self.assertIn("export function rememberFor", patched)
        self.assertNotIn("const turns: Turn[]", patched)

    def test_the_brain_turn_carries_the_station_of_the_connection(self):
        patched = self.patched("routers/uniproxy/input-handler.ts")
        self.assertIn("station: stationKey(input.metadata.internal.deviceId)", patched)
        self.assertIn("station: this.brainTurn.station", patched)

    def test_the_corrector_context_is_asked_for_by_station(self):
        patched = self.patched("backend/asr-correct.ts")
        self.assertIn("recentTurns(station)", patched)
        self.assertIn("requestCorrection (text: string, station: string", patched)

    def test_the_persisted_ambiguity_memory_is_untouched(self):
        # phrases are what the owner confirmed once, not stations: the file and its loader stay global and in their old format
        for relative in TABLES:
            for _, before, after in TABLES[relative]:
                self.assertNotIn("AMBIGUITY_FILE", before + after)
                self.assertNotIn("ambiguityMemory", before + after)
                self.assertNotIn("rememberAmbiguity", before + after)


class NegationFileTests(unittest.TestCase):
    def test_the_new_module_has_its_pinned_hash(self):
        self.assertEqual(hashlib.sha256(overlay.NEGATION_TS.encode("utf-8")).hexdigest(), overlay.NEGATION_SHA256)

    def test_it_exports_what_the_patched_layer_imports(self):
        for name in ("hasNegation", "isNegatedAt"):
            self.assertIn(f"export function {name} (", overlay.NEGATION_TS)
        self.assertIn("import { hasNegation, isNegatedAt } from './negation'", "".join(after for _, _, after in overlay.HA_INTENTS_PATCHES))


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "src"
        for relative in TABLES:
            path = self.src / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(baseline(relative), encoding="utf-8")

    def test_writes_only_the_files_it_changes_where_deploy_overlay_expects_them(self):
        out = self.root / "overlay"
        hashes = overlay.prepare(self.src, out)
        self.assertEqual(sorted(hashes), sorted([*TABLES, overlay.NEGATION_PATH]))
        written = sorted(str(path.relative_to(out)) for path in out.rglob("*") if path.is_file())
        self.assertEqual(written, sorted([*TABLES, overlay.NEGATION_PATH]))
        self.assertEqual((out / overlay.NEGATION_PATH).read_text(encoding="utf-8"), overlay.NEGATION_TS)
        for relative, digest in hashes.items():
            self.assertEqual(hashlib.sha256((out / relative).read_bytes()).hexdigest(), digest)

    def test_another_overlays_output_is_taken_in_place_of_the_image_file(self):
        other = self.root / "ha-intents.ts"
        other.write_text(baseline("backend/ha-intents.ts", CHAINED_END_HANDLER), encoding="utf-8")
        out = self.root / "overlay"
        overlay.prepare(self.src, out, {"backend/ha-intents.ts": other})
        self.assertIn("setPending(null)", (out / "backend/ha-intents.ts").read_text(encoding="utf-8"))

    def test_pristine_only_refuses_a_file_that_is_not_the_images(self):
        out = self.root / "overlay"
        with self.assertRaisesRegex(overlay.OverlayError, "is not the image's"):
            overlay.prepare(self.src, out, pristine_only=True)
        self.assertFalse(out.exists())

    def test_a_file_that_does_not_patch_writes_nothing(self):
        (self.src / "backend/turn-memory.ts").write_text("export {}\n", encoding="utf-8")
        out = self.root / "overlay"
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.src, out)
        self.assertFalse(out.exists())

    def test_an_existing_output_directory_is_refused(self):
        out = self.root / "overlay"
        out.mkdir()
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.src, out)

    def test_an_image_that_already_has_the_module_is_refused(self):
        (self.src / overlay.NEGATION_PATH).write_text("export {}\n", encoding="utf-8")
        with self.assertRaisesRegex(overlay.OverlayError, "obsolete"):
            overlay.prepare(self.src, self.root / "overlay")

    def test_cli_exit_codes(self):
        out = self.root / "overlay"
        self.assertEqual(overlay.main(["--src", str(self.src), "--output-dir", str(out)]), 0)
        self.assertEqual(overlay.main(["--src", str(self.src), "--output-dir", str(out)]), 1)  # exists now
        self.assertEqual(overlay.main(["--src", str(self.root / "missing"), "--output-dir", str(self.root / "other")]), 1)
        self.assertEqual(overlay.main(["--src", str(self.src), "--output-dir", str(self.root / "pristine"), "--pristine-only"]), 1)


class PinnedHashesTests(unittest.TestCase):
    def test_every_file_has_an_input_and_a_result_hash(self):
        self.assertEqual(sorted(overlay.IMAGE_HASHES), sorted(TABLES))
        for image_sha256, patched_sha256 in overlay.IMAGE_HASHES.values():
            self.assertRegex(image_sha256, r"^[0-9a-f]{64}$")
            self.assertRegex(patched_sha256, r"^[0-9a-f]{64}$")
            self.assertNotEqual(image_sha256, patched_sha256)


@unittest.skipUnless(BASELINE_DIR, "AISIS_QUASAR_BASELINE_DIR (a copy of the active image's /src) not set")
class CapturedImageTests(unittest.TestCase):
    def test_the_images_files_patch_to_their_pinned_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "overlay"
            hashes = overlay.prepare(Path(BASELINE_DIR), out, pristine_only=True)
            for relative, (_, patched_sha256) in overlay.IMAGE_HASHES.items():
                self.assertEqual(hashes[relative], patched_sha256, relative)
            self.assertEqual(hashes[overlay.NEGATION_PATH], overlay.NEGATION_SHA256)

    def test_the_patched_layer_leaves_no_process_global_state_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "overlay"
            overlay.prepare(Path(BASELINE_DIR), out)
            text = (out / "backend/ha-intents.ts").read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\bpendingClarification\b", text))
            self.assertIsNone(re.search(r"\blastRepromptAt\b", text))


if __name__ == "__main__":
    unittest.main()
