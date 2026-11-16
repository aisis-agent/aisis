"""prepare_overlay: exact single-block patches of the quasar ha-intents / asr-correct, refused on anything else."""
import os
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

HA_BASELINE = ("import { x } from './x'\n" + overlay.HA_IMPORT_BEFORE + "\nasync function matchFastIntentCore () {\n"
               "    // A question is pending: this turn is the answer.\n" + overlay.HA_HOOK_BEFORE + "    const math = 1\n}\n")
ASR_BASELINE = "// header\n" + overlay.ASR_HEADER_BEFORE + "const MODE = 'on'\n" + overlay.ASR_TIERS_BEFORE
BASELINE_DIR = os.environ.get("AISIS_QUASAR_BASELINE_DIR")


class ApplyPatchesTests(unittest.TestCase):
    def test_the_hook_is_called_after_the_pending_answer_and_before_the_room_follow_up(self):
        patched = overlay.apply_patches(HA_BASELINE, overlay.HA_INTENTS_PATCHES)
        call = patched.index("await grammarRoute(text,")
        self.assertLess(patched.index("A question is pending"), call)
        self.assertLess(call, patched.index("«а на кухне?»"))
        self.assertIn(overlay.HA_IMPORT_AFTER, patched)

    def test_the_clarify_tier_is_no_longer_eligible_for_the_corrector(self):
        patched = overlay.apply_patches(ASR_BASELINE, overlay.ASR_CORRECT_PATCHES)
        self.assertNotIn("clarify: true", patched)
        self.assertIn("{ confirm: true, llm: true, reprompt: true }", patched)

    def test_a_baseline_without_a_block_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches("const unrelated = 1\n", overlay.ASR_CORRECT_PATCHES)

    def test_a_baseline_with_a_block_twice_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(ASR_BASELINE + overlay.ASR_TIERS_BEFORE, overlay.ASR_CORRECT_PATCHES)
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(HA_BASELINE + overlay.HA_HOOK_BEFORE, overlay.HA_INTENTS_PATCHES)

    def test_a_partially_matching_baseline_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(HA_BASELINE.replace(overlay.HA_IMPORT_BEFORE, ""), overlay.HA_INTENTS_PATCHES)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ha = self.root / "ha-intents.ts"
        self.ha.write_text(HA_BASELINE, encoding="utf-8")
        self.asr = self.root / "asr-correct.ts"
        self.asr.write_text(ASR_BASELINE, encoding="utf-8")
        self.hook = self.root / "grammar-hook.ts"
        self.hook.write_text("export const hook = 1\n", encoding="utf-8")

    def test_writes_the_three_files_where_deploy_overlay_expects_them(self):
        out = self.root / "overlay"
        target = overlay.prepare(self.ha, self.asr, out, check_hashes=False, hook_source=self.hook)
        self.assertEqual(target, out / "backend")
        self.assertEqual((target / "ha-intents.ts").read_text(encoding="utf-8"),
                         overlay.apply_patches(HA_BASELINE, overlay.HA_INTENTS_PATCHES))
        self.assertEqual((target / "asr-correct.ts").read_text(encoding="utf-8"),
                         overlay.apply_patches(ASR_BASELINE, overlay.ASR_CORRECT_PATCHES))
        self.assertEqual((target / "grammar-hook.ts").read_text(encoding="utf-8"), "export const hook = 1\n")

    def test_an_unknown_baseline_is_refused_by_hash_and_nothing_is_written(self):
        out = self.root / "overlay"
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, self.asr, out, hook_source=self.hook)
        self.assertFalse(out.exists())

    def test_an_existing_output_directory_is_refused(self):
        out = self.root / "overlay"
        out.mkdir()
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, self.asr, out, check_hashes=False, hook_source=self.hook)
        self.assertEqual(list(out.iterdir()), [])

    def test_a_failing_patch_leaves_no_output(self):
        out = self.root / "overlay"
        self.asr.write_text("const nothing = 1\n", encoding="utf-8")
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, self.asr, out, check_hashes=False, hook_source=self.hook)
        self.assertFalse(out.exists())

    def test_cli_exit_codes(self):
        out = self.root / "overlay"
        args = ["--ha-intents", str(self.ha), "--asr-correct", str(self.asr), "--output-dir", str(out)]
        self.assertEqual(overlay.main(args), 1)  # synthetic files: wrong SHA-256
        self.assertEqual(overlay.main(["--ha-intents", str(self.ha) + ".missing", *args[2:]]), 1)


class AcceptedInputsTests(unittest.TestCase):
    def test_every_accepted_input_maps_to_its_own_pinned_result(self):
        accepted = {"in-a": "out-a", "in-b": "out-b"}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "asr-correct.ts"
            source.write_text(ASR_BASELINE, encoding="utf-8")
            patched = overlay.apply_patches(ASR_BASELINE, overlay.ASR_CORRECT_PATCHES).encode("utf-8")
            digest = overlay.sha256(source.read_bytes())
            self.assertEqual(overlay.patch_file(source, overlay.ASR_CORRECT_PATCHES,
                                                {digest: overlay.sha256(patched)}, check_hashes=True), patched)
            with self.assertRaises(overlay.OverlayError):  # a known input whose result differs from the pinned one
                overlay.patch_file(source, overlay.ASR_CORRECT_PATCHES, {digest: "out-b"}, check_hashes=True)
            with self.assertRaises(overlay.OverlayError):  # an input that is none of the accepted ones
                overlay.patch_file(source, overlay.ASR_CORRECT_PATCHES, accepted, check_hashes=True)

    def test_the_speaker_room_result_is_an_accepted_ha_intents_input(self):
        self.assertEqual(len(overlay.HA_INTENTS_PATCHED_SHA256), 2)
        self.assertEqual(len(set(overlay.HA_INTENTS_PATCHED_SHA256.values())), 2)


@unittest.skipUnless(BASELINE_DIR, "AISIS_QUASAR_BASELINE_DIR (ha-intents.ts, asr-correct.ts from the image) not set")
class CapturedBaselineTests(unittest.TestCase):
    def test_the_captured_baselines_patch_to_the_pinned_hashes(self):
        base = Path(BASELINE_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            target = overlay.prepare(base / "ha-intents.ts", base / "asr-correct.ts", Path(tmp) / "overlay")
            ha_in = overlay.sha256((base / "ha-intents.ts").read_bytes())
            asr_in = overlay.sha256((base / "asr-correct.ts").read_bytes())
            self.assertEqual(overlay.sha256((target / "ha-intents.ts").read_bytes()), overlay.HA_INTENTS_PATCHED_SHA256[ha_in])
            self.assertEqual(overlay.sha256((target / "asr-correct.ts").read_bytes()), overlay.ASR_CORRECT_PATCHED_SHA256[asr_in])
            self.assertTrue((target / "grammar-hook.ts").is_file())


if __name__ == "__main__":
    unittest.main()
