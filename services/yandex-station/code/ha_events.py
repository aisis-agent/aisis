"""The two events AISIS gives Home Assistant so a native automation can pause the TV while the speaker listens (ha_autopause.py):

  aisis_listening     a spoken turn opened (the wake word fired on the station): the speech gateway fires it as the turn's websocket opens,
                      with `at`, the epoch second it happened
  aisis_dialog_ended  the dialog closed: the dashboard's container-log follower fires it on `Dialog <id> ended`, with `dialog_id`, `ended_at` and,
                      when the follower saw the start, `started_at` (epoch seconds of the log lines): the automation matches the end to the
                      activation it holds (a dialog that started later, e.g. a text command, or ended before it, is not its end)

They go through the shared client (`POST /api/events/<type>`) with a short timeout and never raise: a Home Assistant that is down, wedged or not
connected costs the voice turn nothing. The pause itself, the resume, the owner's own actions and the hold timer are Home Assistant's
(the automation and its helpers); nothing of ours holds state or a timer beyond the start times of dialogs in flight.

While the legacy tvpause.py is enabled neither event is fired, so the legacy path and the automation can never both act: `legacy_enabled` is the
switch (the host's tv_pause.json `enabled`).
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Callable

import ha_client

LOG = logging.getLogger("aisis.ha_events")

LISTENING = "aisis_listening"
DIALOG_ENDED = "aisis_dialog_ended"
EVENT_TIMEOUT_S = 1.0
DIALOG_RE = re.compile(r"Dialog ([0-9a-f-]{36}) (started|ended)")
STARTS_KEPT = 64


def fire(event_type: str, data: dict | None = None, *, client: ha_client.HaClient | None = None, timeout: float = EVENT_TIMEOUT_S) -> bool:
    """Fire a Home Assistant event; True when Home Assistant accepted it. Never raises."""
    try:
        reply = (client or ha_client.default()).post(f"/api/events/{event_type}", data or {}, timeout=timeout)
    except ha_client.HaError as err:
        LOG.warning("ha_event_failed event=%s why=%s", event_type, err)
        return False
    if not reply.ok:
        LOG.warning("ha_event_failed event=%s status=%s error=%s", event_type, reply.status, reply.error)
        return False
    return True


class DialogEndFollower:
    """`observe(ts, message)` for dialogstore.Ingester's `on_log`: a dialog's end becomes the `aisis_dialog_ended` event, sent from its own
    thread so the log reader never waits for Home Assistant."""

    def __init__(self, send: Callable[..., bool], legacy_enabled: Callable[[], bool]) -> None:
        self.send = send
        self.legacy_enabled = legacy_enabled
        self._started: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, ts: float, message: str) -> None:
        match = DIALOG_RE.search(message)
        if match is None:
            return
        dialog_id, what = match.groups()
        with self._lock:
            if what == "started":
                self._started[dialog_id] = ts
                if len(self._started) > STARTS_KEPT:
                    self._started.pop(next(iter(self._started)))
                return
            started = self._started.pop(dialog_id, None)
        if self.legacy_enabled():
            return
        data = {"dialog_id": dialog_id, "ended_at": ts}
        if started is not None:
            data["started_at"] = started
        threading.Thread(target=self.send, args=(DIALOG_ENDED, data), daemon=True, name="ha-event").start()
