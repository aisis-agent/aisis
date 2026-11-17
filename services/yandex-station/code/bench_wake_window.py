#!/usr/bin/env python3
"""Back-test of the wake window (wake_window.py) on the stored audio: where the cut falls and what the recognizer writes with and without it.

  sessions   the real activation sessions of the gateway journal, rebuilt from the clip store (the archive keeps the cuts the VAD
             made, so the stream is the cuts at their stream positions with room noise between them), cut where the gateway cuts
             them, and recognised uncut and cut
  stress     a real name clip in front of a real request clip, pauses from 60 to 400 ms, the name ending 0.22-0.38 s before the end
             of the history (the measured range): name in the transcript and first request word lost, uncut and cut

Both play the gateway's own pieces (Endpoint with the VAD primed on the cut audio, judge, the level front-end) into the Wyoming
recognizer of `AISIS_WYOMING_URI`. Needs numpy and webrtcvad (the gateway's venv). Nothing is written to the archive, the TV or the
clip feed. `python3 bench_wake_window.py sessions|stress [--since 2026-11-10]`.
"""
from __future__ import annotations

import argparse
import array
import asyncio
import datetime
import os
import random
import re
import sqlite3
import subprocess
import sys

import wake_window

RATE = 16000
FRAME = RATE // 50
FRAME_MS = 20
HISTORY_RANGE = (2.0, 2.6)      # seconds: the station's history (2.32 s, sd 0.04) with room; a session outside it is not replayed
PAUSES_MS = (60, 100, 150, 250, 400)
NAME_ENDS_BEFORE_MS = (220, 300, 380)


def journal_sessions(text: str) -> list[dict]:
    """Sessions of `journalctl -u aisis-station-stt -o short-iso-precise`: open time, rate, every VAD cut and the stream's totals."""
    sessions: list[dict] = []
    current = None
    for line in text.splitlines():
        fields = line.split(None, 1)
        if len(fields) < 2:
            continue
        try:
            at = datetime.datetime.fromisoformat(fields[0]).timestamp()
        except ValueError:
            continue
        if "stt_session_open" in line:
            current = {"open": at, "rate": None, "cuts": []}
            sessions.append(current)
        elif current is None:
            continue
        elif "stt_init" in line:
            current["rate"] = int(re.search(r"rate=(\d+)", line).group(1))
        elif "stt_finalize" in line:
            found = re.search(r"reason=(\w+) pcm=(\d+)B .* received=(\d+)B", line)
            if found:
                current["cuts"].append({"at": at, "reason": found.group(1), "pcm": int(found.group(2)), "received": int(found.group(3))})
        elif "stt_session_close" in line:
            current["close"] = at
            current["bytes"] = int(re.search(r"bytes=(\d+)B", line).group(1))
    for session in sessions:
        rate = session["rate"]
        if rate and session.get("bytes"):
            session["history"] = session["bytes"] / (2 * rate) - (session["close"] - session["open"])
            for cut in session["cuts"]:
                cut["start"] = (cut["received"] - cut["pcm"]) / (2 * rate)
    return [s for s in sessions if s.get("history") is not None]


def room(rng: random.Random, samples: int, level: float) -> array.array:
    return array.array("h", [int(rng.gauss(0, level)) for _ in range(samples)])


def stream_of(pieces: list[tuple[float, bytes]], level: float, tail_s: float = 1.0) -> bytes:
    """16 kHz audio: `pieces` (start second, audio) laid into room noise of RMS `level`, `tail_s` of room after the last (the real stream
    ends with at least the 800 ms of silence that closes the last cut)."""
    end = max(int(start * RATE) * 2 + len(pcm) for start, pcm in pieces) + int(tail_s * RATE) * 2
    out = bytearray(room(random.Random(7), end // 2, level).tobytes())
    for start, pcm in pieces:
        offset = int(start * RATE) * 2
        out[offset:offset + len(pcm)] = pcm
    return bytes(out)


def splice(name: bytes, request: bytes, history_ms: int, name_ends_before_ms: int, pause_ms: int, level: float,
           request_lead: int = 0) -> tuple[bytes, int, int]:
    """(stream, name end ms, request start ms): the click every stream starts with, `name` ending `name_ends_before_ms` before the end
    of the history, `request` whose first voiced frame (`request_lead` frames into the clip) starts `pause_ms` after it. The name is
    given trimmed to its last voiced frame."""
    name_end = history_ms - name_ends_before_ms
    request_start = name_end + pause_ms
    click = array.array("h", [int(random.Random(3).gauss(0, 14)) for _ in range(3 * FRAME)]).tobytes()
    skip = max(0, request_lead - pause_ms // FRAME_MS)   # a lead longer than the pause would be laid over the name's end
    request, request_lead = request[skip * FRAME * 2:], request_lead - skip
    pieces = [(0.0, click), ((name_end * RATE // 1000 - len(name) // 2) / RATE, name), ((request_start - request_lead * FRAME_MS) / 1000, request)]
    return stream_of(pieces, level, tail_s=(history_ms + 900) / 1000 - request_start / 1000), name_end, request_start


def voiced_span(pcm: bytes) -> tuple[bytes, int, int]:
    """(pcm trimmed to six frames either side of its voiced part, leading frames, voiced frames): the clip as the splice places it."""
    levels = wake_window.frame_levels(pcm, RATE)
    quiet = max(0.3 * max(levels), 6.0)
    voiced = [i for i, v in enumerate(levels) if v > quiet]
    first, last = max(0, voiced[0] - 6), min(len(levels), voiced[-1] + 7)
    return pcm[first * FRAME * 2:last * FRAME * 2], voiced[0] - first, voiced[-1] - voiced[0] + 1


def decode(path: str) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"], capture_output=True, check=True).stdout


async def recognise(pcm: bytes, prime: bytes, uri: str, lead_voiced_ms: int = 0) -> str:
    """What the gateway's recognition path writes for `pcm`: its Endpoint (VAD primed on `prime`), judge and level front-end, the recognizer."""
    import webrtcvad

    import stt_wyoming
    tuning = stt_wyoming.Tuning.from_env(os.environ)
    endpoint = stt_wyoming.Endpoint(webrtcvad.Vad(2), RATE, silence_ms=tuning.silence_ms, vad_gain=tuning.vad_gain)
    if prime:
        endpoint.prime(prime)
    utterances = []
    for offset in range(0, len(pcm), FRAME * 2 * 3):
        utterances.extend(endpoint.feed(pcm[offset:offset + FRAME * 2 * 3]))
    tail = endpoint.flush(include_partial=True)
    texts = []
    pending = lead_voiced_ms
    for utt in utterances + ([tail] if tail else []):
        if pending and utt.voiced_ms >= stt_wyoming.LEAD_MIN_OWN_MS:   # what the gateway does: the cut name counts for the first real utterance that passes with it
            led = stt_wyoming.replace(utt, lead_voiced_ms=pending)
            if stt_wyoming.judge(led, tuning) is None:
                utt, pending = led, 0
        if stt_wyoming.judge(utt, tuning):
            continue
        lifted, _ = stt_wyoming.frontend_for_recognizer(utt.speech_pcm, utt.voiced_rms, tuning)
        texts.append(await asyncio.wait_for(stt_wyoming.transcribe(uri, lifted, RATE, "ru"), 40))
    return " ".join(t for t in texts if t).strip()


def cut_of(pcm: bytes, history_ms: int) -> wake_window.Located:
    return wake_window.locate(wake_window.frame_levels(pcm[:(history_ms + wake_window.LOOKAHEAD_MS) * RATE // 1000 * 2], RATE), history_ms)


def run_sessions(since: str, uri: str, db: str, archive: str) -> int:
    journal = subprocess.run(["journalctl", "--user", "-u", "aisis-station-stt", "--since", since, "--no-pager", "-o", "short-iso-precise"],
                             capture_output=True, text=True, check=True).stdout
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    clips = conn.execute("SELECT id, ts, path, dur, voiced_ms, floor_rms FROM voice_clips ORDER BY ts").fetchall()
    print("%-18s %-6s %-9s %-30s %-30s" % ("session", "H ms", "cut ms", "uncut", "cut"))
    for session in journal_sessions(journal):
        if not HISTORY_RANGE[0] <= session["history"] <= HISTORY_RANGE[1]:
            continue
        pieces, floors, taken = [], [], set()
        for cut in session["cuts"]:
            clip = next((c for c in clips if c[0] not in taken and cut["at"] - 0.2 <= c[1] <= cut["at"] + 4 and c[3] is not None
                         and abs(c[3] - cut["pcm"] / (2 * session["rate"])) < 0.7), None)
            if clip and os.path.exists(os.path.join(archive, clip[2])):
                taken.add(clip[0])
                pieces.append((cut["start"], decode(os.path.join(archive, clip[2]))))
                if clip[5]:
                    floors.append(clip[5])
        if not pieces:
            continue
        level = sorted(floors)[len(floors) // 2] if floors else 4.0
        pcm = stream_of(pieces, level)
        history_ms = int(round(session["history"] * 1000))
        found = cut_of(pcm, history_ms)
        uncut = asyncio.run(recognise(pcm, b"", uri))
        if not uncut:
            continue
        cut = asyncio.run(recognise(pcm[found.end_ms * RATE // 1000 * 2:], pcm[:found.end_ms * RATE // 1000 * 2], uri, found.voiced_ms)) if found.end_ms is not None else None
        print("%-18s %-6d %-9s %-30s %-30s" % (datetime.datetime.fromtimestamp(session["open"]).strftime("%m-%d %H:%M:%S"), history_ms,
                                               found.end_ms if found.end_ms is not None else found.reason, uncut[:29], "(not cut)" if cut is None else cut[:29]), flush=True)
    return 0


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", (text or "").lower().replace("ё", "е"))


def edits(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        row = [i]
        for j, right in enumerate(b, 1):
            row.append(min(previous[j] + 1, row[j - 1] + 1, previous[j - 1] + (left != right)))
        previous = row
    return previous[-1]


def same_word(heard: str, wanted: str) -> bool:
    """The recogniser's variants of one word (a case ending, a dropped letter) are the same word."""
    return edits(heard, wanted) <= max(1, len(wanted) // 4)


def outcome(heard: str, reference: str) -> str:
    """How `heard` (a transcript of the spliced stream) relates to `reference` (the request alone): ok; leak (words in front of the
    request's first word that the request does not have itself: the name); lost (the request's first word is not among the first three);
    empty."""
    words, wanted = tokens(heard), tokens(reference)
    if not words or not wanted:
        return "empty"
    for index, word in enumerate(words[:3]):
        if same_word(word, wanted[0]):
            return "leak" if any(w not in wanted for w in words[:index]) else "ok"
    return "lost"


def run_stress(names: list[str], requests: list[str], uri: str, db: str, archive: str, level: float, history_ms: int = 2330) -> int:
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)

    def clip(clip_id: str) -> tuple[bytes, int, int]:
        path = conn.execute("SELECT path FROM voice_clips WHERE id=?", (clip_id,)).fetchone()[0]
        return voiced_span(decode(os.path.join(archive, path)))
    references = {}
    for request_id in requests:
        alone, lead, _ = clip(request_id)
        references[request_id] = asyncio.run(recognise(stream_of([(1.0, alone)], level), b"", uri))
    counts: dict[tuple[str, str], int] = {}
    for name_id in names:
        name, name_lead, name_voiced = clip(name_id)
        for request_id in requests:
            request, request_lead, _ = clip(request_id)
            for before in NAME_ENDS_BEFORE_MS:
                for pause in PAUSES_MS:
                    pcm, name_end, request_start = splice(name[:(name_lead + name_voiced) * FRAME * 2], request, history_ms, before, pause, level, request_lead)
                    found = cut_of(pcm, history_ms)
                    uncut = outcome(asyncio.run(recognise(pcm, b"", uri)), references[request_id])
                    if found.end_ms is None:
                        cut = "not_cut:" + found.reason
                    else:
                        at = found.end_ms * RATE // 1000 * 2
                        cut = outcome(asyncio.run(recognise(pcm[at:], pcm[:at], uri, found.voiced_ms)), references[request_id])
                    counts[(uncut, cut)] = counts.get((uncut, cut), 0) + 1
    total = sum(counts.values())
    print("references:", {k[4:15]: v for k, v in references.items()})
    print("%d spliced streams; uncut -> cut" % total)
    for (uncut, cut), n in sorted(counts.items()):
        print("  %-6s -> %-18s %d" % (uncut, cut, n))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("sessions", "stress"))
    parser.add_argument("--since", default="2026-11-10")
    parser.add_argument("--names", nargs="*", default=[])
    parser.add_argument("--requests", nargs="*", default=[])
    parser.add_argument("--level", type=float, default=2.5, help="room noise RMS between the clips of a stress stream")
    args = parser.parse_args()
    uri = os.environ.get("AISIS_WYOMING_URI", "tcp://127.0.0.1:10300")
    db = os.environ.get("AISIS_DIALOGS_DB") or os.path.expanduser("~/.local/share/aisis/dialogs.sqlite3")
    archive = os.environ.get("AISIS_VOICE_ARCHIVE") or os.path.expanduser("~/.local/share/aisis/voice-archive")
    if args.mode == "sessions":
        return run_sessions(args.since, uri, db, archive)
    return run_stress(args.names, args.requests, uri, db, archive, args.level)


if __name__ == "__main__":
    sys.exit(main())
