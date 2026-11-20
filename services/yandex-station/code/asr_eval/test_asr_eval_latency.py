"""The latency harness against the real gateway protocol: quiet clips, clips with no final, the default work directory, private modes."""
import array
import asyncio
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    import numpy  # noqa: F401  (the gateway's level front-end needs it; these tests switch that front-end off)
except ImportError:
    sys.modules.setdefault("speech_frontend", types.ModuleType("speech_frontend"))   # CI has no numpy: only the import is satisfied

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

import stt_wyoming  # noqa: E402
from asr_eval import latency, private, tier_a, tier_b, tier_c, wyoming_batch  # noqa: E402

RATE = latency.RATE
FRAME = latency.FRAME


class LoudIsSpeech:
    """A VAD stand-in: a frame is speech when its peak is above 1000."""

    def is_speech(self, frame, rate):
        samples = array.array("h")
        samples.frombytes(frame)
        return max(map(abs, samples)) > 1000


def speech(ms, level):
    """Frames whose level rises and falls as speech does (a flat level is rejected as machinery), `level` is the quietest."""
    out = array.array("h")
    for index in range(ms // 20):
        amplitude = level + level * (index % 10) // 3
        out.extend(amplitude if (n // 40) % 2 == 0 else -amplitude for n in range(FRAME))
    return out.tobytes()


def silence(ms):
    return b"\0\0" * (FRAME * (ms // 20))


class VadBoost(unittest.TestCase):
    def test_a_quiet_clip_is_heard_through_the_gateways_boost(self):
        clip = silence(200) + speech(400, 200) + silence(200)   # peaks 200..800: below the stand-in VAD's threshold unless lifted
        self.assertEqual(latency.last_voiced_end(clip, 1.0, LoudIsSpeech()), 0)
        end = latency.last_voiced_end(clip, 6.0, LoudIsSpeech())
        self.assertGreater(end, 0)
        self.assertLessEqual(end, (200 + 400) * RATE * 2 // 1000)
        self.assertGreater(end, 200 * RATE * 2 // 1000)

    def test_the_default_gain_is_the_gateways(self):
        self.assertEqual(latency.gateway_vad_gain(), stt_wyoming.Tuning().vad_gain)


class RealProtocol(unittest.TestCase):
    """`play` against an in-process gateway over a real websocket (real time: about 3-4 s per clip)."""

    def play(self, clip, heard="включи свет", silence_ms=800):
        async def go():
            async def recognizer(uri, pcm, rate, language, **extras):
                return heard
            with tempfile.TemporaryDirectory() as scratch:
                env = {"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_DIALOGS_DB": os.path.join(scratch, "dialogs.sqlite3"),
                       "AISIS_STT_AB_PERCENT": "0", "AISIS_STT_DSP": "0", "AISIS_STT_WAKE_WINDOW": "0", "AISIS_STT_TONE_GUARD": "0",
                       "AISIS_STT_SILENCE_MS": str(silence_ms)}
                gateway = stt_wyoming.STTGateway(env=env, vad_factory=LoudIsSpeech, recognizer=recognizer, verifier=object(), archive=False)
                async with TestServer(gateway.app()) as server:
                    async with aiohttp.ClientSession() as session:
                        return await latency.play(f"ws://127.0.0.1:{server.port}/", clip, session, 6.0, LoudIsSpeech(), silence_ms=silence_ms)
        return asyncio.run(go())

    def test_a_request_has_a_latency_from_its_end(self):
        result = self.play(silence(200) + speech(1000, 1200) + silence(200))
        self.assertEqual(result["text"], "включи свет")
        self.assertGreater(result["latency_ms"], 600)       # the endpoint wait is part of it
        self.assertLess(result["latency_ms"], 2500)

    def test_a_silent_clip_gets_the_final_of_the_stream_end_not_a_timeout(self):
        result = self.play(silence(600))
        self.assertEqual(result, {"text": "", "rejected": None, "no_speech": True})

    def test_a_quiet_request_is_timed_from_its_end_not_from_its_start(self):
        result = self.play(silence(200) + speech(1500, 200) + silence(200))   # peaks 200..800: heard only through the boost
        self.assertEqual(result["text"], "включи свет")
        self.assertLess(result["latency_ms"], 2000)          # unboosted, the end of speech was 0 and the 1.5 s clip counted as latency

    def test_a_long_endpoint_wait_is_measured_not_cut_short_by_the_finish_message(self):
        """With a 3 s endpoint wait the tail must outlast it: a finish sent after 2.5 s would flush at once and under-report the wait."""
        result = self.play(silence(200) + speech(1000, 1200) + silence(200), silence_ms=3000)
        self.assertGreaterEqual(result["latency_ms"], 2900)

    def test_the_tail_outlasts_the_endpoint_wait(self):
        self.assertEqual(latency.tail_seconds(800), 2.5)
        self.assertGreater(latency.tail_seconds(3000), 3.0 + 1.0)


class PrivateModes(unittest.TestCase):
    def test_the_default_work_directory_is_created_with_its_parent(self):
        with tempfile.TemporaryDirectory() as home:
            previous = os.umask(0o022)
            try:
                with private.owner_only():
                    work = latency.default_work_dir(Path(home))
            finally:
                os.umask(previous)
            self.assertTrue(work.is_dir())
            self.assertEqual(work.parent, Path(home) / "asr-eval")
            self.assertEqual(stat.S_IMODE(work.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(work.parent.stat().st_mode), 0o700)

    def test_every_command_line_entry_point_is_owner_only(self):
        for module in (latency, tier_a, tier_b, tier_c, wyoming_batch):
            with self.subTest(module=module.__name__):
                self.assertTrue(hasattr(module.main, "__wrapped__"))

    def test_the_decorator_overrides_a_permissive_umask(self):
        @private.owner_only_cli
        def make(directory):
            (directory / "sub").mkdir()
            (directory / "sub" / "report.txt").write_text("x")
            return 7

        with tempfile.TemporaryDirectory() as scratch:
            previous = os.umask(0o022)
            try:
                self.assertEqual(make(Path(scratch)), 7)
                self.assertEqual(os.umask(0o022), 0o022)      # restored
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE((Path(scratch) / "sub").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((Path(scratch) / "sub" / "report.txt").stat().st_mode), 0o600)

    def test_existing_public_files_are_tightened(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "out.jsonl"
            path.write_text("old")
            os.chmod(path, 0o644)
            private.write_text(path, "new")
            self.assertEqual((path.read_text(), stat.S_IMODE(path.stat().st_mode)), ("new", 0o600))
            os.chmod(path, 0o644)
            with private.open_text(path, "a") as handle:
                handle.write("+")
            self.assertEqual((path.read_text(), stat.S_IMODE(path.stat().st_mode)), ("new+", 0o600))
            private.write_bytes(path, b"x")
            self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), (b"x", 0o600))


if __name__ == "__main__":
    unittest.main()
