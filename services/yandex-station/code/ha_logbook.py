"""Home Assistant's own record of what AISIS did: the logbook entries made by the user AISIS is signed in as.

The clip feed and the session page show these lines next to the adapter's trace, so a command the quasar layer sent straight to Home
Assistant is visible too. A logbook entry carries `context_user_id`, the user whose call changed the entity; only entries of AISIS's own
user are kept. That user id is asked over the WebSocket (`auth/current_user`) every time and remembered nowhere: a grant for another user
at the same address must change whose entries are shown at once. Nothing here raises and
nothing waits longer than TOTAL_S: the dashboard page and the clip feed must not stall on a Home Assistant that is down.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import ha_client
import ha_ws

TOTAL_S = 2.0
RANK = 2                       # the caption rank of a line (the same scale as the adapter's trace lines)



def _current_user_id(client: ha_client.HaClient, deadline: float) -> str | None:
    session = ha_ws.Session.open(client, timeout=max(0.1, deadline - time.monotonic()))
    try:
        want = session.send("auth/current_user")
        while time.monotonic() < deadline:
            message = session.recv(max(0.1, deadline - time.monotonic()))
            if message is None:
                return None
            if message.get("id") == want and message.get("type") == "result":
                user = (message.get("result") or {}) if message.get("success") else {}
                found = user.get("id")
                return found if isinstance(found, str) and found else None
        return None
    finally:
        session.close()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _clock(when: Any) -> str:
    try:
        return datetime.fromisoformat(str(when).replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ""


def _line(entry: dict[str, Any]) -> str:
    name = entry.get("name") or entry.get("entity_id") or "?"
    what = entry.get("state") or entry.get("message") or ""
    at = _clock(entry.get("when"))
    return f"Home Assistant: {name} → {what}" + (f" ({at})" if at else "")


def _collect(client: ha_client.HaClient, start_ts: float, end_ts: float, deadline: float) -> list[tuple[int, str]]:
    user = _current_user_id(client, deadline)
    if not user:
        return []
    path = f"/api/logbook/{urllib.parse.quote(_iso(start_ts), safe=':')}?end_time={urllib.parse.quote(_iso(end_ts), safe=':')}"
    reply = client.get(path, timeout=max(0.1, deadline - time.monotonic()))
    entries = reply.json() if reply.ok else None
    if not isinstance(entries, list):
        return []
    return [(RANK, _line(entry)) for entry in entries if isinstance(entry, dict) and entry.get("context_user_id") == user]


def lines(client: ha_client.HaClient, start_ts: float, end_ts: float) -> list[tuple[int, str]]:
    """`(rank, text)` caption lines for the logbook entries of AISIS's own user between the two epochs; [] on any failure or after TOTAL_S."""
    deadline = time.monotonic() + TOTAL_S
    found: list[list[tuple[int, str]]] = []

    def work() -> None:
        try:
            found.append(_collect(client, start_ts, end_ts, deadline))
        except Exception:        # NotConnected, AuthRevoked, a broken socket: the diagnostics line is just absent
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(TOTAL_S)
    return found[0] if found else []
