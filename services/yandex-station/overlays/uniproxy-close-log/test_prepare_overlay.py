"""prepare_overlay: exact three-anchor patch of the quasar UniProxy router, refused on anything else."""
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

BASELINE = (overlay.LOGGER_ANCHOR + "\ninterface UniProxyRouter {}\n\n" + overlay.REGISTER_ANCHOR + "parameters, server) {\n  wsServer.on('connection', websocket => {\n"
            + overlay.BEFORE_CLOSE + "  })\n}\n")


class ApplyPatchTests(unittest.TestCase):
    def test_the_close_handler_logs_through_the_code_aware_line(self):
        patched = overlay.apply_patch(BASELINE)
        self.assertNotIn("logger.warn('UniProxy WebSocket closed')", patched)
        self.assertIn("websocket.on('close', (code, reason) => {", patched)
        self.assertIn("logger[line.level](line.message)", patched)
        self.assertEqual(patched.count("const logger = getLogger()"), 1)

    def test_the_normal_codes_and_the_line_builder_come_before_the_router(self):
        patched = overlay.apply_patch(BASELINE)
        self.assertIn("const NORMAL_CLOSE_CODES = new Set([1000, 1001, 1005])", patched)
        self.assertLess(patched.index("NORMAL_CLOSE_CODES = "), patched.index("export function closeLogLine"))
        self.assertLess(patched.index("export function closeLogLine"), patched.index("export function registerUniproxyAliceYandexNetRouter"))

    def test_the_two_texts_dialogstore_reads_are_the_ones_built(self):
        patched = overlay.apply_patch(BASELINE)
        self.assertIn("UniProxy WebSocket closed${normal ? ' (normal)' : ' abnormally'}: code=${code}", patched)

    def test_each_anchor_missing_or_doubled_is_refused(self):
        for anchor in (overlay.LOGGER_ANCHOR, overlay.REGISTER_ANCHOR, overlay.BEFORE_CLOSE):
            with self.subTest(anchor=anchor.splitlines()[0], problem="missing"), self.assertRaises(overlay.OverlayError):
                overlay.apply_patch(BASELINE.replace(anchor, "", 1))
            with self.subTest(anchor=anchor.splitlines()[0], problem="twice"), self.assertRaises(overlay.OverlayError):
                overlay.apply_patch(BASELINE + anchor)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.baseline = Path(self.tmp.name) / "uniproxy.alice.yandex.net.ts"
        self.baseline.write_text(BASELINE, encoding="utf-8")

    def test_writes_the_file_where_deploy_overlay_expects_it(self):
        out = Path(self.tmp.name) / "overlay"
        target = overlay.prepare(self.baseline, out, check_hashes=False)
        self.assertEqual(target, out / "routers" / "uniproxy.alice.yandex.net.ts")
        self.assertEqual(target.read_text(encoding="utf-8"), overlay.apply_patch(BASELINE))

    def test_an_unknown_baseline_is_refused_by_hash_and_nothing_is_written(self):
        out = Path(self.tmp.name) / "overlay"
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.baseline, out)
        self.assertFalse(out.exists())

    def test_cli_exit_codes(self):
        out = Path(self.tmp.name) / "overlay"
        self.assertEqual(overlay.main(["--baseline", str(self.baseline), "--output-dir", str(out)]), 1)
        self.assertEqual(overlay.main(["--baseline", str(Path(self.tmp.name) / "missing.ts"), "--output-dir", str(out)]), 1)


if __name__ == "__main__":
    unittest.main()
