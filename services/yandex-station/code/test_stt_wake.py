"""The activation word (wake word) in the STT gateway: a segment that is only the name is an activation event (never a
request), the name is cut from the head of a request, and a request that follows an activation stays in the same
gateway session (so quasar's dialog and the clip feed see one conversation).

The real pair behind the tests (clip feed tg#30176 / tg#30177, 2026-11-17 16:03): the speaker said «Алиса», paused, then
asked. Whisper heard «Малис.» for the first segment; the gateway sent it on as the whole request, the grammar's
catch-all answered «Не расслышал. Повтори», and the real request arrived in a second session.
"""
import array
import asyncio
import math
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
import voiceclips

RATE = 16000
FRAME = RATE // 50                      # 20 ms of samples


class LoudIsSpeech:
    """A VAD stand-in: a frame is speech when it is loud."""

    def is_speech(self, frame, rate):
        samples = array.array("h")
        samples.frombytes(frame)
        return max(map(abs, samples)) > 1000


def speech(ms):
    """Loud frames whose level rises and falls, as speech does (a flat level is rejected as machinery)."""
    frames = ms // 20
    out = array.array("h")
    for index in range(frames):
        amplitude = 2000 + 600 * (index % 10)
        out.extend(amplitude if (n // 40) % 2 == 0 else -amplitude for n in range(FRAME))
    return out.tobytes()


def silence(ms):
    return b"\0\0" * (FRAME * (ms // 20))


class GatewayReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "dialogs.sqlite3")
        self.env = {"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_DIALOGS_DB": self.db, "AISIS_STT_AB_PERCENT": "0", "AISIS_STT_DSP": "0",
                    "AISIS_STT_WAKE_WINDOW": "0",   # these sessions test the text-level strip on a real clock: the audio cut stays out of it
                    "AISIS_STT_TONE_GUARD": "0"}    # the fake speech is a square wave, which is a tone: tone_guard has its own tests
        previous = os.environ.get("AISIS_VOICE_ARCHIVE")
        os.environ["AISIS_VOICE_ARCHIVE"] = os.path.join(self.tmp.name, "archive")
        self.addCleanup(lambda: os.environ.pop("AISIS_VOICE_ARCHIVE", None) if previous is None
                        else os.environ.__setitem__("AISIS_VOICE_ARCHIVE", previous))

    def run_session(self, script, heard, finish=True, slow_first=0.0, pause=0.15):
        """Play `script` (bytes, with a pause after each part) into one websocket session. `heard` is what the recognizer
        answers, in order. Returns (messages the gateway sent, recognizer calls, clip rows)."""
        answers = list(heard)
        calls = []

        async def recognizer(uri, pcm, rate, language, **extras):
            calls.append(len(pcm))
            if len(calls) == 1:
                await asyncio.sleep(slow_first)
            return answers.pop(0)

        async def play():
            gateway = stt_wyoming.STTGateway(env=self.env, vad_factory=LoudIsSpeech, recognizer=recognizer, verifier=object())
            messages = []
            async with TestClient(TestServer(gateway.app())) as client:
                async with client.ws_connect("/") as ws:
                    await ws.send_json({"sample_rate": RATE})
                    for part in script:
                        part, wait = part if isinstance(part, tuple) else (part, pause)
                        for offset in range(0, len(part), 6400):
                            await ws.send_bytes(part[offset:offset + 6400])
                        await asyncio.sleep(wait)             # the recognizer task of a cut runs before the next one
                    if finish:
                        await ws.send_json({"type": "finish"})
                    try:
                        while True:
                            message = await asyncio.wait_for(ws.receive_json(), 1.5)
                            messages.append(message)
                    except (asyncio.TimeoutError, TypeError):
                        pass
                for _ in range(60):                           # archive writes run in worker threads
                    await asyncio.sleep(0.05)
                    if len(self.rows()) >= len([1 for part in script if len(part[0] if isinstance(part, tuple) else part) >= FRAME * 10]) and not gateway.speaker_background:
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

    def test_the_activation_chime_never_reaches_the_recognizer(self):
        """Ticket #158: the station's own 313 Hz chime in its microphone is a gate reject, not a request for the recognizer."""
        self.env["AISIS_STT_TONE_GUARD"] = "1"
        chime = array.array("h", (int(3000 * math.sin(2 * math.pi * 313 * n / RATE)) for n in range(RATE * 3 // 10))).tobytes()
        messages, calls, rows = self.run_session([chime + silence(900)], [])
        self.assertEqual(calls, [])
        self.assertEqual(rows, [("gate", "tone", None)])
        self.assertEqual([message["text"] for message in messages if message.get("text")], [])

    def test_shadow_mode_counts_the_chime_and_still_recognizes_it(self):
        """Before enforcing, the guard only logs what it would drop: the cut still goes to the recognizer, so a lost request costs nothing."""
        self.env["AISIS_STT_TONE_GUARD"] = "shadow"
        chime = array.array("h", (int(3000 * math.sin(2 * math.pi * 313 * n / RATE)) for n in range(RATE * 3 // 10))).tobytes()
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            messages, calls, rows = self.run_session([chime + silence(900)], ["Громкость."])
        self.assertEqual(len(calls), 1)
        self.assertEqual([row[0] for row in rows], ["sent"])
        self.assertEqual(len([line for line in logs.output if "stt_tone_shadow would_drop" in line]), 1)

    def test_shadow_mode_is_silent_for_speech(self):
        self.env["AISIS_STT_TONE_GUARD"] = "shadow"
        with self.assertLogs("aisis.stt", level="INFO") as logs:
            self.run_session([speech(1500) + silence(900)], ["включи свет"])
        self.assertEqual([line for line in logs.output if "stt_tone_shadow" in line], [])

    def test_the_real_pair_is_one_request_with_no_question_in_between(self):
        """tg#30176 + tg#30177: a click, «Малис» (the name), a pause, then the request."""
        script = [speech(80) + silence(900), speech(260) + silence(900), speech(1900) + silence(900)]
        messages, calls, rows = self.run_session(script, ["Малис.", "Напомню, выключить грелку через десять минут."])
        self.assertEqual(messages, [{"text": "Напомню, выключить грелку через десять минут.", "end_of_utt": True}])
        self.assertEqual(len(calls), 2)
        self.assertEqual(rows, [("gate", "short", None), ("activation", "wake", "Малис."),
                                ("sent", None, "Напомню, выключить грелку через десять минут.")])

    def test_the_name_in_front_of_a_request_is_cut_and_recorded(self):
        script = [speech(1500) + silence(900)]
        messages, _, rows = self.run_session(script, ["Алиса, включи свет в студии."])
        self.assertEqual(messages, [{"text": "Включи свет в студии.", "end_of_utt": True}])
        self.assertEqual(rows, [("sent", "wake_cut:Алиса", "Включи свет в студии.")])

    def test_a_request_without_the_name_is_untouched(self):
        messages, _, rows = self.run_session([speech(1500) + silence(900)], ["Включи свет в студии."])
        self.assertEqual(messages, [{"text": "Включи свет в студии.", "end_of_utt": True}])
        self.assertEqual(rows, [("sent", None, "Включи свет в студии.")])

    def test_a_name_nobody_followed_ends_the_turn_silently(self):
        """No request after the name: no reprompt text, no «Слушаю», the station's turn closes like an unanswered window."""
        messages, _, rows = self.run_session([speech(400) + silence(900)], ["Алиса."])
        self.assertEqual(messages, [{"text": "", "end_of_utt": True, "rejected": "wake_word"}])
        self.assertEqual(rows, [("activation", "wake", "Алиса.")])

    def test_a_name_then_the_end_of_the_stream_does_not_lose_a_late_request(self):
        """The request that ends with the station's finish (no VAD cut yet) is still the session's one final."""
        messages, _, rows = self.run_session([speech(400) + silence(900), speech(1500)], ["Лисы.", "Какая погода?"])
        self.assertEqual(messages, [{"text": "Какая погода?", "end_of_utt": True}])
        self.assertEqual([row[0] for row in rows], ["activation", "sent"])

    def test_an_empty_recognition_does_not_end_the_turn_over_a_waiting_request(self):
        script = [speech(400) + silence(900), speech(300) + silence(900), speech(1500) + silence(900)]
        messages, calls, _ = self.run_session(script, ["Алиса.", "", "Какая погода?"], slow_first=0.6, pause=0)
        self.assertEqual(messages, [{"text": "Какая погода?", "end_of_utt": True}])
        self.assertEqual(len(calls), 3)

    def test_noise_cut_behind_a_slow_recognition_never_pushes_the_request_out(self):
        clicks = [speech(80) + silence(900)] * (stt_wyoming.MAX_HELD + 2)
        script = [speech(400) + silence(900), speech(1500) + silence(900)] + clicks
        messages, calls, _ = self.run_session(script, ["Алиса.", "Какая погода?"], slow_first=0.6, pause=0)
        self.assertEqual(messages, [{"text": "Какая погода?", "end_of_utt": True}])
        self.assertEqual(len(calls), 2)

    def test_overflow_behind_a_slow_recognition_drops_the_latest_cuts_not_the_request(self):
        fillers = [speech(400) + silence(900)] * (stt_wyoming.MAX_HELD + 2)
        script = [speech(400) + silence(900), speech(1500) + silence(900)] + fillers
        messages, _, _ = self.run_session(script, ["Алиса.", "Какая погода?"] + ["Спасибо."] * 10, slow_first=0.6, pause=0)
        self.assertEqual(messages, [{"text": "Какая погода?", "end_of_utt": True}])

    def test_a_busy_recognizer_does_not_make_the_gateway_hoard_audio(self):
        """A stream of cuts behind one slow recognition keeps at most MAX_HELD of them; the rest are dropped, not queued."""
        script = [speech(400) + silence(900)] * 14
        messages, calls, _ = self.run_session(script, ["Алиса."] * 30, slow_first=1.5, pause=0, finish=False)
        self.assertEqual(messages, [])
        self.assertLessEqual(len(calls), 1 + stt_wyoming.MAX_HELD)
        self.assertGreater(len(calls), 1)

    def test_a_name_cut_by_the_idle_timeout_keeps_the_turn_open(self):
        """The stream stalls for the receive timeout after the name (no VAD cut yet): still no final, the request follows."""
        script = [(speech(400) + silence(100), 1.6), speech(1500) + silence(900)]
        messages, calls, rows = self.run_session(script, ["Алиса.", "Какая погода?"])
        self.assertEqual(messages, [{"text": "Какая погода?", "end_of_utt": True}])
        self.assertEqual(len(calls), 2)
        self.assertEqual([row[0] for row in rows], ["activation", "sent"])

    def test_a_request_cut_while_the_name_is_still_recognised_is_kept(self):
        """A busy recognizer: the request is cut by the VAD before the answer for the name came back."""
        script = [speech(400) + silence(900), speech(1500) + silence(900)]
        messages, calls, rows = self.run_session(script, ["Алиса.", "Какая погода?"], slow_first=0.6, pause=0)
        self.assertEqual(messages, [{"text": "Какая погода?", "end_of_utt": True}])
        self.assertEqual(len(calls), 2)
        self.assertEqual([row[0] for row in rows], ["activation", "sent"])

    def test_the_end_of_the_stream_during_the_names_recognition_keeps_the_request(self):
        """The station's finish arrives while the name is still being recognised; what it had buffered is the request."""
        script = [speech(400) + silence(900), speech(1500)]
        messages, _, rows = self.run_session(script, ["Алиса.", "Какая погода?"], slow_first=0.6, pause=0)
        self.assertEqual(messages, [{"text": "Какая погода?", "end_of_utt": True}])
        self.assertEqual([row[0] for row in rows], ["activation", "sent"])


if __name__ == "__main__":
    unittest.main()
