"""Transcribe many 16 kHz mono WAV files through one Wyoming recogniser, a bounded number at a time.

One JSON line per file: {"id", "engine", "text", "ms", "err"}. `ms` is the wall time of the whole Wyoming exchange (connect, audio,
transcript), the number a gateway utterance pays. Files already present in the output are skipped, so an interrupted run resumes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stt_wyoming  # noqa: E402  (the gateway's own Wyoming client)
from asr_eval import private  # noqa: E402

RATE = 16000


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise ValueError("expected 16 kHz mono 16-bit WAV")
        pcm = handle.readframes(handle.getnframes())
        if len(pcm) != 2 * handle.getnframes():
            raise ValueError("truncated WAV data")
        return pcm


def audio_sha(pcm: bytes) -> str:
    return hashlib.sha1(pcm).hexdigest()[:12]


def done_ids(out: Path, shas: dict[str, str | None] | None = None) -> set[str]:
    """Ids whose last result line is a transcript (an empty text is one) of the audio the file holds now (`shas`: id -> fingerprint of
    the current audio; a result line without a fingerprint is an older run and stays valid). A line that carries an error is retried."""
    shas = shas or {}
    return {clip_id for clip_id, row in load(out).items()
            if not row.get("err") and (row.get("sha") is None or clip_id not in shas or row["sha"] == shas[clip_id])}


async def run(uri: str, engine: str, files: list[tuple[str, Path]], out: Path, concurrency: int = 2, language: str = "ru",
              timeout: float = 120.0) -> int:
    """Append one result line per (id, path) without a transcript in `out`; returns how many were transcribed now. A clip whose audio
    changed since its result was written (a rebuilt fixture under the same id) is transcribed again."""
    shas: dict[str, str | None] = {}
    for clip_id, path in files:
        try:
            shas[clip_id] = audio_sha(read_wav(path))
        except (OSError, EOFError, ValueError, wave.Error):   # EOFError: an empty or truncated WAV header
            shas[clip_id] = None   # unreadable now: the run records the error as the file's result line
    skip = done_ids(out, shas)
    todo = [(clip_id, path) for clip_id, path in files if clip_id not in skip]
    gate = asyncio.Semaphore(concurrency)
    with private.owner_only():
        out.parent.mkdir(parents=True, exist_ok=True)
        drop_partial_line(out)
        handle = private.open_text(out, "a")

    async def one(clip_id: str, path: Path) -> None:
        async with gate:
            text, err = "", None
            started = time.perf_counter()
            try:
                text = await stt_wyoming.transcribe(uri, read_wav(path), RATE, language, timeout=timeout)
            except Exception as exc:  # a failed file is a result line, not a crashed run
                err = f"{type(exc).__name__}: {str(exc)[:120]}"
            ms = int((time.perf_counter() - started) * 1000)
            handle.write(json.dumps({"id": clip_id, "engine": engine, "text": text, "ms": ms, "err": err, "sha": shas[clip_id]}, ensure_ascii=False) + "\n")
            handle.flush()

    try:
        await asyncio.gather(*(one(clip_id, path) for clip_id, path in todo))
    finally:
        handle.close()
    return len(todo)


def load(out: Path) -> dict[str, dict]:
    """id -> result line, the last line of an id winning. A last line without its newline is a write that was cut short (possibly in the
    middle of a UTF-8 character): ignored, the clip is transcribed again; an unreadable line anywhere else is corruption and raises."""
    rows: dict[str, dict] = {}
    if out.exists():
        data = out.read_bytes()
        for line in data[:data.rfind(b"\n") + 1].decode("utf-8").split("\n"):
            if line.strip():
                row = json.loads(line)
                rows[row["id"]] = row
    return rows


def drop_partial_line(out: Path) -> None:
    """Cut a half-written last line off `out` so the next append starts on a line of its own."""
    if out.exists():
        data = out.read_bytes()
        if data and not data.endswith(b"\n"):
            private.write_bytes(out, data[:data.rfind(b"\n") + 1])


@private.owner_only_cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--uri", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--manifest", required=True, help="JSON lines with `id` and `path` (absolute, or relative to the manifest)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args(argv)
    manifest = Path(args.manifest)
    files = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            files.append((row["id"], manifest.parent / row["path"]))
    count = asyncio.run(run(args.uri, args.engine, files, Path(args.out), args.concurrency))
    print(json.dumps({"engine": args.engine, "transcribed": count, "files": len(files)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
