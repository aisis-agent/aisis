#!/usr/bin/env python3
"""Quasar PCM WebSocket -> existing Wyoming ASR with local WebRTC endpointing.

No model download or duplicate recognizer. Audio is held in RAM, at most 30s per
utterance, and every utterance is also archived locally (voiceclips.py) with its transcript.
Wyoming sees only utterances that pass the energy/VAD gate (judge); its transcripts pass
distrust() before they can become a command.
"""
from __future__ import annotations

import array
import asyncio
from collections import deque
from dataclasses import dataclass, replace
import json
import logging
import math
import operator
import os
import re
import time
import random
from urllib.parse import unquote, urlsplit

from aiohttp import WSMsgType, web

import dsp_settings
import speech_frontend
import spotter_clips
import stt_tuning
import voice_vocab
import voiceclips
import wake_window
import wake_word

LOG = logging.getLogger("aisis.stt")
FRAME_MS = 20

# Whisper-family recognizers answer noise and near-silence with captions learned from video
# subtitles. The mic window that opens after a question («Продолжить воспроизведение?») is
# exactly that input; a transcript like «Продолжение следует...» then looks like a command,
# triggers another question, and the station loops. These phrases are never spoken to the
# station, so the gateway reports them as «heard nothing».
_HALLUCINATIONS = re.compile(
    r"^(продолжение следует"
    r"|субтитры\b.*|редактор субтитров\b.*|корректор\b.*|dimatorzok\b.*"
    r"|спасибо за (просмотр|внимание)"
    r"|подписывайтесь на (мой |наш )?канал"
    r"|не забудьте подписаться.*"
    r"|ставьте лайки.*"
    r"|благодарю за (просмотр|внимание)"
    r"|до (новых|скорых) встреч.*"
    r"|увидимся в следующем.*"
    r"|субтитры (?:от|by)\b.*|titles? by\b.*|subtitles? (?:by|made)\b.*|amara org.*"
    r"|thanks for watching|thank you for watching|please subscribe.*)$")
# Innocent-looking one-word captions Whisper also emits on noise. Only distrusted when the audio is weak:
# a clearly spoken «спасибо» is real, a 200 ms blip transcribed as «спасибо» is not.
_FILLER = frozenset(("спасибо", "благодарю", "пока", "до свидания", "thank you", "thanks", "bye", "титры", "угу", "ага",
                     "ну", "так", "а", "и", "э", "эм", "хм", "м", "ммм", "мм"))
STEADY_AFTER_MS = 500  # the steady-level rule needs enough speech frames for a meaningful spread
TAIL_MS = 200          # closing silence kept in the audio sent on (the rest only invites caption hallucinations)
WEAK_VOICED_MS = 600
WEAK_CONTRAST = 3.0
# Distrusted transcripts of audio that WAS speech (a short «да» captioned «Спасибо.», a stuck loop): unlike a
# hallucination on silence, the owner talked, so the empty end-of-stream final carries the reason (see finish()).
UNCLEAR_SPEECH = ("weak_filler", "repetition", "too_many_words")
# What the recognizer heard was only the activation word (wake_word.py): not a request. Mid-stream the gateway drops it and keeps
# listening (the request follows in the same session); at the end of the stream it is the empty final of an unanswered window.
WAKE_ONLY = "wake_word"
LEAD_MIN_OWN_MS = 120   # the cut name counts for an utterance only when the utterance itself has this much speech (webrtcvad's hangover after the cut is at most 100 ms)
LEAD_MAX_AGE_MS = 6000  # … and only when it starts within this much stream audio after the cut: later speech is not the request that followed the name
MAX_HELD = 4   # cuts kept behind a running recognition; a stream of them behind a slow recognizer drops the newest, never grows the heap
PROBE_RATE = 16000                    # /healthz/asr: half a second of 16-bit mono silence, enough to run a whole transcription
PROBE_PCM_BYTES = PROBE_RATE
PROBE_TIMEOUT_S = 4
# The failures of a probe that prove the recognizer itself is broken: RuntimeError is transcribe()'s own verdict (empty or
# oversized header, error event, no transcript), ValueError covers a malformed JSON or UTF-8 reply, KeyError/AttributeError/TypeError a
# JSON reply of the wrong shape, the rest are a connection the recognizer dropped mid-exchange. Timeouts and a refused connection are deliberately not in it (see health_asr).
RECOGNIZER_BROKEN = (RuntimeError, ValueError, KeyError, AttributeError, TypeError, EOFError,
                     ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


class NoVerdict(RuntimeError):
    """The recognizer refused on purpose, with a code that says nothing about its health: `gpu-lent` (its GPU copy is lent to another
    program, Orca's preview) or `model-unloaded` (idle-unloaded, and a probe never reloads it). A RuntimeError so live callers see
    what they always saw; /healthz/asr tells it apart before it reads as broken."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


NO_VERDICT_CODES = {"gpu-lent": "gpu-lent", "model-unloaded": "unloaded"}


WHISPER_CTL_STATES_WITHOUT_VERDICT = ("unloaded", "pinned")   # idle-unloaded or lent: probing would reload the model or only be refused
PROBE_CONTEXT = {"aisis_probe": True}   # the wrapper does not count a request with this context as activity (idle unload)
CTL_TIMEOUT_S = 2.0


async def whisper_ctl_status(env):
    """`status` of the offload control socket named by AISIS_WHISPER_CTL_SOCKET, or None when none is configured, the socket is
    missing, or it does not answer (then the probe runs as it always did). The socket is used ONLY when configured: the operator
    declares it belongs to the recognizer at AISIS_WYOMING_URI (aisis-station-stt.service does for the local whisper); a guessed
    default could hide a broken recognizer behind an unrelated daemon's `unloaded`."""
    path = env.get("AISIS_WHISPER_CTL_SOCKET")
    if not path or not os.path.exists(path):
        return None
    async def exchange():
        reader, writer = await asyncio.open_unix_connection(path)
        try:
            writer.write(b'{"op":"status"}\n')
            await writer.drain()
            reply = json.loads(await reader.readline())
        finally:
            writer.close()
        return reply
    try:
        reply = await asyncio.wait_for(exchange(), CTL_TIMEOUT_S)
    except (OSError, ValueError, asyncio.TimeoutError):
        return None
    if not (isinstance(reply, dict) and reply.get("ok") is True and isinstance(reply.get("state"), str)
            and isinstance(reply.get("leases", []), list)):
        return None    # a reply of the wrong shape is no status at all: the probe runs as it always did
    return reply


def _fold(text):
    folded = re.sub(r"[^a-zа-я0-9 ]+", " ", text.lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", folded).strip()


def is_silence_hallucination(text):
    """True for captions that are never addressed to the station (subtitle credits, «продолжение следует»)."""
    return bool(_HALLUCINATIONS.match(_fold(text)))


async def write_event(writer, kind, data=None, payload=b""):
    # Wyoming framing: JSON header + LF, optional JSON data bytes, raw payload.
    raw = json.dumps(data or {}, ensure_ascii=False).encode()
    header = {"type": kind, "data_length": len(raw)}
    if payload:
        header["payload_length"] = len(payload)
    writer.write(json.dumps(header).encode() + b"\n" + raw + payload)
    await writer.drain()


async def read_event(reader):
    line = await reader.readline()
    if not line or len(line) > 65536:
        raise RuntimeError("wyoming_invalid_header")
    event = json.loads(line)
    data = event.get("data", {})
    length = event.get("data_length", 0)
    payload_length = event.get("payload_length", 0)
    if not isinstance(length, int) or not 0 <= length <= 1048576 or not isinstance(payload_length, int) or not 0 <= payload_length <= 1048576:
        raise RuntimeError("wyoming_invalid_length")
    if length:
        data.update(json.loads(await reader.readexactly(length)))
    if payload_length:
        await reader.readexactly(payload_length)
    return event["type"], data


def wyoming_address(uri):
    """The parsed recognizer address; ValueError for anything but tcp://host:port (a setting, not a recognizer failure)."""
    address = urlsplit(uri)
    if address.scheme != "tcp" or not address.hostname or not address.port:
        raise ValueError("AISIS_WYOMING_URI must be tcp://host:port")
    return address


async def transcribe(uri, pcm, sample_rate, language="ru", timeout=30, profile=None, context=None):
    """One utterance to a Wyoming recognizer. `profile` (the event's `name`) and `context` ({"prompt", "hotwords"})
    are stt-cli `stt serve` extensions: it merges them with that profile's dictionaries. Stock Wyoming servers
    ignore unknown fields, so they are only sent when set."""
    address = wyoming_address(uri)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(address.hostname, address.port), 5)
    try:
        async def exchange():
            request = {"language": language}
            if profile:
                request["name"] = profile
            if context:
                request["context"] = context
            await write_event(writer, "transcribe", request)
            fmt = {"rate": sample_rate, "width": 2, "channels": 1}
            await write_event(writer, "audio-start", fmt)
            for offset in range(0, len(pcm), 4096):
                await write_event(writer, "audio-chunk", fmt, pcm[offset:offset+4096])
            await write_event(writer, "audio-stop")
            for _ in range(100):
                kind, data = await read_event(reader)
                if kind == "error":
                    if data.get("code") in NO_VERDICT_CODES:
                        raise NoVerdict(NO_VERDICT_CODES[data["code"]])
                    raise RuntimeError("wyoming_asr_error")
                if kind == "transcript":
                    text = data.get("text")
                    if not isinstance(text, str):
                        raise RuntimeError("wyoming_invalid_transcript")
                    return text.strip()
            raise RuntimeError("wyoming_missing_transcript")
        return await asyncio.wait_for(exchange(), timeout)
    finally:
        writer.close()
        await writer.wait_closed()


@dataclass(frozen=True)
class Tuning:
    """Endpointing and gate knobs; every one is an AISIS_STT_* environment override."""
    silence_ms: int = 800        # trailing silence that closes an utterance (end of speech -> final latency floor)
    vad_gain: float = 6.0        # boost of QUIET frames fed to the VAD only; the recognizer gets the original audio
    min_voiced_ms: int = 200     # fewer VAD-speech milliseconds than this is a click/blip, never sent
    min_rms: float = 4.0         # voiced-frame RMS below this is dither/silence, never sent
    min_contrast: float = 2.0    # voiced RMS / room-floor RMS below this is flat noise, never sent
    min_swing: float = 1.8       # a long "speech" stretch whose level never moves is machinery, never sent
    norm_dbfs: float = -30.0     # voiced RMS the recognizer copy is lifted to (dBFS); >= 0 turns the lift off
    norm_max_gain_db: float = 45.0  # never lift by more than this: the station floor is ~4 LSB, lifting it past ~45 dB only amplifies hiss
    dsp_enabled: bool = True     # the whole recognizer-copy front-end (lift, compressor, high-pass, limiter)
    compressor: bool = False
    highpass_hz: float = 0.0
    ab_percent: float = 10.0     # share of utterances also sent to the recognizer UNprocessed, in the background, for the A/B counters
    wake_cut: bool = True        # cut the activation word from the audio before the recognizer (wake_window.py); off: the text-level strip only

    @classmethod
    def from_env(cls, env):
        def number(key, default, cast):
            try:
                return cast(env.get(key, default))
            except (TypeError, ValueError):
                return default
        return cls(silence_ms=max(100, number("AISIS_STT_SILENCE_MS", cls.silence_ms, int)),
                   vad_gain=max(1.0, number("AISIS_STT_VAD_GAIN", cls.vad_gain, float)),
                   min_voiced_ms=number("AISIS_STT_MIN_VOICED_MS", cls.min_voiced_ms, int),
                   min_rms=number("AISIS_STT_MIN_RMS", cls.min_rms, float),
                   min_contrast=number("AISIS_STT_MIN_CONTRAST", cls.min_contrast, float),
                   min_swing=number("AISIS_STT_MIN_SWING", cls.min_swing, float),
                   norm_dbfs=number("AISIS_STT_NORM_DBFS", cls.norm_dbfs, float),
                   norm_max_gain_db=max(0.0, number("AISIS_STT_NORM_MAX_GAIN_DB", cls.norm_max_gain_db, float)),
                   dsp_enabled=bool(number("AISIS_STT_DSP", int(cls.dsp_enabled), int)),
                   compressor=bool(number("AISIS_STT_COMPRESSOR", int(cls.compressor), int)),
                   highpass_hz=max(0.0, number("AISIS_STT_HIGHPASS_HZ", cls.highpass_hz, float)),
                   wake_cut=bool(number("AISIS_STT_WAKE_WINDOW", int(cls.wake_cut), int)),
                   ab_percent=min(100.0, max(0.0, number("AISIS_STT_AB_PERCENT", cls.ab_percent, float))))


@dataclass(frozen=True)
class Utterance:
    """One cut-out utterance: the audio plus the level facts the gate and the archive need."""
    pcm: bytes
    rate: int
    voiced: int                  # 20 ms frames the VAD called speech
    trailing_ms: int             # silence after the last voiced frame
    rms: int
    peak: int
    voiced_rms: float
    floor_rms: float | None      # median RMS of the non-speech frames (None when too few to tell)
    swing: float = 0.0           # p90/p10 of the speech frames' RMS: speech rises and falls, hum/hiss/fan do not
    lead_pcm: bytes = b""        # the activation word the wake window cut off in front of this utterance: never recognised, still the speaker's voice
    lead_voiced_ms: int = 0

    @property
    def duration_ms(self):
        return len(self.pcm) * 1000 // (2 * self.rate)

    @property
    def speech_pcm(self):
        """What the recognizer and the archive get: the utterance minus all but TAIL_MS of the closing silence.
        Long silent tails are where Whisper appends «Продолжение следует...» after a real command."""
        cut = max(0, self.trailing_ms - TAIL_MS) * self.rate * 2 // 1000
        cut -= cut % 2
        return self.pcm[:len(self.pcm) - cut] if cut else self.pcm

    @property
    def speaker_pcm(self):
        """What the speaker check hears: the activation word (cut from the recognizer's audio) and the utterance, as before the cut existed."""
        return self.lead_pcm + self.speech_pcm

    @property
    def voiced_ms(self):
        return self.voiced * FRAME_MS

    @property
    def contrast(self):
        return None if self.floor_rms is None else self.voiced_rms / max(self.floor_rms, 1.0)


def activation_utterance(pcm, rate, voiced_ms):
    """The audio the wake window cut away (the activation event: never recognised), shaped as an Utterance for the archive row."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    return Utterance(pcm=pcm, rate=rate, voiced=voiced_ms // FRAME_MS, trailing_ms=0,
                     rms=int(math.sqrt(sum(map(operator.mul, samples, samples)) / len(samples))) if samples else 0,
                     peak=max(map(abs, samples), default=0), voiced_rms=0.0, floor_rms=None)


def judge(utt, tuning):
    """Reason this utterance must NOT reach the recognizer, or None. Whisper invents text on non-speech."""
    if utt.voiced_ms + utt.lead_voiced_ms < tuning.min_voiced_ms:   # a one-word request after a cut name is as long as the pair was before the cut
        return "short"
    if utt.voiced_rms < tuning.min_rms:
        return "silence"
    if utt.contrast is not None and utt.contrast < tuning.min_contrast:
        return "flat_noise"
    if utt.voiced_ms >= STEADY_AFTER_MS and utt.swing < tuning.min_swing:
        return "steady_noise"
    return None


def distrust(text, utt):
    """Reason a transcript of this utterance is not believable speech, or None."""
    words = _fold(text).split()
    if not words:
        return "empty"
    if is_silence_hallucination(text):
        return "hallucination"
    weak = utt.voiced_ms < WEAK_VOICED_MS or (utt.contrast is not None and utt.contrast < WEAK_CONTRAST)
    if weak and " ".join(words) in _FILLER:
        return "weak_filler"
    if len(words) >= 6 and len(set(words)) / len(words) <= 0.34:
        return "repetition"
    if len(words) > 4 + 7 * max(0.3, (utt.duration_ms - utt.trailing_ms) / 1000):
        return "too_many_words"
    return None


def frontend_for_recognizer(pcm, voiced_rms, tuning):
    """(pcm, info) for the recognizer's copy of an utterance: speech lifted to tuning.norm_dbfs voiced RMS, optional
    compressor/high-pass, always peak-limited (speech_frontend). The station's uplink speaks at ~-45 dBFS peak (60-170
    of 32767): recognizers trained on normal-level audio answer it with captions unless it is lifted. Only the
    recognizer gets this copy; the archive, the speaker check and the gate keep the original samples. When nothing had
    to change the very same `pcm` object comes back (callers test identity)."""
    if not tuning.dsp_enabled:
        return pcm, {"gain_db": 0.0, "compressed": False, "in_rms": 0.0, "out_rms": 0.0, "out_peak": 0}
    return speech_frontend.process(pcm, 16000, speech_frontend.FrontEndConfig(
        target_dbfs=tuning.norm_dbfs, max_gain_db=tuning.norm_max_gain_db, highpass_hz=tuning.highpass_hz,
        compressor=tuning.compressor), voiced_rms=voiced_rms)


def lift_for_recognizer(pcm, voiced_rms, tuning):
    """(pcm, gain_db): frontend_for_recognizer without the details (bench_asr_archive.py imports this)."""
    out, info = frontend_for_recognizer(pcm, voiced_rms, tuning)
    return out, info["gain_db"]


class Endpoint:
    """20ms frames, 200ms preroll, configurable trailing silence and 30s hard cap."""
    def __init__(self, vad, sample_rate=16000, silence_ms=800, vad_gain=1.0):
        self.vad = vad
        self.rate = sample_rate
        self.frame_size = sample_rate * 2 // 50
        self.vad_gain = vad_gain
        self.pending = bytearray()
        self.preroll = deque(maxlen=10)
        self.audio = bytearray()
        self.levels = []        # (energy, peak, voiced) per frame in `audio`
        self.voiced = 0
        self.speech_frames = 0  # cumulative, unlike `voiced` which every flush resets
        self.silence = 0
        self.silence_limit = max(1, silence_ms // FRAME_MS)

    def boosted(self, frame, samples, peak):
        """VAD input: quiet frames lifted (never past full scale). webrtcvad is an energy model and
        sees almost nothing in far-field speech 45 dB down; the recognizer still gets the raw frame."""
        if self.vad_gain <= 1.0 or not peak:
            return frame
        gain = min(self.vad_gain, 32000.0 / peak)
        if gain <= 1.0:
            return frame
        return array.array("h", [int(sample * gain) for sample in samples]).tobytes()

    def prime(self, pcm):
        """Let the VAD hear audio that belongs to no utterance (the activation word the wake window cut away): its noise model adapts to
        the stream as it did before the cut existed, so the request right behind the name is judged the way it always was."""
        size = self.frame_size
        for offset in range(0, len(pcm) - size + 1, size):
            frame = bytes(pcm[offset:offset + size])
            samples = array.array("h")
            samples.frombytes(frame)
            self.vad.is_speech(self.boosted(frame, samples, max(map(abs, samples))), self.rate)

    def feed(self, chunk):
        self.pending.extend(chunk)
        completed = []
        while len(self.pending) >= self.frame_size:
            frame = bytes(self.pending[:self.frame_size])
            del self.pending[:self.frame_size]
            samples = array.array("h")
            samples.frombytes(frame)
            peak = max(map(abs, samples))
            level = (sum(map(operator.mul, samples, samples)), peak)
            voice = self.vad.is_speech(self.boosted(frame, samples, peak), self.rate)
            if voice:
                self.speech_frames += 1
            if not self.audio:
                self.preroll.append((frame, level + (voice,)))
                if not voice:
                    continue
                for early, early_level in self.preroll:
                    self.audio.extend(early)
                    self.levels.append(early_level)
                self.preroll.clear()
            else:
                self.audio.extend(frame)
                self.levels.append(level + (voice,))
            if voice:
                self.voiced += 1
                self.silence = 0
            else:
                self.silence += 1
            if self.silence >= self.silence_limit or len(self.audio) >= self.rate * 2 * 30:
                utterance = self.flush()
                if utterance:
                    completed.append(utterance)
        return completed

    def flush(self, include_partial=False):
        """The buffered utterance with its level stats, or None when nothing was buffered."""
        if include_partial and self.audio:
            self.audio.extend(self.pending[:len(self.pending)//2*2])
            self.pending.clear()
        utterance = self.utterance() if self.audio else None
        self.audio.clear()
        self.levels = []
        self.voiced = self.silence = 0
        return utterance

    def utterance(self):
        per_frame = self.frame_size // 2
        levels = self.levels
        voiced = [energy for energy, _, voice in levels if voice]
        quiet = sorted(math.sqrt(energy / per_frame) for energy, _, voice in levels if not voice)
        trailing = 0
        for _, _, voice in reversed(levels):
            if voice:
                break
            trailing += 1
        voiced_rms = sorted(math.sqrt(energy / per_frame) for energy in voiced)
        return Utterance(
            pcm=bytes(self.audio), rate=self.rate, voiced=len(voiced), trailing_ms=trailing * FRAME_MS,
            rms=int(math.sqrt(sum(energy for energy, _, _ in levels) / (len(levels) * per_frame))) if levels else 0,
            peak=max((peak for _, peak, _ in levels), default=0),
            voiced_rms=math.sqrt(sum(voiced) / (len(voiced) * per_frame)) if voiced else 0.0,
            floor_rms=quiet[len(quiet) // 2] if len(quiet) >= 5 else None,
            swing=voiced_rms[len(voiced_rms) * 9 // 10] / max(voiced_rms[len(voiced_rms) // 10], 1.0) if voiced_rms else 0.0)


class STTGateway:
    SLOTS = 2   # recognitions in flight at once (live utterances; /healthz/asr runs only when none is)
    # How long, after the recognizer returned, enforce mode waits for a slower speaker check before failing open.
    SPEAKER_GRACE = 2.0
    SPEAKER_RETRY_AFTER = 60.0

    def __init__(self, env=None, vad_factory=None, recognizer=transcribe, verifier=None, tv=None, archive=True, domain_catalog=None, clock=time.monotonic):
        self.env = dict(os.environ) if env is None else env
        self.tuning = Tuning.from_env(self.env)
        self.clock = clock            # the stream's age (wake_window.Gate); a test owns it
        self.vad_factory = vad_factory
        self.recognizer = recognizer
        # Domain bias for the recognizer, opt-in (AISIS_STT_DOMAIN_CONTEXT=1): only an stt-cli recognizer reads it.
        # `domain_catalog` is injectable for tests.
        self.domain_catalog = domain_catalog if domain_catalog is not None else (
            voice_vocab.CatalogCache(lambda: voice_vocab.fetch_catalog(
                self.env.get("AISIS_HACLI_URL", "http://127.0.0.1:8795"), voice_vocab.library_artists))
            if self.env.get("AISIS_STT_DOMAIN_CONTEXT") == "1" else None)
        self.tv = tv                  # tvpause.TvPause: pause the TV when the station starts listening
        self.slots = asyncio.Semaphore(self.SLOTS)
        self.verifier = verifier
        self.archive_enabled = archive
        self.pruned_at = 0.0
        self.speaker_disabled_until = 0.0
        self.speaker_background = set()

    def live_tuning(self):
        """The startup tuning with the dashboard's end-of-speech wait and level front-end overrides; read per conversation, no restart."""
        tuning = self.tuning
        silence_ms = stt_tuning.override()
        if silence_ms is not None:
            tuning = replace(tuning, silence_ms=silence_ms)
        names = {"enabled": "dsp_enabled", "target_dbfs": "norm_dbfs", "max_gain_db": "norm_max_gain_db", "compressor": "compressor",
                 "highpass_hz": "highpass_hz", "ab_percent": "ab_percent"}
        dsp = {names[key]: value for key, value in dsp_settings.overrides().items() if key in names}
        return replace(tuning, **dsp) if dsp else tuning

    def ab_probe(self, raw_pcm, rate, lifted_text, gain_db, in_rms):
        """Background A/B sample: the recognizer's answer for the UNPROCESSED copy, next to the processed copy's answer.
        Skipped when the recognizer is busy; never delays or changes the real answer."""
        if self.slots.locked():
            return
        async def run():
            try:
                async with self.slots:
                    raw_text = await asyncio.wait_for(self.recognizer(self.env["AISIS_WYOMING_URI"], raw_pcm, rate,
                        self.env.get("AISIS_STT_LANGUAGE", "ru"), **self.recognizer_extras()), 35)
                usable = lambda text: bool(_fold(text)) and not is_silence_hallucination(text)
                await asyncio.get_running_loop().run_in_executor(None, dsp_settings.record_ab, self.db_path(), round(gain_db, 1),
                    round(in_rms, 1), dsp_settings.verdict(raw_text, lifted_text, usable), raw_text, lifted_text)
            except Exception as exc:
                LOG.warning("stt_ab_failed class=%s", type(exc).__name__)
        task = asyncio.ensure_future(run())
        self.speaker_background.add(task)
        task.add_done_callback(self.speaker_background.discard)

    def recognizer_extras(self):
        """stt-cli profile + the speaker's domain vocabulary (verbs, HA device names, rooms, library artists) for the
        recognizer's `context`. Only what is configured is passed, so a stock recognizer sees the plain call."""
        extras = {}
        profile = self.env.get("AISIS_STT_PROFILE")
        if profile:
            extras["profile"] = profile
        if self.domain_catalog is not None:
            extras["context"] = voice_vocab.domain_context(self.domain_catalog.get(), with_hotwords=self.env.get("AISIS_STT_HOTWORDS") == "1")
        return extras

    def tv_activation(self):
        """Activation = a spoken turn's websocket just opened (wake word/spotter fired on the station).
        The TV call runs in a worker thread: the STT session never waits for Home Assistant."""
        if self.tv is None:
            return
        job = asyncio.get_running_loop().run_in_executor(None, self.tv.activation)
        self.speaker_background.add(job)
        job.add_done_callback(self.tv_done)

    def tv_done(self, job):
        self.speaker_background.discard(job)
        if not job.cancelled() and job.exception() is not None:
            LOG.warning("tv_pause_failed class=%s", type(job.exception()).__name__)

    def db_path(self):
        return (self.env.get("AISIS_DIALOGS_DB") or os.environ.get("AISIS_DIALOGS_DB")
                or os.path.expanduser("~/.local/share/aisis/dialogs.sqlite3"))

    def prune_archive(self):
        """Retention: at most once a day, from whichever path just archived a clip (gateway utterance or spotter upload)."""
        if time.time() - self.pruned_at > 86400:
            self.pruned_at = time.time()
            voiceclips.prune(self.db_path())

    def archive(self, utt, meta):
        """Keep the utterance and what became of it (voiceclips). Off the event loop; never delays or fails a turn."""
        if not self.archive_enabled:
            return
        meta = dict(meta, ts=time.time(), rms=utt.rms, peak=utt.peak, voiced_ms=utt.voiced_ms, silence_ms=utt.trailing_ms,
                    floor_rms=None if utt.floor_rms is None else round(utt.floor_rms, 1),
                    contrast=None if utt.contrast is None else round(utt.contrast, 2),
                    engine=self.env.get("AISIS_STT_ENGINE", "faster-whisper large-v3-turbo (wyoming)"))
        def work():
            voiceclips.save(self.db_path(), utt.speech_pcm, utt.rate, meta)
            self.prune_archive()
        job = asyncio.get_running_loop().run_in_executor(None, work)
        self.speaker_background.add(job)
        job.add_done_callback(self.speaker_done)

    def speaker_failed(self, exc):
        # One warning per outage; transcription carries on exactly as before.
        if time.monotonic() >= self.speaker_disabled_until:
            LOG.warning("stt_speaker_disabled class=%s", type(exc).__name__)
        self.speaker_disabled_until = time.monotonic() + self.SPEAKER_RETRY_AFTER

    def speaker_settings(self):
        """Settings dict when verification should run now, else None. numpy/onnxruntime stay lazy."""
        if time.monotonic() < self.speaker_disabled_until:
            return None
        try:
            import speakerid
            settings = speakerid.load_settings()
            return None if settings["mode"] == "off" else settings
        except Exception as exc:
            self.speaker_failed(exc)
            return None

    def speaker_score(self, pcm, rate):
        import speakerid
        verifier = self.verifier or speakerid.default_verifier()
        return verifier.score(pcm, rate)

    async def speaker_check(self, pcm, rate):
        """Speaker verdict dict (with mode), or None when disabled or failed. Never raises."""
        settings = self.speaker_settings()
        if settings is None:
            return None
        try:
            result = await asyncio.get_running_loop().run_in_executor(None, self.speaker_score, pcm, rate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.speaker_failed(exc)
            return None
        result = dict(result, mode=settings["mode"])
        LOG.info("stt_speaker score=%s verdict=%s ms=%.0f mode=%s", "%.3f" % result["score"] if result["score"] is not None else "-",
                 result["verdict"], result["ms"], result["mode"])
        return result

    async def speaker_result(self, task, pcm, rate, text):
        """Collect the check after the recognizer finished: wait only in enforce mode (the verdict is needed)."""
        if not task.done():
            settings = self.speaker_settings()
            if settings is None or settings["mode"] != "enforce":
                # A slow shadow check never delays the final; its row is still recorded when it lands.
                self.speaker_background.add(task)
                task.add_done_callback(lambda done: self.speaker_late(done, pcm, rate, text))
                return None
            done, _ = await asyncio.wait({task}, timeout=self.SPEAKER_GRACE)
            if not done:
                LOG.warning("stt_speaker_slow grace=%.1fs", self.SPEAKER_GRACE)
                task.cancel()
                return None
        return task.result()

    def speaker_late(self, task, pcm, rate, text):
        self.speaker_background.discard(task)
        if not task.cancelled() and task.result() is not None:
            self.speaker_record(pcm, rate, text, task.result())

    def speaker_record(self, pcm, rate, text, speaker):
        """Persist the score row and optional sample off the event loop; fire and forget."""
        def work():
            import speakerid
            ts = time.time()
            if speaker["score"] is not None:
                speakerid.record_score(self.db_path(), {"ts": ts, "dur": speaker["dur"], "text": text, "score": speaker["score"],
                    "best": speaker["best"], "verdict": speaker["verdict"], "mode": speaker["mode"], "ms": speaker["ms"], "model": speaker["model"]})
            speakerid.capture_sample(pcm, rate, text, ts, speaker)
        job = asyncio.get_running_loop().run_in_executor(None, work)
        self.speaker_background.add(job)
        job.add_done_callback(self.speaker_done)

    def speaker_done(self, job):
        self.speaker_background.discard(job)
        if not job.cancelled() and job.exception() is not None:
            LOG.warning("stt_speaker_record_failed class=%s", type(job.exception()).__name__)

    async def warm_speaker(self, app):
        """Load the model off the request path so the first utterance is not slow; failures are non-fatal."""
        async def warm():
            if self.speaker_settings() is None:
                return
            try:
                import speakerid
                verifier = self.verifier or speakerid.default_verifier()
                warm = getattr(verifier, "warm", None)
                if warm is not None:
                    await asyncio.get_running_loop().run_in_executor(None, warm)
            except Exception as exc:
                self.speaker_failed(exc)
        task = asyncio.ensure_future(warm())
        self.speaker_background.add(task)
        task.add_done_callback(self.speaker_background.discard)

    async def health(self, request):
        return web.json_response({"service": "aisis-wyoming-stt", "configured": bool(self.env.get("AISIS_WYOMING_URI")), "model_downloads": False})

    async def health_asr(self, request):
        """200 only when the recognizer really transcribed a half second of silence. The tcp port answering (or a Describe)
        proves nothing: on 2026-11-17 wyoming-whisper kept answering Describe while every transcription died on a full
        temp directory and the client got an invalid header. aisis-failover restarts the recognizer on a 503 only, and a 503
        is evidence that the recognizer ANSWERED WRONGLY: it hung up, replied with an empty or malformed header, an error
        event, or no transcript (RECOGNIZER_BROKEN). Everything that says nothing about a broken recognizer gives no
        verdict, because a restart cannot fix it and would drop other clients' work (Home Assistant shares the recognizer):
        504 for a timeout or a refused or unreachable address (slow, contended, starting, or gone: systemd restarts a dead
        process), 202 while live utterances hold a slot (the probe waits for none, and leaves one slot open while it runs; the
        recognizer itself may still serialize a live utterance behind its half second of silence),
        501 when no usable recognizer address is configured. 202 also when the recognizer is idle-unloaded or its GPU is lent
        (its offload control socket says so, or it answers a `gpu-lent` error): the probe would only reload the model. The probe sends the language and profile of live traffic."""
        uri = self.env.get("AISIS_WYOMING_URI")
        try:
            wyoming_address(uri or "")
        except ValueError:
            return web.json_response({"error": "bad_uri" if uri else "not_configured"}, status=501)
        language, extras = self.env.get("AISIS_STT_LANGUAGE", "ru"), self.recognizer_extras()
        extras["context"] = {**(extras.get("context") or {}), **PROBE_CONTEXT}
        status = await whisper_ctl_status(self.env)
        if status and status.get("state") in WHISPER_CTL_STATES_WITHOUT_VERDICT:
            return web.json_response({"skipped": status["state"], "idle_s": status.get("idle_s"), "leases": len(status.get("leases") or [])},
                                     status=202)
        taken = 0
        while taken < self.SLOTS and not self.slots.locked():   # free slots are taken without yielding: no waiting, no queue jumping
            await self.slots.acquire()
            taken += 1
        if taken < self.SLOTS:
            for _ in range(taken):
                self.slots.release()
            return web.json_response({"skipped": "busy"}, status=202)
        for _ in range(self.SLOTS - 1):   # idle confirmed; one slot is enough for the probe, the others stay open to live utterances
            self.slots.release()
        started = time.monotonic()
        try:
            await asyncio.wait_for(self.recognizer(uri, bytes(PROBE_PCM_BYTES), PROBE_RATE, language, timeout=PROBE_TIMEOUT_S, **extras),
                                   PROBE_TIMEOUT_S + 1)
        except NoVerdict as refused:   # lent or unloaded on purpose: a restart cannot fix it and would drop other work
            return web.json_response({"skipped": refused.reason}, status=202)
        except RECOGNIZER_BROKEN as exc:  # the class name is the diagnosis, never the text
            LOG.warning("asr_probe_broken class=%s", type(exc).__name__)
            return web.json_response({"error": type(exc).__name__}, status=503)
        except Exception as exc:
            LOG.warning("asr_probe_inconclusive class=%s", type(exc).__name__)
            return web.json_response({"error": type(exc).__name__}, status=504)
        finally:
            self.slots.release()
        return web.json_response({"ok": True, "seconds": round(time.monotonic() - started, 2)})

    async def spotter_clip(self, request):
        """A command the speaker recognised on the device: quasar posts the audio the station uploaded for it (spotter_clips.py)."""
        if not self.archive_enabled:
            return web.Response(status=204)
        audio = await request.read()
        transcript = unquote(request.headers.get("x-aisis-transcript", ""))
        station, sent_at = request.headers.get("x-aisis-station"), request.headers.get("x-aisis-ts")
        def work():
            saved = spotter_clips.archive(self.db_path(), audio, transcript, station, sent_at)
            try:
                self.prune_archive()
            except Exception as exc:  # the clip is saved; retention retries on the next one
                LOG.warning("spotter_clip_prune_failed class=%s", type(exc).__name__)
            return saved
        try:
            saved = await asyncio.get_running_loop().run_in_executor(None, work)
        except spotter_clips.Rejected as rejected:
            LOG.info("spotter_clip_rejected reason=%s station=%s bytes=%d", rejected.reason, request.headers.get("x-aisis-station", "-"), len(audio))
            return web.json_response({"error": rejected.reason}, status=422)
        LOG.info("spotter_clip_archived station=%s source=%s frames=%s", request.headers.get("x-aisis-station", "-"),
                 request.headers.get("x-aisis-source", "-"), request.headers.get("x-aisis-frames", "-"))
        return web.json_response(saved, status=201)

    async def websocket(self, request):
        if not self.env.get("AISIS_WYOMING_URI"):
            raise web.HTTPServiceUnavailable(text="Wyoming recognizer not configured")
        if self.vad_factory is None:
            import webrtcvad
            factory = lambda: webrtcvad.Vad(2)
        else:
            factory = self.vad_factory
        tuning = self.live_tuning()
        ws = web.WebSocketResponse(max_msg_size=1024*1024, heartbeat=15)
        await ws.prepare(request)
        endpoint = Endpoint(factory(), silence_ms=tuning.silence_ms, vad_gain=tuning.vad_gain)
        initialized = False
        last_audio = time.monotonic()
        recognition = None
        unclear = None  # why the last VAD cut was dropped, when it sounded like speech the recognizer could not make out
        final_sent = False
        cancelled = False
        eof = False  # the station asked for the final: nothing may keep the turn open any longer
        held = deque()  # cuts that came while an earlier one was still being recognised (a dropped activation word, noise)
        stats = {"bytes": 0, "bin": 0, "voiced": 0, "peak": 0, "squares": 0, "samples": 0}
        LOG.info("stt_session_open")
        self.tv_activation()
        gate = wake_window.Gate(16000, enabled=False)   # the real one is made when the sample rate arrives
        opened = self.clock()
        window_noted = False
        name_lead = None   # (audio, voiced ms) of the cut name until the first real utterance after it takes it
        fed = 0            # bytes handed to the endpoint: stream time after the cut, whatever the wall clock does (idle waits, stalls)

        def elapsed_ms():
            return int((self.clock() - opened) * 1000)

        def window_audio(audio):
            """`audio` as the gate let it out. The first time it holds a verdict: archive the audio it cut away (the activation event,
            never recognised) and remember that the turn so far was only the name, so an empty final says so."""
            nonlocal unclear, window_noted, name_lead
            if gate.located is not None and not window_noted:
                window_noted = True
                if gate.removed is None:
                    LOG.info("stt_wake_window history_ms=%d skipped=%s", gate.history, gate.located.reason)
                else:
                    unclear = WAKE_ONLY
                    name_lead = (gate.name, gate.located.voiced_ms)
                    endpoint.prime(gate.removed)
                    LOG.info("stt_wake_window history_ms=%d cut_ms=%d voiced_ms=%d", gate.history, len(gate.removed) * 1000 // (2 * gate.rate),
                             gate.located.voiced_ms)
                    self.archive(activation_utterance(gate.removed, gate.rate, gate.located.voiced_ms),
                                 {"trigger": "vad", "status": voiceclips.ACTIVATION, "reason": "wake_window", "transcript": None})
            return audio

        def feed_gate(audio):
            nonlocal fed
            fed += len(audio)
            for utt in endpoint.feed(audio):
                finalize(utt, "vad")

        async def finish(utt, trigger, rejected):
            """One utterance -> one final. `rejected` is the gate's verdict (None = send it to the recognizer)."""
            nonlocal final_sent, recognition, unclear
            try:
                text = heard = wake_cut = ""
                speaker = None
                asr_ms = None
                if utt is not None and rejected is None:
                    rate = utt.rate
                    # Speaker check runs in a worker thread beside the recognizer, so it adds no latency.
                    speaker_task = asyncio.ensure_future(self.speaker_check(utt.speaker_pcm, rate))
                    recognizer_pcm, info = frontend_for_recognizer(utt.speech_pcm, utt.voiced_rms, tuning)
                    gain_db = info["gain_db"]
                    if recognizer_pcm is not utt.speech_pcm:
                        LOG.info("stt_norm gain_db=%.1f voiced_rms=%.1f out_rms=%.1f out_peak=%d compressed=%s", gain_db, utt.voiced_rms,
                                 info["out_rms"], info["out_peak"], info["compressed"])
                    began = time.monotonic()
                    try:
                        async with self.slots:
                            heard = await asyncio.wait_for(self.recognizer(self.env["AISIS_WYOMING_URI"], recognizer_pcm, rate,
                                self.env.get("AISIS_STT_LANGUAGE", "ru"), **self.recognizer_extras()), 35)
                    except BaseException:
                        speaker_task.cancel()
                        raise
                    asr_ms = int((time.monotonic() - began) * 1000)
                    if recognizer_pcm is not utt.speech_pcm and tuning.ab_percent > 0 and random.random() * 100 < tuning.ab_percent:
                        self.ab_probe(utt.speech_pcm, rate, heard, gain_db, utt.voiced_rms)
                    rejected = distrust(heard, utt)
                    if rejected == "empty":
                        rejected = None
                    named = wake_word.split(heard) if rejected is None else None
                    if named is not None and named.only:
                        rejected = WAKE_ONLY
                    if rejected is None:
                        text, wake_cut = named.clean, named.head
                        speaker = await self.speaker_result(speaker_task, utt.speaker_pcm, rate, text)
                    else:
                        speaker_task.cancel()
                if rejected:
                    LOG.info("stt_rejected reason=%s trigger=%s heard=%r", rejected, trigger, heard)
                if wake_cut:
                    LOG.info("stt_wake cut=%r heard=%r", wake_cut, heard)
                if utt is not None:
                    activation = rejected == WAKE_ONLY
                    self.archive(utt, {"trigger": trigger, "status": voiceclips.ACTIVATION if activation else "sent" if rejected is None else ("gate" if asr_ms is None else "blacklist"),
                        "reason": "wake" if activation else "wake_cut:" + wake_cut if wake_cut else rejected,
                        "transcript": text if rejected is None else heard, "asr_ms": asr_ms,
                        "final_ms": None if asr_ms is None else utt.trailing_ms + asr_ms})
                dropped = rejected and (trigger == "vad" or trigger == "idle" and rejected == WAKE_ONLY) and not eof
                if (dropped or held and not text) and not ws.closed and not cancelled:
                    # Noise/caption/activation word mid-stream: the station may still be waiting for the real command. Drop it,
                    # keep listening, and take up what was cut meanwhile.
                    recognition = None
                    if rejected in UNCLEAR_SPEECH or rejected == WAKE_ONLY:
                        unclear = rejected
                    drain()
                    return
                if not ws.closed and not cancelled and not final_sent:
                    final_sent = True
                    held.clear()
                    LOG.info("stt_final pcm=%dB text=%r asr_ms=%s end_to_final_ms=%s", len(utt.pcm) if utt else 0, text, asr_ms,
                             None if asr_ms is None else utt.trailing_ms + asr_ms)
                    reply = {"text": text, "end_of_utt": True}
                    if rejected:
                        reply["rejected"] = rejected
                    elif not text and unclear:
                        # The station's end-of-stream final of an utterance dropped earlier: the owner DID speak
                        # («да» heard as a lone «Спасибо.»), so the caller may ask again instead of ending the dialog.
                        reply["rejected"] = unclear
                    if speaker and speaker["mode"] == "enforce" and speaker["verdict"] == "foreign":
                        reply = {"text": "", "end_of_utt": True, "rejected": "foreign_voice", "score": speaker["score"]}
                    await ws.send_json(reply)
                if speaker:
                    self.speaker_record(utt.speaker_pcm, utt.rate, text, speaker)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("stt_failed class=%s", type(exc).__name__)
                if not ws.closed:
                    await ws.close(code=1011, message=b"recognizer unavailable")

        def finalize(utt, reason):
            nonlocal recognition, name_lead
            # Exactly one final per Quasar STT session. Native finish, VAD, and idle endpointing all
            # converge here; a VAD cut that fails the gate is dropped and the session keeps listening.
            # Level of everything received so far: tells "silence" (a follow-up window nobody
            # answered) from "speech the VAD missed" when a final comes back empty.
            rms = int(math.sqrt(stats["squares"] / stats["samples"])) if stats["samples"] else 0
            LOG.info("stt_finalize reason=%s pcm=%dB speech_frames=%d peak=%d rms=%d received=%dB frames=%d voiced_ms=%s floor=%s contrast=%s",
                reason, len(utt.pcm) if utt else 0, endpoint.speech_frames, stats["peak"], rms, stats["bytes"], stats["bin"],
                utt.voiced_ms if utt else 0, "%.1f" % utt.floor_rms if utt and utt.floor_rms is not None else "-",
                "%.1f" % utt.contrast if utt and utt.contrast is not None else "-")
            if final_sent:
                return
            if utt is not None and name_lead is not None and utt.voiced_ms >= LEAD_MIN_OWN_MS \
                    and fed * 1000 // (2 * utt.rate) - utt.duration_ms <= LEAD_MAX_AGE_MS:
                # The first stretch of real speech after the cut is the request: it counts the name's length and voice, as the pair did before
                # the cut. A blip (the VAD's hangover at the cut, a click) or noise the gate rejects neither borrows the name nor uses it up.
                led = replace(utt, lead_pcm=name_lead[0], lead_voiced_ms=name_lead[1])
                if judge(led, tuning) is None:
                    utt, name_lead = led, None
            rejected = judge(utt, tuning) if utt else None
            if reason == "vad" and rejected and not eof:   # noise: dropped now, never queued (it must not push a request out of `held`)
                LOG.info("stt_gate reason=%s voiced_ms=%d rms=%.1f", rejected, utt.voiced_ms, utt.voiced_rms)
                self.archive(utt, {"trigger": reason, "status": "gate", "reason": rejected})
                return
            if recognition is not None:
                if utt is not None:
                    if len(held) < MAX_HELD:   # the earliest cuts stay: the request comes right after the name, what overflows is later noise
                        held.append((utt, reason, rejected))   # recognised after the running one, unless that one becomes the final
                return
            recognition = asyncio.create_task(finish(utt, reason, rejected))

        def drain():
            nonlocal recognition
            if held and recognition is None and not final_sent and not cancelled:
                utt, reason, rejected = held.popleft()
                recognition = asyncio.create_task(finish(utt, reason, rejected))

        try:
            while not ws.closed:
                try:
                    message = await ws.receive(timeout=1.2)
                except asyncio.TimeoutError:
                    if not final_sent and initialized:
                        feed_gate(window_audio(gate.release(elapsed_ms())))
                    if endpoint.audio and recognition is None:
                        finalize(endpoint.flush(include_partial=True), "idle")
                    if time.monotonic() - last_audio > 40:
                        await ws.close(code=1000, message=b"idle audio stream")
                    continue
                if message.type == WSMsgType.TEXT:
                    config = json.loads(message.data)
                    if config.get("type") == "cancel":
                        cancelled = True
                        await ws.close(code=1000, message=b"audio cancelled")
                        break
                    if config.get("type") == "finish":
                        # Normal native EOF flushes immediately; it never cancels
                        # an ASR call already started by VAD.
                        if not final_sent and initialized:
                            feed_gate(window_audio(gate.release(elapsed_ms())))
                        eof = True
                        finalize(endpoint.flush(include_partial=True), "finish")
                        continue
                    rate = config.get("sample_rate")
                    if initialized or type(rate) is not int or rate not in {8000, 16000, 32000, 48000}:
                        await ws.close(code=1008, message=b"invalid sample rate")
                        break
                    endpoint = Endpoint(factory(), rate, silence_ms=tuning.silence_ms, vad_gain=tuning.vad_gain)
                    gate = wake_window.Gate(rate, enabled=tuning.wake_cut)
                    initialized = True
                    LOG.info("stt_init rate=%d", rate)
                elif message.type == WSMsgType.BINARY:
                    if not initialized:
                        await ws.close(code=1008, message=b"sample rate required")
                        break
                    last_audio = time.monotonic()
                    stats["bytes"] += len(message.data)
                    stats["bin"] += 1
                    samples = array.array("h")
                    samples.frombytes(message.data[:len(message.data) // 2 * 2])
                    if samples:
                        stats["peak"] = max(stats["peak"], max(map(abs, samples)))
                        stats["squares"] += sum(sample * sample for sample in samples)
                        stats["samples"] += len(samples)
                    if not final_sent:
                        feed_gate(window_audio(gate.push(message.data, elapsed_ms())))
                elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("stt_failed class=%s", type(exc).__name__)
            await ws.close(code=1011, message=b"recognizer unavailable")
        finally:
            LOG.info("stt_session_close bytes=%dB frames=%d", stats["bytes"], stats["bin"])
            cancelled = True
            # A websocket transport close is cancellation, not normal EOF. The
            # native caller must send finish and wait for its final JSON first.
            if recognition is not None and not recognition.done():
                recognition.cancel()
                await asyncio.gather(recognition, return_exceptions=True)

        return ws

    def app(self):
        app = web.Application()
        app.on_startup.append(self.warm_speaker)
        app.router.add_get("/", self.websocket)
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/healthz/asr", self.health_asr)
        app.router.add_post("/spotter-clip", self.spotter_clip)
        return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import tvpause
    tv = tvpause.default()
    tv.recover()
    web.run_app(STTGateway(tv=tv).app(), host="127.0.0.1", port=int(os.environ.get("AISIS_STT_PORT", "31121")), access_log=None)
