#!/usr/bin/env python3
"""Labelled audio set of Russian command confusables (aisis ticket #182): the truth of every clip is known because the clip is synthesised from it.

A confusable pair is two commands one word apart that mean the opposite (включи/выключи свет, громче/тише, да/нет, ...). A recogniser that
writes the other member does a wrong thing with confidence, which is what the correction policy has to catch; this set is what its thresholds
are chosen on. Split: `synthetic` ONLY. Every clip is made by a TTS engine, so no number measured on it says anything about the owner's voice
(the real clips are measured separately by `bench_confusables.py real`).

Manifest line: {id, text (truth), pair_set, polarity ("a": first member of the pair, "b": the opposite), voice, augmentation, path, split,
engine, speed, seconds, peak, rms}. Clips are 16 kHz mono 16-bit WAV, one per phrase x voice x augmentation (the full product, so every
cell of the summary tables has the same n).

TTS: Piper (rhasspy) Russian medium voices denis, dmitri, irina, ruslan, run locally through the `piper-tts` wheel (ONNX Runtime, CPU, no
network and no speaker at synthesis time). Fetch the models once (sherpa-onnx `vits-piper-ru_RU-<voice>-medium` archives hold the same
.onnx, its .onnx.json config (present in the archives, checked) and espeak-ng-data) and run THIS file with an interpreter that has piper-tts and numpy:

    python confusables_corpus.py build [--out ~/.local/share/aisis/corpus/confusables] [--models <dir of vits-piper-ru_RU-*>] [--limit N]

Augmentations mimic the station microphone. The real spotter clip behind tg#30662 has peak 91 and RMS ~8 on the 16-bit scale (about -51 dBFS
peak) and is lifted x60 by the gateway front-end before recognition; `station` reproduces that level (speech peak 91, voiced RMS ~22 against the real clip's 13-25, plus a 1 LSB RMS microphone hiss like its pauses), the others add what
a room does: less gain, a telephone band, room noise at a given speech-to-noise ratio, a short reverb, and all of it together (`far`).
Reverb is ffmpeg's `aecho`; the rest is numpy. Noise is pink noise seeded by the clip id, so a rebuild is bit-identical.

stdlib only at import time (numpy, piper and ffmpeg are used inside `build`), so the phrase tables and the polarity detector are testable
anywhere.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import subprocess
import sys
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path

RATE = 16000
FRAME = RATE // 50
LEAD_S = 0.3        # silence before the phrase: the gateway's endpointer keeps ~200 ms of pre-roll
TAIL_S = 0.6
VOICES = ("denis", "dmitri", "irina", "ruslan")
# Piper length_scale by phrasing variant index: speaking rate varies across variants (1.0 normal, >1 slower), the same for every voice.
LENGTH_SCALE = (1.0, 1.1, 0.9, 1.0)
DEFAULT_OUT = Path.home() / ".local/share/aisis/corpus/confusables"
REVERB = "aecho=0.8:0.8:40|70:0.35|0.25"
BAND = "highpass=f=300,lowpass=f=3400"


@dataclass(frozen=True)
class PairSet:
    """Two opposite commands. `a`/`b`: phrasing variants of the first member and of the opposite; `mark_a`/`mark_b`: what a transcript
    contains when it says that member (regexes on the normalised text, see `normalize`)."""
    id: str
    a: tuple[str, ...]
    b: tuple[str, ...]
    mark_a: str
    mark_b: str


def _device(id_: str, thing: str) -> PairSet:
    forms = ("{v} " + thing, "{v} " + thing + " пожалуйста", "пожалуйста {v} " + thing, "{v} " + thing + " сейчас")
    return PairSet(id_, tuple(f.format(v="включи") for f in forms), tuple(f.format(v="выключи") for f in forms),
                   r"\b(?:включ|зажги)", r"\b(?:выключ|отключ|погаси)")


PAIR_SETS: tuple[PairSet, ...] = (
    _device("light", "свет"),
    _device("light-kitchen", "свет на кухне"),
    _device("light-bedroom", "свет в спальне"),
    _device("light-hall", "свет в коридоре"),
    _device("tv", "телевизор"),
    _device("kettle", "чайник"),
    _device("fan", "вентилятор"),
    _device("nightlight", "ночник"),
    PairSet("volume", ("громче", "сделай громче", "сделай погромче", "громче пожалуйста"),
            ("тише", "сделай тише", "сделай потише", "тише пожалуйста"), r"громч", r"тиш"),
    PairSet("volume-step", ("добавь громкость", "добавь громкости", "добавь немного громкости", "добавь громкость пожалуйста"),
            ("убавь громкость", "убавь громкости", "убавь немного громкости", "убавь громкость пожалуйста"),
            r"\b(?:до|при)бав", r"\bубав|уменьш"),
    PairSet("curtain", ("открой штору", "открой шторы", "открой шторы в спальне", "открой шторы пожалуйста"),
            ("закрой штору", "закрой шторы", "закрой шторы в спальне", "закрой шторы пожалуйста"), r"\bоткр", r"\bзакр"),
    PairSet("yes-no", ("да", "да, пожалуйста", "да, давай", "да, конечно"), ("нет", "нет, спасибо", "нет, не надо", "нет, не нужно"),
            r"\bда\b", r"\bнет\b"),
    PairSet("pause", ("пауза", "поставь на паузу", "поставь на паузу пожалуйста", "нажми паузу"),
            ("продолжи", "продолжи воспроизведение", "продолжи пожалуйста", "давай продолжим"), r"\bпауз|\bприостанов", r"\bпродолж(?!ени)"),
    PairSet("call", ("позвони", "позвони маме", "позвони пожалуйста", "позвони папе"),
            ("не звони", "не звони маме", "не звони пожалуйста", "не звони папе"),
            r"(?<![а-я])(?<!\bне )(?:по)?звони", r"\bне (?:по)?звони|\bне звон"),
)
PAIR_SET_BY_ID = {pair.id: pair for pair in PAIR_SETS}


STATION_PEAK = 91
STATION_FLOOR = 1.0   # the pauses of the real clip sit at 0.8-1.4 LSB RMS; its voiced frames at 13-25


@dataclass(frozen=True)
class Augmentation:
    """What happens to a phrase after synthesis. `peak`: 16-bit peak of the SPEECH (noise comes on top); `snr_db`: voiced speech RMS over
    noise RMS, None for no noise; `filters`: ffmpeg filters applied before the noise."""
    name: str
    peak: int
    filters: tuple[str, ...] = ()
    snr_db: float | None = None
    floor: float = STATION_FLOOR


AUGMENTATIONS: tuple[Augmentation, ...] = (
    Augmentation("clean", 16000, floor=0.0),
    Augmentation("station", STATION_PEAK),
    Augmentation("lowgain", 35),
    Augmentation("band", STATION_PEAK, (BAND,)),
    Augmentation("snr20", STATION_PEAK, (), 20.0),
    Augmentation("snr10", STATION_PEAK, (), 10.0),
    Augmentation("snr5", STATION_PEAK, (), 5.0),
    Augmentation("reverb", STATION_PEAK, (REVERB,)),
    Augmentation("far", 35, (BAND, REVERB), 10.0),
)
AUGMENTATION_BY_NAME = {aug.name: aug for aug in AUGMENTATIONS}


@dataclass(frozen=True)
class Phrase:
    pair_set: str
    polarity: str
    variant: int
    text: str


def phrases() -> list[Phrase]:
    out = []
    for pair in PAIR_SETS:
        for polarity, texts in (("a", pair.a), ("b", pair.b)):
            out.extend(Phrase(pair.id, polarity, index, text) for index, text in enumerate(texts))
    return out


def clip_id(phrase: Phrase, voice: str, augmentation: str) -> str:
    return f"{phrase.pair_set}-{phrase.polarity}{phrase.variant}-{voice}-{augmentation}"


def manifest_rows(voices: tuple[str, ...] = VOICES, augmentations: tuple[Augmentation, ...] = AUGMENTATIONS) -> list[dict]:
    """One row per phrase x voice x augmentation (the product, so every table cell has the same n); measured fields are added by `build`."""
    rows = []
    for phrase in phrases():
        for voice in voices:
            for aug in augmentations:
                id_ = clip_id(phrase, voice, aug.name)
                rows.append({"id": id_, "text": phrase.text, "pair_set": phrase.pair_set, "polarity": phrase.polarity, "voice": voice,
                             "augmentation": aug.name, "path": f"clips/{id_}.wav", "split": "synthetic", "engine": "piper",
                             "speed": LENGTH_SCALE[phrase.variant % len(LENGTH_SCALE)]})
    return rows


def normalize(text: str) -> str:
    """Lower case, ё as е, everything that is not a letter or digit is a space, one space between words."""
    return " ".join(re.sub(r"[^\w]+|_", " ", (text or "").lower().replace("ё", "е")).split())


def same_text(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def heard_polarity(pair_set: str, text: str) -> str:
    """Which member of the pair a transcript says: "a", "b", "both" (marks of each) or "none" (neither: a drop, a hallucination, other words)."""
    pair = PAIR_SET_BY_ID[pair_set]
    said = normalize(text)
    a, b = bool(re.search(pair.mark_a, said)), bool(re.search(pair.mark_b, said))
    return "both" if a and b else "a" if a else "b" if b else "none"


def opposite(polarity: str) -> str:
    return "b" if polarity == "a" else "a"


def verdict(row: dict, text: str) -> dict:
    """How a transcript reads against the clip's truth: `exact` (same normalised text), `heard` (polarity said), `flip` (the OPPOSITE member)."""
    heard = heard_polarity(row["pair_set"], text)
    return {"exact": same_text(row["text"], text), "heard": heard, "flip": heard == opposite(row["polarity"])}


# ---------------------------------------------------------------- audio (numpy, ffmpeg and piper are imported here only)

def voiced_rms(x) -> float:
    """RMS over the 20 ms frames whose own RMS is at least 30 % of the loudest frame's: the level of the speech, not of the pauses."""
    import numpy as np
    count = len(x) // FRAME
    if count == 0:
        return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0
    frames = x[:count * FRAME].astype("float64").reshape(count, FRAME)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    loud = energy >= 0.3 * energy.max()
    return float(np.sqrt((frames[loud] ** 2).mean())) if loud.any() else 0.0


def pink_noise(samples: int, seed: int):
    import numpy as np
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(samples))
    freqs = np.arange(len(spectrum), dtype="float64")
    freqs[0] = 1.0
    shaped = np.fft.irfft(spectrum / np.sqrt(freqs), n=samples)
    return shaped / np.sqrt(np.mean(np.square(shaped)))


def condition(speech, aug: Augmentation, seed: int):
    """int16 array: float speech at 16 kHz (padded) through the augmentation. `speech` already went through the ffmpeg filters."""
    import numpy as np
    gain = aug.peak / max(1e-9, float(np.abs(speech).max()))
    mixed = speech * gain
    if aug.floor:
        mixed = mixed + np.random.default_rng(seed ^ 0x5EED).standard_normal(len(speech)) * aug.floor
    if aug.snr_db is not None:
        noise = pink_noise(len(speech), seed)
        mixed = mixed + noise * voiced_rms(speech * gain) / (10 ** (aug.snr_db / 20))
    return np.clip(np.rint(mixed), -32768, 32767).astype("<i2")


def resample_and_filter(samples, rate: int, filters: tuple[str, ...]):
    """float32 mono at `rate` -> float32 at 16 kHz through ffmpeg (always the resample, plus `filters`)."""
    import numpy as np
    chain = ",".join(filters) if filters else "anull"
    raw = np.asarray(samples, dtype="<f4").tobytes()
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0", "-af", chain,
                          "-f", "f32le", "-ar", str(RATE), "-ac", "1", "pipe:1"], input=raw, capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(out, dtype="<f4").astype("float64")


def write_wav(path: Path, pcm) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


def synthesize(voice, text: str, length_scale: float):
    """(float32 samples, rate) of `text` from a loaded PiperVoice."""
    import numpy as np
    from piper import SynthesisConfig
    chunks = list(voice.synthesize(text, SynthesisConfig(length_scale=length_scale)))
    return np.concatenate([chunk.audio_float_array for chunk in chunks]), chunks[0].sample_rate


def build(out: Path, models: Path, limit: int | None = None) -> list[dict]:
    """Synthesise every phrase x voice once, run every augmentation, write the clips and `manifest.jsonl` (measured fields added).

    Under the lock the benchmark holds from reading the manifest to its last measurement, so a rebuild never changes clips
    under a running benchmark, and never runs over measurements (they are keyed by clip id and resumed without looking at the audio)."""
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.jsonl.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (out / "results.jsonl").exists():
            raise FileExistsError(f"{out / 'results.jsonl'} holds measurements of the current clips: move it away before rebuilding")
        return _build_locked(out, models, limit)


def _build_locked(out: Path, models: Path, limit: int | None) -> list[dict]:
    import numpy as np
    from piper import PiperVoice
    (out / "manifest.jsonl").unlink(missing_ok=True)   # first: a rebuild that fails half way must not leave the old manifest beside changed clips
    rows = manifest_rows()
    if limit is not None:
        rows = rows[:limit]
    loaded: dict[str, object] = {}
    base: dict[tuple[str, str, float], tuple] = {}
    for row in rows:
        voice_name = row["voice"]
        if voice_name not in loaded:
            loaded[voice_name] = PiperVoice.load(next((models / (f"vits-piper-ru_RU-{voice_name}-medium")).glob("*.onnx")))
        key = (voice_name, row["text"], row["speed"])
        if key not in base:
            samples, rate = synthesize(loaded[voice_name], row["text"], row["speed"])
            padded = np.concatenate([np.zeros(int(LEAD_S * rate)), samples, np.zeros(int(TAIL_S * rate))])
            base[key] = (padded, rate)
    filtered: dict[tuple, object] = {}
    for row in rows:
        aug = AUGMENTATION_BY_NAME[row["augmentation"]]
        padded, rate = base[(row["voice"], row["text"], row["speed"])]
        cache_key = (row["voice"], row["text"], row["speed"], aug.filters)
        if cache_key not in filtered:
            filtered[cache_key] = resample_and_filter(padded, rate, aug.filters)
        pcm = condition(filtered[cache_key], aug, zlib.crc32(row["id"].encode()))
        write_wav(out / row["path"], pcm)
        row.update({"trailing_ms": round(len(pcm) * 1000 / RATE - (len(padded) / rate - TAIL_S) * 1000),   # closing silence incl. what a filter (reverb echo) appended
                    "seconds": round(len(pcm) / RATE, 3), "peak": int(np.abs(pcm).max()), "rms": round(voiced_rms(pcm.astype("float64")), 1)})
    (out / "manifest.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    built = sub.add_parser("build", help="synthesise the clips and write manifest.jsonl")
    built.add_argument("--out", type=Path, default=DEFAULT_OUT)
    built.add_argument("--models", type=Path, default=None, help="directory holding vits-piper-ru_RU-<voice>-medium (default: <out>/tts-models)")
    built.add_argument("--limit", type=int, default=None, help="only the first N manifest rows (a smoke run)")
    args = parser.parse_args(argv)
    rows = build(args.out, args.models or args.out / "tts-models", args.limit)
    print(json.dumps({"clips": len(rows), "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
