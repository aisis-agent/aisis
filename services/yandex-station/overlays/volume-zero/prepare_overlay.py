#!/usr/bin/env python3
"""Prepare the exact Quasar overlay that permits integer volume zero.

The baseline must be routers/alice/base-provider.ts from the verified image.
Only the volume schema lower bound changes; conversion and payloads stay intact.
The output is a new deploy.sh --overlay directory mapping to /src. This command
does not build, deploy, restart or contact a speaker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

BASELINE_SHA256 = "9d67c34303b94b9d0ca90c778036ad1afddab1d92d4ca1e24f0c62279e7605b2"
PATCHED_SHA256 = "3721f9160a307ef5eea1f3f034810183c66eb4a304a23b39cd96ad9578cb9f5d"
BEFORE = b"      volume: z.number().int().min(1).max(10)\n"
AFTER = b"      volume: z.number().int().min(0).max(10)\n"


class OverlayError(Exception):
    """The source differs from the reviewed volume schema."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(baseline_path: Path, output_dir: Path) -> Path:
    """Verify both hashes and write only the reviewed file into a new directory."""
    data = baseline_path.read_bytes()
    if sha256(data) != BASELINE_SHA256:
        raise OverlayError("baseline SHA-256 does not match the reviewed source")
    if data.count(BEFORE) != 1:
        raise OverlayError("the exact volume schema anchor must occur once")
    patched = data.replace(BEFORE, AFTER)
    if sha256(patched) != PATCHED_SHA256:
        raise OverlayError("patched SHA-256 does not match the reviewed result")

    # Refuse an existing output before creating any descendants or writing bytes.
    output_dir.mkdir(mode=0o700, exist_ok=False)
    target = output_dir / "routers" / "alice" / "base-provider.ts"
    target.parent.mkdir(mode=0o700, parents=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(patched)
    return target


def record(log: TextIO, stage: str, **details: object) -> None:
    event = {"time": datetime.now(timezone.utc).isoformat(), "stage": stage, **details}
    line = json.dumps(event, ensure_ascii=False)
    log.write(line + "\n")
    log.flush()
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument(
        "--output-dir", type=Path, required=True,
        help="new directory under an existing parent; existing output is refused",
    )
    args = parser.parse_args(argv)
    os.umask(0o077)
    started = time.monotonic()
    log_dir = Path(tempfile.mkdtemp(prefix="aisis-volume-zero-overlay-"))
    log_path = log_dir / "run.jsonl"
    print(f"Log: {log_path}", file=sys.stderr, flush=True)
    returncode = 1
    with log_path.open("x", encoding="utf-8") as log:
        record(log, "start", baseline=str(args.baseline), output_dir=str(args.output_dir))
        try:
            target = prepare(args.baseline, args.output_dir)
            record(log, "prepared", path=str(target), sha256=PATCHED_SHA256)
            returncode = 0
        except (OverlayError, OSError) as error:
            record(log, "refused", error=str(error))
        finally:
            record(
                log, "complete", returncode=returncode,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
    print(f"Completed with exit {returncode}. Log: {log_path}", file=sys.stderr)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
