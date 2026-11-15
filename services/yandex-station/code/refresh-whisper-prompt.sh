#!/usr/bin/env bash
# Regenerates the Whisper initial prompt (voice_vocab.py prompt: home vocabulary only, rooms and devices from HA, favourite
# artists) into an EnvironmentFile read by wyoming-whisper.service; restarts the recognizer only when the prompt changed.
# When Home Assistant cannot be read the previous prompt stays (a prompt built without the live names would replace a
# good one for hours); with no previous prompt the static one is installed, so the recognizer is never left unprompted.
set -euo pipefail
data="$HOME/.local/share/aisis"
code="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
envfile="$HOME/.config/aisis/whisper-prompt.env"
vocab=("$data/venv/bin/python" "$code/voice_vocab.py")
status=0
prompt=$("${vocab[@]}" prompt) || status=$?
if ((status == 3)); then  # voice_vocab.py: Home Assistant could not be read; any other failure is a real one
    [[ -f $envfile ]] && { echo "Home Assistant unreachable, keeping the previous prompt" >&2; exit 0; }
    echo "Home Assistant unreachable and no previous prompt, installing the static one" >&2
    prompt=$("${vocab[@]}" prompt --static)
elif ((status != 0)); then
    echo "prompt generator failed with status $status, keeping the previous prompt" >&2
    exit "$status"
fi
[[ -n $prompt ]] || { echo "empty prompt, keeping the previous one" >&2; exit 1; }
prompt=${prompt//\\/\\\\}; prompt=${prompt//\"/\\\"}
new=$(printf 'WHISPER_INITIAL_PROMPT="%s"\n' "${prompt//$'\n'/ }")
mkdir -p -- "$(dirname -- "$envfile")"
if [[ -f $envfile && $(<"$envfile") == "$new" ]]; then exit 0; fi
tmp=$(mktemp "$envfile.XXXXXX")
printf '%s\n' "$new" >"$tmp"; chmod 600 "$tmp"; mv -f -- "$tmp" "$envfile"
systemctl --user try-restart wyoming-whisper.service
