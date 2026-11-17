#!/usr/bin/env bash
# Puts weather-cli on the speaker host so the adapter's weather answers work on a fresh machine:
# the checkout, the `weather` link in ~/.local/bin, and (only when you give one) the place config.
# Idempotent: a second run changes nothing.
#
#   install-weather-cli.sh            clone if missing, run the checkout's install.sh, write the place config if absent
#   install-weather-cli.sh --check    read-only: exit 0 = ready, 3 = a named step is missing (one line each)
#
# Environment:
#   WEATHER_CLI_DIR    checkout location           (default ~/xp/weather-cli)
#   WEATHER_CLI_REPO   clone URL                   (default https://github.com/alex-mextner/weather-cli)
#   AISIS_WEATHER_COMMAND
#                      JSON argv the adapter runs instead of `weather`; --check then checks that command, not the link.
#   WEATHER_LAT / WEATHER_LON / WEATHER_TZ
#                      home place (the zone is checked with Python's zoneinfo, which is what weather-cli >= 0.2 uses); written (mode 0600) to ~/.config/weather-cli/config.json only when that path does
#                      not exist yet.
#                      Without them weather-cli takes the home zone of Home Assistant (reached through aisis-ha-cli),
#                      so a fresh host needs no file edit.
#
# Exit codes: 0 ok, 2 usage, 3 drift (--check), anything else a failed step (a line says which).
set -euo pipefail

check=0
case "${1:-}" in
  "") ;;
  --check) check=1 ;;
  -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

dir="${WEATHER_CLI_DIR:-$HOME/xp/weather-cli}"
repo="${WEATHER_CLI_REPO:-https://github.com/alex-mextner/weather-cli}"
link="$HOME/.local/bin/weather"
# weather-cli ignores a relative XDG_CONFIG_HOME (XDG says absolute only) and reads $HOME/.config, so must this script.
case "${XDG_CONFIG_HOME:-}" in
  /*) config="$XDG_CONFIG_HOME/weather-cli/config.json" ;;
  *) config="$HOME/.config/weather-cli/config.json" ;;
esac
# CDPATH= : an exported CDPATH makes `cd` print the directory it chose, which would end up in code_dir.
code_dir="$(CDPATH= cd "$(dirname "$0")/../services/yandex-station/code" && pwd)"
drift=0

say_drift() { echo "weather-cli: $1" >&2; drift=3; }

if [ "$check" = 1 ]; then
  child_path="${PATH:-/usr/bin:/bin}:$HOME/.local/bin"
  # Runs `<command> --version` exactly as the adapter would start weather-cli: the same argv parsing and the same
  # environment allowlist (voice_skills), so a wrapper that needs WEATHER_* settings is judged as it will run.
  # $1 = the command when AISIS_WEATHER_COMMAND is unset. Prints unset | invalid | not_runnable | ok
  # (unset: no override and no command given; the adapter treats a blank override as unset).
  adapter_run() {
    python3 -I - "$code_dir" "$1" <<'PY'
import os
import subprocess
import sys

sys.path.insert(0, sys.argv[1])
import voice_skills

try:
    argv = voice_skills.configured_weather_command(dict(os.environ))
except ValueError:  # json.JSONDecodeError is one
    print("invalid")
    sys.exit(0)
if argv is None:
    if not sys.argv[2]:
        print("unset")  # the adapter's own rule: blank counts as unset
        sys.exit(0)
    argv = [sys.argv[2]]
env = voice_skills.weather_child_env(dict(os.environ), os.environ.get("AISIS_HACLI_URL", voice_skills.DEFAULT_HACLI_URL))
try:
    # Its own session, like the adapter's child: on a timeout the whole group goes, not only the wrapper.
    probe = subprocess.Popen([*argv, "--version"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             cwd=sys.argv[1])
except OSError:
    print("not_runnable")
    sys.exit(0)
try:
    print("ok" if probe.wait(timeout=voice_skills.WEATHER_TIMEOUT_S) == 0 else "not_runnable")
except subprocess.TimeoutExpired:
    voice_skills._kill_group(probe.pid)
    probe.wait()
    print("not_runnable")
PY
  }
  # The absolute path the adapter would find `weather` at: shutil.which over its PATH, resolved from the directory
  # the adapter runs in (a relative PATH entry means something different from the caller's directory).
  adapter_which() {
    python3 -I - "$code_dir" <<'PY'
import os
import shutil
import sys

sys.path.insert(0, sys.argv[1])
import voice_skills

env = voice_skills.weather_child_env(dict(os.environ), os.environ.get("AISIS_HACLI_URL", voice_skills.DEFAULT_HACLI_URL))
os.chdir(sys.argv[1])
found = shutil.which("weather", path=env["PATH"])
print(os.path.abspath(found) if found else "")
PY
  }
  override="$(adapter_run "")"
  if [ "$override" != unset ]; then
    # The adapter runs this argv as is, so it is the thing to check (no link, no PATH lookup).
    case "$override" in
      ok) ;;
      invalid) say_drift "weather_command_invalid: AISIS_WEATHER_COMMAND must be a JSON argv array of NUL-free strings" ;;
      *) say_drift "weather_cli_not_runnable: AISIS_WEATHER_COMMAND --version fails" ;;
    esac
  else
    [ -x "$dir/bin/weather" ] || say_drift "weather_cli_missing: no executable $dir/bin/weather (run install-weather-cli.sh)"
    if [ "$(readlink -f "$link" 2>/dev/null || true)" != "$(readlink -f "$dir/bin/weather" 2>/dev/null || echo "$dir/bin/weather")" ]; then
      say_drift "weather_link_missing: $link does not point at $dir/bin/weather"
    fi
    # Resolve `weather` the way the adapter's child does: its PATH plus ~/.local/bin, first match wins.
    found="$(PATH="$child_path" adapter_which)"
    if [ -n "$found" ] && [ "$(readlink -f "$found")" != "$(readlink -f "$dir/bin/weather" 2>/dev/null || true)" ]; then
      say_drift "weather_shadowed: the adapter would run $found, not $dir/bin/weather"
    fi
    if [ "$drift" = 0 ] && [ "$(PATH="$child_path" adapter_run "$found")" != ok ]; then
      say_drift "weather_cli_not_runnable: weather --version fails under PATH=$child_path"
    fi
  fi
  if [ ! -e "$config" ]; then
    echo "weather-cli: weather_place_config_missing: $config absent; every answer then depends on Home Assistant being up (home zone read from it)" >&2
  fi
  exit "$drift"
fi

# Checks WEATHER_LAT/LON/TZ and, when $1 is a path, writes the place config there (0600, never over an existing
# path). Called with "" before any download so a typo fails with nothing installed.
place_config() {
  python3 -I - "$1" "$WEATHER_LAT" "$WEATHER_LON" "${WEATHER_TZ:-}" <<'PY'
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

path, lat, lon, tz = sys.argv[1:]
try:
    place = {"lat": float(lat), "lon": float(lon)}
    if not (math.isfinite(place["lat"]) and -90 <= place["lat"] <= 90):
        raise ValueError("WEATHER_LAT must be a number in -90..90")
    if not (math.isfinite(place["lon"]) and -180 <= place["lon"] <= 180):
        raise ValueError("WEATHER_LON must be a number in -180..180")
    if tz:
        # An IANA zone name every runtime resolves: ZoneInfo alone also takes tzdata pseudo-zones (localtime,
        # Factory, posixrules) that other implementations of the CLI reject at the first answer.
        if "/" not in tz and tz != "UTC":
            raise ValueError("WEATHER_TZ must be an IANA zone like Europe/Belgrade")
        ZoneInfo(tz)
        place["tz"] = tz
except (ValueError, KeyError, OSError) as err:
    sys.exit(f"weather_place_invalid: {err}")
if path:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:  # O_EXCL: never replaces a file (or a dangling symlink) that appeared since the check
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"kept existing place config {path}")
        sys.exit(0)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(place, handle, allow_nan=False)
        handle.write("\n")
    print(f"wrote place config {path}")
PY
}

write_place=0
if [ ! -e "$config" ] && [ -n "${WEATHER_LAT:-}" ] && [ -n "${WEATHER_LON:-}" ]; then
  write_place=1
  place_config ""
fi

if [ ! -d "$dir/.git" ] && [ ! -x "$dir/bin/weather" ]; then
  mkdir -p "$(dirname "$dir")"
  git clone --quiet -- "$repo" "$dir"
  echo "cloned $(printf '%s' "$repo" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@#\1#') -> $dir"
fi
[ -x "$dir/install.sh" ] || { echo "weather_cli_missing: $dir has no install.sh" >&2; exit 1; }
# From its own checkout, never the caller's directory: the upstream installer runs `python3`, which puts the current
# directory first on sys.path, so a stray zoneinfo.py or types.py there would run with the caller's rights.
(CDPATH= cd -- "$dir" && ./install.sh) || { rc=$?; echo "weather_cli_install_failed rc=$rc" >&2; exit 1; }

if [ "$write_place" = 1 ]; then
  place_config "$config"
elif [ -e "$config" ]; then
  echo "kept existing place config $config"
else
  [ -z "${WEATHER_LAT:-}${WEATHER_LON:-}" ] || echo "WEATHER_LAT and WEATHER_LON are both needed; ignored" >&2
  echo "no place config: weather-cli will use the Home Assistant home zone"
fi
