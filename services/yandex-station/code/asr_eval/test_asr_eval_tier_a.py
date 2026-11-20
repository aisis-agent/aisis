import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asr_eval import tier_a  # noqa: E402

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


def caption(text: str) -> bool:
    return "продолжение следует" in text.lower()


def clip(fid, category="silence", kind="nonspeech", seconds=1, reference=""):
    return {"id": fid, "category": category, "kind": kind, "seconds": seconds, "reference": reference}


class PlanTests(unittest.TestCase):
    def test_ids_unique_and_enough_nonspeech(self):
        specs = tier_a.plan_nonspeech()
        self.assertEqual(len({s["id"] for s in specs}), len(specs))
        self.assertGreaterEqual(len(specs), 200)
        self.assertEqual({s["category"] for s in specs}, set(tier_a.SYNTHETIC_CATEGORIES))

    def test_half_second_silence_has_repeats_for_latency(self):
        half = [s for s in tier_a.plan_nonspeech() if s["category"] == "silence" and s["seconds"] == 0.5]
        self.assertEqual(len(half), tier_a.LATENCY_REPEATS)

    def test_speech_and_babble_plans(self):
        phrases = [f"фраза {i}" for i in range(10)]
        self.assertEqual(len(tier_a.plan_speech(phrases)), 10 * 2 * (len(tier_a.TAIL_SECONDS) + 1))   # every tail length plus the control
        self.assertTrue(all(s["reference"] for s in tier_a.plan_speech(phrases)))
        self.assertTrue(all(s["kind"] == "babble" for s in tier_a.plan_babble(phrases)))


class TextTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(tier_a.normalize("Включи, ЁЛКУ!  Свет."), "включи елку свет")

    def test_invented_tail(self):
        self.assertFalse(tier_a.invented_tail("включи свет", "Включи свет.", False))
        self.assertFalse(tier_a.invented_tail("включи свет", "выключи свет", False))
        self.assertTrue(tier_a.invented_tail("включи свет", "Включи свет. Субтитры сделал DimaTorzok", False))
        self.assertTrue(tier_a.invented_tail("включи свет", "включи свет", True))

    def test_control_id_and_spelled_out_numbers_are_not_an_invented_tail(self):
        self.assertEqual(tier_a.control_id("tail-p3-denis-15s"), "tail-p3-denis-0s")
        rows = [clip("tail-p3-denis-0s", "speech-control", "speech_control", None, "температура 22 градуса"),
                clip("tail-p3-denis-5s", "speech-then-silence", "speech_tail", None, "температура 22 градуса")]
        spelled = "температура двадцать два градуса"
        results = {"tail-p3-denis-0s": {"text": spelled, "ms": 5, "err": None}, "tail-p3-denis-5s": {"text": spelled, "ms": 5, "err": None}}
        score = tier_a.score_engine(rows, results, caption)
        self.assertEqual(score["headline"]["tail_invented"], 0)
        self.assertEqual(score["categories"]["speech-then-silence"]["exact"], 1)
        results["tail-p3-denis-5s"]["text"] = spelled + " спасибо"
        self.assertEqual(tier_a.score_engine(rows, results, caption)["headline"]["tail_invented"], 1)

    def test_every_tail_clip_has_a_control_and_the_control_is_not_a_bar_item(self):
        specs = tier_a.plan_speech(["включи свет"])
        ids = {s["id"] for s in specs}
        for spec in specs:
            if spec["kind"] == "speech_tail":
                self.assertIn(tier_a.control_id(spec["id"]), ids)
        self.assertTrue(all(s["kind"] == "speech_control" for s in specs if s["id"].endswith("-0s")))

    def test_station_chime_fixtures_cover_the_ticket_158_band(self):
        chimes = [s for s in tier_a.plan_nonspeech() if s["category"] == "station-chime"]
        self.assertGreaterEqual(len(chimes), 50)
        self.assertEqual({s["params"]["tones"][0] for s in chimes}, set(tier_a.STATION_CHIME_HZ))
        self.assertTrue(all(0.2 <= s["params"]["dur"] <= 0.5 for s in chimes))

    def test_percentile(self):
        self.assertIsNone(tier_a.percentile([], 0.95))
        self.assertEqual(tier_a.percentile([10, 20, 30, 40], 0.95), 40)


class ScoreTests(unittest.TestCase):
    def test_counts_and_pass(self):
        rows = [clip("s1"), clip("s2"), clip("t1", "speech-then-silence", "speech_tail", None, "включи свет"),
                clip("a1", "archive-gated", "nonspeech_archive")]
        results = {"s1": {"text": "", "ms": 5, "err": None}, "s2": {"text": "", "ms": 7, "err": None},
                   "t1": {"text": "Включи свет.", "ms": 9, "err": None}, "a1": {"text": "private", "ms": 1, "err": None}}
        score = tier_a.score_engine(rows, results, caption)
        self.assertEqual(score["headline"], {"synthetic_n": 2, "synthetic_nonempty": 0, "natural_n": 2, "natural_nonempty": 0, "earcon_n": 0, "earcon_nonempty": 0,
                                             "tail_n": 1, "tail_invented": 0, "incomplete": 0, "pass": True})
        self.assertEqual(score["categories"]["speech-then-silence"]["exact"], 1)
        self.assertEqual(score["categories"]["archive-gated"]["nonempty"], 1)
        self.assertNotIn("private", tier_a.format_report(rows, {"e": score}))

    def test_hit_fails_bar_and_is_printed(self):
        rows = [clip("s1"), clip("t1", "speech-then-silence", "speech_tail", None, "включи свет")]
        results = {"s1": {"text": "Продолжение следует.", "ms": 5, "err": None},
                   "t1": {"text": "включи свет продолжение следует", "ms": 5, "err": None}}
        score = tier_a.score_engine(rows, results, caption)
        self.assertFalse(score["headline"]["pass"])
        self.assertEqual(score["categories"]["silence"]["captions"], 1)
        self.assertEqual(score["headline"]["tail_invented"], 1)
        self.assertIn("Продолжение следует.", tier_a.format_report(rows, {"e": score}))

    def test_missing_and_errors_fail_the_bar_and_latency_p95(self):
        rows = [clip("a", seconds=0.5), clip("b", seconds=0.5), clip("c", seconds=0.5)]
        results = {"a": {"text": "", "ms": 100, "err": None}, "b": {"text": "", "ms": 0, "err": "Timeout"}}
        score = tier_a.score_engine(rows, results, caption)
        self.assertEqual(score["headline"]["incomplete"], 2)
        self.assertFalse(score["headline"]["pass"])
        self.assertEqual(score["silence_0_5s_ms_p95"], 100)


@unittest.skipUnless(HAS_NUMPY, "numpy missing")
class AudioTests(unittest.TestCase):
    def render(self, **kw):
        spec = next(s for s in tier_a.plan_nonspeech() if all(s["id"] == v for v in kw.values()))
        return spec, tier_a.render_nonspeech(spec)

    def test_deterministic_and_levels(self):
        import numpy as np
        spec, a = self.render(id="pink-50db-2s")
        self.assertTrue(np.array_equal(a, tier_a.render_nonspeech(spec)))
        rms = 20 * np.log10(np.sqrt(np.mean((a / 32768.0) ** 2)))
        self.assertAlmostEqual(rms, -50, delta=0.3)

    def test_silence_is_digital_and_station_floor_is_lifted(self):
        import numpy as np
        _, silence = self.render(id="silence-3s")
        self.assertFalse(silence.any())
        self.assertEqual(len(silence), 3 * tier_a.RATE)
        _, floor = self.render(id="station-floor-2.0lsb-3s-s0")
        self.assertTrue(np.all(floor % tier_a.FLOOR_LIFT == 0))
        self.assertTrue(floor.any())

    def test_every_category_renders_nonclipped_int16(self):
        import numpy as np
        seen = set()
        for spec in tier_a.plan_nonspeech():
            if spec["category"] in seen:
                continue
            seen.add(spec["category"])
            pcm = tier_a.render_nonspeech(spec)
            self.assertEqual(pcm.dtype, np.dtype("<i2"))
            self.assertGreater(len(pcm), 0)
        self.assertEqual(seen, set(tier_a.SYNTHETIC_CATEGORIES))


if __name__ == "__main__":
    unittest.main()
