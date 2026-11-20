import array
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import bench_asr_archive as bench
import voiceclips

RATE = 16000
PCM = array.array("h", [int(3000 * ((i // 40) % 2 * 2 - 1)) for i in range(RATE)]).tobytes()


class ArchiveBenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = os.environ.get("AISIS_VOICE_ARCHIVE")
        os.environ["AISIS_VOICE_ARCHIVE"] = self.tmp.name + "/archive"
        self.db = self.tmp.name + "/dialogs.sqlite3"
        self.speech = voiceclips.save(self.db, PCM, RATE, {"ts": time.time(), "status": "sent", "transcript": "включи свет"})
        self.gated = voiceclips.save(self.db, PCM, RATE, {"ts": time.time() + 1, "status": "gate", "reason": "short"})

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("AISIS_VOICE_ARCHIVE", None)
        else:
            os.environ["AISIS_VOICE_ARCHIVE"] = self.saved
        self.tmp.cleanup()

    def run_bench(self, engines, **kwargs):
        # decode/lift need numpy and ffmpeg (the benchmark venv); the engines here are fakes that never look at the samples.
        with mock.patch.object(bench, "load_too_high", return_value=None), \
                mock.patch.object(bench, "decode", return_value=[0.0] * RATE), mock.patch.object(bench, "lift", side_effect=lambda wave, voiced_ms: wave):
            return bench.run(self.db, engines, kwargs.get("max_clips", 10), kwargs.get("budget_s", 60))

    def test_hallucinating_engine_is_flagged_and_silent_engine_is_clean(self):
        recognizers = {"plain": lambda wave: "включи свет",
                       "credits": lambda wave: "Субтитры сделал DimaTorzok"}
        with mock.patch.object(bench, "load_engine", side_effect=recognizers.__getitem__):
            self.assertEqual(self.run_bench(["plain", "credits"])["done"], 4)
        out = bench.summary(self.db)
        self.assertEqual(out["clips"], 2)
        self.assertEqual(out["gated"], 1)
        self.assertEqual(out["engines"]["credits"]["pattern"], 2)
        self.assertEqual(out["engines"]["credits"]["text_on_gated"], 1)
        self.assertEqual(out["engines"]["plain"]["pattern"], 0)
        self.assertFalse(out["engines"]["credits"]["eligible"])
        self.assertIn("subtitle-credit captions", out["engines"]["credits"]["blockers"])
        self.assertIn("credits|plain", out["pairwise"])

    def test_human_reference_gives_wer_and_marks_no_speech(self):
        bench.set_reference(self.db, self.speech, "включи свет в кухне")
        bench.set_reference(self.db, self.gated, "")
        with mock.patch.object(bench, "load_engine", return_value=lambda wave: "включи свет"):
            self.run_bench(["fake"])
        engine = bench.summary(self.db)["engines"]["fake"]
        self.assertEqual(engine["ref_clips"], 1)  # a "" reference marks no speech; it counts in text_on_gated, not in WER
        self.assertAlmostEqual(engine["wer_ref"], 0.5)  # two of four words deleted
        self.assertEqual(engine["text_on_gated"], 1)

    def test_only_missing_pairs_are_processed(self):
        with mock.patch.object(bench, "load_engine", return_value=lambda wave: "да"):
            self.assertEqual(self.run_bench(["fake"])["done"], 2)
            self.assertEqual(self.run_bench(["fake"])["done"], 0)

    def test_high_load_skips_the_run_and_records_why(self):
        with mock.patch.object(bench, "load_too_high", return_value=9.5), \
                mock.patch.object(bench, "load_engine") as load:
            result = bench.run(self.db, ["fake"], 10, 60)
        load.assert_not_called()
        self.assertIn("load 9.5", result["skipped"])
        self.assertIn("load 9.5", bench.summary(self.db)["last_run"]["skipped"])

    def test_unavailable_engine_does_not_stop_the_others(self):
        def load(name):
            if name == "broken":
                raise RuntimeError("model missing")
            return lambda wave: "да"
        with mock.patch.object(bench, "load_engine", side_effect=load):
            self.assertEqual(self.run_bench(["broken", "ok"])["done"], 2)
        self.assertEqual(set(bench.summary(self.db)["engines"]), {"ok"})

    def test_activation_clip_is_no_utterance_for_any_engine(self):
        activation = voiceclips.save(self.db, PCM, RATE, {"ts": time.time() + 2, "status": voiceclips.ACTIVATION, "reason": "wake_window"})
        with mock.patch.object(bench, "load_engine", return_value=lambda wave: "алиса"):
            self.assertEqual(self.run_bench(["fake"])["done"], 2)
        out = bench.summary(self.db)
        self.assertEqual(out["clips"], 2)
        self.assertEqual(out["engines"]["fake"]["n"], 2)
        conn = bench.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM asr_bench WHERE clip_id=?", (activation,)).fetchone()[0], 0)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy lives in the benchmark venv, not in the CI image")
    def test_quiet_clip_is_lifted_like_the_gateway_and_loud_one_is_not(self):
        import numpy as np
        quiet = (np.sin(np.arange(RATE) / 5) * 60 / 32768).astype("float32")   # ~-55 dBFS peak, station level
        lifted = bench.lift(quiet, 1000)
        self.assertAlmostEqual(float(np.sqrt((lifted ** 2).mean())), 10 ** (-30 / 20), delta=0.004)  # voiced RMS -30 dBFS
        self.assertLessEqual(float(np.abs(lifted).max()), 10 ** (-2 / 20) + 1e-3)
        loud = (np.sin(np.arange(RATE) / 5) * 0.8).astype("float32")
        np.testing.assert_allclose(bench.lift(loud, 1000), loud, atol=1e-4)
        with mock.patch.dict(os.environ, {"AISIS_ASR_BENCH_LIFT": "0"}):
            self.assertIs(bench.lift(quiet, 1000), quiet)

    def test_loop_detection(self):
        self.assertEqual(bench.flags("да да да да да")[2], 1)
        self.assertEqual(bench.flags("да нет да нет")[2], 0)

    def test_wer(self):
        self.assertEqual(bench.wer("включи свет", "включи свет"), 0.0)
        self.assertAlmostEqual(bench.wer("а б в г", "а б в"), 0.25)


class InstallScriptTests(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "install-asr-bench.sh"

    def test_enabling_the_timer_refuses_a_custom_venv_the_unit_could_not_run(self):
        """The unit's ExecStart is fixed to the default venv: a venv elsewhere would be installed and never used."""
        with tempfile.TemporaryDirectory() as scratch:
            custom = os.path.join(scratch, "venv")
            done = subprocess.run(["bash", str(self.SCRIPT), "--enable"], capture_output=True, text=True,
                                  env={**os.environ, "AISIS_ASR_BENCH_VENV": custom}, timeout=60)
            self.assertEqual(done.returncode, 2)
            self.assertIn("ExecStart", done.stderr)
            self.assertFalse(os.path.exists(custom))


if __name__ == "__main__":
    unittest.main()
