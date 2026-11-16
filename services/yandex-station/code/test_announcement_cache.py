"""The AISIS fast path accepts only prepared fixed announcements."""
import hashlib
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    paths = [Path(__file__).resolve().parent / "announcement_cache.py",
        ROOT / "announcement_cache.py", ROOT / "services/yandex-station/code/announcement_cache.py",
        ROOT / "aisis_patch/services/yandex-station/code/announcement_cache.py"]
    path = next((path for path in paths if path.exists()), None)
    if path is None:
        raise AssertionError("AISIS cached speech path is not implemented")
    spec = importlib.util.spec_from_file_location("aisis_announcement_cache", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@lru_cache(maxsize=16)
def audio_fixture(label="fixture", duration=0.6):
    frequency = 220 + sum(label.encode()) % 400
    result = subprocess.run([os.environ.get("FFMPEG", "ffmpeg"), "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
        "-t", str(duration), "-c:a", "libopus", "-f", "ogg", "pipe:1"],
        capture_output=True, check=True, timeout=5)
    return result.stdout


def populate(module, folder):
    manifest = {"voice_id": module.VOICE_ID, "clips": {}}
    for index, target in enumerate(sorted(module.TARGETS)):
        key = f"{index + 1:064x}"
        audio = audio_fixture(target)
        info = {"key": key, "sha256": hashlib.sha256(audio).hexdigest(), "duration": 0.6}
        (folder / (key + ".ogg")).write_bytes(audio)
        (folder / (key + ".json")).write_text(json.dumps({**info, "voice": module.VOICE_ID}))
        manifest["clips"][target] = info
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return manifest


class CacheTests(unittest.TestCase):
    def test_import_source_rejects_all_userinfo_before_any_request(self):
        module = load_module()
        for source in ("http://:secret@ha.local", "https://user@ha.local", "http://@ha.local"):
            with self.subTest(source=source), patch.object(module, "urlopen") as fetch:
                with self.assertRaises(module.CacheValidationError):
                    module.import_manifest(source)
                fetch.assert_not_called()

    def test_published_generation_and_pointer_are_synced_in_order(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            clips = module.manifest_clips(manifest)
            prepared = {info["key"]: (path / (info["key"] + ".ogg")).read_bytes() for info in clips.values()}
            events = []
            fsync, replace = os.fsync, Path.replace
            def synced(descriptor):
                name = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                events.append(("sync", name))
                return fsync(descriptor)
            def switched(source, target):
                if Path(target).name == "current":
                    generation = path / os.readlink(source)
                    self.assertEqual(sum(event == "sync" and name.parent == generation and name.name.endswith(".part") for event, name in events), 12)
                    self.assertIn(("sync", generation), events)
                    self.assertIn(("sync", path), events)
                    events.append(("publish", Path(target)))
                return replace(source, target)
            with patch.object(module.os, "fsync", synced), patch.object(Path, "replace", switched):
                self.assertTrue(module.publish_generation(path, manifest, clips, prepared)["ready"])
            self.assertEqual(events[-1], ("sync", path))
            self.assertEqual(events[-2], ("publish", path / "current"))

    def test_postswitch_sync_failure_never_deletes_visible_generation(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            clips = module.manifest_clips(manifest)
            prepared = {info["key"]: (path / (info["key"] + ".ogg")).read_bytes() for info in clips.values()}
            original = module.sync_directory
            def sync(directory):
                if directory == path and (path / "current").is_symlink():
                    raise OSError(errno.EIO, "post-publication sync failed")
                return original(directory)
            with patch.object(module, "sync_directory", sync), self.assertRaises(OSError):
                module.publish_generation(path, manifest, clips, prepared)
            self.assertTrue((path / "current").resolve().is_dir())
            self.assertTrue(module.status(path)["ready"])
            self.assertEqual(list(path.glob("*.current")), [])

    def test_preswitch_sync_failure_preserves_old_cache_and_cleans_staging(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            clips = module.manifest_clips(manifest)
            prepared = {info["key"]: (path / (info["key"] + ".ogg")).read_bytes() for info in clips.values()}
            with patch.object(module.os, "fsync", side_effect=OSError(errno.ENOSPC, "sync failed")), self.assertRaises(OSError):
                module.publish_generation(path, manifest, clips, prepared)
            self.assertFalse((path / "current").exists())
            self.assertEqual(list(path.glob("generation-*")), [])
            self.assertTrue(module.status(path)["ready"])

    def test_multiple_audio_streams_are_rejected_even_when_first_clip_is_short(self):
        module = load_module()
        audio = subprocess.run([os.environ.get("FFMPEG", "ffmpeg"), "-v", "error",
            "-f", "lavfi", "-i", "sine=duration=0.5:sample_rate=48000",
            "-f", "lavfi", "-i", "sine=duration=5:sample_rate=48000",
            "-map", "0:a", "-map", "1:a", "-c:a", "libopus", "-b:a", "12000",
            "-ac:a:1", "2", "-disposition:a:0", "0", "-disposition:a:1", "default",
            "-f", "ogg", "pipe:1"], capture_output=True, check=True, timeout=5).stdout
        self.assertLess(len(audio), 65536)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            info = manifest["clips"]["tv"]
            info.update(sha256=hashlib.sha256(audio).hexdigest(), duration=0.5)
            (path / (info["key"] + ".ogg")).write_bytes(audio)
            (path / (info["key"] + ".json")).write_text(json.dumps({**info, "voice": module.VOICE_ID}))
            (path / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(module.CacheNotReady):
                module.read_clip(module.PREFIX + info["key"] + "]", path)
            self.assertFalse(module.status(path)["ready"])

    def test_malformed_pointer_never_masks_staging_enospc_or_leaks_generation(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "current").write_text("malformed pointer")
            failure = OSError(errno.ENOSPC, "injected staging disk exhaustion")
            with patch.object(module, "atomic_write", side_effect=failure), self.assertRaises(OSError) as raised:
                module.publish_generation(path, {}, {}, {})
            self.assertIs(raised.exception, failure)
            self.assertEqual(list(path.glob("generation-*")), [])
            self.assertEqual((path / "current").read_text(), "malformed pointer")

    def test_cli_progress_is_flushed_before_network_and_publication_stages(self):
        module = load_module()
        real_mkstemp, real_publish = tempfile.mkstemp, module.publish_generation
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder)
                cache = path / "cache"
                cache.mkdir()
                manifest = populate(module, cache)
                def log_text():
                    logs = list(path.glob("*.log"))
                    self.assertEqual(len(logs), 1)
                    self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
                    return logs[0].read_text()
                def fetch(*args, **kwargs):
                    self.assertIn("Fetching manifest", log_text())
                    self.assertNotIn("fixture-private", log_text())
                    return io.BytesIO(json.dumps(manifest).encode())
                def publish(*args, **kwargs):
                    self.assertIn("Staging generation", log_text())
                    self.assertIn("Validating tv", log_text())
                    return real_publish(*args, **kwargs)
                def mkstemp(**kwargs):
                    kwargs.setdefault("dir", folder)
                    return real_mkstemp(**kwargs)
                with patch("sys.argv", ["importer", "--source", "http://fixture-private", "--folder", str(cache)]), \
                        patch.object(module, "urlopen", fetch), patch.object(module, "publish_generation", publish), \
                        patch.object(module.tempfile, "mkstemp", mkstemp), redirect_stdout(io.StringIO()):
                    module.main()
                content = log_text()
                self.assertIn("Import complete", content)
                self.assertRegex(content, r"[0-9]+\.[0-9]{3} \+[0-9]+\.[0-9]{3}s Import started")
        finally:
            os.umask(previous_umask)

    def test_normal_speech_does_not_enter_cache_path(self):
        module = load_module()
        self.assertFalse(module.is_marker("Обычная речь"))
        self.assertTrue(module.is_marker("[ha-remote:bad]"))

    def test_missing_or_malformed_markers_fail_without_tts_fallback(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            for text in ["[ha-remote:../../secret]", "[ha-remote:" + "a" * 64 + "]"]:
                with self.subTest(text=text), self.assertRaises(module.CacheNotReady):
                    module.read_clip(text, Path(folder))

    def test_cached_audio_validated_and_returned_without_provider(self):
        module = load_module()
        key = "a" * 64
        audio = audio_fixture()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / (key + ".ogg")).write_bytes(audio)
            (path / (key + ".json")).write_text(json.dumps({
                "sha256": hashlib.sha256(audio).hexdigest(), "duration": 0.6,
                "voice": module.VOICE_ID}))
            self.assertEqual(module.read_clip("[ha-remote:" + key + "]", path), audio)
            (path / (key + ".ogg")).write_bytes(audio + b"corrupt")
            with self.assertRaises(module.CacheNotReady):
                module.read_clip("[ha-remote:" + key + "]", path)

    def test_warm_readiness_rechecks_current_files(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            self.assertTrue(module.status(path)["ready"])
            key = manifest["clips"]["tv"]["key"]
            (path / (key + ".ogg")).unlink()
            self.assertFalse(module.status(path)["ready"])

    def test_malformed_manifests_fail_closed(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            invalid = [[], {"voice_id": module.VOICE_ID, "clips": sorted(module.TARGETS)}]
            for value in [None, [], "x", {"key": [], "sha256": "a" * 64, "duration": 0.5}]:
                invalid.append({**manifest, "clips": {**manifest["clips"], "tv": value}})
            for duration in [True, "0.5", float("nan"), float("inf"), 0, 3, 10**400]:
                invalid.append({**manifest, "clips": {**manifest["clips"], "tv": {
                    **manifest["clips"]["tv"], "duration": duration}}})
            for value in invalid:
                with self.subTest(manifest=value):
                    (path / "manifest.json").write_text(json.dumps(value))
                    self.assertFalse(module.status(path)["ready"])

    def test_repeat_import_refreshes_changed_checksum_for_same_key(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            self.assertTrue(module.status(path)["ready"])
            info = manifest["clips"]["tv"]
            changed = audio_fixture("replacement")
            info["sha256"] = hashlib.sha256(changed).hexdigest()
            responses = [io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(changed)]
            with patch.object(module, "urlopen", side_effect=responses) as fetch:
                self.assertTrue(module.install("http://ha.example", path)["ready"])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(module.read_clip(module.PREFIX + info["key"] + "]", path), changed)

    def test_failed_refresh_preserves_the_working_cache(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            original_manifest = (path / "manifest.json").read_bytes()
            info = manifest["clips"]["tv"]
            key = info["key"]
            original_audio = (path / (key + ".ogg")).read_bytes()
            invalid = b"not a playable Opus stream"
            info["sha256"] = hashlib.sha256(invalid).hexdigest()
            responses = [io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(invalid)]
            with patch.object(module, "urlopen", side_effect=responses):
                with self.assertRaises(module.CacheNotReady):
                    module.install("http://ha.example", path)
            self.assertEqual((path / (key + ".ogg")).read_bytes(), original_audio)
            self.assertEqual((path / "manifest.json").read_bytes(), original_manifest)
            self.assertTrue(module.status(path)["ready"])

    def test_changed_key_refresh_keeps_an_already_queued_marker_playable(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            with patch.object(module, "urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())):
                module.install("http://ha.example", path)
            old_key = manifest["clips"]["tv"]["key"]
            marker = module.PREFIX + old_key + "]"
            previous = module.read_clip(marker, path)
            manifest["clips"]["tv"]["key"] = "f" * 64
            responses = [io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(previous)]
            with patch.object(module, "urlopen", side_effect=responses):
                self.assertTrue(module.install("http://ha.example", path)["ready"])
            self.assertEqual(module.read_clip(marker, path), previous)
            self.assertNotIn(old_key, module.status(path)["keys"])

    def test_corrupt_history_does_not_prevent_valid_changed_key_repair(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            generation = path / "generation-fixture"
            generation.mkdir()
            manifest = populate(module, generation)
            (path / "current").symlink_to(generation.name)
            (generation / "previous").write_bytes(b"\xff")
            previous = module.read_clip(module.PREFIX + manifest["clips"]["tv"]["key"] + "]", path)
            manifest["clips"]["tv"]["key"] = "f" * 64
            responses = [io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(previous)]
            with patch.object(module, "urlopen", side_effect=responses):
                self.assertTrue(module.install("http://ha.example", path)["ready"])

    def test_publication_failure_preserves_the_previous_generation(self):
        module = load_module()
        for failure in ("metadata", "manifest", "switch"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                path = Path(folder)
                manifest = populate(module, path)
                with patch.object(module, "urlopen", return_value=io.BytesIO(json.dumps(manifest).encode())):
                    module.install("http://ha.example", path)
                info = manifest["clips"]["tv"]
                marker = module.PREFIX + info["key"] + "]"
                previous = module.read_clip(marker, path)
                changed = audio_fixture("publication replacement")
                info["sha256"] = hashlib.sha256(changed).hexdigest()
                original_write, original_replace = module.atomic_write, Path.replace

                def write(target, content):
                    if ((failure == "metadata" and target.name == info["key"] + ".json")
                            or (failure == "manifest" and target.name == "manifest.json")):
                        raise OSError(errno.ENOSPC, "injected cache publication failure")
                    original_write(target, content)

                def replace(source, target):
                    if failure == "switch" and Path(target).name == "current":
                        raise OSError(errno.ENOSPC, "injected current generation switch failure")
                    return original_replace(source, target)

                responses = [io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(changed)]
                with patch.object(module, "urlopen", side_effect=responses), \
                        patch.object(module, "atomic_write", write), \
                        patch.object(Path, "replace", replace), self.assertRaises(OSError):
                    module.install("http://ha.example", path)
                self.assertEqual(module.read_clip(marker, path), previous)
                self.assertTrue(module.status(path)["ready"])

    def test_incomplete_staged_audio_never_replaces_playable_generation(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            info = manifest["clips"]["tv"]
            marker = module.PREFIX + info["key"] + "]"
            previous = module.read_clip(marker, path)
            changed = audio_fixture("partial staged write")
            info["sha256"] = hashlib.sha256(changed).hexdigest()
            original_write = module.atomic_write

            def write(target, content):
                if target.name == info["key"] + ".ogg":
                    content = content[:len(content) // 2]
                original_write(target, content)

            responses = [io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(changed)]
            with patch.object(module, "urlopen", side_effect=responses), \
                    patch.object(module, "atomic_write", write), self.assertRaises(module.CacheNotReady):
                module.install("http://ha.example", path)
            self.assertEqual(module.read_clip(marker, path), previous)
            self.assertTrue(module.status(path)["ready"])

    def test_actual_decoding_and_duration_are_required(self):
        module = load_module()
        fake = b"OggS" + b"\x00" * 24 + b"OpusHead" + b"not an Opus stream"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            for audio in [fake, audio_fixture("too long", duration=5)]:
                key = "a" * 64
                (path / (key + ".ogg")).write_bytes(audio)
                (path / (key + ".json")).write_text(json.dumps({"voice": module.VOICE_ID,
                    "duration": 0.6, "sha256": hashlib.sha256(audio).hexdigest()}))
                with self.assertRaises(module.CacheNotReady):
                    module.read_clip(module.PREFIX + key + "]", path)

    def test_oversized_integer_in_clip_metadata_fails_closed(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            key = manifest["clips"]["tv"]["key"]
            metadata = json.loads((path / (key + ".json")).read_text())
            metadata["duration"] = 10**400
            (path / (key + ".json")).write_text(json.dumps(metadata))
            with self.assertRaises(module.CacheNotReady):
                module.read_clip(module.PREFIX + key + "]", path)

    def test_warm_audio_validation_never_redecodes_or_hides_deleted_files(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)
            self.assertTrue(module.status(path)["ready"])
            key = manifest["clips"]["tv"]["key"]
            with patch.object(module.subprocess, "run", side_effect=AssertionError("unexpected decoding")):
                module.read_clip(module.PREFIX + key + "]", path)
                (path / (key + ".ogg")).unlink()
                self.assertFalse(module.status(path)["ready"])

    def test_import_logs_actionable_reasons_without_transport_details(self):
        module = load_module()
        failures = [
            (module.CacheValidationError("Use a Home Assistant base URL without credentials or query"), "base URL"),
            (module.CacheNotReady("announcement checksum mismatch"), "checksum mismatch"),
            (HTTPError("http://ha.invalid/?private=fixture-sensitive-detail", 503,
                "fixture-sensitive-detail", None, None), "HTTP 503"),
        ]
        real_mkstemp = tempfile.mkstemp
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory() as folder:
                for error, expected in failures:
                    output = io.StringIO()
                    with patch("sys.argv", ["importer", "--source", "http://ha.invalid"]), \
                            patch.object(module, "install", side_effect=error), \
                            patch.object(module.tempfile, "mkstemp", side_effect=lambda **kw: real_mkstemp(dir=folder, **kw)), \
                            redirect_stdout(output), self.assertRaises(SystemExit) as exit_error:
                        module.main()
                    self.assertEqual(exit_error.exception.code, 1)
                    self.assertIn(expected, output.getvalue())
                    self.assertNotIn("fixture-sensitive-detail", output.getvalue())
                for log in Path(folder).glob("*.log"):
                    self.assertIn("Import failed:", log.read_text())
                    self.assertNotIn("fixture-sensitive-detail", log.read_text())
        finally:
            os.umask(previous_umask)

    def test_overlapping_imports_publish_consistent_clip_pairs(self):
        module = load_module()
        first_audio_written, second_metadata_written = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            initial = populate(module, path)
            key = initial["clips"]["tv"]["key"]
            manifests, audio = {}, {}
            for host in ["first", "second"]:
                audio[host] = audio_fixture(host)
                manifests[host] = json.loads(json.dumps(initial))
                manifests[host]["clips"]["tv"]["sha256"] = hashlib.sha256(audio[host]).hexdigest()
            owner = threading.local()
            original_write = module.atomic_write

            def fetch(url, **kwargs):
                host = url.split("/")[2]
                if url.endswith("/manifest"):
                    return io.BytesIO(json.dumps(manifests[host]).encode())
                return io.BytesIO(audio[host])

            def write(target, content):
                original_write(target, content)
                if target.name == key + ".ogg" and owner.host == "first":
                    first_audio_written.set()
                    second_metadata_written.wait(0.2)
                if target.name == key + ".json" and owner.host == "second":
                    second_metadata_written.set()

            def install(host):
                owner.host = host
                return module.install("http://" + host, path)

            with patch.object(module, "urlopen", fetch), patch.object(module, "atomic_write", write), ThreadPoolExecutor(2) as workers:
                first = workers.submit(install, "first")
                self.assertTrue(first_audio_written.wait(3))
                second = workers.submit(install, "second")
                self.assertTrue(first.result(timeout=4)["ready"])
                self.assertTrue(second.result(timeout=4)["ready"])
            self.assertTrue(module.status(path)["ready"])

    def test_atomic_writes_have_independent_temporary_files(self):
        module = load_module()
        barrier = threading.Barrier(2)
        original = Path.replace

        def synchronized_replace(source, destination):
            barrier.wait(timeout=3)
            return original(source, destination)

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "clip.ogg"
            with patch.object(Path, "replace", synchronized_replace), ThreadPoolExecutor(2) as workers:
                writes = [workers.submit(module.atomic_write, target, content) for content in (b"one", b"two")]
                for result in writes:
                    result.result(timeout=4)
            self.assertIn(target.read_bytes(), (b"one", b"two"))
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_later_import_never_deletes_an_earlier_published_rollback_copy(self):
        module = load_module()
        first_switched, second_finished = threading.Event(), threading.Event()
        owner = threading.local()
        original_lock = module.cache_lock

        @contextmanager
        def lock(folder, *, exclusive=False):
            with original_lock(folder, exclusive=exclusive):
                yield
            if exclusive and getattr(owner, "first", False):
                first_switched.set()
                self.assertTrue(second_finished.wait(3))

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            manifest = populate(module, path)

            def install_first():
                owner.first = True
                return module.install("http://fixture", path)

            def fetch(*args, **kwargs):
                return io.BytesIO(json.dumps(manifest).encode())

            with patch.object(module, "cache_lock", lock), patch.object(module, "urlopen", fetch), ThreadPoolExecutor(1) as workers:
                first = workers.submit(install_first)
                try:
                    self.assertTrue(first_switched.wait(3))
                    published = path / os.readlink(path / "current")
                    self.assertTrue(module.install("http://fixture", path)["ready"])
                finally:
                    second_finished.set()
                self.assertTrue(first.result(timeout=3)["ready"])
            self.assertTrue(published.is_dir())
            self.assertTrue(module.status(path)["ready"])


if __name__ == "__main__":
    unittest.main()
