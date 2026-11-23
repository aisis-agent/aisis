#!/usr/bin/env python3
"""Verify or privately stage one pinned Mini 2 image; never contact a device."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import zlib


class ArtifactError(ValueError):
    """The image or staging location does not meet the preparation contract."""


@dataclass(frozen=True)
class FirmwareSpec:
    platform: str
    version: str
    size: int
    crc32: int
    sha256: str
    source_url: str


MINI2 = FirmwareSpec(
    platform="yandexmini_2",
    version="0.369.1.46.4778262943.20261013.299",
    size=67796480,
    crc32=1524631406,
    sha256="192aa9842c77257c4955c9a02cea837f91217d25206d2368815700280ab3c8c3",
    source_url="https://quasar.s3.yandex.net/yandexmini_2/ota/user/"
    "7ce77cd8-0dba-45ab-8938-b751dc2d1988/"
    "0.369.1.46.4778262943.20261013.299.zip",
)
CHUNK = 1024 * 1024


def checked_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if ".." in path.parts:
        raise ArtifactError("Parent traversal is not accepted")
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ArtifactError("Symlink paths are not accepted")
    return path


def open_directory(path: Path) -> int:
    """Resolve every directory component without following symlinks."""
    path = checked_path(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise ArtifactError("Directory path is unavailable or changed") from exc


def open_image(path: Path):
    path = checked_path(path)
    parent = open_directory(path.parent)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ArtifactError("Image is not a regular file")
        image = os.fdopen(fd, "rb")
        fd = None
        return image
    except OSError as exc:
        raise ArtifactError("Image is not an accessible regular file") from exc
    finally:
        os.close(parent)
        if fd is not None:
            os.close(fd)


def _verify_stream(image, spec: FirmwareSpec) -> dict:
    if (spec.platform, spec.version) != (MINI2.platform, MINI2.version):
        raise ArtifactError("Only the pinned Mini 2 model and version are supported")
    sha = hashlib.sha256()
    crc, size = 0, 0
    image.seek(0)
    magic = image.read(6)
    if magic not in (b"070701", b"070702"):
        raise ArtifactError("Expected a CPIO/SWUpdate image, not HTML or a ZIP file")
    image.seek(0)
    while chunk := image.read(CHUNK):
        size += len(chunk)
        if size > spec.size:
            raise ArtifactError("Image exceeds the pinned size")
        sha.update(chunk)
        crc = zlib.crc32(chunk, crc)
    if (size, crc & 0xffffffff, sha.hexdigest()) != (spec.size, spec.crc32, spec.sha256):
        raise ArtifactError("Image size or checksum differs from the pinned artifact")
    return {**asdict(spec),
            "format": "cpio-newc-crc" if magic == b"070702" else "cpio-newc",
            "status": "verified_not_deployed", "device_changes": False}


def verify_image(path: Path, spec: FirmwareSpec = MINI2) -> dict:
    """The CLI always uses MINI2; spec is an explicit trusted test-fixture seam."""
    with open_image(path) as image:
        return _verify_stream(image, spec)


def private_file(name, *, dir_fd=None):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=dir_fd)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w+b")


def _same_directory(parent: int, name: str, opened: int) -> None:
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    expected = os.fstat(opened)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ArtifactError("Staging directory changed during preparation")


def prepare_image(image: Path, output: Path, spec: FirmwareSpec = MINI2) -> dict:
    verified = verify_image(image, spec)
    output = checked_path(output)
    parent = open_directory(output.parent)
    folder = None
    try:
        try:
            os.mkdir(output.name, 0o700, dir_fd=parent)
        except FileExistsError as exc:
            raise ArtifactError("Destination exists; use a new path, no files were changed") from exc
        folder = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        if os.fstat(folder).st_uid != os.getuid():
            raise ArtifactError("Staging directory owner changed")
        os.fchmod(folder, 0o700)
        with private_file(".incomplete", dir_fd=folder) as marker:
            marker.write(b"Preparation is incomplete. Do not use this directory.\n")
            marker.flush()
            os.fsync(marker.fileno())
        _same_directory(parent, output.name, folder)
        filename = spec.version + ".zip"
        with open_image(image) as source, private_file(filename, dir_fd=folder) as dest:
            copied = 0
            while chunk := source.read(CHUNK):
                copied += len(chunk)
                if copied > spec.size:
                    raise ArtifactError("Source grew during copying")
                dest.write(chunk)
            dest.flush()
            os.fsync(dest.fileno())
            _verify_stream(dest, spec)  # verify the exact open file, not a reopened path
        manifest = {**verified, "filename": filename, "status": "prepared_not_deployed",
                    "firmware_server_running": False, "factory_reset_performed": False}
        with private_file(".manifest.pending", dir_fd=folder) as dest:
            dest.write((json.dumps(manifest, indent=2) + "\n").encode())
            dest.flush()
            os.fsync(dest.fileno())
        _same_directory(parent, output.name, folder)
        os.unlink(".incomplete", dir_fd=folder)
        # A missing manifest always means incomplete, even if the marker was lost.
        os.rename(".manifest.pending", "manifest.json", src_dir_fd=folder, dst_dir_fd=folder)
        os.fsync(folder)
        os.fsync(parent)
        _same_directory(parent, output.name, folder)
        return manifest
    finally:
        # Failed preparation is retained privately for diagnosis, not overwritten.
        if folder is not None:
            os.close(folder)
        os.close(parent)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "prepare"))
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--platform", required=True, choices=(MINI2.platform,),
                        help="Explicit confirmation of the only supported device model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.action == "prepare" and args.output is None:
        parser.error("prepare requires --output (a new private directory)")
    if args.action == "verify" and args.output is not None:
        parser.error("verify does not write an output directory")
    try:
        result = (verify_image(args.image) if args.action == "verify"
                  else prepare_image(args.image, args.output))
    except (ArtifactError, OSError) as exc:
        print(json.dumps({"status": "refused", "error": str(exc), "device_changes": False}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
