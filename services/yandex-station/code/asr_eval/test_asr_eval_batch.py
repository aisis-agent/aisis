import asyncio
import os
import stat
import struct
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import numpy  # noqa: F401  (the gateway's level front-end needs it; the batch client does not)
except ImportError:
    sys.modules.setdefault("speech_frontend", types.ModuleType("speech_frontend"))   # CI has no numpy: only the import is satisfied
from asr_eval import wyoming_batch  # noqa: E402  (imports the gateway's Wyoming client, so aiohttp)


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * 1600)


class BatchTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        self.files = []
        for name in ("a", "b", "c"):
            write_wav(self.dir / f"{name}.wav")
            self.files.append((name, self.dir / f"{name}.wav"))
        self.out = self.dir / "results.jsonl"

    def run_batch(self, transcribe):
        with mock.patch.object(wyoming_batch.stt_wyoming, "transcribe", side_effect=transcribe):
            return asyncio.run(wyoming_batch.run("tcp://127.0.0.1:1", "e", self.files, self.out, concurrency=1))

    def test_a_failed_file_is_a_result_line_and_is_retried_on_the_next_run(self):
        async def flaky(_uri, _pcm, _rate, _language, timeout):
            raise ConnectionRefusedError("down")
        self.assertEqual(self.run_batch(flaky), 3)
        self.assertTrue(all(row["err"] for row in wyoming_batch.load(self.out).values()))
        self.assertEqual(wyoming_batch.done_ids(self.out), set())

        async def fine(_uri, _pcm, _rate, _language, timeout):
            return "включи свет"
        self.assertEqual(self.run_batch(fine), 3)
        rows = wyoming_batch.load(self.out)
        self.assertEqual({row["text"] for row in rows.values()}, {"включи свет"})
        self.assertFalse(any(row["err"] for row in rows.values()))

    def test_done_files_are_skipped_and_an_empty_transcript_counts_as_done(self):
        async def silent(_uri, _pcm, _rate, _language, timeout):
            return ""
        self.assertEqual(self.run_batch(silent), 3)
        self.assertEqual(wyoming_batch.done_ids(self.out), {"a", "b", "c"})
        self.assertEqual(self.run_batch(silent), 0)
        self.assertEqual(len(self.out.read_text().splitlines()), 3)

    def test_wrong_audio_format_is_refused(self):
        bad = self.dir / "bad.wav"
        with wave.open(str(bad), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44100)
            handle.writeframes(b"\0" * 400)
        with self.assertRaises(ValueError):
            wyoming_batch.read_wav(bad)

    def test_a_result_line_cut_short_does_not_stop_the_resume(self):
        async def fine(_uri, _pcm, _rate, _language, timeout):
            return "включи свет"
        self.assertEqual(self.run_batch(fine), 3)
        with self.out.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "c", "engine": "e", "te')   # killed mid-write
        self.assertEqual(set(wyoming_batch.load(self.out)), {"a", "b", "c"})
        self.assertEqual(self.run_batch(fine), 0)
        self.assertTrue(self.out.read_text().endswith("\n"))
        self.assertEqual(len(self.out.read_text().splitlines()), 3)

    def test_a_corrupt_line_in_the_middle_is_not_hidden(self):
        self.out.write_text('{"id": "a", "text": "x"}\nnot json\n{"id": "b", "text": "y"}\n')
        with self.assertRaises(ValueError):
            wyoming_batch.load(self.out)

    def test_a_rebuilt_clip_under_the_same_id_is_transcribed_again(self):
        async def fine(_uri, _pcm, _rate, _language, timeout):
            return "включи свет"
        self.assertEqual(self.run_batch(fine), 3)
        with wave.open(str(self.dir / "b.wav"), "wb") as handle:   # same id, other audio
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(struct.pack("<h", 900) * 1600)
        self.assertEqual(self.run_batch(fine), 1)

    def test_results_are_private_to_the_owner_whatever_the_umask(self):
        async def fine(_uri, _pcm, _rate, _language, timeout):
            return "включи свет"
        previous = os.umask(0o022)
        try:
            self.out = self.dir / "fresh" / "results.jsonl"
            self.run_batch(fine)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out.parent.stat().st_mode), 0o700)

    def test_a_result_line_cut_inside_a_cyrillic_character_does_not_stop_the_resume(self):
        async def fine(_uri, _pcm, _rate, _language, timeout):
            return "включи свет"
        self.assertEqual(self.run_batch(fine), 3)
        line = '{"id": "c", "engine": "e", "text": "свет", "ms": 1, "err": null, "sha": null}\n'.encode("utf-8")
        with self.out.open("ab") as handle:
            handle.write(line[:line.index("с".encode("utf-8")) + 1])   # the first byte of «с»: killed in the middle of a character
        self.assertEqual(set(wyoming_batch.load(self.out)), {"a", "b", "c"})
        self.assertEqual(self.run_batch(fine), 0)
        self.assertTrue(self.out.read_bytes().endswith(b"\n"))

    def test_a_truncated_wav_is_that_clips_error_not_the_batchs(self):
        broken = self.dir / "broken.wav"
        broken.write_bytes(b"")
        self.files.append(("broken", broken))
        async def fine(_uri, _pcm, _rate, _language, timeout):
            return "включи свет"
        self.assertEqual(self.run_batch(fine), 4)
        rows = wyoming_batch.load(self.out)
        self.assertTrue(rows["broken"]["err"])
        self.assertEqual([rows[name]["text"] for name in ("a", "b", "c")], ["включи свет"] * 3)

    def test_an_existing_public_results_file_is_tightened(self):
        self.out.write_text("")
        os.chmod(self.out, 0o644)
        async def fine(_uri, _pcm, _rate, _language, timeout):
            return "включи свет"
        self.run_batch(fine)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)

    def test_a_wav_whose_data_is_shorter_than_its_header_says_is_refused(self):
        cut = self.dir / "cut.wav"
        write_wav(cut)
        data = cut.read_bytes()
        cut.write_bytes(data[:len(data) - 1000])       # the header still declares 1600 frames
        with self.assertRaises(ValueError):
            wyoming_batch.read_wav(cut)


if __name__ == "__main__":
    unittest.main()
