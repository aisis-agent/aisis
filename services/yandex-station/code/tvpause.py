#!/usr/bin/env python3
"""Pause the TV (and any other player) of the speaker's own room while the speaker listens, resume it after the dialog.

Activation signal: the STT gateway (stt_wyoming.py) opens one websocket per spoken turn, right
after the wake word/spotter fired on the speaker; it calls `TvPause.activation(station)` with the station id the
websocket names (x-aisis-station / ?station=), None when it names none.
End signal: dialogstore's docker log follower (dashboard process) sees `Dialog <id> ended` and
calls `TvPause.dialog_ended()`. Both processes share one state file under flock, so the one
that paused is not required to be the one that resumes.

Home Assistant is reached only through the shared client (ha_client.HaClient: the standard REST API, the standard sign-in): one
template call reads every media player with its state, its area and its device, the pause and the resume are `media_player`
service calls. Which players count is a rule, not a list (#276):
  * a player that is `playing` (TV, projector, receiver, any other player: every media_player is one) and stands in the Home
    Assistant AREA of the speaker that was activated; a player of another room is never touched;
  * never the speaker itself: the entities that end in `_<station id>` (media_player.aisis_speaker_<id>, the Yandex integration's
    player), the entities of the same Home Assistant device, the Music Assistant player that fronts one of them (its
    `active_queue` is that entity; a Music Assistant queue id names no entity: such a player, e.g. the speaker's AirPlay endpoint,
    goes in the `exclude` list), and the optional `exclude` list of the settings;
  * the room of the speaker is the area of its DEVICE, as for the voice commands (ha_actions.speaker_area); the station is one a
    speaker has in Home Assistant, named by the websocket, else the only media_player.aisis_speaker_* Home Assistant has. No room
    known (no or several speakers, a speaker without an area, an unknown station) pauses nothing and the reason is the event «skip»;
    so does an unreachable or unreadable Home Assistant (a state that cannot be read is not «playing»).
    Known limit (UB-277): the dialog log carries no station, so with two speakers talking at once the resume of one room is not told
    from the other's: a dialog end releases every pause we hold.

Rules of the hold (all deliberate):
  * resume only what *we* paused, only if it is still in the state our pause left it in (`paused`; `idle` for the players that say so,
    read once the pause took) - someone pressed play or switched it off: leave it - and only if the dialog did not itself act on a
    player of that room (`Fast intent executed: ... [media_player.x]` for a held player or another player of a room we paused in: the
    owner said pause/stop/play, never undo it; a command for a player of another room does not count);
  * a held player that Home Assistant cannot tell for a moment (`unavailable`, `unknown`, gone) stays ours; one in any other state
    than the pause left it in is no longer ours;
  * a new activation inside the resume grace cancels the resume (next turn/dialog owns it);
  * if no end signal ever arrives, a timer resumes after `max_hold_s` since the last activation;
  * `AISIS_TVPAUSE_DRY_RUN=1` reads states but never calls a service (events say "dry-run").

Settings: ~/.config/aisis/tv_pause.json (atomic, 0600), edited in the dashboard. The `entities` list of the first version is not read
any more (the rule replaced it); a file that still has it loads, its `enabled` and `resume` kept, and the key is gone at the next save.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import ha_actions
import ha_client

LOG = logging.getLogger("aisis.tvpause")

DEFAULTS = {"enabled": True, "resume": True, "exclude": [], "resume_delay_s": 1.5, "max_hold_s": 300}
ENTITY_RE = re.compile(r"^media_player\.[a-z0-9_]+$")
# A dialog belongs to an activation if it started at most this long after it (clock/queue jitter
# between the STT open and the container's "Dialog started"); a later start is another dialog.
START_TOLERANCE_S = 3.0
EVENT_LIMIT = 30
DIALOG_RE = re.compile(r"Dialog ([0-9a-f-]{36}) (started|ended)")
EXECUTED_RE = re.compile(r"Fast intent executed: .*\[([^\]]*)\]")
# Every media player with its live state, its area (its own, else its device's), its device and the device's area, and the queue it
# fronts: one call, always fresh. The speaker's room is its DEVICE's area (README, «Which room»), the other players' room is their area.
ROOM_TEMPLATE = (
    "{% set ns = namespace(rows=[]) %}{% for e in states.media_player %}{% set d = device_id(e.entity_id) %}"
    "{% set ns.rows = ns.rows + [{'entity_id': e.entity_id, 'state': e.state, 'area': area_name(e.entity_id), "
    "'device': d, 'device_area': area_name(d) if d else None, 'queue': e.attributes.get('active_queue')}] %}"
    "{% endfor %}{{ ns.rows | tojson }}"
)
# The entities a speaker has in Home Assistant: the AISIS integration's player, else the Yandex integration's one (README, «Which room»).
SPEAKER_PREFIXES = (ha_actions.OWN_PLAYER_PREFIX, "media_player.yandex_station_")
CALL_TIMEOUT_S = 5.0
SETTLE_S = 2.0          # how long after our pause a player may still say `playing` before its answer is taken as `paused`
SETTLE_STEP_S = 0.25


@dataclass(frozen=True)
class Player:
    entity: str
    state: str
    area: str | None
    device: str | None
    queue: str | None = None      # Music Assistant: the entity whose queue this player plays (a speaker's own player for its wrapper)
    device_area: str | None = None


@dataclass(frozen=True)
class Room:
    """The room an activation is about: `area` None means it is not known (`why` says why); `players` are the media players of
    that room that are not the speaker itself and not excluded, in any state."""
    area: str | None
    players: tuple[Player, ...] = ()
    why: str = ""


def _text(value) -> str | None:
    """An optional attribute as Home Assistant's template gave it: a string, else nothing (a list or object there is not a name)."""
    return value if isinstance(value, str) and value else None


def discover(players: list[Player], station: str | None, exclude=()) -> Room:
    """The room of the activated speaker and its players. Pure: `players` is one read of Home Assistant.

    The station is one a speaker has in Home Assistant (media_player.aisis_speaker_<id> or media_player.yandex_station_<id>): an id that
    only ends an ordinary player («tv» for media_player.studio_tv) is no station. A turn that names none is the one AISIS speaker Home
    Assistant has (the integration registers exactly the stations connected to this server; a station that has only the Yandex
    integration's player talks to Yandex's own cloud, not to the gateway; one that is `unavailable` or `unknown` is unplugged and does
    not count); with none or several the speaker is not known."""
    if not station:
        ids = {p.entity[len(ha_actions.OWN_PLAYER_PREFIX):] for p in players
               if p.entity.startswith(ha_actions.OWN_PLAYER_PREFIX) and p.state not in ("unavailable", "unknown")}
        if len(ids) != 1:
            return Room(None, why=f"speaker unknown: no station named and Home Assistant has {len(ids)} speakers")
        station = ids.pop()
    ident = station.strip().lower()
    speakers = [p for p in players if any(p.entity == prefix + ident for prefix in SPEAKER_PREFIXES)]
    if not speakers:
        return Room(None, why=f"speaker {station} is not in Home Assistant")
    area = ha_actions.speaker_area({p.entity: p.device_area for p in speakers if p.device_area}, ident)
    if area is None:
        return Room(None, why=f"speaker {station} has no area in Home Assistant")
    suffix = "_" + ident
    own = {p.entity for p in players if p.entity.endswith(suffix)}
    own_devices = {p.device for p in players if p.entity in own and p.device}
    skipped = set(exclude)
    return Room(area, tuple(p for p in players if p.area == area and p.entity not in own and p.queue not in own
                            and p.device not in own_devices and p.entity not in skipped))


def config_dir() -> Path:
    return Path(os.environ.get("AISIS_CONFIG_DIR", str(Path.home() / ".config" / "aisis")))


def settings_path() -> Path:
    return Path(os.environ.get("AISIS_TVPAUSE_SETTINGS", str(config_dir() / "tv_pause.json")))


def state_path() -> Path:
    default = Path.home() / ".local" / "state" / "aisis" / "tv_pause_state.json"
    return Path(os.environ.get("AISIS_TVPAUSE_STATE", str(default)))


def load_settings() -> dict:
    settings = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    try:
        stored = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key in DEFAULTS:             # one key at a time: a bad value never resets the others (`enabled` back to its default)
            if key in stored:
                try:
                    settings.update(_validated({key: stored[key]}))
                except ValueError:
                    pass
    return settings


def _validated(changes: dict) -> dict:
    out = {}
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        if key in ("enabled", "resume"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true/false")
        elif key == "exclude":
            if not isinstance(value, list) or not all(isinstance(e, str) and ENTITY_RE.match(e) for e in value):
                raise ValueError("exclude must be a list of media_player.* ids")
            value = list(dict.fromkeys(value))
        elif key == "resume_delay_s":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 30:
                raise ValueError("resume_delay_s must be 0..30")
        elif key == "max_hold_s":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 10 <= value <= 3600:
                raise ValueError("max_hold_s must be 10..3600")
        out[key] = value
    return out


def update_settings(changes: dict) -> dict:
    merged = load_settings()
    merged.update(_validated(changes))
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tv_pause.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(merged, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return merged


class HaPlayers:
    """The media players of Home Assistant through the shared client. Nothing here raises: a Home Assistant that is not connected,
    does not answer, refuses or answers something unreadable is None / False, and the caller does nothing; `why` then says which."""

    def __init__(self, client: ha_client.HaClient | None = None, timeout: float = CALL_TIMEOUT_S):
        self._client = client
        self.timeout = timeout
        self.why = ""

    @property
    def client(self) -> ha_client.HaClient:
        return self._client or ha_client.default()

    def players(self) -> list[Player] | None:
        """Every media player with state, area and device, read now; None when Home Assistant could not be read."""
        try:
            reply = self.client.template(ROOM_TEMPLATE, self.timeout)
            if not reply.ok:
                return self._failed("players", reply=reply)
            rows = json.loads(reply.text())
            return [Player(row["entity_id"], row["state"], _text(row.get("area")), _text(row.get("device")), _text(row.get("queue")),
                           _text(row.get("device_area"))) for row in rows]
        except (ha_client.HaError, ValueError, KeyError, TypeError) as err:
            return self._failed("players", err=err)

    def state(self, entity: str) -> str | None:
        """The state of one entity now, None if unreadable."""
        try:
            reply = self.client.get(f"/api/states/{entity}", self.timeout)
            if not reply.ok:
                return self._failed("state", reply=reply)
            state = reply.json().get("state")
            return state if isinstance(state, str) else None
        except (ha_client.HaError, ValueError, AttributeError) as err:
            return self._failed("state", err=err)

    def call(self, service: str, entity: str) -> bool:
        """True only for a service call Home Assistant executed (HTTP 200)."""
        try:
            reply = self.client.service("media_player", service, {"entity_id": entity}, timeout=self.timeout)
        except ha_client.HaError as err:
            self._failed(service, err=err)
            return False
        if reply.status != 200:
            self._failed(service, reply=reply)
        return reply.status == 200

    def _failed(self, what: str, reply: ha_client.Reply | None = None, err: Exception | None = None):
        """Remember and log why Home Assistant gave nothing (never a token or an answer body): a lost connection is not told from a down server."""
        if isinstance(err, ha_client.NotConnected):
            self.why = "Home Assistant is not connected (press Connect on the dashboard /ha page)"
        elif isinstance(err, ha_client.AuthRevoked):
            self.why = "Home Assistant revoked the access (connect again on the dashboard /ha page)"
        elif reply is not None and reply.status == 0:
            self.why = f"Home Assistant unreachable ({reply.error})"
        elif reply is not None:
            self.why = f"Home Assistant answered HTTP {reply.status}"
        elif isinstance(err, ha_client.HaError):
            self.why = f"Home Assistant unreachable ({type(err).__name__})"
        else:
            self.why = "Home Assistant answered something unreadable"
        LOG.warning("tv_pause_ha what=%s why=%r", what, self.why)
        return None


class _Locked:
    """Process-wide + cross-process lock around the state file."""

    _local = threading.RLock()

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self._local.acquire()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self._local.release()


class TvPause:
    def __init__(self, ha=None, settings=load_settings, state_file=None, dry_run=None, clock=time.time, sleep=time.sleep):
        self.ha = ha or HaPlayers()
        self.settings = settings
        self.state_file = Path(state_file) if state_file else state_path()
        self.dry_run = os.environ.get("AISIS_TVPAUSE_DRY_RUN") == "1" if dry_run is None else dry_run
        self.clock = clock
        self.sleep = sleep
        self.timers: dict[str, threading.Timer] = {}
        self._started: dict[str, float] = {}      # dialog id -> start time seen in this process

    # ----- state file -----
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            data = {}
        data.setdefault("paused", {})
        data.setdefault("last_activation", 0.0)
        data.setdefault("user_touched", False)
        data.setdefault("rest", {})          # entity -> the state it showed once our pause took (normally paused; some players say idle)
        data.setdefault("watch", [])         # the players of the rooms we paused in: a dialog acting on one of them is the owner's own action
        data.setdefault("events", [])
        return data

    def _save(self, st: dict):
        st["events"] = st["events"][-EVENT_LIMIT:]
        fd, tmp = tempfile.mkstemp(dir=self.state_file.parent, prefix=".tv_pause_state.")
        with os.fdopen(fd, "w") as fh:
            json.dump(st, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_file)

    def _event(self, st: dict, kind: str, entity: str = "", detail: str = ""):
        st["events"].append({"ts": round(self.clock(), 3), "kind": kind, "entity": entity, "detail": detail})
        LOG.info("tv_pause %s %s %s", kind, entity, detail)

    def status(self) -> dict:
        with _Locked(self.state_file):
            st = self._load()
        return {"paused": st["paused"], "last_activation": st["last_activation"], "events": st["events"][-10:]}

    # ----- activation: pause -----
    def activation(self, station: str | None = None):
        cfg = self.settings()
        if not cfg["enabled"]:
            return
        now = self.clock()
        with _Locked(self.state_file):
            st = self._load()
            st["last_activation"] = now
            if not st["paused"]:
                st["user_touched"] = False
            players = self.ha.players()
            if players is None:
                self._event(st, "skip", detail=getattr(self.ha, "why", "") or "Home Assistant unreachable or unreadable")
                self._save(st)
                # A pause held from an earlier activation must follow this newer last_activation,
                # or its hold timer (keyed to the older one) expires as a no-op and never resumes.
                holding = bool(st["paused"])
            else:
                holding = self._pause_room(cfg, st, now, players, station)
            # Armed under the state lock: an older activation must not replace the timer of a newer one
            # after releasing the lock (its timestamp guard would then expire as a no-op).
            if holding:
                self._arm("hold", cfg["max_hold_s"], self._hold_expired, now)

    def _pause_room(self, cfg, st, now, players, station) -> bool:
        """Pause the playing players of the activated speaker's room (caller holds the state lock); returns whether any pause is held."""
        room = discover(players, station, cfg["exclude"])
        if room.area is None:
            self._event(st, "skip", detail=room.why)
            self._save(st)
            return bool(st["paused"])
        states = {p.entity: p.state for p in players}
        paused_now = set()
        for player in room.players:
            if player.state != "playing":
                continue
            entity = player.entity
            if self.dry_run:
                self._event(st, "dry-run", entity, f"would pause ({room.area})")
            elif self.ha.call("media_pause", entity):
                st["paused"][entity] = now
                st["rest"][entity] = self._settled(entity)
                st["watch"] = sorted({*st["watch"], *(p.entity for p in room.players)})
                paused_now.add(entity)
                self._event(st, "pause", entity, room.area)
            else:
                self._event(st, "error", entity, "media_pause failed")
        for entity in list(st["paused"]):
            state = states.get(entity)
            # Someone else changed it: no longer ours. A player Home Assistant cannot tell for now (unloaded, unavailable) stays ours.
            if entity not in paused_now and state not in (None, st["rest"].get(entity, "paused"), "unavailable", "unknown"):
                del st["paused"][entity]
                st["rest"].pop(entity, None)
                self._event(st, "forget", entity, f"state {state}")
        if not st["paused"]:
            st["watch"] = []
        self._save(st)
        return bool(st["paused"])

    def _settled(self, entity: str) -> str:
        """The state the player shows once our pause took: HTTP 200 only says Home Assistant handed the call on, the player may publish
        its state a moment later. Most say `paused`, some `idle` (ha_verify); one that still says `playing` after SETTLE_S is taken as
        `paused`, the state a pause normally ends in."""
        waited = 0.0
        while True:
            state = self.ha.state(entity)
            if state is not None and state != "playing":
                return state
            if waited >= SETTLE_S:
                return "paused"
            self.sleep(SETTLE_STEP_S)
            waited += SETTLE_STEP_S

    def _arm(self, name, delay, fn, *args):
        old = self.timers.pop(name, None)
        if old:
            old.cancel()
        timer = threading.Timer(delay, fn, args)
        timer.daemon = True
        self.timers[name] = timer
        timer.start()

    def _hold_expired(self, activation_ts):
        # a newer activation owns the hold
        self.resume("max hold elapsed", unless=lambda st: st["last_activation"] != activation_ts)

    # ----- dialog log events -----
    def observe(self, ts: float, message: str):
        """Feed one container log message (already ANSI-free, with its epoch)."""
        m = DIALOG_RE.search(message)
        if m:
            did, what = m.groups()
            if what == "started":
                self._started[did] = ts
                if len(self._started) > 64:
                    self._started.pop(next(iter(self._started)))
            else:
                self.dialog_ended(did, ts)
            return
        m = EXECUTED_RE.search(message)
        if m:
            targets = {t.strip() for t in m.group(1).split(",")}
            if targets:
                with _Locked(self.state_file):
                    st = self._load()
                    if st["paused"] and targets & {*st["paused"], *st["watch"]}:
                        st["user_touched"] = True
                        self._event(st, "user-action", ",".join(sorted(targets)), "dialog acted on a player of the room; will not resume")
                        self._save(st)

    def dialog_ended(self, did: str, ended_ts: float):
        cfg = self.settings()
        started = self._started.pop(did, None)
        with _Locked(self.state_file):
            st = self._load()
            holding = bool(st["paused"])
            last = st["last_activation"]
        if not holding or not cfg["resume"]:
            return
        if started is not None and started > last + START_TOLERANCE_S:
            return                                   # a dialog opened after the activation we hold (e.g. text command)
        self._arm("resume", cfg["resume_delay_s"], self._resume_if_idle, ended_ts)

    def _resume_if_idle(self, ended_ts):
        # the next turn/dialog started during the grace
        self.resume("dialog ended", unless=lambda st: st["last_activation"] > ended_ts)

    # ----- resume -----
    def resume(self, reason: str, unless=None):
        """Resume what we paused. `unless(state)` is evaluated under the same lock as the resume,
        so an activation cannot slip in between the idle check and the release."""
        cfg = self.settings()
        with _Locked(self.state_file):
            st = self._load()
            if unless is not None and unless(st):
                return
            ours, rest, touched = dict(st["paused"]), dict(st["rest"]), st["user_touched"]
            st["paused"], st["rest"], st["watch"], st["user_touched"] = {}, {}, [], False
            for entity in ours:
                if not cfg["resume"]:
                    self._event(st, "skip-resume", entity, "resume disabled")
                    continue
                if touched:
                    self._event(st, "skip-resume", entity, "dialog acted on a player of the room")
                    continue
                state = self.ha.state(entity)
                if state != rest.get(entity, "paused"):
                    self._event(st, "skip-resume", entity, f"state {state}")
                elif self.dry_run:
                    self._event(st, "dry-run", entity, f"would resume ({reason})")
                elif self.ha.call("media_play", entity):
                    self._event(st, "resume", entity, reason)
                else:
                    self._event(st, "error", entity, "media_play failed")
            self._save(st)

    def recover(self):
        """After a service restart: a pause we still hold is released once max_hold_s has passed."""
        cfg = self.settings()
        with _Locked(self.state_file):
            st = self._load()
            holding, last = bool(st["paused"]), st["last_activation"]
        if holding:
            self._arm("hold", max(1.0, last + cfg["max_hold_s"] - self.clock()), self._hold_expired, last)


_default = None
_default_lock = threading.Lock()


def default() -> TvPause:
    global _default
    with _default_lock:
        if _default is None:
            _default = TvPause()
        return _default
