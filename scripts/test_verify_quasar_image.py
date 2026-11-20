"""Tests for prepare-quasar-source.sh and verify_quasar_image.py: run with `python3 -m unittest discover -s scripts`.

Everything is offline: a local git repository stands in for aisis-quasar, a fake `docker` script for the daemon.
"""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_quasar_image as verify

SCRIPTS = Path(__file__).resolve().parent
PREPARE = SCRIPTS / "prepare-quasar-source.sh"
GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
FILES = {
    "src/main.ts": "main",
    "src/backend/ha.ts": "ha",
    "package.json": "{}",
    "pnpm-lock.yaml": "lock",
    "pnpm-workspace.yaml": "ws",
    "tsconfig.json": "ts",
}
FAKE_DOCKER = """#!/bin/sh
case "$1" in
  create) [ "$FAKE_DOCKER_FAIL" = create ] && { echo "no such image" >&2; exit 1; }; echo cid ;;
  cp) src=${2#cid:}; dest=$3
      if [ "$src" = "/src/." ]; then cp -a "$FAKE_DOCKER_IMAGE/src/." "$dest"; else cp "$FAKE_DOCKER_IMAGE$src" "$dest"; fi ;;
  rm) exit 0 ;;
esac
"""


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True, env=GIT_ENV)
    return done.stdout.strip()


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class CompareTests(unittest.TestCase):
    def test_changed_extra_and_missing_files_are_named(self):
        image = {"same.ts": "1", "changed.ts": "old", "extra.ts": "z"}
        checkout = {"same.ts": "1", "changed.ts": "new", "missing.ts": "m"}
        self.assertEqual(
            verify.compare(image, checkout),
            ["only in image: extra.ts", "only in checkout: missing.ts", "differs: changed.ts"],
        )

    def test_the_untracked_home_assistant_config_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(Path(tmp), {"main.ts": "x", "ha.config.json": "{}"})
            self.assertEqual(list(verify.tree_digests(Path(tmp))), ["main.ts"])


class SourceCase(unittest.TestCase):
    """A local upstream repository with one tagged commit, a lock pointing at it and a scratch HOME."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        write_tree(self.upstream, FILES)
        git(self.upstream, "init", "-q")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "init")
        git(self.upstream, "tag", "v1")
        self.commit = git(self.upstream, "rev-parse", "HEAD")
        self.lock = self.root / "lock"
        self.write_lock(self.commit)
        self.dest = self.root / "dest"

    def write_lock(self, commit: str, tag: str = "v1") -> None:
        self.lock.write_text(f"repo={self.upstream}\ntag={tag}\ncommit={commit}\nimage=x\n")

    def prepare(self, dest=None, cwd=None):
        env = {**GIT_ENV, "HOME": str(self.root), "AISIS_QUASAR_LOCK": str(self.lock)}
        return subprocess.run(["bash", str(PREPARE), str(dest or self.dest)], env=env, capture_output=True, text=True,
                              cwd=cwd, check=False)


class PrepareTests(SourceCase):
    def test_a_clean_checkout_is_made_and_its_absolute_path_printed(self):
        done = self.prepare(dest=Path("rel-dest"), cwd=self.root)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, str((self.root / "rel-dest").resolve())), done.stderr)
        self.assertEqual(git(self.root / "rel-dest", "rev-parse", "HEAD"), self.commit)

    def test_a_second_run_reuses_the_checkout(self):
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(self.prepare().returncode, 0)

    def test_a_tag_that_does_not_point_at_the_locked_commit_is_refused(self):
        write_tree(self.upstream, {"src/main.ts": "other"})
        git(self.upstream, "commit", "-qam", "second")
        git(self.upstream, "tag", "-f", "v1")
        done = self.prepare()
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not point at", done.stderr)

    def test_a_branch_name_is_not_accepted_as_the_tag(self):
        git(self.upstream, "branch", "moving")
        self.write_lock(self.commit, tag="moving")
        done = self.prepare()
        self.assertEqual(done.returncode, 1)
        self.assertIn("does not point at", done.stderr)

    def test_untracked_and_ignored_files_are_refused(self):
        self.assertEqual(self.prepare().returncode, 0)
        (self.dest / "src" / "ha.config.json.bak").write_text("token")
        self.assertIn("not a clean checkout", self.prepare().stderr)
        (self.dest / "src" / "ha.config.json.bak").unlink()
        (self.dest / "node_modules").mkdir()
        (self.dest / "node_modules" / "x.js").write_text("x")
        self.assertEqual(self.prepare().returncode, 1)  # untracked here; ignored ones are refused the same way

    def test_a_local_edit_is_refused(self):
        self.assertEqual(self.prepare().returncode, 0)
        (self.dest / "src" / "main.ts").write_text("edited")
        # checkout of the same commit does not discard a working-tree edit; the clean check does
        self.assertEqual(self.prepare().returncode, 1)

    def test_a_checkout_of_another_repository_is_refused(self):
        self.assertEqual(self.prepare().returncode, 0)
        git(self.dest, "remote", "set-url", "origin", "https://example.invalid/other")
        self.assertIn("another repository", self.prepare().stderr)

    def test_a_destination_that_is_not_a_checkout_is_refused(self):
        self.dest.mkdir()
        self.assertIn("not a git checkout", self.prepare().stderr)

    def test_a_lock_without_a_full_commit_is_refused_before_any_git_use(self):
        self.write_lock("abc123")
        done = self.prepare()
        self.assertEqual(done.returncode, 1)
        self.assertIn("40-hex commit", done.stderr)


class VerifyTests(SourceCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.prepare().returncode, 0)
        self.image = self.root / "image"
        write_tree(self.image, FILES)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)

    def run_main(self, image_env=None, **extra):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_DOCKER_IMAGE": str(image_env or self.image), **extra}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(verify, "LOCK", self.lock), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = verify.main(["img", str(self.dest)])
        return code, out.getvalue(), err.getvalue()

    def test_an_identical_image_passes(self):
        self.assertEqual(self.run_main()[:2], (0, "identical\n"))

    def test_a_changed_source_and_a_changed_root_file_are_named_with_exit_1(self):
        (self.image / "src" / "main.ts").write_text("patched")
        (self.image / "pnpm-lock.yaml").write_text("other")
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("differs: main.ts\n", out)
        self.assertIn("differs: pnpm-lock.yaml (root)\n", out)

    def test_the_image_home_assistant_config_is_reported_not_compared(self):
        (self.image / "src" / "ha.config.json").write_text("{}")
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("holds src/ha.config.json", out)

    def test_a_docker_failure_exits_2_not_1(self):
        code, _, err = self.run_main(FAKE_DOCKER_FAIL="create")
        self.assertEqual(code, 2)
        self.assertIn("docker create failed", err)

    def test_a_checkout_at_another_commit_exits_2(self):
        self.write_lock("0" * 40)
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("the lock pins", err)

    def test_a_checkout_with_an_untracked_file_exits_2(self):
        (self.dest / "src" / "extra.ts").write_text("x")
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("not clean", err)

    def test_usage_error_exit_code(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verify.main([]), 2)


class CommittedLockTests(unittest.TestCase):
    def test_the_committed_lock_is_complete(self):
        lock = SCRIPTS.parent / "services" / "yandex-station" / "quasar-source.lock"
        values = dict(line.split("=", 1) for line in lock.read_text().splitlines() if "=" in line and not line.startswith("#"))
        self.assertEqual(sorted(values), ["commit", "image", "image_id", "repo", "tag"])
        self.assertRegex(values["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(values["image_id"], r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(values["repo"].startswith("https://github.com/alex-mextner/aisis-"))


if __name__ == "__main__":
    unittest.main()
