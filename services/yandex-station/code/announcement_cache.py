"""Prepared Bracho speech on the existing Quasar TTS channel.

Normal speech never enters this path. Missing or invalid cache markers fail closed
instead of being spoken or sent to a synthesis provider.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

VOICE_ID = "5cd1ffa5dbb4419cb5766435139c6d92"
PREFIX = "[ha-remote:"
MARKER = re.compile(r"^\[ha-remote:([0-9a-f]{64})\]$")
KEY = re.compile(r"^[0-9a-f]{64}$")
GENERATION = re.compile(r"^generation-[a-z0-9_]+$")
TARGETS = {"chromecast", "tv", "kodi", "projector", "speaker"}
DEFAULT_FOLDER = Path.home() / ".cache/aisis/remote-announcements"


class CacheNotReady(Exception):
    pass


class CacheValidationError(ValueError):
    """A fixed diagnostic message; never contains input URLs or response bodies."""


def is_marker(text: str) -> bool:
    # Near-miss control markers must fail closed, never enter a voice provider.
    return text.lstrip("\ufeff \t\r\n\v\f").startswith(PREFIX)


def valid_hash(value) -> bool:
    return isinstance(value, str) and KEY.fullmatch(value) is not None


def validate_metadata(info, *, key_required=False):
    if not isinstance(info, dict) or not valid_hash(info.get("sha256")):
        raise CacheValidationError("Invalid clip checksum metadata")
    if key_required and not valid_hash(info.get("key")):
        raise CacheValidationError("Invalid clip key")
    duration = info.get("duration")
    if type(duration) not in (int, float) or not 0 < duration <= 2:
        raise CacheValidationError("Invalid clip duration; expected 0 to 2 seconds")
    return info


def manifest_clips(manifest):
    if not isinstance(manifest, dict) or manifest.get("voice_id") != VOICE_ID:
        raise CacheValidationError("Only the configured Bracho voice is supported")
    clips = manifest.get("clips")
    if not isinstance(clips, dict) or set(clips) != TARGETS:
        raise CacheValidationError("Only the configured five announcements are supported")
    for info in clips.values():
        validate_metadata(info, key_required=True)
    if len({info["key"] for info in clips.values()}) != len(TARGETS):
        raise CacheValidationError("Announcement keys must be distinct")
    return clips


@contextmanager
def cache_lock(folder, *, exclusive=False):
    descriptor = os.open(folder / ".import.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(descriptor)


@lru_cache(maxsize=8)
def decoded_duration(audio: bytes) -> float:
    """Validate all streams through a single-audio-stream PCM muxer and cache bytes."""
    try:
        result = subprocess.run([os.environ.get("FFMPEG", "ffmpeg"), "-v", "error", "-xerror",
            "-nostdin", "-f", "ogg", "-c:a", "opus", "-i", "pipe:0", "-map", "0:a",
            "-vn", "-ac", "1", "-ar", "8000", "-t", "2.1", "-c:a", "pcm_s16le",
            "-f", "s16le", "pipe:1"], input=audio, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CacheNotReady("announcement audio decoder unavailable") from error
    duration = len(result.stdout) / 16000
    if result.returncode or not 0 < duration <= 2:
        raise CacheNotReady("announcement audio is invalid or exceeds 2 seconds")
    return duration


def validate_audio(audio, info):
    if len(audio) > 65536:
        raise CacheNotReady("announcement exceeds size limit")
    if hashlib.sha256(audio).hexdigest() != info["sha256"]:
        raise CacheNotReady("announcement checksum mismatch")
    if not audio.startswith(b"OggS") or b"OpusHead" not in audio[:256]:
        raise CacheNotReady("announcement must be Ogg Opus")
    if abs(decoded_duration(audio) - info["duration"]) > 0.03:
        raise CacheNotReady("announcement duration does not match metadata")


def _read_clip(text, folder, expected_sha=None):
    match = MARKER.fullmatch(text)
    if not match:
        raise CacheNotReady("invalid announcement marker")
    key = match.group(1)
    try:
        metadata = validate_metadata(json.loads((folder / (key + ".json")).read_text()))
        path = folder / (key + ".ogg")
        with path.open("rb") as stream:
            audio = stream.read(65537)
        if (metadata["voice"] != VOICE_ID
                or (expected_sha is not None and metadata["sha256"] != expected_sha)):
            raise CacheNotReady("announcement cache validation failed")
        validate_audio(audio, metadata)
        return audio
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise CacheNotReady("announcement cache unavailable") from error


def current_folder(folder):
    """Resolve the one published generation; support caches from the first importer."""
    current = folder / "current"
    if not current.is_symlink():
        if current.exists():
            raise CacheNotReady("invalid announcement generation pointer")
        return folder
    name = os.readlink(current)
    if not GENERATION.fullmatch(name) or (folder / name).is_symlink():
        raise CacheNotReady("invalid announcement generation pointer")
    return folder / name


def retained_clip(text, folder, expected_sha):
    """Resolve queued keys only through the committed generation history."""
    match = MARKER.fullmatch(text)
    if not match:
        raise CacheNotReady("invalid announcement marker")
    generation = current_folder(folder)
    visited = set()
    while generation not in visited:
        visited.add(generation)
        if (generation / (match.group(1) + ".json")).exists():
            return _read_clip(text, generation, expected_sha)
        previous = generation / "previous"
        if not previous.exists():
            break
        name = previous.read_text()
        if name == ".":
            generation = folder
        elif GENERATION.fullmatch(name) and not (folder / name).is_symlink():
            generation = folder / name
        else:
            break
    raise CacheNotReady("announcement cache unavailable")


def read_clip(text: str, folder: Path | None = None, *, expected_sha=None) -> bytes:
    folder = DEFAULT_FOLDER if folder is None else Path(folder)
    try:
        with cache_lock(folder):
            return retained_clip(text, folder, expected_sha)
    except (OSError, UnicodeError) as error:
        raise CacheNotReady("announcement cache unavailable") from error


def _status(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    clips = manifest_clips(manifest)
    for clip in clips.values():
        _read_clip(PREFIX + clip["key"] + "]", folder, clip["sha256"])
    return {"installed": True, "ready": True, "voice_id": VOICE_ID,
        "keys": sorted(clip["key"] for clip in clips.values())}


def status(folder: Path | None = None) -> dict:
    """Only nonprivate capability metadata for a read-only deployment check."""
    folder = DEFAULT_FOLDER if folder is None else Path(folder)
    try:
        with cache_lock(folder):
            return _status(current_folder(folder))
    except (OSError, KeyError, TypeError, ValueError, CacheNotReady):
        return {"installed": True, "ready": False, "voice_id": VOICE_ID, "keys": []}


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(folder):
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def import_manifest(source):
    parsed = urlsplit(source)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise CacheValidationError("Use a Home Assistant base URL without credentials or query")
    base = source.rstrip("/")
    with urlopen(base + "/api/remote_announcements/manifest", timeout=10) as response:
        manifest = json.loads(response.read(16384))
    return base, manifest, manifest_clips(manifest)


def report_progress(progress, stage):
    if progress is not None:
        progress(stage)


def prepare_import(base, clips, folder, progress=None):
    prepared = {}
    for target, info in clips.items():
        report_progress(progress, "Preparing " + target)
        key = info["key"]
        try:
            audio = read_clip(PREFIX + key + "]", folder, expected_sha=info["sha256"])
        except CacheNotReady:
            report_progress(progress, "Downloading " + target)
            with urlopen(base + "/api/remote_announcements/audio/" + key + ".ogg", timeout=10) as response:
                audio = response.read(65537)
        # Downloads and decoding finish before publication or the exclusive lock.
        report_progress(progress, "Validating " + target)
        validate_audio(audio, info)
        prepared[key] = audio
    return prepared


def publish_generation(folder, manifest, clips, prepared):
    """Validate a complete private generation before one atomic pointer switch."""
    generation = Path(tempfile.mkdtemp(prefix="generation-", dir=folder))
    pointer = folder / (generation.name + ".current")
    published = False
    try:
        for info in clips.values():
            key, audio = info["key"], prepared[info["key"]]
            atomic_write(generation / (key + ".ogg"), audio)
            atomic_write(generation / (key + ".json"), json.dumps({**info, "voice": VOICE_ID}).encode())
        atomic_write(generation / "manifest.json", json.dumps(manifest).encode())
        result = _status(generation)
        pointer.symlink_to(generation.name, target_is_directory=True)
        with cache_lock(folder, exclusive=True):
            previous = current_folder(folder)
            atomic_write(generation / "previous", ("." if previous == folder else previous.name).encode())
            sync_directory(folder)
            pointer.replace(folder / "current")
            published = True
            # A failed post-switch sync must never delete the visible generation.
            sync_directory(folder)
        return result
    finally:
        pointer.unlink(missing_ok=True)
        # A failed write, validation or switch never changes the playable generation.
        # Retain published generations for rollback; only discard our unpublished data.
        if not published:
            shutil.rmtree(generation)


def install(source: str, folder: Path | None = None, *, progress=None) -> dict:
    folder = DEFAULT_FOLDER if folder is None else Path(folder)
    report_progress(progress, "Fetching manifest")
    base, manifest, clips = import_manifest(source)
    report_progress(progress, "Manifest validated")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepared = prepare_import(base, clips, folder, progress)
    report_progress(progress, "Staging generation")
    return publish_generation(folder, manifest, clips, prepared)


def failure_reason(error):
    if isinstance(error, (CacheValidationError, CacheNotReady)):
        return str(error)
    if isinstance(error, HTTPError):
        return f"Home Assistant returned HTTP {error.code}"
    if isinstance(error, (URLError, TimeoutError)):
        return "Home Assistant connection failed or timed out"
    if isinstance(error, json.JSONDecodeError):
        return "Manifest or metadata is not valid JSON"
    if isinstance(error, OSError):
        return f"Cache filesystem operation failed (errno {error.errno})"
    return "Unexpected " + type(error).__name__


def main():
    parser = argparse.ArgumentParser(description="Import prepared HA clips without playback or synthesis")
    parser.add_argument("--source", required=True, help="Reachable Home Assistant base URL")
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    args = parser.parse_args()
    os.umask(0o077)
    fd, log_name = tempfile.mkstemp(prefix="aisis-announcements-import-", suffix=".log")
    started = time.monotonic()
    print(f"Log: {log_name}", flush=True)
    with os.fdopen(fd, "w", buffering=1) as log:
        def progress(stage):
            log.write(f"{time.time():.3f} +{time.monotonic()-started:.3f}s {stage}\n")
            log.flush()
        progress("Import started")
        try:
            result = install(args.source, args.folder, progress=progress)
            progress("Import complete")
            print(json.dumps(result))
        except Exception as error:
            output = "Import failed: " + failure_reason(error)
            progress(output)
            print(output)
            raise SystemExit(1) from None
        finally:
            print(f"Log: {log_name}")


if __name__ == "__main__":
    main()
