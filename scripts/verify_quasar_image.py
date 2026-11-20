#!/usr/bin/env python3
"""Check that the sources of a quasar image are byte for byte those of the pinned aisis-quasar checkout.

    verify_quasar_image.py IMAGE [CHECKOUT]

CHECKOUT defaults to $AISIS_QUASAR_SOURCE, else ~/.cache/aisis/quasar-source (the directories of
prepare-quasar-source.sh). It must be a clean checkout (nothing untracked or ignored) of the commit in
services/yandex-station/quasar-source.lock. Compared: /src of the image with src/, and /package.json,
/pnpm-lock.yaml, /pnpm-workspace.yaml and /tsconfig.json with the checkout root.

The image is only read: `docker create --pull=never` + `docker cp`, never `docker run`, never its environment.
`ha.config.json` (a Home Assistant token, untracked by design) is not compared; its presence in the image is reported.
Exit 0: identical. 1: differences (names only are printed). 2: usage, docker or checkout error.
"""
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

IGNORED = "ha.config.json"
ROOT_FILES = ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "tsconfig.json")
LOCK = Path(__file__).resolve().parent.parent / "services" / "yandex-station" / "quasar-source.lock"


class VerifyError(Exception):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digests(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): digest(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != IGNORED
    }


def compare(image: dict[str, str], checkout: dict[str, str], suffix: str = "") -> list[str]:
    problems = [f"only in image: {name}{suffix}" for name in sorted(image.keys() - checkout.keys())]
    problems += [f"only in checkout: {name}{suffix}" for name in sorted(checkout.keys() - image.keys())]
    problems += [f"differs: {name}{suffix}" for name in sorted(image.keys() & checkout.keys()) if image[name] != checkout[name]]
    return problems


def run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise VerifyError(f"{command[0]} not found") from error
    if done.returncode != 0:
        raise VerifyError(f"{' '.join(command[:2])} failed: {done.stderr.strip()[:200]}")
    return done.stdout.strip()


def lock_commit(lock: Path) -> str:
    for line in lock.read_text().splitlines():
        if line.startswith("commit="):
            return line.removeprefix("commit=").strip()
    raise VerifyError(f"no commit in {lock}")


def check_checkout(checkout: Path, lock: Path) -> None:
    if not (checkout / "src").is_dir():
        raise VerifyError(f"no src/ in {checkout}")
    head = run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    if head != lock_commit(lock):
        raise VerifyError(f"{checkout} is at {head[:12]}, the lock pins {lock_commit(lock)[:12]}")
    if run(["git", "-C", str(checkout), "status", "--porcelain", "--ignored", "--untracked-files=all"]):
        raise VerifyError(f"{checkout} is not clean (local, untracked or ignored files)")


def image_problems(image: str, checkout: Path) -> list[str]:
    container = run(["docker", "create", "--pull=never", image])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source, roots = Path(tmp) / "src", Path(tmp) / "root"
            source.mkdir()
            roots.mkdir()
            run(["docker", "cp", f"{container}:/src/.", str(source)])
            for name in ROOT_FILES:
                run(["docker", "cp", f"{container}:/{name}", str(roots / name)])
            if (source / IGNORED).exists():
                print(f"note: the image holds src/{IGNORED} (not compared)")
            problems = compare(tree_digests(source), tree_digests(checkout / "src"))
            wanted = {name: digest(checkout / name) for name in ROOT_FILES}
            return problems + compare(tree_digests(roots), wanted, " (root)")
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, text=True, check=False)


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print(__doc__, file=sys.stderr)
        return 2
    checkout = Path(argv[1]) if len(argv) == 2 else Path(
        os.environ.get("AISIS_QUASAR_SOURCE") or Path.home() / ".cache" / "aisis" / "quasar-source")
    try:
        check_checkout(checkout, LOCK)
        problems = image_problems(argv[0], checkout)
    except VerifyError as error:
        print(f"verify_quasar_image: {error}", file=sys.stderr)
        return 2
    for problem in problems:
        print(problem)
    print("identical" if not problems else f"{len(problems)} difference(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
