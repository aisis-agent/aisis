"""prepare_overlay: exact-block patch of three quasar sources, refused on anything else."""
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay


def synthetic(relative: str) -> str:
    """A stand-in for the pristine file: its old blocks with filler between them."""
    return "// filler\n" + "\n// filler\n".join(old for _what, old, _new in overlay.FILES[relative][2]) + "\n// filler\n"


class ApplyBlocksTests(unittest.TestCase):
    def test_every_old_block_is_replaced_by_its_new_one(self):
        for relative, (_in, _out, blocks) in overlay.FILES.items():
            patched = overlay.apply_blocks(synthetic(relative), blocks)
            for what, old, new in blocks:
                with self.subTest(file=relative, block=what):
                    self.assertIn(new, patched)
                    if old not in new:
                        self.assertNotIn(old, patched)

    def test_fuzzy_words_sentences_and_word_count_are_capped_and_editdistance_has_no_matrix(self):
        patched = overlay.apply_blocks(synthetic("backend/fuzzy-ru.ts"), overlay.FILES["backend/fuzzy-ru.ts"][2])
        self.assertIn("export const MAX_TOKEN_LENGTH = 64", patched)
        self.assertIn("export const MAX_TOKENS = 200", patched)
        self.assertIn("export const MAX_SENTENCES = 16", patched)
        self.assertNotIn("rows[i - 1][j]", patched)

    def test_the_processor_empties_itself_when_the_limit_trips(self):
        patched = overlay.apply_blocks(synthetic("codecs/opus-processor.ts"), overlay.FILES["codecs/opus-processor.ts"][2])
        self.assertIn("this.packetSegments.push(Buffer.from(segment))", patched)
        self.assertLess(patched.index("this.fail()"), patched.index("throw new Error('Ogg packet exceeds voice input limit')"))
        self.assertIn("if (this.failed) throw", patched)

    def test_the_stitcher_puts_later_granules_on_the_running_total(self):
        patched = overlay.apply_blocks(synthetic("codecs/ogg-stitcher.ts"), overlay.FILES["codecs/ogg-stitcher.ts"][2])
        self.assertIn("const granule = this.granule + page.absoluteGranulePosition", patched)
        self.assertNotIn("baseGranule", patched)

    def test_each_anchor_missing_or_doubled_is_refused(self):
        for relative, (_in, _out, blocks) in overlay.FILES.items():
            text = synthetic(relative)
            for what, old, _new in blocks:
                with self.subTest(file=relative, block=what, problem="missing"), self.assertRaises(overlay.OverlayError):
                    overlay.apply_blocks(text.replace(old, "", 1), blocks)
                with self.subTest(file=relative, block=what, problem="twice"), self.assertRaises(overlay.OverlayError):
                    overlay.apply_blocks(text + old, blocks)

    def test_overlapping_blocks_are_refused(self):
        outer = "alpha\nbeta\n"
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_blocks("alpha\nbeta\n", [("outer", outer, "x"), ("inner", "beta\n", "y")])


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = self.root / "baseline"
        for relative in overlay.FILES:
            path = self.baseline / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(synthetic(relative), encoding="utf-8")

    def test_writes_the_files_where_deploy_overlay_expects_them(self):
        out = self.root / "overlay"
        written = overlay.prepare(self.baseline, out, check_hashes=False)
        self.assertEqual(sorted(path.relative_to(out).as_posix() for path in written), sorted(overlay.FILES))
        for relative, (_in, _out, blocks) in overlay.FILES.items():
            self.assertEqual((out / relative).read_text(encoding="utf-8"), overlay.apply_blocks(synthetic(relative), blocks))

    def test_an_unknown_baseline_is_refused_by_hash_and_nothing_is_written(self):
        out = self.root / "overlay"
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.baseline, out)
        self.assertFalse(out.exists())

    def test_one_unpatchable_file_writes_nothing_at_all(self):
        out = self.root / "overlay"
        (self.baseline / "codecs" / "ogg-stitcher.ts").write_text("// something else\n", encoding="utf-8")
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.baseline, out, check_hashes=False)
        self.assertFalse(out.exists())

    def test_cli_exit_codes(self):
        out = self.root / "overlay"
        self.assertEqual(overlay.main(["--baseline-dir", str(self.baseline), "--output-dir", str(out)]), 1)
        self.assertEqual(overlay.main(["--baseline-dir", str(self.root / "missing"), "--output-dir", str(out)]), 1)


if __name__ == "__main__":
    unittest.main()
