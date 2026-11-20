"""Archive the commands the speaker recognised on the device.

The station's own commands spotter («включи свет», «громче», «пауза») recognises a command locally and sends the quasar
server text only, so the STT gateway never hears it and no clip reaches the voice archive or the Telegram clip feed
(2026-11-16 15:53: two «включи свет», nothing in the chat). The station does upload the audio it spotted, as an Ogg/Opus
stream next to its LogSpotter event; quasar collects it and posts it to the gateway's `POST /spotter-clip`, which lands
here. The recording becomes an ordinary `voice_clips` row (trigger `spotter`, transcript = the on-device recognition,
engine says so) and the clip feed posts it like any other utterance.
"""
from __future__ import annotations

import array
import logging
import os
import shutil
import subprocess
import time

import voiceclips

LOG = logging.getLogger("aisis.spotter_clips")

RATE = 16000
MIN_MS = 150                # shorter than a syllable: the stream held its headers only
MAX_S = 20                  # a spoken command is a few seconds; whatever is longer is cut, never held whole in memory
MAX_TS_SKEW_S = 120         # a sender clock further off than this is not trusted for the clip's time
DECODE_TIMEOUT_S = 20


class Rejected(Exception):
    """The upload cannot become a clip; `reason` is a short machine word for the log and the HTTP answer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _ffmpeg() -> str | None:
    configured = os.environ.get("FFMPEG")
    if configured and os.access(configured, os.X_OK):
        return configured
    return shutil.which("ffmpeg")


def decode(audio: bytes) -> bytes:
    """16 kHz mono s16le PCM (at most MAX_S seconds) of an Ogg/Opus upload. The format is forced and only the pipe may be opened:
    the bytes come off the network, so ffmpeg must not guess a demuxer (playlists and manifests make it fetch URLs).
    The station's pages can arrive with sequence jumps: ffmpeg copes."""
    binary = _ffmpeg()
    if binary is None:
        raise Rejected("no_ffmpeg")
    try:
        done = subprocess.run([binary, "-v", "error", "-protocol_whitelist", "pipe", "-f", "ogg", "-i", "pipe:0", "-t", str(MAX_S),
                               "-fs", str(MAX_S * RATE * 2), "-f", "s16le", "-ac", "1", "-ar", str(RATE), "pipe:1"],
                              input=audio, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=DECODE_TIMEOUT_S, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Rejected("undecodable") from exc
    # -t counts timestamps, which chained Ogg streams restart; -fs bounds the bytes ffmpeg writes (and we buffer).
    return done.stdout[:MAX_S * RATE * 2]


def levels(pcm: bytes) -> tuple[int, int]:
    """(rms, peak) of s16le samples, in the units the gateway's own clips use."""
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - len(pcm) % 2])
    if not samples:
        return 0, 0
    return int((sum(s * s for s in samples) / len(samples)) ** 0.5), max(map(abs, samples))


def clip_time(sent_ms: str | None, now: float) -> float:
    """The moment the command was spoken: the sender's timestamp when it is plausible, else now."""
    try:
        sent = int(sent_ms or "") / 1000
    except (ValueError, OverflowError):  # not a number, or one too long for a float
        return now
    return sent if abs(now - sent) <= MAX_TS_SKEW_S else now


def archive(db_path: str, audio: bytes, transcript: str, station: str | None = None, sent_ms: str | None = None,
            now: float | None = None) -> dict:
    """Decode and archive one upload; returns {"clip": id, "dur": seconds}. Raises Rejected."""
    return archive_decoded(db_path, audio, transcript, station, sent_ms, now)[0]


def archive_decoded(db_path: str, audio: bytes, transcript: str, station: str | None = None, sent_ms: str | None = None,
                    now: float | None = None) -> tuple[dict, bytes]:
    """`archive`, and the 16 kHz mono s16le PCM it decoded (for a second recogniser: the upload is decoded once). Raises Rejected."""
    now = time.time() if now is None else now
    transcript = transcript.strip()
    if not transcript:
        raise Rejected("no_transcript")
    if not audio:
        raise Rejected("no_audio")
    pcm = decode(audio)
    duration_ms = len(pcm) // 2 * 1000 // RATE
    if duration_ms < MIN_MS:
        raise Rejected("short")
    rms, peak = levels(pcm)
    clip_id = voiceclips.save(db_path, pcm, RATE, {
        "ts": clip_time(sent_ms, now), "rms": rms, "peak": peak, "status": "sent", "trigger": voiceclips.SPOTTER_TRIGGER,
        "engine": voiceclips.SPOTTER_ENGINE, "transcript": transcript, "station": station or None})
    if clip_id is None:
        raise Rejected("save_failed")
    LOG.info("spotter_clip clip=%s dur_ms=%d rms=%d peak=%d text=%r", clip_id, duration_ms, rms, peak, transcript)
    return {"clip": clip_id, "dur": duration_ms / 1000}, pcm
