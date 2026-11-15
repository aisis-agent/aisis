"""Two speakers hear one utterance: the STT gateway answers it once (utterance_arbiter wired into stt_wyoming)."""
from __future__ import annotations

import array
import asyncio
import math
import sys
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).parent))

import stt_wyoming
from stt_wyoming import HEARD_ELSEWHERE, STTGateway

RATE = 16000
FRAME = RATE // 50                      # 20 ms
SENTENCE = "Алиса, включи кондиционер на втором этаже на обогрев."


class LoudVad:
    """webrtcvad stand-in: a frame is speech when its peak is clearly above the room noise."""

    def is_speech(self, frame, rate):
        samples = array.array("h")
        samples.frombytes(frame)
        return max(map(abs, samples)) > 1000


def frames(amplitude, count, ramp=True):
    out = []
    for index in range(count):
        level = amplitude * (0.4 + 0.6 * index / max(count - 1, 1)) if ramp else amplitude
        out.append(array.array("h", [int(level * math.sin(2 * math.pi * 300 * n / RATE)) for n in range(FRAME)]).tobytes())
    return out


LONG_UTTERANCE_BYTES = 40 * FRAME * 2 + 2 * FRAME * 2   # what the recognizer receives is longer than this for 24 speech frames


def utterance(amplitude, speech_frames=20):
    """20 quiet room frames, `speech_frames` speech frames (under 500 ms), 25 quiet frames (500 ms) so the VAD cut closes it."""
    return frames(30, 20, ramp=False) + frames(amplitude, speech_frames) + frames(30, 25, ramp=False)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AISIS_CONFIG_DIR", str(tmp_path))


def gateway(recognizer, **env):
    gate = STTGateway(env={"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_STT_SILENCE_MS": "400", **env},
                      vad_factory=LoudVad, recognizer=recognizer, archive=False)
    gate.speaker_settings = lambda: None
    return gate


async def speak(client, amplitude, start_after=0.0, speech_frames=20):
    """One speaker: connect, stream one utterance, return the final the gateway sent."""
    await asyncio.sleep(start_after)
    async with client.ws_connect("/") as ws:
        await ws.send_json({"sample_rate": RATE})
        for chunk in utterance(amplitude, speech_frames):
            await ws.send_bytes(chunk)
            await asyncio.sleep(0.001)
        await ws.send_json({"type": "finish"})                  # the station's end of stream, as in production
        return await ws.receive_json(timeout=8)


def scenario(gate, *speakers):
    async def run():
        server = TestServer(gate.app())
        await server.start_server()
        try:
            async with ClientSession(base_url=server.make_url("/")) as client:
                return await asyncio.gather(*(speak(client, *speaker) for speaker in speakers))
        finally:
            await server.close()
    return asyncio.run(run())


def test_one_speaker_is_answered_as_before():
    async def recognizer(uri, pcm, rate, language, **extras):
        return SENTENCE
    (final,) = scenario(gateway(recognizer), (6000,))
    assert final["text"] == SENTENCE
    assert "rejected" not in final


def test_two_speakers_hearing_one_utterance_produce_one_final_with_text():
    heard = []

    async def recognizer(uri, pcm, rate, language, **extras):
        heard.append(len(pcm))
        return SENTENCE

    far, near = scenario(gateway(recognizer), (2500,), (9000, 0.03))
    assert len(heard) == 2                                   # both sessions were recognized, only one may act
    assert near["text"] == SENTENCE and "rejected" not in near
    assert far == {"text": "", "end_of_utt": True, "rejected": HEARD_ELSEWHERE}


def test_a_slow_recognizer_on_the_louder_speaker_still_decides_by_level():
    calls = []

    async def recognizer(uri, pcm, rate, language, **extras):
        calls.append(1)
        if len(calls) == 1:
            await asyncio.sleep(0.4)                         # the first recognition to start is the slow one
        return SENTENCE

    near, far = scenario(gateway(recognizer), (9000,), (2500, 0.03))
    assert near["text"] == SENTENCE
    assert far["rejected"] == HEARD_ELSEWHERE


def test_two_different_commands_at_the_same_moment_are_both_executed():
    async def recognizer(uri, pcm, rate, language, **extras):
        return "Алиса, поставь таймер на десять минут" if len(pcm) > LONG_UTTERANCE_BYTES else "Алиса, выключи свет на кухне"

    kitchen, bedroom = scenario(gateway(recognizer), (9000,), (2500, 0.03, 24))
    assert {kitchen["text"], bedroom["text"]} == {"Алиса, выключи свет на кухне", "Алиса, поставь таймер на десять минут"}


def test_a_speaker_that_heard_only_noise_does_not_block_or_steal_the_answer():
    async def recognizer(uri, pcm, rate, language, **extras):
        return "Продолжение следует." if len(pcm) > LONG_UTTERANCE_BYTES else SENTENCE

    noisy, speaker = scenario(gateway(recognizer), (2500, 0.0, 24), (9000, 0.03))
    assert speaker["text"] == SENTENCE
    assert noisy["text"] == ""
    assert noisy.get("rejected") != HEARD_ELSEWHERE


def test_dedup_can_be_turned_off():
    async def recognizer(uri, pcm, rate, language, **extras):
        return SENTENCE

    finals = scenario(gateway(recognizer, AISIS_STT_DEDUP="0"), (2500,), (9000, 0.03))
    assert [final["text"] for final in finals] == [SENTENCE, SENTENCE]


def test_the_clip_feed_says_that_another_speaker_answered_instead_of_calling_it_a_hallucination():
    import clipfeed
    clip = {"status": "blacklist", "reason": HEARD_ELSEWHERE, "transcript": SENTENCE, "ts": 0.0}
    line = clipfeed.describe_result(clip, None, 0.0)
    assert "другая колонка" in line and "галлюцинаци" not in line
