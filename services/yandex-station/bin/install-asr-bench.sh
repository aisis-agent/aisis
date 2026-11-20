#!/usr/bin/env bash
# Prepare the nightly ASR benchmark (code/bench_asr_archive.py): its own venv, nothing in the service venvs, speaker, router or the live STT path.
#
#   install-asr-bench.sh            build ~/.local/share/aisis/asr-bench-venv (idempotent; models download on the first run, about 1.5 GB)
#   install-asr-bench.sh --enable   the same, then install the repository's units through scripts/install-systemd-units.sh --no-start
#                                   (a unit that is already running is never restarted) and start only aisis-station-asr-bench.timer
#
# Run once by hand: systemctl --user start aisis-station-asr-bench.service     (logs: journalctl --user -u aisis-station-asr-bench)
set -euo pipefail
repo=$(cd "$(dirname "$(readlink -f "$0")")/../../.." && pwd)
code="$repo/services/yandex-station/code"
venv="${AISIS_ASR_BENCH_VENV:-$HOME/.local/share/aisis/asr-bench-venv}"
enable=0
for arg in "$@"; do
  case "$arg" in
    --enable) enable=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: $0 [--enable]" >&2; exit 2 ;;
  esac
done
if [[ "$enable" -eq 1 && "$venv" != "$HOME/.local/share/aisis/asr-bench-venv" ]]; then
  echo "--enable installs a unit whose ExecStart is fixed to $HOME/.local/share/aisis/asr-bench-venv: unset AISIS_ASR_BENCH_VENV (it is for manual runs)" >&2
  exit 2
fi
[[ -f "$code/requirements-asr-bench.txt" ]] || { echo "missing $code/requirements-asr-bench.txt" >&2; exit 1; }
command -v uv >/dev/null || { echo "uv is required" >&2; exit 1; }
install -d -m700 "$(dirname "$venv")"
[[ -x "$venv/bin/python" ]] || uv venv "$venv" --python 3.12
nice -n 19 uv pip install --python "$venv/bin/python" -r "$code/requirements-asr-bench.txt"
if [[ "$enable" -eq 1 ]]; then
  "$repo/scripts/install-systemd-units.sh" --no-start
  systemctl --user enable --now aisis-station-asr-bench.timer
fi
echo "ASR benchmark ready: $venv. Run once: systemctl --user start aisis-station-asr-bench.service"
