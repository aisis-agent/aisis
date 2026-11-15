"""refresh-whisper-prompt.sh: a new prompt is installed and the recognizer restarted; Home Assistant being down keeps the
previous prompt; any other generator failure is a failure."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent / "refresh-whisper-prompt.sh"
FAKE_PYTHON = """#!/usr/bin/env bash
# stands in for the venv python running voice_vocab.py: `prompt` answers FAKE_STATUS, `prompt --static` the static prompt
if [[ " $* " == *" --static "* ]]; then echo "static prompt"; exit 0; fi
((FAKE_STATUS == 0)) && echo "live prompt with \\"quotes\\""
exit "$FAKE_STATUS"
"""
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "$*" >>"$HOME/systemctl.log"
"""


@pytest.fixture
def home(tmp_path):
    for path, body in ((tmp_path / ".local/share/aisis/venv/bin/python", FAKE_PYTHON), (tmp_path / "bin/systemctl", FAKE_SYSTEMCTL)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def refresh(home, status):
    env = {**os.environ, "HOME": str(home), "PATH": f"{home / 'bin'}:{os.environ['PATH']}", "FAKE_STATUS": str(status)}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=20)


def installed(home):
    path = home / ".config/aisis/whisper-prompt.env"
    return path.read_text() if path.exists() else None


def restarts(home):
    log = home / "systemctl.log"
    return log.read_text().splitlines() if log.exists() else []


def test_a_new_prompt_is_installed_escaped_and_the_recognizer_restarted(home):
    result = refresh(home, 0)
    assert result.returncode == 0, result.stderr
    assert installed(home) == 'WHISPER_INITIAL_PROMPT="live prompt with \\"quotes\\""\n'
    assert restarts(home) == ["--user try-restart wyoming-whisper.service"]


def test_an_unchanged_prompt_does_not_restart_the_recognizer(home):
    refresh(home, 0)
    (home / "systemctl.log").unlink()
    assert refresh(home, 0).returncode == 0
    assert restarts(home) == []


def test_home_assistant_down_keeps_the_previous_prompt(home):
    refresh(home, 0)
    before = installed(home)
    (home / "systemctl.log").unlink()
    result = refresh(home, 3)
    assert result.returncode == 0
    assert "keeping the previous prompt" in result.stderr
    assert installed(home) == before and restarts(home) == []


def test_home_assistant_down_with_no_previous_prompt_installs_the_static_one(home):
    assert refresh(home, 3).returncode == 0
    assert installed(home) == 'WHISPER_INITIAL_PROMPT="static prompt"\n'


@pytest.mark.parametrize("status", [1, 2, 127])
def test_any_other_generator_failure_is_a_failure_and_changes_nothing(home, status):
    refresh(home, 0)
    before = installed(home)
    (home / "systemctl.log").unlink()
    result = refresh(home, status)
    assert result.returncode == status
    assert installed(home) == before and restarts(home) == []
