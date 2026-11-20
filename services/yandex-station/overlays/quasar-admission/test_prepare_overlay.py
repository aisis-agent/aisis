"""prepare_overlay: exact single-block patches of the quasar main.ts and UniProxy router, refused on anything else."""
import os
import tempfile
import unittest
from pathlib import Path

import prepare_overlay as overlay

BASELINE_DIR = os.environ.get("AISIS_QUASAR_BASELINE_DIR")  # a copy of the active image's /src: adds the real-file hash tests
CLOSE_LOG_ROUTER = os.environ.get("AISIS_QUASAR_CLOSE_LOG_ROUTER")  # the router after the uniproxy-close-log overlay
FORK_SRC = os.environ.get("AISIS_FORK_SRC")  # src/ of the aisis-quasar worktree that carries the same change

PATCH_SETS = (("main.ts", overlay.MAIN_PATCHES), ("router", overlay.ROUTER_PATCHES))


def baseline_of(patches):
    return "\n".join(before for before, _, _ in patches)


class ApplyPatchesTests(unittest.TestCase):
    def test_every_block_is_replaced(self):
        for name, patches in PATCH_SETS:
            patched = overlay.apply_patches(baseline_of(patches), patches)
            for before, after, label in patches:
                self.assertIn(after, patched, msg=f"{name}: {label}")

    def test_a_baseline_without_a_block_is_refused(self):
        for name, patches in PATCH_SETS:
            for before, _, label in patches:
                with self.assertRaises(overlay.OverlayError, msg=f"{name}: {label}"):
                    overlay.apply_patches(baseline_of(patches).replace(before, ""), patches)

    def test_a_baseline_with_a_block_twice_is_refused(self):
        for name, patches in PATCH_SETS:
            for before, _, label in patches:
                with self.assertRaises(overlay.OverlayError, msg=f"{name}: {label}"):
                    overlay.apply_patches(baseline_of(patches) + "\n" + before, patches)

    def test_an_already_patched_file_is_refused(self):
        for name, patches in PATCH_SETS:
            with self.assertRaises(overlay.OverlayError, msg=name):
                overlay.apply_patches(overlay.apply_patches(baseline_of(patches), patches), patches)

    def test_the_http_server_binds_to_bind_host_and_the_function_state_server_always_to_loopback(self):
        patched = overlay.apply_patches(baseline_of(overlay.MAIN_PATCHES), overlay.MAIN_PATCHES)
        self.assertIn("process.env.BIND_HOST ?? '127.0.0.1'", patched)
        self.assertIn("app.listen(environment.PORT, bindHost,", patched)
        self.assertIn("functionStateServer.listen({ hostname: '127.0.0.1',", patched)
        self.assertLess(patched.index("app.use(admissionMiddleware("), patched.index("const server = app.listen("))

    def test_the_upgrade_is_admitted_before_it_becomes_a_connection(self):
        patched = overlay.apply_patches(baseline_of(overlay.ROUTER_PATCHES), overlay.ROUTER_PATCHES)
        self.assertLess(patched.index("admitUpgrade(admission, request, knownStations)"), patched.index("wsServer.handleUpgrade("))
        self.assertIn("refuseUpgrade(socket)", patched)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.baseline = self.root / "baseline"
        (self.baseline / "routers").mkdir(parents=True)
        (self.baseline / "main.ts").write_text(baseline_of(overlay.MAIN_PATCHES), encoding="utf-8")
        (self.baseline / overlay.ROUTER_TS).write_text(baseline_of(overlay.ROUTER_PATCHES), encoding="utf-8")

    def test_writes_exactly_the_three_files_where_deploy_overlay_expects_them(self):
        out = self.root / "overlay"
        written = overlay.prepare(self.baseline, out, check_hashes=False)
        self.assertEqual(sorted(str(path.relative_to(out)) for path in written),
                         ["admission.ts", "main.ts", "routers/uniproxy.alice.yandex.net.ts"])
        self.assertEqual((out / "admission.ts").read_bytes(), (overlay.HERE / "src" / "admission.ts").read_bytes())

    def test_the_router_can_come_from_another_overlay(self):
        other = self.root / "other-router.ts"
        other.write_text("// close-log\n" + baseline_of(overlay.ROUTER_PATCHES), encoding="utf-8")
        out = self.root / "overlay"
        overlay.prepare(self.baseline, out, router=other, check_hashes=False)
        self.assertTrue((out / overlay.ROUTER_TS).read_text(encoding="utf-8").startswith("// close-log\n"))

    def test_an_input_that_is_not_pinned_is_refused(self):
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.baseline, self.root / "overlay")

    def test_an_existing_output_is_never_overwritten(self):
        out = self.root / "overlay"
        overlay.prepare(self.baseline, out, check_hashes=False)
        with self.assertRaises(overlay.OverlayError):
            overlay.prepare(self.baseline, out, check_hashes=False)

    def test_the_shipped_admission_source_is_the_pinned_one(self):
        self.assertEqual(overlay.sha256((overlay.HERE / "src" / "admission.ts").read_bytes()), overlay.ADMISSION_SHA256)


@unittest.skipUnless(BASELINE_DIR, "AISIS_QUASAR_BASELINE_DIR (a copy of the active image's /src) is not set")
class RealFileTests(unittest.TestCase):
    def test_the_active_images_files_build_with_their_pinned_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "overlay"
            overlay.prepare(Path(BASELINE_DIR), out)
            if FORK_SRC:
                for name in (overlay.ADMISSION_TS, overlay.MAIN_TS, overlay.ROUTER_TS):
                    self.assertEqual((out / name).read_bytes(), (Path(FORK_SRC) / name).read_bytes(), msg=name)

    @unittest.skipUnless(CLOSE_LOG_ROUTER, "AISIS_QUASAR_CLOSE_LOG_ROUTER is not set")
    def test_the_router_after_the_close_log_overlay_builds_with_its_pinned_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay.prepare(Path(BASELINE_DIR), Path(tmp) / "overlay", router=Path(CLOSE_LOG_ROUTER))


if __name__ == "__main__":
    unittest.main()
