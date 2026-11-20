"""The station's activation chime (a 0.2-0.5 s tone near 313 Hz, ticket #158) and other earcons never reach the recognizer; speech does.

Fixtures are synthetic and stdlib-only (the gateway's CI has no numpy): tones with room noise, and a speech stand-in made of pitch
harmonics under moving formant peaks and a syllable envelope, which is what a narrow-band test must not mistake for a tone.
"""
import array
import math
import random
import sys
import time
import types
import unittest

try:
    import numpy  # noqa: F401  (the gateway's level front-end needs it)
except ImportError:
    sys.modules.setdefault("speech_frontend", types.ModuleType("speech_frontend"))   # CI has no numpy: only the import is satisfied

import stt_wyoming
import tone_guard

RATE = 16000


def pcm_of(samples):
    out = array.array("h", (max(-32768, min(32767, int(s))) for s in samples))
    return out.tobytes()


def rms(samples):
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def silence(ms):
    return [0.0] * (RATE * ms // 1000)


def noise(ms, level, seed):
    rng = random.Random(seed)
    return [rng.gauss(0, level) for _ in range(RATE * ms // 1000)]


def tone(ms, hz_list, level, seed=1, floor=4.0):
    """A chime: partials at `hz_list` with a 10 ms attack and decay, over a station-like noise floor."""
    count = RATE * ms // 1000
    rng = random.Random(seed)
    ramp = RATE // 100
    out = []
    for i in range(count):
        envelope = min(1.0, i / ramp, (count - 1 - i) / ramp)
        value = sum(math.sin(2 * math.pi * hz * i / RATE) for hz in hz_list) / len(hz_list)
        out.append(level * envelope * value + rng.gauss(0, floor))
    return out


def speechlike(ms, seed=1, f0=120.0, level=3000.0):
    """Voiced speech stand-in: f0 gliding ±25 %, 40 harmonics shaped by two formants that move per syllable, 4 syllables a second."""
    rng = random.Random(seed)
    count = RATE * ms // 1000
    phase = 0.0
    out = []
    formants = [(rng.uniform(300, 800), rng.uniform(1100, 2400)) for _ in range(8)]
    for i in range(count):
        t = i / RATE
        pitch = f0 * (1 + 0.25 * math.sin(2 * math.pi * 1.7 * t + seed))
        phase += 2 * math.pi * pitch / RATE
        f1, f2 = formants[int(t * 4) % len(formants)]
        syllable = 0.5 - 0.5 * math.cos(2 * math.pi * 4 * t)
        value = 0.0
        for h in range(1, 41):
            hz = h * pitch
            if hz > 3800:
                break
            gain = math.exp(-((hz - f1) / 160.0) ** 2) + 0.6 * math.exp(-((hz - f2) / 260.0) ** 2) + 0.05
            value += gain * math.sin(h * phase) / h ** 0.5
        out.append(level * syllable * value / 3 + rng.gauss(0, 4))
    return out


class ToneGuardTest(unittest.TestCase):
    def test_station_chime_is_a_tone_with_its_noise_floor(self):
        for hz in (313, 316):
            for ms in (200, 350, 500):
                for level in (150, 600, 3000):
                    cut = silence(300) + tone(ms, [hz], level) + silence(300)
                    self.assertTrue(tone_guard.is_tone(pcm_of(cut), RATE), (hz, ms, level))

    def test_two_tone_earcon_is_a_tone(self):
        for pair in ((880, 1320), (660, 990)):
            self.assertTrue(tone_guard.is_tone(pcm_of(silence(200) + tone(400, pair, 1500) + silence(200)), RATE), pair)

    def test_speech_is_not_a_tone(self):
        for seed in range(1, 9):
            for ms in (250, 400, 600):
                cut = silence(200) + speechlike(ms, seed=seed, f0=95 + 20 * seed) + silence(200)
                self.assertFalse(tone_guard.is_tone(pcm_of(cut), RATE), (seed, ms))

    def test_noise_is_not_a_tone(self):
        self.assertFalse(tone_guard.is_tone(pcm_of(noise(400, 300, 5)), RATE))

    def test_a_request_run_into_the_chime_is_never_dropped(self):
        for gap_ms in (0, 100, 200, 300):
            cut = tone(300, [313], 600) + silence(gap_ms) + speechlike(900, seed=3)
            self.assertFalse(tone_guard.is_tone(pcm_of(cut), RATE), gap_ms)

    def test_a_short_quiet_request_after_a_loud_chime_is_never_dropped(self):
        """The cut is ≤ 600 ms audible in all, but the request is 20-25 dB under the chime that precedes it."""
        chime = tone(300, [313], 3000)
        request = speechlike(250, seed=4)
        for share in (0.04, 0.06, 0.09):
            scale = share * rms(chime) / rms(request)
            cut = silence(100) + chime + [s * scale for s in request] + silence(100)
            self.assertFalse(tone_guard.is_tone(pcm_of(cut), RATE), share)

    def test_a_quiet_request_right_after_the_chime_with_no_silence_around_it_is_never_dropped(self):
        """A native finish can end the cut on the request: there are no quiet frames at all, and a floor taken from the cut (or from
        frames the VAD missed) would drop the request from the audible span."""
        chime = tone(300, [313], 3000)
        request = speechlike(200, seed=4)
        for share in (0.03, 0.06, 0.1):
            scale = share * rms(chime) / rms(request)
            cut = chime + [s * scale for s in request]
            self.assertFalse(tone_guard.is_tone(pcm_of(cut), RATE), share)

    def test_a_short_reply_off_the_window_grid_is_never_dropped(self):
        """The cut's length is not a multiple of the 100 ms window: the suffix must still be examined (the reply is 20-25 dB under the chime)."""
        for chime_ms in (280, 310, 340, 370, 400):
            chime = tone(chime_ms, [313], 3000)
            for reply_ms in (120, 150, 200):
                request = speechlike(reply_ms, seed=reply_ms)
                scale = 0.06 * rms(chime) / rms(request)
                cut = chime + [s * scale for s in request]
                self.assertFalse(tone_guard.is_tone(pcm_of(cut), RATE), (chime_ms, reply_ms))

    def test_a_chime_is_a_tone_at_every_length(self):
        for length in range(100, 600, 30):
            pcm = pcm_of(silence(100) + tone(length, [313], 1500) + silence(100))
            self.assertTrue(tone_guard.is_tone(pcm, RATE), length)

    def test_the_vad_hangover_does_not_hide_a_chime_from_the_check(self):
        """A 500 ms chime plus the VAD's hangover counts 620 ms of voiced audio: still examined, still a tone."""
        utt = utterance(tone(500, [313], 1500) + tone(120, [313], 0))
        self.assertEqual(utt.voiced_ms, 620)
        self.assertEqual(stt_wyoming.judge(utt, ENFORCE), "tone")

    def test_a_long_steady_note_is_left_to_the_other_gates(self):
        self.assertFalse(tone_guard.is_tone(pcm_of(tone(1500, [440], 2000)), RATE))

    def test_unusable_input_answers_false(self):
        self.assertFalse(tone_guard.is_tone(b"", RATE))
        self.assertFalse(tone_guard.is_tone(pcm_of(silence(400)), RATE))
        self.assertFalse(tone_guard.is_tone(pcm_of(tone(300, [313], 600)), 22050))

    def test_the_check_is_cheap(self):
        pcm = pcm_of(silence(300) + tone(500, [313], 600) + silence(800))
        started = time.perf_counter()
        for _ in range(5):
            tone_guard.is_tone(pcm, RATE)
        self.assertLess((time.perf_counter() - started) / 5, 0.15)


def utterance(samples):
    pcm = pcm_of(samples)
    frames = len(samples) // (RATE // 50)
    return stt_wyoming.Utterance(pcm=pcm, rate=RATE, voiced=frames, trailing_ms=0, rms=300, peak=2000, voiced_rms=300.0, floor_rms=4.0, swing=3.0)


ENFORCE = stt_wyoming.Tuning.from_env({"AISIS_STT_TONE_GUARD": "enforce"})


class JudgeTest(unittest.TestCase):
    def test_a_chime_is_rejected_before_the_recognizer(self):
        utt = utterance(tone(300, [313], 600))
        self.assertEqual(stt_wyoming.judge(utt, ENFORCE), "tone")

    def test_the_default_is_shadow_and_drops_nothing(self):
        utt = utterance(tone(300, [313], 600))
        tuning = stt_wyoming.Tuning.from_env({})
        self.assertEqual(tuning.tone_guard, "shadow")
        self.assertEqual(stt_wyoming.Tuning().tone_guard, "shadow")
        self.assertTrue(stt_wyoming.tone_found(utt))
        self.assertIsNone(stt_wyoming.judge(utt, tuning))

    def test_the_guard_can_be_switched_off(self):
        utt = utterance(tone(300, [313], 600))
        tuning = stt_wyoming.Tuning.from_env({"AISIS_STT_TONE_GUARD": "0"})
        self.assertEqual(tuning.tone_guard, "off")
        self.assertIsNone(stt_wyoming.judge(utt, tuning))

    def test_mode_spellings(self):
        for raw, mode in [("1", "enforce"), ("on", "enforce"), (" Enforce ", "enforce"), ("0", "off"), ("OFF", "off"),
                          ("shadow", "shadow"), ("", "shadow"), ("maybe", "shadow"), (None, "shadow")]:
            with self.subTest(raw=raw):
                self.assertEqual(tone_guard.parse_mode(raw, "shadow"), mode)

    def test_speech_passes(self):
        utt = utterance(speechlike(400, seed=2))
        self.assertIsNone(stt_wyoming.judge(utt, ENFORCE))
        self.assertFalse(stt_wyoming.tone_found(utt))

    def test_a_cut_longer_than_a_chime_is_not_examined(self):
        utt = utterance(tone(900, [313], 600))
        self.assertIsNone(stt_wyoming.judge(utt, ENFORCE))
        self.assertFalse(stt_wyoming.tone_found(utt))


if __name__ == "__main__":
    unittest.main()
