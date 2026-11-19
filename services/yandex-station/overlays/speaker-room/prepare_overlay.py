#!/usr/bin/env python3
"""Build the speaker-room source overlay from the exact production sources it was written against.

The quasar fast-intent layer is not part of this repository: it lives in the quasar image. This overlay changes three of its
files and adds two. A phrase without a room («включи свет») acts in the room of the speaker that heard it (HA area of the
station's media_player, `backend/speaker-room.ts`), and the commands the speaker recognises on the device get their recording
archived and posted to the clip feed (`routers/uniproxy/spotter-clips.ts` -> gateway `POST /spotter-clip`).

    docker cp aisis-quasar-prod-<active colour>:/src/. BASELINE_DIR      # the active image's sources
    prepare_overlay.py --baseline BASELINE_DIR --output-dir OVERLAY_DIR
    # Prebuild one immutable image from OVERLAY_DIR, then follow corpus-runner/README.md
    # for native RunPod execution and collection of its exact source-bound receipt.
    services/yandex-station/bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"

The baseline is the active image `aisis-quasar:bg-tv-fireplace-contract-20261113` (image id sha256:025022a86...). A baseline that
differs by one byte is refused: the patches were reviewed against exactly these files, so a different image needs them rebased.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# file under /src -> sha256 of the production source the patch applies to, and of the result
PATCHED = {
    "backend/ha-intents.ts": ("245c920baafb0709493e30547c147ebc50d475513595c10c4663708478a3cca9", "ha-intents.patch",
                              "81fac5ca320c639471382b58fbf466f701b88ccf50beda895e75a443737e405b"),
    "routers/uniproxy/input-handler.ts": ("385c0b2aaebd844dbf23216a1ba0472f43e098992ae8d062c3ce6aa029c8a441", "input-handler.patch",
                                          "d01c3d5ef1a7959c4b3c866354963bf656d0e8a311ae9e613db9028703b432ba"),
    "routers/uniproxy/uniproxy-connection.ts": ("e2b21632c4fb4ae3f3c9747720de6b53011953bb506e02e6cacadd157613c397",
                                                "uniproxy-connection.patch",
                                                "f470a53ac4dcdf8d25139353948e58a87c02bc150a06b05f08810708a3b56292"),
}
NEW_FILES = ("backend/speaker-room.ts", "routers/uniproxy/spotter-clips.ts")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(baseline: Path, output: Path) -> list[str]:
    for name, (before, _, _) in PATCHED.items():
        source = baseline / name
        if not source.is_file():
            raise SystemExit("baseline_missing: %s" % name)
        if digest(source) != before:
            raise SystemExit("baseline_mismatch: %s is not the production source the patch was written against" % name)
    if output.exists():
        raise SystemExit("output_directory_already_exists: %s" % output)
    output.mkdir(parents=True, mode=0o700)
    try:
        for name, (_, patch, after) in PATCHED.items():
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(baseline / name, target)
            done = subprocess.run(["patch", "--batch", "--fuzz=0", "-p1", "-i", str(HERE / "patches" / patch)],
                                  cwd=output, capture_output=True, text=True, timeout=30)
            if done.returncode != 0:
                raise SystemExit("patch_did_not_apply: %s\n%s" % (name, done.stdout + done.stderr))
            if digest(target) != after:
                raise SystemExit("patched_source_mismatch: %s" % name)
        for name in NEW_FILES:
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / "src" / name, target)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return sorted([*PATCHED, *NEW_FILES])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--baseline", required=True, type=Path, help="a copy of the active quasar image's /src")
    parser.add_argument("--output-dir", required=True, type=Path, help="new directory for deploy.sh --overlay")
    args = parser.parse_args()
    for name in build(args.baseline, args.output_dir):
        print("%s  %s" % (digest(args.output_dir / name)[:12], name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
