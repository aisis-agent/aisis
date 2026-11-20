"""The activation word cut from the audio in the STT gateway: the name never reaches the recognizer, a name-only activation is
never recognised at all, a follow-up turn is never cut, and where the name cannot be told from the request the text-level
strip (wake_word.py) still answers.

The station's delivery is replayed with a clock the test owns: the history (2.32 s the station buffered before its spotter
fired) arrives at t=0, live audio in real time behind it (wake_window.py, gateway journal 2026-11-10 … 11-17).
"""
import array
import asyncio
import os
import sys
import tempfile
import types
import unittest

from aiohttp.test_utils import TestClient, TestServer

try:
    import numpy  # noqa: F401  (the gateway's level front-end needs it; this test switches that front-end off)
except ImportError:
    sys.modules.setdefault("speech_frontend", types.ModuleType("speech_frontend"))   # CI has no numpy: only the import is satisfied

import stt_wyoming
import test_wake_window as layout
import voiceclips

RATE = layout.RATE
HISTORY = layout.HISTORY
NAME_SIGNATURE = range(2900, 3100)       # amplitudes only the name has: a recognizer call whose audio holds one heard the name


class Vad:
    """A VAD stand-in: a frame is speech when it is loud."""

    def is_speech(self, frame, rate):
        samples = array.array("h")
        samples.frombytes(frame)
        return max(map(abs, samples)) > 1000


class CountingVad(Vad):
    """The stateful part of a real VAD, reduced to what matters here: how much audio it has heard."""

    def __init__(self):
        self.heard = 0

    def is_speech(self, frame, rate):
        self.heard += 1
        return super().is_speech(frame, rate)


class HangoverVad(Vad):
    """A VAD that, like webrtcvad, keeps calling frames speech for a few frames after the loud ones end."""

    def __init__(self, frames=6):
        self.frames, self.left = frames, 0

    def is_speech(self, frame, rate):
        if super().is_speech(frame, rate):
            self.left = self.frames
            return True
        self.left = max(0, self.left - 1)
        return self.left > 0 or False


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def speech(ms):
    """Levels of speech: loud, rising and falling (a flat level is rejected as machinery), all above the name's 3000."""
    return [4000.0 + 700.0 * (index % 10) for index in range(ms // 20)]


def activation_stream(name_end_ms=2040, gap_ms=140, request_ms=1200, live_ms=2000):
    """Levels of the station's activation stream (history + live audio): a click, the name at 3000 (its own amplitude), the request at 7000."""
    total = HISTORY + live_ms
    levels = layout.room(total // 20, 0.0)
    levels[0:3] = [3000.0, 3000.0, 3000.0]
    name = [2900.0 + 200.0 * i / 19 for i in range(19)]       # 380 ms at 2900-3100
    start = (name_end_ms - 380) // 20
    levels[start:start + 19] = name
    if gap_ms is not None:
        onset = (name_end_ms + gap_ms) // 20
        levels[onset:onset + request_ms // 20] = speech(request_ms)
    del levels[total // 20:]                                  # a slice assigned past the end would lengthen the stream
    return levels


def squares(levels):
    """16 kHz audio with exactly these amplitudes per frame; the name's frames keep their own amplitude."""
    out = array.array("h")
    for level in levels:
        out.extend(int(level) if (n // 40) % 2 == 0 else -int(level) for n in range(layout.FRAME))
    return out.tobytes()


class PrimedVad(unittest.TestCase):
    """The recogniser's VAD adapts to the stream it hears (webrtcvad's noise model). The audio the wake window cuts away is still
    played to it, or the request that follows the name would meet a VAD that has heard nothing (the real replay lost requests
    that started 0.1 s after the cut)."""

    def test_the_cut_audio_is_heard_by_the_vad_and_leaves_no_trace_in_the_utterances(self):
        vad = CountingVad()
        endpoint = stt_wyoming.Endpoint(vad, RATE, silence_ms=800)
        name = squares(layout.room(50, 0.0)[:20] + [3000.0] * 30)             # 1 s: a quiet start, then loud frames
        endpoint.prime(name)
        self.assertEqual(vad.heard, 50)
        self.assertEqual(endpoint.audio, bytearray(), "primed audio is not part of any utterance")
        self.assertEqual(endpoint.flush(), None)
        endpoint.feed(squares(speech(400)))
        self.assertEqual(len(endpoint.flush(include_partial=True).pcm), len(squares(speech(400))))


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        self.env = {"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_DIALOGS_DB": self.db, "AISIS_STT_AB_PERCENT": "0", "AISIS_STT_DSP": "0",
                    "AISIS_STT_TONE_GUARD": "0"}   # the fake speech is a square wave, which is a tone: tone_guard has its own tests
        previous = os.environ.get("AISIS_VOICE_ARCHIVE")
        os.environ["AISIS_VOICE_ARCHIVE"] = os.path.join(self.tmp.name, "archive")
        self.addCleanup(lambda: os.environ.pop("AISIS_VOICE_ARCHIVE", None) if previous is None
                        else os.environ.__setitem__("AISIS_VOICE_ARCHIVE", previous))

    def session(self, pcm, heard, burst_ms=HISTORY, finish=True, env=None, setup=None, clock_jump=None):
        """Play `pcm` as the station delivers it: the first `burst_ms` at t=0, the rest in real time. Returns (messages, audio the recognizer got, rows)."""
        answers = list(heard)
        calls = []
        clock = Clock()
        jump_chunk, jump_seconds = clock_jump or (-1, 0.0)

        async def recognizer(uri, audio, rate, language, **extras):
            calls.append(audio)
            return answers.pop(0)

        async def play():
            gateway = stt_wyoming.STTGateway(env=dict(self.env, **(env or {})), vad_factory=getattr(self, "vad_factory", Vad), recognizer=recognizer, verifier=object(), clock=clock)
            if setup:
                setup(gateway)
            messages = []
            async with TestClient(TestServer(gateway.app())) as client:
                async with client.ws_connect("/") as ws:
                    await ws.send_json({"sample_rate": RATE})
                    head = RATE * 2 * burst_ms // 1000
                    await ws.send_bytes(pcm[:head])
                    await asyncio.sleep(0.05)
                    step = RATE * 2 * 200 // 1000
                    for index, offset in enumerate(range(head, len(pcm), step)):
                        clock.now += 0.2 + (jump_seconds if index == jump_chunk else 0.0)
                        await ws.send_bytes(pcm[offset:offset + step])
                        await asyncio.sleep(0.05)
                    if finish:
                        await ws.send_json({"type": "finish"})
                    try:
                        while True:
                            messages.append(await asyncio.wait_for(ws.receive_json(), 1.5))
                    except (asyncio.TimeoutError, TypeError):
                        pass
                for _ in range(60):                       # archive writes run in worker threads
                    await asyncio.sleep(0.05)
                    if not gateway.speaker_background:
                        break
            return messages

        messages = asyncio.run(play())
        return messages, calls, self.rows()

    def rows(self):
        if not os.path.exists(self.db):
            return []
        conn = voiceclips.connect(self.db)
        try:
            return conn.execute("SELECT status, reason, transcript FROM voice_clips ORDER BY ts, id").fetchall()
        finally:
            conn.close()

    def heard_the_name(self, audio):
        samples = array.array("h")
        samples.frombytes(audio)
        return any(abs(sample) in NAME_SIGNATURE for sample in samples)

    def test_the_name_in_front_of_a_request_never_reaches_the_recognizer(self):
        pcm = squares(activation_stream())
        messages, calls, rows = self.session(pcm, ["Включи свет в студии."])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.heard_the_name(calls[0]), "the recognizer was handed the name")
        self.assertEqual(messages, [{"text": "Включи свет в студии.", "end_of_utt": True}])
        self.assertEqual([row[0] for row in rows], ["activation", "sent"])
        self.assertEqual(rows[0][1], "wake_window")
        self.assertIsNone(rows[0][2], "the cut audio was never recognised: no transcript to show")

    def test_the_vad_has_heard_the_whole_stream_including_the_cut_audio(self):
        vads = []

        def factory():
            vads.append(CountingVad())
            return vads[-1]

        pcm = squares(activation_stream())
        self.vad_factory = factory
        self.session(pcm, ["Включи свет в студии."])
        self.assertEqual(vads[-1].heard, len(pcm) // (2 * layout.FRAME), "every frame of the stream went through the one VAD")

    def test_a_name_with_nothing_after_it_is_never_recognised_and_the_turn_ends_as_an_unanswered_window(self):
        pcm = squares(activation_stream(gap_ms=None))
        messages, calls, rows = self.session(pcm, [])
        self.assertEqual(calls, [])
        self.assertEqual(messages, [{"text": "", "end_of_utt": True, "rejected": "wake_word"}])
        self.assertEqual([(row[0], row[1], row[2]) for row in rows], [("activation", "wake_window", None)])

    def test_a_follow_up_turn_is_never_cut(self):
        """No wake word, so no history: the first word of the answer is whatever the speaker says first."""
        levels = layout.room(200, 0.0)
        levels[20:80] = speech(1200)
        pcm = squares(levels)
        messages, calls, rows = self.session(pcm, ["Да."], burst_ms=200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(messages, [{"text": "Да.", "end_of_utt": True}])
        self.assertEqual([row[0] for row in rows], ["sent"])

    def test_a_request_run_into_the_name_is_cut_by_the_text_as_before(self):
        pcm = squares(activation_stream(gap_ms=0))
        messages, calls, rows = self.session(pcm, ["Алиса, включи свет в студии."])
        self.assertEqual(len(calls), 1)
        self.assertEqual(messages, [{"text": "Включи свет в студии.", "end_of_utt": True}])
        self.assertEqual(rows, [("gate", "short", None), ("sent", "wake_cut:Алиса", "Включи свет в студии.")])   # the click, then name and request in one cut

    def test_a_one_word_request_after_the_cut_name_is_not_dropped_as_a_blip(self):
        """The pair was one utterance of 380 + 160 ms of speech before the cut; the request alone is under the 200 ms voiced minimum."""
        pcm = squares(activation_stream(request_ms=160))
        messages, calls, rows = self.session(pcm, ["Стоп."])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.heard_the_name(calls[0]))
        self.assertEqual(messages, [{"text": "Стоп.", "end_of_utt": True}])
        self.assertEqual([row[0] for row in rows], ["activation", "sent"])

    def test_the_name_lead_is_counted_by_the_gate_and_heard_by_the_speaker_check_only(self):
        utt = stt_wyoming.Utterance(pcm=bytes(640), rate=RATE, voiced=8, trailing_ms=0, rms=1, peak=1, voiced_rms=4000.0, floor_rms=None, swing=9.0)
        tuning = stt_wyoming.Tuning.from_env({})
        self.assertEqual(stt_wyoming.judge(utt, tuning), "short")
        led = stt_wyoming.replace(utt, lead_pcm=b"\x01\x00" * 8, lead_voiced_ms=380)
        self.assertIsNone(stt_wyoming.judge(led, tuning))
        self.assertEqual(led.speaker_pcm, b"\x01\x00" * 8 + utt.speech_pcm)
        self.assertEqual(led.speech_pcm, utt.speech_pcm, "the recognizer's and the archive's audio stays the request alone")

    def test_the_speaker_check_hears_the_name_the_recognizer_does_not(self):
        """Enforce mode judges the voice on at least a second of audio; the request alone may be shorter than that."""
        heard = []

        def spy(gateway):
            async def check(pcm, rate):
                heard.append(pcm)
                return None
            gateway.speaker_check = check
        pcm = squares(activation_stream(request_ms=400))
        messages, calls, rows = self.session(pcm, ["Выключи свет."], setup=spy)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.heard_the_name(calls[0]))
        self.assertEqual(len(heard), 1)
        self.assertTrue(self.heard_the_name(heard[0]))
        self.assertGreater(len(heard[0]), len(calls[0]))

    def test_the_vad_hangover_right_after_the_cut_does_not_take_the_name_credit_meant_for_the_request(self):
        """A pause longer than the Endpoint's silence closes the hangover frames as an utterance of their own: it is a blip. The request
        that follows a second later is 60 ms of loud frames plus the VAD's own hangover, 160 ms: it passes only with the name's credit,
        and its speaker check hears the name."""
        heard = []

        def spy(gateway):
            async def check(pcm, rate):
                heard.append(pcm)
                return None
            gateway.speaker_check = check
        self.vad_factory = HangoverVad
        pcm = squares(activation_stream(request_ms=60, gap_ms=1000, live_ms=3000))
        messages, calls, rows = self.session(pcm, ["Стоп."], setup=spy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(messages, [{"text": "Стоп.", "end_of_utt": True}])
        self.assertEqual([row[:2] for row in rows], [("activation", "wake_window"), ("gate", "short"), ("sent", None)])
        self.assertEqual(len(heard), 1)
        self.assertTrue(self.heard_the_name(heard[0]))

    def test_an_utterance_with_less_speech_than_the_vad_hangover_does_not_get_the_name(self):
        """100 ms of speech after the cut (5 frames) is what a hangover alone can make: no credit, dropped as a blip."""
        pcm = squares(activation_stream(request_ms=100))
        messages, calls, rows = self.session(pcm, [])
        self.assertEqual(calls, [])
        self.assertIn(("gate", "short"), [row[:2] for row in rows])

    def test_noise_the_gate_rejects_does_not_use_up_the_name_credit(self):
        """600 ms of flat loud noise after the name is rejected (steady noise) whatever the credit; the request seconds later still has the name."""
        levels = activation_stream(request_ms=140, gap_ms=2500, live_ms=4500)
        noise = HISTORY // 20 + 20
        levels[noise:noise + 30] = [5000.0] * 30
        messages, calls, rows = self.session(squares(levels), ["Стоп."])
        self.assertEqual(len(calls), 1)
        self.assertEqual(messages, [{"text": "Стоп.", "end_of_utt": True}])
        self.assertIn(("gate", "steady_noise"), [row[:2] for row in rows])

    def test_a_one_word_request_five_seconds_after_the_name_still_has_the_name(self):
        """Past any wall-clock-measured limit: the age is stream audio after the cut."""
        heard = []

        def spy(gateway):
            async def check(pcm, rate):
                heard.append(pcm)
                return None
            gateway.speaker_check = check
        pcm = squares(activation_stream(request_ms=140, gap_ms=5000, live_ms=6500))
        messages, calls, rows = self.session(pcm, ["Стоп."], setup=spy)
        self.assertEqual(len(calls), 1)
        self.assertEqual(messages, [{"text": "Стоп.", "end_of_utt": True}])
        self.assertTrue(self.heard_the_name(heard[0]))

    def test_a_wall_clock_jump_between_chunks_does_not_expire_the_name(self):
        """The age counts stream audio, not the clock: a 30 s jump (stalled loop, NTP step) between chunks, after the name is cut, leaves the name credit intact."""
        heard = []

        def spy(gateway):
            async def check(pcm, rate):
                heard.append(pcm)
                return None
            gateway.speaker_check = check
        pcm = squares(activation_stream(request_ms=140, gap_ms=2500, live_ms=4000))
        messages, calls, rows = self.session(pcm, ["Стоп."], setup=spy, clock_jump=(4, 30.0))
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.heard_the_name(calls[0]), "the name was not cut: the jump came before the decision")
        self.assertEqual(messages, [{"text": "Стоп.", "end_of_utt": True}])
        self.assertTrue(self.heard_the_name(heard[0]))
        self.assertEqual(rows, [("activation", "wake_window", None), ("sent", None, "Стоп.")])

    def test_a_short_sound_long_after_the_name_does_not_get_it(self):
        """A knock eight seconds after «Алиса» is the 140 ms blip it is: no credit, dropped."""
        pcm = squares(activation_stream(request_ms=140, gap_ms=8000, live_ms=9500))
        messages, calls, rows = self.session(pcm, [])
        self.assertEqual(calls, [])
        self.assertIn(("gate", "short"), [row[:2] for row in rows])

    def test_the_cut_can_be_switched_off(self):
        pcm = squares(activation_stream())
        messages, calls, rows = self.session(pcm, ["Алиса, включи свет в студии."], env={"AISIS_STT_WAKE_WINDOW": "0"})
        self.assertTrue(self.heard_the_name(calls[0]))
        self.assertEqual(rows, [("gate", "short", None), ("sent", "wake_cut:Алиса", "Включи свет в студии.")])

    def test_a_stream_that_ends_right_after_the_history_is_recognised_whole(self):
        """The station's finish arrives before the decision could be made: nothing is cut on a guess."""
        pcm = squares(activation_stream(live_ms=0))
        messages, calls, rows = self.session(pcm, ["Алиса, включи свет."], burst_ms=HISTORY)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
