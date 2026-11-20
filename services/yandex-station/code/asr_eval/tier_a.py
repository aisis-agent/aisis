"""Tier A of the recogniser acceptance (ticket #59): does it invent text on audio that holds no command?

Fixtures: digital silence, white/pink/brown noise, mains hum, room tone with clicks, the station's lifted microphone floor, synthetic
music, the activation earcon, and a Piper command followed by long digital silence (the classic trailing-caption trigger of Whisper).
Also reported, never part of the zero-text bar: `babble` (overlapped Piper voices, i.e. speech) and `archive-gated` (real clips the live
gateway dropped; their truth is unknown because a gated clip can hide quiet speech, so they are review items).

Bar: `nonspeech synthetic` (kind `nonspeech`) all empty AND `speech_tail` without an invented tail, per engine.

    build --out DIR [--parts nonspeech,speech,archive]   (speech needs piper; archive needs stt_wyoming + ffmpeg + the dialogs copy)
    run   --dir DIR --engine NAME --uri URI [--concurrency 2]
    score --dir DIR --engines a,b,c
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asr_eval import private  # noqa: E402

RATE = 16000
FRAME = RATE // 50
INTENTS = Path.home() / ".local/share/aisis/corpus/intents-ru.jsonl"
MODELS = Path.home() / ".local/share/aisis/corpus/confusables/tts-models"
SPEECH_DBFS = -27.0          # voiced RMS of speech fixtures: the level the gateway lifts the station's audio to is about -30
SILENCE_SECONDS = (0.5, 1, 2, 3, 5, 8, 12, 20)
LATENCY_REPEATS = 12         # identical 0.5 s silences: the /healthz/asr probe, repeated so its latency has a p95
NOISE_LEVELS = (-70, -60, -50, -45, -40, -35, -30)
NOISE_SECONDS = (2, 6)
HUM_HZ = (50, 100, 60)
HUM_LEVELS = (-60, -50, -45, -40, -30)
ROOM_LEVELS = (-55, -50, -45)
ROOM_SEEDS = 8
FLOOR_LSB = (1.0, 1.5, 2.0, 3.0, 4.0)
FLOOR_LIFT = 60              # the gateway's lift of a silent room
MUSIC_KINDS = ("chord-major", "chord-minor", "arpeggio-slow", "arpeggio-fast", "drums")
MUSIC_LEVELS = (-45, -35, -28, -20)
CHIME_TONES = ((880, 1320), (660, 990))
CHIME_LEVELS = (-45, -38, -30, -24, -18)
CHIME_PADS = ((0.3, 0.3), (1.0, 1.0), (2.0, 0.5), (0.5, 2.0))
# The station's own activation chime leaks into its microphone as a 0.2-0.5 s narrow-band tone at 310-318 Hz (ticket #158).
STATION_CHIME_HZ = (313, 316)
STATION_CHIME_SECONDS = (0.2, 0.35, 0.5)
STATION_CHIME_PADS = ((0.3, 0.3), (1.0, 1.0))
TAIL_SECONDS = (5, 15, 30, 60)
TAIL_VOICES = ("denis", "irina")
BABBLE_VOICES = ("denis", "dmitri", "irina", "ruslan")
BABBLE_COUNTS = (4, 6)
BABBLE_LEVELS = (-28, -40)
BABBLE_SEEDS = 3
BABBLE_SECONDS = 8
SYNTHETIC_CATEGORIES = ("silence", "white", "pink", "brown", "hum", "room", "station-floor", "music", "chime", "station-chime")
ALL_CATEGORIES = SYNTHETIC_CATEGORIES + ("speech-then-silence", "speech-control", "babble", "archive-gated")
EARCON_CATEGORIES = ("chime", "station-chime")   # tonal events: reported next to the natural sounds, both count towards the zero-text bar


# ---------------------------------------------------------------- plan and text logic (no numpy)

def intent_phrases(path: Path = INTENTS, step: int = 47, count: int = 10, offset: int = 0) -> list[str]:
    rows = [json.loads(line)["text"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[offset::step][:count]


def plan_nonspeech() -> list[dict]:
    """One spec per synthetic non-speech fixture; the audio is a function of the spec and the id-seeded RNG."""
    specs: list[dict] = []

    def add(fid: str, category: str, seconds: float, **params) -> None:
        specs.append({"id": fid, "category": category, "kind": "nonspeech", "seconds": seconds, "reference": "", "params": params})

    for sec in SILENCE_SECONDS:
        for rep in range(LATENCY_REPEATS if sec == 0.5 else 1):
            add(f"silence-{sec}s-r{rep}" if sec == 0.5 else f"silence-{sec}s", "silence", sec)
    for color in ("white", "pink", "brown"):
        for level in NOISE_LEVELS:
            for sec in NOISE_SECONDS:
                add(f"{color}-{-level}db-{sec}s", color, sec, level=level)
    for hz in HUM_HZ:
        for level in HUM_LEVELS:
            for floor in (False, True):
                add(f"hum-{hz}hz-{-level}db-{'floor' if floor else 'clean'}", "hum", 3, hz=hz, level=level, floor=floor)
    for level in ROOM_LEVELS:
        for seed in range(ROOM_SEEDS):
            add(f"room-{-level}db-s{seed}", "room", 6, level=level)
    for lsb in FLOOR_LSB:
        for sec in (3, 6):
            for seed in range(2):
                add(f"station-floor-{lsb}lsb-{sec}s-s{seed}", "station-floor", sec, lsb=lsb)
    for kind in MUSIC_KINDS:
        for level in MUSIC_LEVELS:
            for sec in (4, 8):
                add(f"music-{kind}-{-level}db-{sec}s", "music", sec, music=kind, level=level)
    for tones in CHIME_TONES:
        for level in CHIME_LEVELS:
            for lead, tail in CHIME_PADS:
                add(f"chime-{tones[0]}-{-level}db-{lead}+{tail}", "chime", round(lead + tail + 0.35, 2), tones=list(tones), level=level, lead=lead, tail=tail)
    for hz in STATION_CHIME_HZ:
        for dur in STATION_CHIME_SECONDS:
            for level in CHIME_LEVELS:
                for lead, tail in STATION_CHIME_PADS:
                    add(f"station-chime-{hz}hz-{dur}s-{-level}db-{lead}+{tail}", "station-chime", round(lead + tail + dur + 0.1, 2),
                        tones=[hz], dur=dur, level=level, lead=lead, tail=tail)
    return specs


def plan_speech(phrases: list[str]) -> list[dict]:
    """Each phrase and voice once with no silence after it (`speech_control`: what the engine writes for the bare command) and once per
    tail length (`speech_tail`). A tail clip is judged against its own control, so a number or abbreviation the engine spells out in words
    is not mistaken for an invented tail."""
    specs = []
    for index, text in enumerate(phrases):
        for voice in TAIL_VOICES:
            specs.append({"id": f"tail-p{index}-{voice}-0s", "category": "speech-control", "kind": "speech_control", "seconds": None,
                          "reference": text, "params": {"text": text, "voice": voice, "tail": 0}})
            for tail in TAIL_SECONDS:
                specs.append({"id": f"tail-p{index}-{voice}-{tail}s", "category": "speech-then-silence", "kind": "speech_tail", "seconds": None,
                              "reference": text, "params": {"text": text, "voice": voice, "tail": tail}})
    return specs


def plan_babble(phrases: list[str]) -> list[dict]:
    specs = []
    for count in BABBLE_COUNTS:
        for level in BABBLE_LEVELS:
            for seed in range(BABBLE_SEEDS):
                specs.append({"id": f"babble-{count}v-{-level}db-s{seed}", "category": "babble", "kind": "babble", "seconds": BABBLE_SECONDS,
                              "reference": "", "params": {"count": count, "level": level, "phrases": phrases}})
    return specs


def normalize(text: str) -> str:
    """Lower case, ё as е, punctuation dropped: Whisper/nemo print capitals and commas, GigaAM/T-one do not."""
    return " ".join(re.sub(r"[^\w]+|_", " ", (text or "").lower().replace("ё", "е")).split())


def control_id(tail_id: str) -> str:
    return re.sub(r"-\d+s$", "-0s", tail_id)


def invented_tail(control_text: str, text: str, caption_hit: bool) -> bool:
    """More words than the engine wrote for the same command without the silence, or a known caption. Different words in equal number are
    a misrecognition, not an invention."""
    return caption_hit or len(normalize(text).split()) > len(normalize(control_text).split())


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def load_manifest(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def score_engine(rows: list[dict], results: dict[str, dict], is_caption) -> dict:
    """Per category: n (clips with a result), missing, errors, nonempty, caption hits; speech_tail: invented/exact; plus the headline numbers
    and the latency of the 0.5 s silences. `hits` lists id+text of non-empty synthetic fixtures (private archive texts never leave here)."""
    cats: dict[str, dict] = {}
    hits: list[tuple[str, str, str]] = []
    latency = []
    for row in rows:
        cat = cats.setdefault(row["category"], {"kind": row["kind"], "n": 0, "missing": 0, "errors": 0, "nonempty": 0, "captions": 0,
                                                "invented": 0, "exact": 0})
        result = results.get(row["id"])
        if result is None:
            cat["missing"] += 1
            continue
        if result.get("err"):
            cat["errors"] += 1
            continue
        text = (result.get("text") or "").strip()
        caption = bool(text) and bool(is_caption(text))
        cat["n"] += 1
        cat["nonempty"] += bool(text)
        cat["captions"] += caption
        if row["kind"] == "speech_tail":
            control = results.get(control_id(row["id"]))
            control_text = row["reference"] if control is None or control.get("err") else control.get("text") or ""
            cat["invented"] += bool(text) and invented_tail(control_text, text, caption)
            cat["exact"] += bool(text) and normalize(text) == normalize(control_text)
        if text and row["kind"] in ("nonspeech", "speech_tail"):
            hits.append((row["id"], row["category"], text))
        if row["category"] == "silence" and row["seconds"] == 0.5:
            latency.append(result["ms"])
    syn = [c for c in cats.values() if c["kind"] == "nonspeech"]
    tail = [c for c in cats.values() if c["kind"] == "speech_tail"]
    earcons = [c for name, c in cats.items() if c["kind"] == "nonspeech" and name in EARCON_CATEGORIES]
    natural = [c for name, c in cats.items() if c["kind"] == "nonspeech" and name not in EARCON_CATEGORIES]
    head = {"synthetic_n": sum(c["n"] for c in syn), "synthetic_nonempty": sum(c["nonempty"] for c in syn),
            "natural_n": sum(c["n"] for c in natural), "natural_nonempty": sum(c["nonempty"] for c in natural),
            "earcon_n": sum(c["n"] for c in earcons), "earcon_nonempty": sum(c["nonempty"] for c in earcons),
            "tail_n": sum(c["n"] for c in tail), "tail_invented": sum(c["invented"] for c in tail),
            "incomplete": sum(c["missing"] + c["errors"] for c in cats.values() if c["kind"] != "nonspeech_archive")}
    head["pass"] = (head["synthetic_n"] > 0 and head["tail_n"] > 0   # no acceptance fixtures, no verdict
                    and head["synthetic_nonempty"] == 0 and head["tail_invented"] == 0 and head["incomplete"] == 0)
    return {"categories": cats, "headline": head, "hits": hits, "silence_0_5s_ms_p95": percentile(latency, 0.95), "silence_0_5s_n": len(latency)}


def format_report(rows: list[dict], scores: dict[str, dict]) -> str:
    out = []
    for engine, score in scores.items():
        head = score["headline"]
        out.append(f"== {engine}")
        out.append(f"nonspeech synthetic n={head['synthetic_n']} nonempty={head['synthetic_nonempty']}"
                   f"  (natural sounds n={head['natural_n']} nonempty={head['natural_nonempty']}; earcons n={head['earcon_n']} nonempty={head['earcon_nonempty']})")
        out.append(f"speech_tail n={head['tail_n']} invented_tail={head['tail_invented']}")
        out.append(f"bar (all fixtures answered, 0 non-empty, 0 invented tails): {'PASS' if head['pass'] else 'FAIL'}"
                   f"  (unanswered/errors={head['incomplete']})")
        p95 = score["silence_0_5s_ms_p95"]
        out.append(f"0.5 s digital silence latency p95={p95} ms over n={score['silence_0_5s_n']}")
        out.append(f"{'category':22}{'kind':19}{'n':>5}{'err':>5}{'miss':>5}{'nonempty':>9}{'caption':>8}{'invented':>9}{'exact':>6}")
        for cat in ALL_CATEGORIES:
            c = score["categories"].get(cat)
            if c:
                tail = c["kind"] == "speech_tail"
                out.append(f"{cat:22}{c['kind']:19}{c['n']:>5}{c['errors']:>5}{c['missing']:>5}{c['nonempty']:>9}{c['captions']:>8}"
                           f"{(c['invented'] if tail else '-'):>9}{(c['exact'] if tail else '-'):>6}")
        for fid, cat, text in score["hits"]:
            out.append(f"  HIT {fid} [{cat}]: {text!r}")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- audio (numpy; piper and the archive stack only in their parts)

def to_pcm(x):
    import numpy as np
    return np.clip(np.rint(x * 32768.0), -32768, 32767).astype("<i2")


def scale_to_dbfs(x, dbfs: float):
    import numpy as np
    rms = float(np.sqrt(np.mean(np.square(x))))
    return x * (10 ** (dbfs / 20) / rms) if rms > 0 else x


def voiced_rms(x) -> float:
    """RMS over the 20 ms frames within 30 % of the loudest frame's RMS: the level of speech, not of its pauses (full scale = 1.0)."""
    import numpy as np
    count = len(x) // FRAME
    if count == 0:
        return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0
    frames = x[:count * FRAME].astype("float64").reshape(count, FRAME)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    loud = energy >= 0.3 * energy.max()
    return float(np.sqrt((frames[loud] ** 2).mean())) if loud.any() else 0.0


def colored_noise(samples: int, exponent: float, rng):
    """Unit-RMS noise with power ~ 1/f^exponent (0 white, 1 pink, 2 brown)."""
    import numpy as np
    spectrum = np.fft.rfft(rng.standard_normal(samples))
    freqs = np.arange(len(spectrum), dtype="float64")
    freqs[0] = 1.0
    shaped = np.fft.irfft(spectrum / freqs ** (exponent / 2), n=samples)
    shaped -= shaped.mean()
    return shaped / np.sqrt(np.mean(np.square(shaped)))


def hum(samples: int, hz: int, rng):
    import numpy as np
    t = np.arange(samples) / RATE
    amps = (1.0, 0.6, 0.35, 0.25, 0.15, 0.1)
    return sum(a * np.sin(2 * np.pi * hz * (k + 1) * t + rng.uniform(0, 2 * np.pi)) for k, a in enumerate(amps))


def room(samples: int, level: float, rng):
    """Pink room tone with random clicks (decaying noise bursts) and thumps (decaying low sine), peaks near -25 dBFS."""
    import numpy as np
    x = scale_to_dbfs(colored_noise(samples, 1.0, rng), level)
    t = np.arange(int(0.15 * RATE)) / RATE
    for _ in range(int(rng.integers(3, 9))):
        at = int(rng.integers(0, samples - len(t)))
        if rng.random() < 0.5:
            x[at:at + len(t)] += 0.056 * np.exp(-t / 0.004) * rng.standard_normal(len(t))
        else:
            x[at:at + len(t)] += 0.056 * np.exp(-t / 0.04) * np.sin(2 * np.pi * rng.uniform(60, 120) * t)
    return x


def note(freq: float, seconds: float, decay: float = 0.6):
    import numpy as np
    t = np.arange(int(seconds * RATE)) / RATE
    tone = sum(a * np.sin(2 * np.pi * freq * (k + 1) * t) for k, a in enumerate((1.0, 0.5, 0.25)))
    return tone * np.exp(-t / decay) * np.minimum(1.0, t / 0.01)


def music(kind: str, seconds: float, rng):
    import numpy as np
    out = np.zeros(int(seconds * RATE))

    def put(sound, at: float) -> None:
        start = int(at * RATE)
        out[start:start + len(sound)] += sound[:max(0, len(out) - start)]

    root = float(rng.choice((196.0, 220.0, 261.63)))
    third = 2 ** (4 / 12) if kind != "chord-minor" else 2 ** (3 / 12)
    triad = (root, root * third, root * 2 ** (7 / 12))
    if kind.startswith("chord"):
        for bar in range(int(seconds // 2) + 1):
            for f in triad:
                put(note(f, 2.5, 1.0), bar * 2.0)
    elif kind.startswith("arpeggio"):
        step = 0.5 if kind.endswith("slow") else 0.15
        for i in range(int(seconds / step)):
            put(note(triad[i % 3] * (2 if (i // 3) % 2 else 1), 0.8, 0.3), i * step)
    else:
        t = np.arange(int(0.3 * RATE)) / RATE
        kick = np.sin(2 * np.pi * (50 + 120 * np.exp(-t / 0.03)) * t) * np.exp(-t / 0.08)
        snare = rng.standard_normal(len(t)) * np.exp(-t / 0.06)
        hat = rng.standard_normal(int(0.05 * RATE)) * np.exp(-np.arange(int(0.05 * RATE)) / RATE / 0.01)
        for beat in range(int(seconds / 0.25)):
            if beat % 4 == 0:
                put(kick, beat * 0.25)
            elif beat % 4 == 2:
                put(snare, beat * 0.25)
            put(hat * 0.4, beat * 0.25)
    return out


def chime(tones, lead: float, tail: float, level: float, dur: float = 0.12, harmonic: float = 0.0):
    """Rising two-tone earcon (two tones of `dur` s), level = RMS over the sounding part (it is a short event, so voiced-RMS-like).
    A single tone with a small second harmonic is the station's leaked activation chime."""
    import numpy as np
    t = np.arange(int(dur * RATE)) / RATE
    env = np.sin(np.pi * np.minimum(1.0, t / dur)) ** 0.5 * np.exp(-t / (1.25 * dur))
    sound = np.concatenate([(np.sin(2 * np.pi * f * t) + harmonic * np.sin(4 * np.pi * f * t)) * env for f in tones])
    sound = scale_to_dbfs(sound, level)
    return np.concatenate([np.zeros(int(lead * RATE)), sound, np.zeros(int(tail * RATE) + int(0.11 * RATE))])


def render_nonspeech(spec: dict):
    import numpy as np
    rng = np.random.default_rng(zlib.crc32(spec["id"].encode()))
    p, cat, samples = spec["params"], spec["category"], int(spec["seconds"] * RATE)
    if cat == "silence":
        return np.zeros(samples, dtype="<i2")
    if cat in ("white", "pink", "brown"):
        return to_pcm(scale_to_dbfs(colored_noise(samples, {"white": 0.0, "pink": 1.0, "brown": 2.0}[cat], rng), p["level"]))
    if cat == "hum":
        x = scale_to_dbfs(hum(samples, p["hz"], rng), p["level"])
        if p["floor"]:
            x = x + scale_to_dbfs(colored_noise(samples, 1.0, rng), p["level"] - 12)
        return to_pcm(x)
    if cat == "room":
        return to_pcm(room(samples, p["level"], rng))
    if cat == "station-floor":
        raw = np.rint(rng.standard_normal(samples) * p["lsb"])  # quantise at the microphone, then lift: the gateway's order
        return np.clip(raw * FLOOR_LIFT, -32768, 32767).astype("<i2")
    if cat == "music":
        return to_pcm(scale_to_dbfs(music(p["music"], spec["seconds"], rng), p["level"]))
    if cat == "chime":
        return to_pcm(chime(p["tones"], p["lead"], p["tail"], p["level"]))
    if cat == "station-chime":
        return to_pcm(chime(p["tones"], p["lead"], p["tail"], p["level"], dur=p["dur"], harmonic=0.1))
    raise ValueError(cat)


class Voices:
    """Piper voices loaded once, phrases synthesised once, everything at 16 kHz float."""

    def __init__(self, models: Path = MODELS) -> None:
        self.models, self.loaded, self.cache = models, {}, {}

    def say(self, voice: str, text: str):
        import numpy as np
        from piper import PiperVoice, SynthesisConfig
        if (voice, text) not in self.cache:
            if voice not in self.loaded:
                self.loaded[voice] = PiperVoice.load(next((self.models / f"vits-piper-ru_RU-{voice}-medium").glob("*.onnx")))
            chunks = list(self.loaded[voice].synthesize(text, SynthesisConfig(length_scale=1.0)))
            wave = np.concatenate([c.audio_float_array for c in chunks]).astype("float64")
            rate = chunks[0].sample_rate
            n = int(len(wave) * RATE / rate)
            self.cache[(voice, text)] = np.interp(np.linspace(0, len(wave) - 1, n), np.arange(len(wave)), wave)
        return self.cache[(voice, text)]


def render_speech_tail(spec: dict, voices: Voices):
    import numpy as np
    p = spec["params"]
    speech = voices.say(p["voice"], p["text"])
    speech = speech * (10 ** (SPEECH_DBFS / 20) / voiced_rms(speech))
    return to_pcm(np.concatenate([np.zeros(int(0.3 * RATE)), speech, np.zeros(int(0.4 * RATE)), np.zeros(int(p["tail"] * RATE))]))


def render_babble(spec: dict, voices: Voices):
    import numpy as np
    rng = np.random.default_rng(zlib.crc32(spec["id"].encode()))
    p, total = spec["params"], int(BABBLE_SECONDS * RATE)
    mix = np.zeros(total)
    for i in range(p["count"]):
        voice, cursor = BABBLE_VOICES[i % len(BABBLE_VOICES)], int(rng.uniform(0, 1.5) * RATE)
        while cursor < total:
            wave = voices.say(voice, p["phrases"][int(rng.integers(len(p["phrases"])))])
            wave = wave * (10 ** (SPEECH_DBFS / 20) / voiced_rms(wave))
            end = min(total, cursor + len(wave))
            mix[cursor:end] += wave[:end - cursor]
            cursor = end + int(rng.uniform(0.2, 1.0) * RATE)
    return to_pcm(mix * (10 ** (p["level"] / 20) / voiced_rms(mix)))


def write_wav(path: Path, pcm) -> None:
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with private.open_binary(path) as raw, wave.open(raw, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


def archive_rows(directory: Path) -> list[dict]:
    """archive_rows_private with every directory and file created owner-only (household audio)."""
    from asr_eval import private
    with private.owner_only():
        return archive_rows_private(directory)


def archive_rows_private(directory: Path) -> list[dict]:
    """Gateway-lifted copies of the archive clips the gateway gated or blacklisted, under DIR/private/. Needs FFMPEG and
    AISIS_DIALOGS_DB (a copy of the dialogs DB); opened read-only. Texts are never read."""
    import os
    import sqlite3
    import bench_asr_archive as archive
    import voiceclips
    conn = sqlite3.connect(f"file:{os.environ['AISIS_DIALOGS_DB']}?mode=ro", uri=True)
    clips = conn.execute("SELECT id, path, voiced_ms FROM voice_clips WHERE status IN ('gate','blacklist') ORDER BY id").fetchall()
    rows = []
    for clip_id, path, voiced_ms in clips:
        try:
            pcm = to_pcm(archive.lift(archive.decode(str(voiceclips.root() / path)), voiced_ms))
        except Exception:  # an undecodable clip is skipped and counted below
            continue
        rel = f"private/arch-{clip_id}.wav"
        write_wav(directory / rel, pcm)
        rows.append({"id": f"arch-{clip_id}", "path": rel, "category": "archive-gated", "kind": "nonspeech_archive",
                     "seconds": round(len(pcm) / RATE, 3), "reference": ""})
    print(f"archive clips: {len(clips)} selected, {len(rows)} written", file=sys.stderr)
    return rows


# ---------------------------------------------------------------- commands

def cmd_build(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    parts = set(args.parts.split(","))
    manifest_path = out / "manifest.jsonl"
    kept = [r for r in (load_manifest(out) if manifest_path.exists() else [])
            if not (("nonspeech" in parts and r["kind"] == "nonspeech") or ("speech" in parts and r["kind"] in ("speech_tail", "speech_control", "babble"))
                    or ("archive" in parts and r["kind"] == "nonspeech_archive"))]
    new: list[dict] = []

    def emit(spec: dict, pcm) -> None:
        rel = f"audio/{spec['id']}.wav"
        write_wav(out / rel, pcm)
        new.append({"id": spec["id"], "path": rel, "category": spec["category"], "kind": spec["kind"],
                    "seconds": round(len(pcm) / RATE, 3), "reference": spec["reference"]})

    if "nonspeech" in parts:
        for spec in plan_nonspeech():
            emit(spec, render_nonspeech(spec))
    if "speech" in parts:
        voices = Voices()
        for spec in plan_speech(intent_phrases()):
            emit(spec, render_speech_tail(spec, voices))
        for spec in plan_babble(intent_phrases(offset=5, count=12)):
            emit(spec, render_babble(spec, voices))
    if "archive" in parts:
        new += archive_rows(out)
    rows = kept + new
    private.write_text(manifest_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    print(json.dumps({"total": len(rows), "per_category": counts}, ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    from asr_eval import wyoming_batch  # imports aiohttp: not needed (and absent from the piper venv) for `build`
    directory = Path(args.dir)
    files = [(r["id"], directory / r["path"]) for r in load_manifest(directory)]
    done = asyncio.run(wyoming_batch.run(args.uri, args.engine, files, directory / f"results-{args.engine}.jsonl", args.concurrency))
    print(json.dumps({"engine": args.engine, "transcribed": done, "files": len(files)}))
    return 0


def cmd_score(args) -> int:
    import stt_wyoming
    from asr_eval import wyoming_batch
    directory = Path(args.dir)
    rows = load_manifest(directory)
    scores = {engine: score_engine(rows, wyoming_batch.load(directory / f"results-{engine}.jsonl"), stt_wyoming.is_silence_hallucination)
              for engine in args.engines.split(",")}
    report = format_report(rows, scores)
    private.write_text(directory / "report.txt", report)
    print(report)
    return 0


@private.owner_only_cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build")
    build.add_argument("--out", required=True)
    build.add_argument("--parts", default="nonspeech,speech,archive")
    run = sub.add_parser("run")
    run.add_argument("--dir", required=True)
    run.add_argument("--engine", required=True)
    run.add_argument("--uri", required=True)
    run.add_argument("--concurrency", type=int, default=2)
    score = sub.add_parser("score")
    score.add_argument("--dir", required=True)
    score.add_argument("--engines", required=True)
    args = parser.parse_args(argv)
    return {"build": cmd_build, "run": cmd_run, "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
