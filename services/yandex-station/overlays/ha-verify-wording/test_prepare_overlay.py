"""prepare_overlay: exact single-block patches of the quasar ha-intents, refused on anything else."""
import os
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

BASELINE = "\n".join(before for before, _, _ in overlay.HA_INTENTS_PATCHES)
BASELINE_DIR = os.environ.get("AISIS_QUASAR_BASELINE_DIR")


class ApplyPatchesTests(unittest.TestCase):
    def test_every_block_is_replaced(self):
        patched = overlay.apply_patches(BASELINE, overlay.HA_INTENTS_PATCHES)
        for before, after, _ in overlay.HA_INTENTS_PATCHES:
            self.assertNotIn(before, patched)
            self.assertIn(after, patched)

    def test_a_baseline_without_a_block_is_refused(self):
        for before, _, label in overlay.HA_INTENTS_PATCHES:
            with self.assertRaises(overlay.OverlayError, msg=label):
                overlay.apply_patches(BASELINE.replace(before, ""), overlay.HA_INTENTS_PATCHES)

    def test_a_baseline_with_a_block_twice_is_refused(self):
        for before, _, label in overlay.HA_INTENTS_PATCHES:
            with self.assertRaises(overlay.OverlayError, msg=label):
                overlay.apply_patches(BASELINE + "\n" + before, overlay.HA_INTENTS_PATCHES)

    def test_an_already_patched_file_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patches(overlay.apply_patches(BASELINE, overlay.HA_INTENTS_PATCHES), overlay.HA_INTENTS_PATCHES)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ha = self.root / "ha-intents.ts"
        self.ha.write_text(BASELINE, encoding="utf-8")

    def test_writes_the_file_where_deploy_overlay_expects_it(self):
        out = self.root / "overlay"
        target = overlay.prepare(self.ha, out, check_hashes=False)
        self.assertEqual(target, out / "backend")
        self.assertEqual((target / "ha-intents.ts").read_text(encoding="utf-8"),
                         overlay.apply_patches(BASELINE, overlay.HA_INTENTS_PATCHES))
        self.assertEqual([path.name for path in target.iterdir()], ["ha-intents.ts"])

    def test_an_unknown_baseline_is_refused_by_hash_and_nothing_is_written(self):
        out = self.root / "overlay"
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, out)
        self.assertFalse(out.exists())

    def test_an_existing_output_directory_is_refused(self):
        out = self.root / "overlay"
        out.mkdir()
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, out, check_hashes=False)
        self.assertEqual(list(out.iterdir()), [])

    def test_a_failing_patch_leaves_no_output(self):
        out = self.root / "overlay"
        self.ha.write_text("const nothing = 1\n", encoding="utf-8")
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.ha, out, check_hashes=False)
        self.assertFalse(out.exists())

    def test_cli_exit_codes(self):
        out = self.root / "overlay"
        args = ["--ha-intents", str(self.ha), "--output-dir", str(out)]
        self.assertEqual(overlay.main(args), 1)  # synthetic file: wrong SHA-256
        self.assertEqual(overlay.main(["--ha-intents", str(self.ha) + ".missing", *args[2:]]), 1)


class AcceptedInputsTests(unittest.TestCase):
    def test_every_accepted_input_maps_to_its_own_pinned_result(self):
        accepted = {"in-a": "out-a", "in-b": "out-b"}
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "ha-intents.ts"
            source.write_text(BASELINE, encoding="utf-8")
            patched = overlay.apply_patches(BASELINE, overlay.HA_INTENTS_PATCHES).encode("utf-8")
            digest = overlay.sha256(source.read_bytes())
            self.assertEqual(overlay.patch_file(source, overlay.HA_INTENTS_PATCHES,
                                                {digest: overlay.sha256(patched)}, check_hashes=True), patched)
            with self.assertRaises(overlay.OverlayError):  # a known input whose result differs from the pinned one
                overlay.patch_file(source, overlay.HA_INTENTS_PATCHES, {digest: "out-b"}, check_hashes=True)
            with self.assertRaises(overlay.OverlayError):  # an input that is none of the accepted ones
                overlay.patch_file(source, overlay.HA_INTENTS_PATCHES, accepted, check_hashes=True)

    def test_pinned_hashes_are_complete(self):
        self.assertEqual(len(set(overlay.HA_INTENTS_PATCHED_SHA256.values())), len(overlay.HA_INTENTS_PATCHED_SHA256))
        self.assertFalse(set(overlay.HA_INTENTS_PATCHED_SHA256.values()) & set(overlay.HA_INTENTS_PATCHED_SHA256))
        for before, after in overlay.HA_INTENTS_PATCHED_SHA256.items():
            self.assertRegex(before, r"^[0-9a-f]{64}$")
            self.assertRegex(after, r"^[0-9a-f]{64}$")


@unittest.skipUnless(BASELINE_DIR, "AISIS_QUASAR_BASELINE_DIR (ha-intents.ts from the active image) not set")
class CapturedBaselineTests(unittest.TestCase):
    def test_the_captured_baseline_patches_to_the_pinned_hash(self):
        source = Path(BASELINE_DIR) / "ha-intents.ts"
        with tempfile.TemporaryDirectory() as tmp:
            target = overlay.prepare(source, Path(tmp) / "overlay")
            patched = (target / "ha-intents.ts").read_text(encoding="utf-8")
            self.assertNotIn("перезагруж", patched)
            self.assertNotIn("secondsWord", patched)
            self.assertEqual(patched.count("haRecoveryText("), 1)
            self.assertEqual(overlay.sha256((target / "ha-intents.ts").read_bytes()),
                             overlay.HA_INTENTS_PATCHED_SHA256[overlay.sha256(source.read_bytes())])


if __name__ == "__main__":
    unittest.main()
