"""prepare_overlay: exact single-block patch of the quasar entity-index, refused on anything else."""
import json
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

CONTEXT = "const NONE = {}\nfunction tokenize() {}\n" + overlay.TABLE_ANCHOR + "function matchEntityQuery() {\n  if (parsed === null) return NONE\n\n"
TAIL = "    const weather = index.subjects.find(subject => subject.rows.some(row => row.domain === 'weather'))\n  }\n}\n"
BASELINE = CONTEXT + overlay.BEFORE + TAIL
HANDOFF = {"lead_ins": ["алиса"], "phrases": ["какая погода", "что на улице"]}


class ApplyPatchTests(unittest.TestCase):
    def test_only_table_phrases_return_none_before_the_entity_lookup(self):
        patched = overlay.apply_patch(BASELINE, HANDOFF)
        guard = "if (handedToWeatherCli(text)) return NONE"
        self.assertIn(guard, patched)
        self.assertLess(patched.index(guard), patched.index("const weather = index.subjects.find"))
        self.assertEqual(patched.count("return NONE"), BASELINE.count("return NONE") + 1)
        self.assertNotIn("parsed.weather ||", patched)

    def test_the_table_is_embedded_before_the_entry_point(self):
        patched = overlay.apply_patch(BASELINE, HANDOFF)
        self.assertIn('const WEATHER_HANDOFF = new Set(["какая погода", "что на улице"])', patched)
        self.assertIn('const WEATHER_LEAD_INS = new Set(["алиса"])', patched)
        self.assertLess(patched.index("const WEATHER_HANDOFF"), patched.index("function matchEntityQuery"))

    def test_a_baseline_without_the_block_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patch("function matchEntityQuery() {}\n", HANDOFF)

    def test_a_baseline_with_the_block_twice_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patch(BASELINE + overlay.BEFORE, HANDOFF)

    def test_a_baseline_without_the_banner_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.apply_patch(BASELINE.replace(overlay.TABLE_ANCHOR, ""), HANDOFF)


class HandoffTableTests(unittest.TestCase):
    def write(self, table):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "table.json"
        path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        return path

    def test_the_shipped_table_is_valid(self):
        table = overlay.load_handoff()
        self.assertGreater(len(table["phrases"]), 100)

    def test_unsorted_duplicate_empty_and_unnormalized_entries_are_refused(self):
        for phrases in (["б", "а"], ["а", "а"], [], ["Какая погода"], ["какая  погода"], ["какая погода?"], ["ёж"]):
            with self.subTest(phrases=phrases), self.assertRaises(overlay.OverlayError):
                overlay.load_handoff(self.write({"lead_ins": ["алиса"], "phrases": phrases}))


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.baseline = Path(self.tmp.name) / "entity-index.ts"
        self.baseline.write_text(BASELINE, encoding="utf-8")

    def test_writes_the_file_where_deploy_overlay_expects_it(self):
        out = Path(self.tmp.name) / "overlay"
        target = overlay.prepare(self.baseline, out, check_hashes=False)
        self.assertEqual(target, out / "backend" / "entity-index.ts")
        self.assertEqual(target.read_text(encoding="utf-8"), overlay.apply_patch(BASELINE, overlay.load_handoff()))

    def test_an_unknown_baseline_is_refused_by_hash_and_nothing_is_written(self):
        out = Path(self.tmp.name) / "overlay"
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.baseline, out)
        self.assertFalse(out.exists())

    def test_cli_exit_codes(self):
        out = Path(self.tmp.name) / "overlay"
        self.assertEqual(overlay.main(["--baseline", str(self.baseline), "--output-dir", str(out)]), 1)
        self.assertEqual(overlay.main(["--baseline", str(self.baseline) + ".missing", "--output-dir", str(out)]), 1)


if __name__ == "__main__":
    unittest.main()
