"""Tests for install-systemd-units.sh: run with `python3 -m unittest discover -s scripts`.

The script runs against a throwaway HOME and a stand-in `systemctl` that keeps its state in a directory: it records
every call, writes the .wants links `enable` would write, and fails `start` for the units named in $FAIL_START.
Nothing touches the real systemd or the real ~/.config.
"""
import fcntl
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "install-systemd-units.sh"
UNITS = Path(__file__).resolve().parents[1] / "services" / "yandex-station" / "systemd"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
shift  # --user
state="$FAKE_STATE"; unit_dir="$XDG_CONFIG_HOME/systemd/user"
echo "$*" >> "$state/calls"
case "$1" in
  show-environment) exit 0 ;;
  daemon-reload) [ -e "$state/reload-fails" ] && exit 1; exit 0 ;;
  show) for last; do :; done; [ -e "$unit_dir/$last" ] && echo "$unit_dir/$last"; exit 0 ;;
  is-active) [ -e "$state/active-$2" ] && { echo active; exit 0; }; echo inactive; exit 3 ;;
  start)
    case " $FAIL_START " in *" $2 "*) exit 1 ;; esac
    case " $SKIP_START " in *" $2 "*) exit 0 ;; esac
    : > "$state/active-$2" ;;
  enable)
    [ "$2" = --force ] && { set -- "$1" "$3" --force; }
    for target in $(awk '/^\\[/{i=($0=="[Install]");next} i&&/^WantedBy=/{sub(/^WantedBy=/,"");print}' "$unit_dir/$2"); do
      mkdir -p "$unit_dir/$target.wants"; link="$unit_dir/$target.wants/$2"
      if [ -L "$link" ] && [ "$(readlink "$link")" != "$unit_dir/$2" ] && [ "$3" != --force ]; then exit 1; fi   # a conflicting link needs --force, as in systemd
      ln -sf "$unit_dir/$2" "$link"
    done ;;
esac
"""


class InstallerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        bin_dir = self.root / "bin"
        for directory in (self.home, self.state, bin_dir):
            directory.mkdir()
        stub = bin_dir / "systemctl"
        stub.write_text(FAKE_SYSTEMCTL)
        stub.chmod(0o755)
        self.bin_dir = bin_dir
        self.dest = self.home / ".config" / "systemd" / "user"
        self.bg_state = self.root / "bg-state"
        self.make_repo()

    def make_repo(self):
        """A scratch git repo with a copy of the installer, the real unit files and the deploy entry points, plus
        ~/xp/aisis as a linked worktree of it: the installer needs the deployment worktree, so it must find a repo."""
        self.repo = self.root / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
        for src in UNITS.parent.parent.glob("*/systemd"):
            shutil.copytree(src, self.repo / "services" / src.parent.name / "systemd")
        code = self.repo / "services" / "yandex-station" / "code"
        code.mkdir(parents=True, exist_ok=True)
        for name in ("adapter.py", "clipfeed.py", "dashboard.py", "extra_dict_dynamic.py", "stt_wyoming.py"):
            (code / name).write_text("# entry\n")
        (code / "quasar_lan_proxy.py").write_text("# entry: reads ALLOWED_PEERS\n")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"],
                     ["update-ref", "refs/remotes/origin/main", "HEAD"]):
            self.git(*args)
        self.primary = self.home / "xp" / "aisis"
        self.primary.parent.mkdir(parents=True)
        self.git("worktree", "add", "-q", "-b", "primary", str(self.primary))
        self.deploy_tree = self.primary / ".worktrees" / "deploy-main"

    def run_script(self, *args, fail_start="", skip_start="", lock_wait="120"):
        env = {**os.environ, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"), "AISIS_LOCK_WAIT": lock_wait,
               "AISIS_BG_STATE": str(self.bg_state),
               "PATH": f"{self.bin_dir}:{os.environ['PATH']}", "FAKE_STATE": str(self.state), "FAIL_START": fail_start, "SKIP_START": skip_start}
        return subprocess.run(["bash", str(self.repo / "scripts" / SCRIPT.name), *args], capture_output=True, text=True, env=env)

    def calls(self):
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def enabled_units(self):
        return {p.name for p in self.dest.glob("*.wants/*")}

    def test_check_on_an_empty_home_reports_drift_and_writes_nothing(self):
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing:", result.stdout)
        self.assertIn("not enabled: aisis-station-adapter.service", result.stdout)
        self.assertFalse(self.dest.exists())
        self.assertEqual([c for c in self.calls() if c.split()[0] not in ("show-environment", "show", "is-active")], [])

    def test_install_copies_enables_and_starts_every_unit_then_checks_clean(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in UNITS.rglob("*"):
            if path.is_file():
                self.assertEqual((self.dest / path.relative_to(UNITS)).read_bytes(), path.read_bytes())
        self.assertIn("wyoming-whisper.service", self.enabled_units())
        self.assertIn("aisis-whisper-prompt.timer", self.enabled_units())
        self.assertNotIn("aisis-whisper-prompt.service", self.enabled_units())  # oneshot without [Install]
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_check_reports_an_enabled_unit_that_is_not_running_until_it_runs(self):
        self.run_script(fail_start="aisis-station-embed.service")
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not active: aisis-station-embed.service", result.stdout)
        self.assertEqual(self.run_script().returncode, 0)   # the next apply starts it
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_a_start_that_succeeds_without_activating_the_unit_is_a_failure(self):
        result = self.run_script(skip_start="aisis-dashboard.service")
        self.assertEqual(result.returncode, 10)
        self.assertIn("aisis-dashboard.service", result.stderr)
        self.assertNotIn("started: aisis-dashboard.service", result.stdout)
        self.assertEqual(self.run_script("--check").returncode, 3)

    def test_check_without_a_user_session_does_not_claim_the_units_run(self):
        self.run_script()
        (self.bin_dir / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("not verified running", result.stdout)

    def test_no_start_does_not_hide_a_stopped_unit_from_check(self):
        self.run_script(fail_start="aisis-station-embed.service")
        result = self.run_script("--check", "--no-start")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not active: aisis-station-embed.service", result.stdout)

    def test_a_masked_enable_link_and_a_loose_file_mode_are_drift_and_get_repaired(self):
        self.run_script()
        link = self.dest / "default.target.wants" / "aisis-station-embed.service"
        link.unlink()
        link.symlink_to("/dev/null")
        (self.dest / "aisis-dashboard.service").chmod(0o666)
        check = self.run_script("--check")
        self.assertEqual(check.returncode, 3)
        self.assertIn("not enabled: aisis-station-embed.service", check.stdout)
        self.assertIn("mode:", check.stdout)
        repaired = self.run_script()
        self.assertEqual(repaired.returncode, 0)
        self.assertNotIn("restart to apply", repaired.stdout)   # same content: permissions only
        self.assertEqual((self.dest / "aisis-dashboard.service").stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_second_run_installs_and_restarts_nothing_but_still_reloads(self):
        self.run_script()
        (self.state / "calls").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual([c for c in self.calls() if c.split()[0] in ("start", "stop", "restart", "enable")], [])
        self.assertIn("daemon-reload", self.calls())

    def test_failed_start_still_enables_and_starts_the_rest_and_needs_manual(self):
        result = self.run_script(fail_start="aisis-station-adapter.service")
        self.assertEqual(result.returncode, 10)
        self.assertIn("aisis-station-adapter.service", result.stderr)
        self.assertIn("aisis-station-adapter.service", self.enabled_units())
        self.assertIn("wyoming-whisper.service", self.enabled_units())
        self.assertTrue((self.state / "active-wyoming-whisper.service").exists())
        self.assertTrue((self.state / "active-aisis-whisper-prompt.timer").exists())

    def test_reload_is_retried_after_a_failed_run_even_when_files_already_match(self):
        (self.state / "reload-fails").touch()
        self.assertNotEqual(self.run_script().returncode, 0)
        (self.state / "reload-fails").unlink()
        (self.state / "calls").unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("daemon-reload", self.calls())

    def test_a_running_unit_whose_file_changed_is_named_not_restarted(self):
        self.run_script()
        (self.dest / "aisis-station-embed.service").write_text("[Unit]\nDescription=stale\n")
        (self.state / "calls").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restart to apply: aisis-station-embed.service", result.stdout)
        self.assertEqual([c for c in self.calls() if c.split()[0] in ("stop", "restart")], [])

    def test_check_reports_a_drifted_file_and_exits_3(self):
        self.run_script()
        with (self.dest / "aisis-station-embed.service").open("a") as handle:
            handle.write("# local edit\n")
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("differs:", result.stdout)

    def test_symlinked_dropin_directory_and_unit_file_become_real_copies(self):
        checkout = self.root / "old-checkout"
        (checkout / "wyoming-whisper.service.d").mkdir(parents=True)
        for path in (UNITS / "wyoming-whisper.service.d").iterdir():
            (checkout / "wyoming-whisper.service.d" / path.name).write_bytes(path.read_bytes())
        self.dest.mkdir(parents=True)
        (self.dest / "wyoming-whisper.service.d").symlink_to(checkout / "wyoming-whisper.service.d")
        (self.dest / "aisis-station-embed.service").symlink_to(UNITS / "aisis-station-embed.service")
        check = self.run_script("--check")
        self.assertIn("symlink:", check.stdout)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dest / "wyoming-whisper.service.d").is_symlink())
        self.assertFalse((self.dest / "aisis-station-embed.service").is_symlink())
        for path in (UNITS / "wyoming-whisper.service.d").iterdir():
            self.assertEqual((self.dest / "wyoming-whisper.service.d" / path.name).read_bytes(), path.read_bytes())
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_dead_dropin_link_is_replaced_and_nothing_is_written_through_a_live_one(self):
        self.dest.mkdir(parents=True)
        (self.dest / "wyoming-whisper.service.d").symlink_to(self.root / "gone")
        checkout = self.root / "live-checkout"
        (checkout / "x").mkdir(parents=True)
        (checkout / "x" / "dangling").symlink_to(self.root / "nowhere")
        (self.dest / "aisis-station-adapter.service.d").symlink_to(checkout / "x")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("wyoming-whisper.service.d", "aisis-station-adapter.service.d"):
            self.assertFalse((self.dest / name).is_symlink(), name)
        self.assertEqual(sorted(p.name for p in (checkout / "x").iterdir()), ["dangling"])
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_restart_notice_is_printed_even_when_the_reload_fails(self):
        self.run_script()
        (self.dest / "aisis-station-embed.service").write_text("[Unit]\nDescription=stale\n")
        (self.state / "reload-fails").touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restart to apply: aisis-station-embed.service", result.stdout)

    def test_no_user_session_needs_manual(self):
        (self.bin_dir / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertIn("needs manual", result.stderr)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd or self.repo), *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def test_deploy_worktree_is_created_when_home_checkout_is_a_linked_worktree_of_the_repo(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("created:", result.stdout)
        self.assertTrue((self.deploy_tree / "scripts" / SCRIPT.name).is_file())

    def test_lan_proxy_runs_from_the_deploy_worktree_and_needs_its_entry_point_there(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        dropin = (self.dest / "aisis-station-lan-proxy.service.d" / "deploy-worktree.conf").read_text()
        self.assertIn("/.worktrees/deploy-main/services/yandex-station/code/quasar_lan_proxy.py", dropin)
        self.assertTrue((self.deploy_tree / "services/yandex-station/code/quasar_lan_proxy.py").is_file())

    def test_missing_deploy_worktree_for_an_unrelated_home_stops_before_any_unit_changes(self):
        shutil.rmtree(self.primary)
        self.git("worktree", "prune")
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertIn("needs manual", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_separate_clone_with_a_foreign_home_checkout_installs_nothing(self):
        shutil.rmtree(self.primary)
        subprocess.run(["git", "init", "-q", str(self.primary)], check=True, capture_output=True)
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.deploy_tree.exists())

    def test_existing_empty_deploy_directory_is_refused_before_installing_dropins(self):
        self.deploy_tree.mkdir(parents=True)
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertIn("unusable", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_deploy_worktree_missing_an_entry_point_is_refused(self):
        self.assertEqual(self.run_script().returncode, 0)
        (self.deploy_tree / "services/yandex-station/code/dashboard.py").unlink()
        shutil.rmtree(self.dest)
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertIn("missing services/yandex-station/code/dashboard.py", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_deploy_worktree_whose_lan_proxy_predates_the_allow_list_is_refused(self):
        self.assertEqual(self.run_script().returncode, 0)
        (self.deploy_tree / "services/yandex-station/code/quasar_lan_proxy.py").write_text("# entry: open proxy\n")
        shutil.rmtree(self.dest)
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertIn("no ALLOWED_PEERS allow-list", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_repository_of_another_project_at_the_deploy_path_is_refused(self):
        self.deploy_tree.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(self.deploy_tree)], check=True, capture_output=True)
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertFalse(self.dest.exists())

    def test_existing_deploy_branch_ahead_of_origin_is_reused_not_reset(self):
        self.git("branch", "deploy-main")
        hotfix = self.root / "hotfix"
        self.git("worktree", "add", "-q", str(hotfix), "deploy-main")
        (hotfix / "hotfix.txt").write_text("fix\n")
        self.git("add", "-A", cwd=hotfix)
        self.git("commit", "-q", "-m", "hotfix", cwd=hotfix)
        self.git("worktree", "remove", "--force", str(hotfix))
        ahead = self.git("rev-parse", "deploy-main")
        self.assertNotEqual(ahead, self.git("rev-parse", "origin/main"))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "deploy-main"), ahead)
        self.assertTrue((self.deploy_tree / "hotfix.txt").is_file())

    def test_second_install_keeps_a_valid_existing_deploy_worktree(self):
        self.assertEqual(self.run_script().returncode, 0)
        again = self.run_script()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("created:", again.stdout)

    def test_retained_deploy_branch_missing_an_entry_point_is_refused_after_creation(self):
        self.git("branch", "deploy-main")
        old = self.root / "old"
        self.git("worktree", "add", "-q", str(old), "deploy-main")
        self.git("rm", "-q", "services/yandex-station/code/clipfeed.py", cwd=old)
        self.git("commit", "-q", "-m", "drop clipfeed", cwd=old)
        self.git("worktree", "remove", "--force", str(old))
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertIn("missing services/yandex-station/code/clipfeed.py", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_install_does_not_proceed_while_a_deployment_holds_the_lock(self):
        state = self.bg_state
        state.mkdir(parents=True)
        with (state / "maintenance.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_script(lock_wait="1")
        self.assertEqual(result.returncode, 10)
        self.assertIn("holds", result.stderr)
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.deploy_tree.exists())   # the lock is taken before the worktree is created
        self.assertEqual(self.run_script().returncode, 0)   # released: the install goes through

    def test_check_and_plan_report_a_missing_deploy_worktree_without_creating_it(self):
        for flag in ("--check", "--plan"):
            result = self.run_script(flag)
            self.assertIn(f"missing: {self.deploy_tree}", result.stdout, flag)
            self.assertFalse(self.deploy_tree.exists(), flag)

    def test_check_reports_a_deleted_entry_point_while_its_unit_is_still_running(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script("--check").returncode, 0)
        (self.deploy_tree / "services/yandex-station/code/dashboard.py").unlink()
        self.assertTrue((self.state / "active-aisis-dashboard.service").exists())
        for flag in ("--check", "--plan"):
            result = self.run_script(flag)
            self.assertIn("unusable:", result.stdout, flag)
            self.assertIn("missing services/yandex-station/code/dashboard.py", result.stdout, flag)
        self.assertEqual(self.run_script("--check").returncode, 3)


if __name__ == "__main__":
    unittest.main()
