"""Everything the harness writes from the household's recordings or the intent corpus is private to its owner.

`voiceclips` keeps the originals in 0700 directories and 0600 files; copies and transcripts made from them must not be readable by
another local account just because the caller's umask is 022. `owner_only()` makes every directory and file created inside the block
0700 / 0600 (existing ones keep their mode).
"""
from __future__ import annotations

import contextlib
import functools
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, TypeVar

Result = TypeVar("Result")


@contextlib.contextmanager
def owner_only() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def owner_only_cli(main: Callable[..., Result]) -> Callable[..., Result]:
    """A command-line entry point whose every directory and file (reports, replay caches, work directories) is owner-only."""
    @functools.wraps(main)
    def wrapper(*args: object, **kwargs: object) -> Result:
        with owner_only():
            return main(*args, **kwargs)
    return wrapper


def open_text(path: Path, mode: str = "w") -> IO[str]:
    """open() for UTF-8 text that holds household data: created 0600, and a file that already exists is tightened to 0600 as well
    (the umask only decides the mode of files that do not exist yet)."""
    handle = open(path, mode, encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600))
    os.fchmod(handle.fileno(), 0o600)
    return handle


def write_text(path: Path, text: str) -> None:
    with open_text(path) as handle:
        handle.write(text)


def open_binary(path: Path) -> IO[bytes]:
    """Like open_text, for audio (`wave.open` takes the handle): created 0600, an existing file tightened to 0600."""
    handle = open(path, "wb", opener=lambda name, flags: os.open(name, flags, 0o600))
    os.fchmod(handle.fileno(), 0o600)
    return handle


def write_bytes(path: Path, data: bytes) -> None:
    with open_binary(path) as handle:
        handle.write(data)
