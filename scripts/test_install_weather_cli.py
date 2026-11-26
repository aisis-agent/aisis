"""Tests for install-weather-cli.sh: run with `python3 -m unittest discover -s scripts`.

Runs against a throwaway HOME and a local git repository standing in for weather-cli (its install.sh links
bin/weather like the real one); nothing is cloned from the network.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "install-weather-cli.sh"
FAKE_INSTALL = '#!/usr/bin/env bash\nROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\nmkdir -p "$HOME/.local/bin"\nln -sf "$ROOT/bin/weather" "$HOME/.local/bin/weather"\n'


class InstallWeatherCliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.upstream = self.root / "upstream"
        (self.upstream / "bin").mkdir(parents=True)
        (self.upstream / "bin" / "weather").write_text("#!/bin/sh\necho ok\n")
        (self.upstream / "bin" / "weather").chmod(0o755)
        (self.upstream / "install.sh").write_text(FAKE_INSTALL)
        (self.upstream / "install.sh").chmod(0o755)
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.upstream), *args], check=True, capture_output=True,
                           env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
        self.checkout = self.home / "xp" / "weather-cli"

    def run_script(self, *args, cwd=None, **extra):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "WEATHER_CLI_REPO": str(self.upstream), **extra}
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, cwd=cwd)

    def test_fresh_host_gets_checkout_link_and_place_config(self):
        done = self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4", WEATHER_TZ="Europe/Belgrade")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.checkout / "bin" / "weather").exists())
        self.assertEqual((self.home / ".local/bin/weather").resolve(), (self.checkout / "bin/weather").resolve())
        config = json.loads((self.home / ".config/weather-cli/config.json").read_text())
        self.assertEqual(config, {"lat": 44.8, "lon": 20.4, "tz": "Europe/Belgrade"})
        self.assertEqual(self.run_script("--check", WEATHER_LAT="44.8").returncode, 0)

    def test_second_run_changes_nothing_and_keeps_an_existing_config(self):
        self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4")
        config = self.home / ".config/weather-cli/config.json"
        before = (config.read_text(), config.stat().st_mtime_ns)
        again = self.run_script(WEATHER_LAT="1", WEATHER_LON="2")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((config.read_text(), config.stat().st_mtime_ns), before)

    def test_without_a_place_the_home_assistant_zone_is_the_documented_fallback(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Home Assistant home zone", done.stdout)
        self.assertFalse((self.home / ".config/weather-cli/config.json").exists())

    def test_check_names_each_missing_step(self):
        missing = self.run_script("--check")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("weather_cli_missing", missing.stderr)
        self.assertIn("weather_link_missing", missing.stderr)
        self.run_script()
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_invalid_place_is_refused_and_nothing_is_written(self):
        for lat, lon in (("nan", "20"), ("91", "20"), ("44", "inf"), ("x", "1")):
            done = self.run_script(WEATHER_LAT=lat, WEATHER_LON=lon)
            self.assertNotEqual(done.returncode, 0, (lat, lon))
            self.assertIn("weather_place_invalid", done.stderr)
            self.assertFalse((self.home / ".config/weather-cli/config.json").exists())
            self.assertFalse(self.checkout.exists(), "validation must run before the clone")

    def test_existing_config_is_reported_kept(self):
        self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4")
        again = self.run_script(WEATHER_LAT="1", WEATHER_LON="2")
        self.assertIn("kept existing place config", again.stdout)

    def test_help_has_no_stray_script_line(self):
        out = self.run_script("--help").stdout
        self.assertNotIn("set -euo", out)

    def test_config_is_private_and_xdg_config_home_is_honoured(self):
        xdg = self.root / "xdg"
        done = self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4", XDG_CONFIG_HOME=str(xdg))
        self.assertEqual(done.returncode, 0, done.stderr)
        config = xdg / "weather-cli" / "config.json"
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(config.parent.stat().st_mode), 0o700)
        self.assertFalse((self.home / ".config/weather-cli/config.json").exists())

    def test_a_relative_xdg_config_home_is_ignored_like_the_cli_does(self):
        done = self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4", XDG_CONFIG_HOME="relative-config", cwd=self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.home / ".config/weather-cli/config.json").exists())
        self.assertFalse((self.root / "relative-config").exists())
        self.assertEqual(self.run_script("--check", WEATHER_LAT="44.8", XDG_CONFIG_HOME="relative-config").returncode, 0)

    def test_the_upstream_installer_runs_in_its_own_checkout_not_the_callers_directory(self):
        spy = self.root / "spy"
        (spy / "bin").mkdir(parents=True)
        (spy / "bin" / "weather").write_text("#!/bin/sh\n")
        (spy / "bin" / "weather").chmod(0o755)
        (spy / "install.sh").write_text('#!/bin/sh\npwd -P > "$HOME/install-cwd"\n')
        (spy / "install.sh").chmod(0o755)
        hostile = self.root / "hostile-cwd"
        hostile.mkdir()
        done = self.run_script(WEATHER_CLI_DIR=str(spy), cwd=hostile)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.home / "install-cwd").read_text().strip(), str(spy.resolve()))

    def test_a_dangling_symlink_at_the_config_path_is_left_alone(self):
        config = self.home / ".config/weather-cli/config.json"
        config.parent.mkdir(parents=True)
        target = self.root / "elsewhere.json"
        config.symlink_to(target)
        done = self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("kept existing place config", done.stdout)
        self.assertTrue(config.is_symlink())
        self.assertFalse(target.exists())

    def test_invalid_timezone_and_half_a_place(self):
        bad_tz = self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4", WEATHER_TZ="Mars/Olympus")
        self.assertNotEqual(bad_tz.returncode, 0)
        self.assertIn("weather_place_invalid", bad_tz.stderr)
        self.assertFalse(self.checkout.exists())
        for pseudo in ("localtime", "Factory"):
            with self.subTest(tz=pseudo):
                refused = self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4", WEATHER_TZ=pseudo)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("weather_place_invalid", refused.stderr)
        self.assertEqual(self.run_script(WEATHER_LAT="44.8", WEATHER_LON="20.4", WEATHER_TZ="UTC").returncode, 0)
        self.assertTrue((self.home / ".config/weather-cli/config.json").exists())
        (self.home / ".config/weather-cli/config.json").unlink()
        shutil.rmtree(self.checkout)
        half = self.run_script(WEATHER_LAT="44.8")
        self.assertEqual(half.returncode, 0, half.stderr)
        self.assertIn("both needed", half.stderr)
        self.assertFalse((self.home / ".config/weather-cli/config.json").exists())

    def test_check_reports_a_shadowing_weather_and_a_broken_cli(self):
        self.run_script()
        other = self.root / "other"
        other.mkdir()
        (other / "weather").write_text("#!/bin/sh\necho other\n")
        (other / "weather").chmod(0o755)
        shadowed = self.run_script("--check", PATH=f"{other}:/usr/bin:/bin")
        self.assertEqual(shadowed.returncode, 3)
        self.assertIn("weather_shadowed", shadowed.stderr)
        (self.checkout / "bin" / "weather").write_text("#!/bin/sh\nexit 1\n")
        broken = self.run_script("--check")
        self.assertEqual(broken.returncode, 3)
        self.assertIn("weather_cli_not_runnable", broken.stderr)

    def test_check_uses_the_configured_command_instead_of_the_link(self):
        ok = self.run_script("--check", AISIS_WEATHER_COMMAND='["/bin/true"]')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn("weather_link_missing", ok.stderr)
        self.assertIn("weather_cli_not_runnable", self.run_script("--check", AISIS_WEATHER_COMMAND='["/bin/false"]').stderr)
        self.assertIn("weather_command_invalid", self.run_script("--check", AISIS_WEATHER_COMMAND="weather").stderr)

    def wrapper(self, body: str) -> str:
        path = self.root / "wrapper"
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
        return json.dumps([str(path)])

    def test_check_runs_a_configured_wrapper_under_the_adapters_environment(self):
        # The adapter passes WEATHER_* through and nothing else secret; --check must judge the same run.
        command = self.wrapper('[ "$WEATHER_LAT" = 44.8 ] && [ -z "$HA_TOKEN" ] && [ -z "$WEATHER_HA_TOKEN" ]')
        done = self.run_script("--check", AISIS_WEATHER_COMMAND=command, WEATHER_LAT="44.8", HA_TOKEN="secret")
        self.assertEqual(done.returncode, 0, done.stderr)
        wrong = self.run_script("--check", AISIS_WEATHER_COMMAND=command, WEATHER_LAT="1", HA_TOKEN="secret")
        self.assertEqual(wrong.returncode, 3)
        self.assertIn("weather_cli_not_runnable", wrong.stderr)

    def test_check_shows_the_home_assistant_settings_without_the_token(self):
        command = self.wrapper('[ "$WEATHER_HA_URL" = http://ha.test:8123 ] && [ "$WEATHER_HA_TOKEN" = secret-llat ]')
        store = self.root / "connection.json"
        store.write_text(json.dumps({"version": 1, "url": "http://ha.test:8123", "auth": {"kind": "long_lived", "token": "secret-llat"}}))
        store.chmod(0o600)
        done = self.run_script("--check", AISIS_WEATHER_COMMAND=command, AISIS_HA_STORE=str(store))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("WEATHER_HA_URL=http://ha.test:8123 WEATHER_HA_TOKEN=<set>", done.stderr)
        self.assertNotIn("secret-llat", done.stdout + done.stderr)
        absent = self.run_script("--check", AISIS_WEATHER_COMMAND=self.wrapper('[ -z "$WEATHER_HA_URL" ]'))
        self.assertIn("WEATHER_HA_URL=<unset> WEATHER_HA_TOKEN=<unset>", absent.stderr)

    def test_check_refuses_a_nul_in_the_configured_command(self):
        done = self.run_script("--check", AISIS_WEATHER_COMMAND='["/bin/true\\u0000ignored"]')
        self.assertEqual(done.returncode, 3)
        self.assertIn("weather_command_invalid", done.stderr)

    def test_a_hung_wrapper_is_killed_with_its_children(self):
        marker = self.root / "marker"
        command = self.wrapper(f"(sleep 12; echo late > '{marker}') &\nwait")
        done = self.run_script("--check", AISIS_WEATHER_COMMAND=command)
        self.assertEqual(done.returncode, 3)
        self.assertIn("weather_cli_not_runnable", done.stderr)
        time.sleep(6)
        self.assertFalse(marker.exists())

    def test_a_wrapper_slower_than_the_adapters_deadline_is_not_runnable(self):
        done = self.run_script("--check", AISIS_WEATHER_COMMAND=self.wrapper("sleep 10"))
        self.assertEqual(done.returncode, 3)
        self.assertIn("weather_cli_not_runnable", done.stderr)

    def test_a_module_in_the_callers_directory_does_not_shadow_the_standard_library(self):
        hostile = self.root / "hostile"
        hostile.mkdir()
        marker = self.root / "pwned"
        for name in ("json", "subprocess", "os"):
            (hostile / f"{name}.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
        self.run_script(cwd=hostile)
        self.run_script("--check", cwd=hostile, AISIS_WEATHER_COMMAND='["/bin/true"]')
        self.assertFalse(marker.exists())

    def test_the_probe_runs_in_the_adapters_directory_not_the_callers(self):
        code_dir = SCRIPT.parent.parent / "services" / "yandex-station" / "code"
        marker = self.root / "cwd"
        command = self.wrapper(f"pwd > '{marker}'")
        done = self.run_script("--check", cwd=self.root, AISIS_WEATHER_COMMAND=command)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(Path(marker.read_text().strip()).resolve(), code_dir.resolve())

    def test_a_relative_path_entry_is_resolved_from_the_adapters_directory(self):
        self.run_script()
        code_dir = SCRIPT.parent.parent / "services" / "yandex-station" / "code"
        shadow_dir = self.root / "shadow"
        shadow_dir.mkdir()
        shadow = shadow_dir / "weather"
        shadow.write_text("#!/bin/sh\nexit 17\n")
        shadow.chmod(0o755)
        # Relative to the adapter's directory it finds the shadow; relative to the caller's it would not.
        entry = os.path.relpath(shadow_dir, code_dir)
        done = self.run_script("--check", cwd=self.root, PATH=f"{entry}:/usr/bin:/bin")
        self.assertEqual(done.returncode, 3)
        self.assertIn("weather_shadowed", done.stderr)

    def test_an_exported_cdpath_does_not_leak_into_the_adapter_directory(self):
        self.run_script()
        done = self.run_script("--check", cwd=SCRIPT.parent.parent, CDPATH=".")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_blank_command_override_is_unset_as_in_the_adapter(self):
        self.run_script()
        for blank in ("   ", "\t", " \n "):
            with self.subTest(blank=blank):
                self.assertEqual(self.run_script("--check", AISIS_WEATHER_COMMAND=blank).returncode, 0)
        shadowed = self.run_script("--check", AISIS_WEATHER_COMMAND="  ")
        self.assertNotIn("AISIS_WEATHER_COMMAND", shadowed.stderr)

    def test_a_failing_upstream_install_is_named_and_exits_1(self):
        broken = self.root / "broken"
        (broken / "bin").mkdir(parents=True)
        (broken / "bin" / "weather").write_text("#!/bin/sh\n")
        (broken / "bin" / "weather").chmod(0o755)
        (broken / "install.sh").write_text("#!/bin/sh\nexit 3\n")
        (broken / "install.sh").chmod(0o755)
        done = self.run_script(WEATHER_CLI_DIR=str(broken))
        self.assertEqual(done.returncode, 1)
        self.assertIn("weather_cli_install_failed rc=3", done.stderr)

    def test_usage_error(self):
        self.assertEqual(self.run_script("--bogus").returncode, 2)


if __name__ == "__main__":
    unittest.main()
