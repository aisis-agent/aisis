"""Stable URLs of the dashboard's session and intent pages, and the address the owner can open them at.

The dashboard (dashboard.py, :8781) listens on every interface and has no login; the owner opens it from his phone over
Tailscale. The address is therefore never invented: `AISIS_DASHBOARD_URL` wins, else this machine's own MagicDNS name (or its
Tailscale IPv4) from `tailscale status --self`. When neither can be read, `base_url()` is None and the callers say so instead
of printing a dead link.

  /session/<dialog id>      one session (one dialog): activation, requests, how each was processed
  /session/clip/<clip id>   the same page for a clip that has no dialog (rejected as noise, a command recognised by the commands spotter)
  /grammar#intent-<id>      the definition of a grammar intent on the intents page
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Callable
from urllib.parse import quote

DEFAULT_PORT = 8781
CACHE_S = 600
MISS_S = 60
GITHUB_BLOB = "https://github.com/alex-mextner/aisis/blob/main/services/yandex-station/code/intent_grammar/grammar/ru.yaml"
DIALOG_ID = re.compile(r"^[0-9a-zA-Z._-]{1,80}$")

_cache: tuple[float, str | None] | None = None


def _tailscale(args: list[str], run: Callable = subprocess.run) -> str | None:
    try:
        done = run(["tailscale", *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _discover(env: dict[str, str], run: Callable = subprocess.run) -> str | None:
    port = env.get("AISIS_DASHBOARD_PORT") or DEFAULT_PORT
    explicit = (env.get("AISIS_DASHBOARD_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    status = _tailscale(["status", "--self", "--json"], run)
    if status:
        try:
            name = (json.loads(status).get("Self") or {}).get("DNSName", "").rstrip(".")
        except ValueError:
            name = ""
        if name:
            return f"http://{name}:{port}"
    address = (_tailscale(["ip", "-4"], run) or "").split()
    return f"http://{address[0]}:{port}" if address else None


def base_url(env: dict[str, str] | None = None, run: Callable = subprocess.run, now: Callable[[], float] = time.monotonic) -> str | None:
    """The dashboard's address as the owner's phone reaches it. Without `env` (the process environment) the answer is cached: CACHE_S
    when found, MISS_S when not (a missing tailscale must not stall every post by its lookup time)."""
    global _cache
    if env is not None:
        return _discover(env, run)
    if _cache is not None and now() - _cache[0] < (CACHE_S if _cache[1] else MISS_S):
        return _cache[1]
    found = _discover(dict(os.environ), run)
    _cache = (now(), found)
    return found


def session_path(dialog_id: str | None = None, clip_id: str | None = None) -> str:
    """The session page of a dialog, else of the clip (every clip has one: the page resolves the dialog once it is linked)."""
    if dialog_id and DIALOG_ID.match(dialog_id):
        return "/session/" + quote(dialog_id, safe="")
    if clip_id:
        return "/session/clip/" + quote(clip_id, safe="")
    raise ValueError("a session link needs a dialog id or a clip id")


def intent_path(intent_id: str) -> str:
    return "/grammar#intent-" + quote(intent_id, safe=".")


def github_line_url(line: int) -> str:
    return f"{GITHUB_BLOB}#L{line}"
