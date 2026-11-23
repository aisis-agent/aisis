"""Non-destructive Mini 2 firmware preparation regression tests."""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zlib

try:
    import firmware_artifacts as firmware
except ModuleNotFoundError:
    firmware = None


class FirmwareArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(firmware, "firmware artifact verifier must exist")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = b"070702" + b"synthetic test image" * 10
        self.image = self.root / "image.zip"
        self.image.write_bytes(self.data)
        self.spec = dataclasses.replace(
            firmware.MINI2, size=len(self.data),
            crc32=zlib.crc32(self.data) & 0xffffffff,
            sha256=hashlib.sha256(self.data).hexdigest(),
        )

    def test_cpio_named_zip_is_verified_without_unpacking(self):
        result = firmware.verify_image(self.image, self.spec)
        self.assertEqual(result["format"], "cpio-newc-crc")
        self.assertEqual(result["sha256"], self.spec.sha256)
        self.assertFalse(result["device_changes"])

    def test_actual_reference_is_pinned_to_mini2_not_lite(self):
        self.assertEqual(firmware.MINI2.platform, "yandexmini_2")
        self.assertEqual(firmware.MINI2.size, 67796480)
        self.assertEqual(firmware.MINI2.crc32, 1524631406)
        self.assertEqual(firmware.MINI2.sha256,
            "192aa9842c77257c4955c9a02cea837f91217d25206d2368815700280ab3c8c3")

    def test_changed_payload_is_rejected(self):
        self.image.write_bytes(self.data[:-1] + b"x")
        with self.assertRaises(firmware.ArtifactError):
            firmware.verify_image(self.image, self.spec)

    def test_truncated_and_oversized_payloads_are_rejected(self):
        for data in (self.data[:-1], self.data + b"x"):
            with self.subTest(size=len(data)):
                self.image.write_bytes(data)
                with self.assertRaises(firmware.ArtifactError):
                    firmware.verify_image(self.image, self.spec)

    def test_invalid_archive_magic_is_rejected_even_when_digests_match(self):
        self.data = b"<html>" + self.data[6:]
        self.image.write_bytes(self.data)
        spec = dataclasses.replace(self.spec, crc32=zlib.crc32(self.data),
            sha256=hashlib.sha256(self.data).hexdigest())
        with self.assertRaises(firmware.ArtifactError):
            firmware.verify_image(self.image, spec)

    def test_input_symlink_and_symlink_parent_are_rejected(self):
        link = self.root / "link.zip"
        link.symlink_to(self.image)
        parent = self.root / "linked"
        parent.symlink_to(self.root, target_is_directory=True)
        for path in (link, parent / self.image.name):
            with self.subTest(path=path):
                with self.assertRaises(firmware.ArtifactError):
                    firmware.verify_image(path, self.spec)

    def test_wrong_platform_or_version_is_rejected(self):
        for spec in (dataclasses.replace(self.spec, platform="yandexmicro"),
                     dataclasses.replace(self.spec, version="different")):
            with self.assertRaises(firmware.ArtifactError):
                firmware.verify_image(self.image, spec)

    def test_prepare_creates_private_artifact_and_explicit_status(self):
        output = self.root / "prepared"
        report = firmware.prepare_image(self.image, output, self.spec)
        self.assertEqual(report["status"], "prepared_not_deployed")
        self.assertFalse(report["device_changes"])
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(manifest["platform"], "yandexmini_2")
        self.assertEqual((output / manifest["filename"]).read_bytes(), self.data)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for path in output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertFalse((output / ".incomplete").exists())

    def test_existing_destination_is_not_overwritten(self):
        output = self.root / "prepared"
        output.mkdir()
        sentinel = output / "keep"
        sentinel.write_text("existing")
        with self.assertRaises(firmware.ArtifactError):
            firmware.prepare_image(self.image, output, self.spec)
        self.assertEqual(sentinel.read_text(), "existing")

    def test_failed_preparation_has_no_readiness_manifest(self):
        self.image.write_bytes(b"corrupt")
        output = self.root / "prepared"
        with self.assertRaises(firmware.ArtifactError):
            firmware.prepare_image(self.image, output, self.spec)
        self.assertFalse((output / "manifest.json").exists())

    def test_directory_swap_never_writes_into_replacement(self):
        output = self.root / "prepared"
        displaced = self.root / "displaced"
        victim = self.root / "replacement"
        victim.mkdir()
        original = firmware.private_file
        swapped = False

        def swap_once(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                output.rename(displaced)
                output.symlink_to(victim, target_is_directory=True)
                swapped = True
            return original(*args, **kwargs)

        with patch.object(firmware, "private_file", side_effect=swap_once):
            with self.assertRaises(firmware.ArtifactError):
                firmware.prepare_image(self.image, output, self.spec)
        self.assertEqual(list(victim.iterdir()), [])
        self.assertFalse((displaced / "manifest.json").exists())

    def test_copy_mutation_has_no_readiness_manifest(self):
        output = self.root / "prepared"
        original = firmware.open_image
        calls = 0

        def mutate_on_second_open(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.image.write_bytes(self.data[:-1] + b"x")
            return original(path)

        with patch.object(firmware, "open_image", side_effect=mutate_on_second_open):
            with self.assertRaises(firmware.ArtifactError):
                firmware.prepare_image(self.image, output, self.spec)
        self.assertTrue((output / ".incomplete").exists())
        self.assertFalse((output / "manifest.json").exists())

    def test_fifo_and_directory_are_refused_without_blocking(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for path in (fifo, self.root):
            with self.assertRaises(firmware.ArtifactError):
                firmware.verify_image(path, self.spec)

    def test_symlink_destination_parent_is_rejected(self):
        link = self.root / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(firmware.ArtifactError):
            firmware.prepare_image(self.image, link / "prepared", self.spec)
        self.assertFalse((self.root / "prepared").exists())


if __name__ == "__main__":
    unittest.main()
