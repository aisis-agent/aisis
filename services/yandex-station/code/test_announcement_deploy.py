"""The supported adapter upgrade publishes and rolls back its cache dependency."""
import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

DEPLOY = Path(__file__).resolve().parents[1] / "bluegreen/deploy.sh"


class AdapterDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.app, self.source, self.bin = [self.folder / name for name in ("app", "source", "bin")]
        for folder in (self.app, self.source, self.bin):
            folder.mkdir()
        self.old = "VERSION = 'old'\n"
        self.new = "import announcement_cache\nVERSION = 'new'\n"
        (self.app / "adapter.py").write_text(self.old)
        (self.source / "adapter.py").write_text(self.new)
        (self.source / "announcement_cache.py").write_text("VERSION = 'new cache'\n")
        self.trace = self.folder / "restarts.jsonl"
        systemctl = self.bin / "systemctl"
        systemctl.write_text(f"#!{sys.executable}\n" + '''import json, os, sys, time
from pathlib import Path
app = Path(os.environ['AISIS_CODE_DIR'])
cache = app / 'announcement_cache.py'
version = None
if cache.exists():
    sys.path.insert(0, str(app))
    import announcement_cache
    version = announcement_cache.VERSION
with Path(os.environ['TEST_RESTART_TRACE']).open('a') as stream:
    stream.write(json.dumps({'adapter': (app / 'adapter.py').read_text(),
        'cache': cache.read_text() if cache.exists() else None,
        'imported_cache': version}) + '\\n')
if os.environ.get('TEST_RESTART_HOLD'):
    Path(os.environ['TEST_RESTART_HOLD']).touch()
    deadline = time.monotonic() + 5
    while not Path(os.environ['TEST_RESTART_RELEASE']).exists():
        if time.monotonic() > deadline:
            raise SystemExit(2)
        time.sleep(0.01)
''')
        (self.bin / "curl").write_text('#!/bin/sh\nexit "$TEST_HEALTH_EXIT"\n')
        (self.bin / "sleep").write_text("#!/bin/sh\nexit 0\n")
        for command in self.bin.iterdir():
            command.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "AISIS_CODE_DIR": str(self.app), "AISIS_BG_STATE": str(self.folder / "state"),
            "TEST_RESTART_TRACE": str(self.trace), "TEST_HEALTH_EXIT": "0"}

    def deploy(self):
        return subprocess.run(["bash", str(DEPLOY), "unit", "adapter", str(self.source / "adapter.py")],
            env=self.env, capture_output=True, text=True, timeout=10)

    def fixed_install_clock(self):
        command = self.bin / "install"
        command.write_text(f"#!{sys.executable}\n" + '''import os, subprocess, sys
subprocess.run(['/usr/bin/install', *sys.argv[1:]], check=True)
if sys.argv[-1].endswith('/announcement_cache.py'):
    os.utime(sys.argv[-1], (1700000000, 1700000000))
''')
        command.chmod(0o700)

    def test_changed_or_removed_maintenance_owner_is_not_recreated(self):
        prefix = DEPLOY.read_text().split("cmd=${1:-}", 1)[0]
        script = self.folder / "refresh.sh"
        for operation in ('rm -- "$state_dir/maintenance"', 'printf foreign >"$state_dir/maintenance"'):
            with self.subTest(operation=operation):
                (self.folder / "state/maintenance").unlink(missing_ok=True)
                script.write_text(prefix + "\nmaintenance_on\n" + operation + "\nmaintenance_touch\n")
                result = subprocess.run(["bash", str(script)], env=self.env, capture_output=True, text=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                marker = self.folder / "state/maintenance"
                if operation.startswith("rm"):
                    self.assertFalse(marker.exists())
                else:
                    self.assertEqual(marker.read_text(), "foreign")
                    marker.unlink()

    def test_refresh_updates_only_owned_inode_if_marker_is_replaced(self):
        marker = self.folder / "maintenance"
        owner = "aisis-deployment-lock-v1:123:.maintenance.fixture"
        marker.write_text(owner + "\n")
        source = DEPLOY.read_text().split("maintenance_touch() {", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        original = os.utime
        def replace_marker(descriptor, times):
            self.assertIsInstance(descriptor, int)
            marker.unlink()
            marker.write_text("foreign owner")
            original(marker, (123, 123))
            original(descriptor, times)
        with patch("sys.argv", ["refresh", str(marker), owner]), patch.object(os, "utime", replace_marker), self.assertRaises(SystemExit):
            exec(compile(source, str(DEPLOY), "exec"), {})
        self.assertEqual(marker.read_text(), "foreign owner")
        self.assertEqual(marker.stat().st_mtime_ns, 123000000000)

    def test_rollback_preserves_unknown_legacy_maintenance_owner(self):
        maintenance = self.folder / "state/maintenance"
        maintenance.parent.mkdir()
        maintenance.write_text("")
        result = subprocess.run(["bash", str(DEPLOY), "rollback"], env=self.env, capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(maintenance.read_text(), "")
        self.assertIn("unrecognized maintenance owner", result.stderr)
        self.assertFalse(self.trace.exists())

    def test_restore_failure_names_retained_backup_and_does_not_claim_success(self):
        command = self.bin / "install"
        command.write_text('#!/bin/sh\ncase "$*" in *unit-backups*) exit 7;; esac\nexec /usr/bin/install "$@"\n')
        command.chmod(0o700)
        self.env["TEST_HEALTH_EXIT"] = "22"
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore failed", result.stderr)
        self.assertIn(str(self.folder / "state/unit-backups"), result.stderr)
        self.assertNotIn("rolled back to the previous files", result.stderr)
        self.assertEqual(len(self.trace.read_text().splitlines()), 1)

    def test_failed_restart_after_restore_is_reported_explicitly(self):
        systemctl = self.bin / "systemctl"
        systemctl.write_text(systemctl.read_text() + "\nraise SystemExit(7)\n")
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous files restored but restart failed", result.stderr)
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)
        self.assertEqual(len(self.trace.read_text().splitlines()), 2)

    def test_upgrade_installs_dependency_before_first_restart(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        restarts = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual(restarts, [{"adapter": self.new, "cache": "VERSION = 'new cache'\n",
            "imported_cache": "new cache"}])

    def test_equal_size_same_mtime_dependency_deploy_uses_new_code(self):
        self.fixed_install_clock()
        sentinel = self.app / "__pycache__/other_module.cpython-fixture.pyc"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"unrelated bytecode")
        for version in ("one", "two"):
            (self.source / "announcement_cache.py").write_text(f"VERSION = '{version}'\n")
            result = self.deploy()
            self.assertEqual(result.returncode, 0, result.stderr)
        restarts = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([item["imported_cache"] for item in restarts], ["one", "two"])
        self.assertEqual(sentinel.read_bytes(), b"unrelated bytecode")

    def test_concurrent_deployment_cannot_replace_active_owner_files(self):
        hold, release = self.folder / "restart-hold", self.folder / "restart-release"
        env = {**self.env, "TEST_RESTART_HOLD": str(hold), "TEST_RESTART_RELEASE": str(release)}
        first = subprocess.Popen(["bash", str(DEPLOY), "unit", "adapter", str(self.source / "adapter.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not hold.exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(hold.exists())
            (self.source / "announcement_cache.py").write_text("VERSION = 'competing cache'\n")
            result = self.deploy()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((self.app / "announcement_cache.py").read_text(), "VERSION = 'new cache'\n")
        finally:
            release.touch()
            stdout, stderr = first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0, stderr)

    def test_abandoned_owned_marker_recovers_after_process_lock_release(self):
        maintenance = self.folder / "state/maintenance"
        maintenance.parent.mkdir()
        maintenance.write_text("aisis-deployment-lock-v1:12345:.maintenance.fixture\n")
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(maintenance.exists())

    def test_unknown_legacy_marker_is_preserved_even_when_old(self):
        maintenance = self.folder / "state/maintenance"
        maintenance.parent.mkdir()
        maintenance.write_text("unknown operator maintenance")
        os.utime(maintenance, (1, 1))
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(maintenance.read_text(), "unknown operator maintenance")
        self.assertFalse(self.trace.exists())

    def test_process_owned_lock_preserves_another_active_deployment(self):
        state = self.folder / "state"
        state.mkdir()
        # The other process has acquired its lock but not published a marker yet.
        with (state / "maintenance.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)

    def test_missing_dependency_is_rejected_before_live_changes(self):
        (self.source / "announcement_cache.py").unlink()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)
        self.assertFalse(self.trace.exists())

    def test_declared_non_utf8_adapter_still_requires_its_companion(self):
        (self.source / "adapter.py").write_bytes(
            b"# coding: latin-1\nimport announcement_cache\nCOMMENT = 'caf\xe9'\n")
        (self.source / "announcement_cache.py").unlink()
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)

    def test_dependency_inspector_failure_aborts_before_live_changes(self):
        command = self.bin / "python3"
        command.write_text('#!/bin/sh\nif [ "$1" = - ]; then exit 2; fi\nexec '
            + sys.executable + ' "$@"\n')
        command.chmod(0o700)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.trace.exists())
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)

    def test_existing_maintenance_owner_is_preserved(self):
        maintenance = self.folder / "state/maintenance"
        maintenance.parent.mkdir()
        maintenance.write_text("other deployment")
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(maintenance.read_text(), "other deployment")
        self.assertFalse(self.trace.exists())
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)

    def test_failed_health_restores_both_original_files(self):
        self.fixed_install_clock()
        for existing in (False, True):
            with self.subTest(existing_cache=existing):
                cache = self.app / "announcement_cache.py"
                if existing:
                    cache.write_text("VERSION = 'old cache'\n")
                self.trace.unlink(missing_ok=True)
                self.env["TEST_HEALTH_EXIT"] = "22"
                result = self.deploy()
                self.assertNotEqual(result.returncode, 0)
                restarts = [json.loads(line) for line in self.trace.read_text().splitlines()]
                self.assertEqual(restarts[0], {"adapter": self.new, "cache": "VERSION = 'new cache'\n",
                    "imported_cache": "new cache"})
                self.assertEqual(restarts[-1], {"adapter": self.old,
                    "cache": "VERSION = 'old cache'\n" if existing else None,
                    "imported_cache": "old cache" if existing else None})
                self.assertFalse((self.folder / "state/maintenance").exists())


RESTART_RUNS_THE_ENTRY_FILE = '''import json, os, runpy, sys
from pathlib import Path
app = Path(os.environ['AISIS_CODE_DIR'])
sys.path.insert(0, str(app))
entry = app / os.environ['TEST_ENTRY']
with Path(os.environ['TEST_RESTART_TRACE']).open('a') as stream:
    stream.write(json.dumps({'entry': entry.read_text(),
        'modules': {path.name: path.read_text() for path in sorted(app.glob('*.py')) if path != entry}}) + '\\n')
runpy.run_path(str(entry), run_name='__main__')   # a module the entry file imports and the update did not bring stops the unit here
'''


class UnitCompanionTests(unittest.TestCase):
    """`unit` installs one entry file, so the sibling modules it newly imports must travel with it and leave with it on a rollback."""
    STT_IMPORTS = "import second_opinion\nimport spotter_clips\nimport voiceclips\nVERSION = 'new'\n"

    def setUp(self):
        AdapterDeploymentTests.setUp(self)
        (self.bin / "systemctl").write_text(f"#!{sys.executable}\n" + RESTART_RUNS_THE_ENTRY_FILE)
        (self.bin / "systemctl").chmod(0o700)

    def deploy_unit(self, unit, entry, new_source, **modules):
        (self.source / entry).write_text(new_source)
        for name, text in modules.items():
            (self.source / name).write_text(text)
        self.env["TEST_ENTRY"] = entry
        return subprocess.run(["bash", str(DEPLOY), "unit", unit, str(self.source / entry)],
            env=self.env, capture_output=True, text=True, timeout=10)

    def restarts(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def test_adapter_ships_the_hypothesis_store_it_imports(self):
        adapter = "import announcement_cache\nimport hypothesis_store\nVERSION = 'new'\n"
        result = self.deploy_unit("adapter", "adapter.py", adapter, **{"hypothesis_store.py": "STORE = 'new'\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / "hypothesis_store.py").read_text(), "STORE = 'new'\n")
        (restart,) = self.restarts()
        self.assertEqual((restart["entry"], restart["modules"]["hypothesis_store.py"]), (adapter, "STORE = 'new'\n"))

    def test_adapter_without_its_hypothesis_store_beside_it_is_refused_untouched(self):
        result = self.deploy_unit("adapter", "adapter.py", "import hypothesis_store\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hypothesis_store.py must accompany", result.stderr)
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)
        self.assertFalse(self.trace.exists())

    def test_adapter_that_does_not_import_the_store_does_not_need_it(self):
        result = self.deploy_unit("adapter", "adapter.py", "VERSION = 'new'\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.app / "hypothesis_store.py").exists())

    def test_failed_health_removes_the_store_the_old_adapter_never_had(self):
        self.env["TEST_HEALTH_EXIT"] = "22"
        result = self.deploy_unit("adapter", "adapter.py", "import hypothesis_store\n", **{"hypothesis_store.py": "STORE = 'new'\n"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)
        self.assertFalse((self.app / "hypothesis_store.py").exists())
        self.assertEqual(self.restarts()[-1], {"entry": self.old, "modules": {}})

    def test_adapter_ships_the_laya_route_it_imports(self):
        adapter = "import laya_route\nVERSION = 'new'\n"
        result = self.deploy_unit("adapter", "adapter.py", adapter, **{"laya_route.py": "ROUTE = 'new'\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / "laya_route.py").read_text(), "ROUTE = 'new'\n")
        (restart,) = self.restarts()
        self.assertEqual((restart["entry"], restart["modules"]["laya_route.py"]), (adapter, "ROUTE = 'new'\n"))

    def test_adapter_without_its_laya_route_beside_it_is_refused_untouched(self):
        result = self.deploy_unit("adapter", "adapter.py", "import laya_route\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("laya_route.py must accompany", result.stderr)
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)
        self.assertFalse(self.trace.exists())

    def test_failed_health_removes_the_laya_route_the_old_adapter_never_had(self):
        self.env["TEST_HEALTH_EXIT"] = "22"
        result = self.deploy_unit("adapter", "adapter.py", "import laya_route\n", **{"laya_route.py": "ROUTE = 'new'\n"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app / "adapter.py").read_text(), self.old)
        self.assertFalse((self.app / "laya_route.py").exists())
        self.assertEqual(self.restarts()[-1], {"entry": self.old, "modules": {}})

    def test_stale_bytecode_of_a_shipped_module_is_dropped_and_other_caches_stay(self):
        cache = self.app / "__pycache__"
        cache.mkdir()
        (cache / "hypothesis_store.cpython-fixture.pyc").write_bytes(b"stale")
        (cache / "other_module.cpython-fixture.pyc").write_bytes(b"unrelated")
        result = self.deploy_unit("adapter", "adapter.py", "import hypothesis_store\n", **{"hypothesis_store.py": "STORE = 'new'\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((cache / "hypothesis_store.cpython-fixture.pyc").exists())
        self.assertEqual((cache / "other_module.cpython-fixture.pyc").read_bytes(), b"unrelated")

    def stt_modules(self, tag):
        return {f"{name}.py": f"VERSION = '{tag} {name}'\n" for name in ("second_opinion", "spotter_clips", "voiceclips")}

    def test_stt_ships_the_second_opinion_and_the_recorder_modules_its_new_code_calls(self):
        (self.app / "stt_wyoming.py").write_text(self.old)
        for name in ("spotter_clips.py", "voiceclips.py"):
            (self.app / name).write_text("VERSION = 'old'\n")
        result = self.deploy_unit("stt", "stt_wyoming.py", self.STT_IMPORTS, **self.stt_modules("new"))
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, text in self.stt_modules("new").items():
            self.assertEqual((self.app / name).read_text(), text)
        self.assertEqual(self.restarts()[0]["modules"]["second_opinion.py"], "VERSION = 'new second_opinion'\n")

    def test_stt_without_one_of_its_modules_beside_it_is_refused_untouched(self):
        (self.app / "stt_wyoming.py").write_text(self.old)
        modules = self.stt_modules("new")
        del modules["second_opinion.py"]
        result = self.deploy_unit("stt", "stt_wyoming.py", self.STT_IMPORTS, **modules)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("second_opinion.py must accompany", result.stderr)
        self.assertEqual((self.app / "stt_wyoming.py").read_text(), self.old)
        self.assertFalse(self.trace.exists())

    def test_failed_stt_health_restores_the_previous_modules_and_removes_the_new_one(self):
        (self.app / "stt_wyoming.py").write_text(self.old)
        for name in ("spotter_clips.py", "voiceclips.py"):
            (self.app / name).write_text("VERSION = 'old'\n")
        self.env["TEST_HEALTH_EXIT"] = "22"
        result = self.deploy_unit("stt", "stt_wyoming.py", self.STT_IMPORTS, **self.stt_modules("new"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app / "stt_wyoming.py").read_text(), self.old)
        self.assertEqual({path.name: path.read_text() for path in self.app.glob("*.py")},
                         {"adapter.py": self.old, "stt_wyoming.py": self.old, "spotter_clips.py": "VERSION = 'old'\n", "voiceclips.py": "VERSION = 'old'\n"})


class UnitInstallTargetTests(unittest.TestCase):
    """With AISIS_CODE_DIR unset, the adapter is installed where its loaded unit's effective ExecStart runs it."""
    def setUp(self):
        AdapterDeploymentTests.setUp(self)   # reuse the stub curl/sleep fixture
        home = self.folder / "home dir"
        self.primary = home / "xp/aisis/services/yandex-station/code"
        self.deploy_main = home / "xp/aisis/.worktrees/deploy-main/services/yandex-station/code"
        for folder in (self.primary, self.deploy_main):
            folder.mkdir(parents=True)
            for name in ("adapter.py", "stt_wyoming.py", "hacli.py"):
                (folder / name).write_text(self.old)
        (self.bin / "systemctl").write_text(
            '#!/bin/sh\nif [ "$2" = show ]; then\n'
            '  if [ -n "$TEST_SHOW_TRACE" ]; then if [ -e "$AISIS_BG_STATE/maintenance" ]; then echo locked; else echo unlocked; fi >>"$TEST_SHOW_TRACE"; fi\n'
            '  echo "$TEST_EXEC_START"\nfi\nexit 0\n')
        self.env.pop("AISIS_CODE_DIR")
        self.env["HOME"] = str(home)

    def exec_start(self, folder, entry="adapter.py"):
        python = self.folder / "home dir/.local/share/aisis/venv/bin/python"
        self.env["TEST_EXEC_START"] = (f"{{ path={python} ; argv[]={python} {folder}/{entry} ; "
                                       "ignore_errors=no ; start_time=[n/a] ; status=0/0 }")

    def install(self, unit, name):
        (self.source / name).write_text("VERSION = 'new'\n")
        if unit == "ha-cli":      # hacli.py imports these two; the installer refuses a hacli.py without them
            for companion in ("ha_verify.py", "ha_action_log.py"):
                (self.source / companion).write_text("VERSION = 'new'\n")
        return subprocess.run(["bash", str(DEPLOY), "unit", unit, str(self.source / name)],
            env=self.env, capture_output=True, text=True, timeout=10)

    def test_adapter_installs_into_the_worktree_its_unit_runs_from(self):
        self.exec_start(self.deploy_main)
        result = self.install("adapter", "adapter.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.deploy_main / "adapter.py").read_text(), "VERSION = 'new'\n")
        self.assertEqual((self.primary / "adapter.py").read_text(), self.old)

    def test_existing_deploy_worktree_does_not_attract_an_adapter_whose_unit_runs_the_primary_checkout(self):
        self.exec_start(self.primary)
        result = self.install("adapter", "adapter.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.primary / "adapter.py").read_text(), "VERSION = 'new'\n")
        self.assertEqual((self.deploy_main / "adapter.py").read_text(), self.old)

    def test_target_is_resolved_only_while_the_maintenance_lock_is_held(self):
        self.exec_start(self.deploy_main)
        self.env["TEST_SHOW_TRACE"] = str(self.folder / "show-trace")
        result = self.install("adapter", "adapter.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.folder / "show-trace").read_text().split(), ["locked"])

    def test_target_directory_containing_spaces_is_not_truncated(self):
        self.assertIn(" ", str(self.deploy_main))
        self.exec_start(self.deploy_main)
        result = self.install("adapter", "adapter.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.deploy_main / "adapter.py").read_text(), "VERSION = 'new'\n")

    def test_adapter_without_a_readable_exec_start_is_refused_untouched(self):
        self.env["TEST_EXEC_START"] = ""
        result = self.install("adapter", "adapter.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("effective ExecStart", result.stderr)
        for folder in (self.primary, self.deploy_main):
            self.assertEqual((folder / "adapter.py").read_text(), self.old)

    def test_adapter_whose_unit_target_lacks_the_installed_file_is_refused(self):
        self.exec_start(self.folder / "elsewhere")
        result = self.install("adapter", "adapter.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed file missing", result.stderr)
        for folder in (self.primary, self.deploy_main):
            self.assertEqual((folder / "adapter.py").read_text(), self.old)

    def test_stt_installs_into_the_worktree_its_unit_runs_from(self):
        self.exec_start(self.deploy_main, "stt_wyoming.py")
        result = self.install("stt", "stt_wyoming.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.deploy_main / "stt_wyoming.py").read_text(), "VERSION = 'new'\n")
        self.assertEqual((self.primary / "stt_wyoming.py").read_text(), self.old)

    def test_ha_cli_installs_into_primary_checkout(self):
        for unit, name in (("ha-cli", "hacli.py"),):
            result = self.install(unit, name)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.primary / name).read_text(), "VERSION = 'new'\n", unit)
            if unit == "ha-cli":
                for companion in ("ha_verify.py", "ha_action_log.py"):
                    self.assertEqual((self.primary / companion).read_text(), "VERSION = 'new'\n", companion)
            self.assertEqual((self.deploy_main / name).read_text(), self.old, unit)


if __name__ == "__main__":
    unittest.main()
