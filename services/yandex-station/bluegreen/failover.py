#!/usr/bin/env python3
"""Autofailover watchdog for the voice stack. Standard library only.

Every tick (default 1.5 s) it probes, in parallel:
  * the active and the standby quasar color (API /devices 200 + uniproxy port answers),
  * the switch (end to end, through its canonical ports),
  * with --components (prod default): adapter, stt, pm, postgres, tls-voice,
    dns-relay, kit (sync helper on :18080), lan-proxy, embed sidecar, asr (a real transcription of silence).

A quasar color or the switch counts as failed after `--threshold` consecutive failed ticks
(default 3); other components after `--component-threshold` (default 6).
Then:
  * active color failed, standby healthy  -> flip the switch to the standby (nginx
    reload), then restart the failed color so it becomes the new warm standby;
  * active failed and standby failed too  -> restart the active color (no flip);
  * standby failed                        -> restart it (keeps rollback warm);
  * switch failed while the active color is healthy -> restart the switch container;
  * any other component                   -> restart its systemd user unit / container.
    A component that cannot run without a prerequisite port (pm: the browser of the ChatGPT reserve bridge) fails its probe
    only while that port listens; with nothing there the probe has no verdict (no incident, no alert, no restart, one journal
    line), because no restart can start it.
nginx fails over connects to a dead color by itself in milliseconds; this loop
covers the cases nginx cannot see (a hung process that still accepts connections)
and repairs whatever died.

One Telegram alert is sent when an incident opens and one when it recovers; every
probe transition and action goes to the journal and to state/failover-events.jsonl.
Actions are suspended while state/maintenance is fresh (deploy.sh sets it).

  failover.py --env prod            # the systemd unit aisis-failover.service
  failover.py --env stage --no-components --no-alert   # drills
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bgctl  # noqa: E402
import smoke  # noqa: E402
import speaker_clock  # noqa: E402

CLOCK_CHECK_EVERY_S = 300.0

MAINTENANCE_MAX_AGE_S = 900.0
RESTART_COOLDOWN_S = 30.0
PREREQUISITE_GRACE_S = 20.0  # a returning prerequisite (the pm browser) gets this long for the unit to start by itself: its RestartSec=5 plus a start of a few seconds
PREREQUISITE_TIMEOUT_S = 0.3  # loopback connects take microseconds; a probe must not hold up the tick

# The env file that aisis-station-pm.service loads (EnvironmentFile=): the watchdog reads the bridge's own settings from it.
RUNTIME_ENV = Path.home() / ".config/aisis/runtime.env"


def _env_logical_lines(text: str) -> list[str] | None:
    """The logical lines of a systemd env file. A carriage return and a newline each end a line, as in systemd (Python's own line
    splitting would also cut at U+2028 and others). A comment line (# or ;) ends at its own line end. A line with an assignment
    that ends in an odd number of backslashes continues on the next one (the backslash is dropped). None when a value that was
    continued opens a quote: after an escaped newline systemd keeps quotes as text, and inside single quotes it keeps the
    backslash and the newline, which this reader does not follow."""
    lines, pending = [], ""
    for raw in re.split(r"[\r\n]", text):
        if not pending and raw.lstrip(" \t")[:1] in ("#", ";"):
            continue
        joined = pending + raw
        continues = (len(raw) - len(raw.rstrip("\\"))) % 2 == 1 and bool(joined.partition("=")[0].strip(" \t")) and "=" in joined
        if (continues or pending) and joined.partition("=")[2].lstrip(" \t")[:1] in ("'", '"'):
            return None
        if continues:
            pending = joined[:-1]
            continue
        lines.append(joined)
        pending = ""
    return lines + [pending] if pending else lines


def runtime_setting(name: str) -> str | None:
    """The value of `name` in RUNTIME_ENV as systemd reads it (NAME=value lines, spaces and tabs around the name and the value
    ignored, matching quotes removed, the last assignment wins); None when unset or the file is missing. An empty value is a
    value: systemd passes it on. A file this line-by-line reader cannot read exactly (unreadable, bytes that are not UTF-8, a NUL,
    a quoted value that spans lines, continues, holds its own quote or a backslash, or has text after the closing quote) yields
    "": no usable endpoint, so the component claims no prerequisite and is restarted as before."""
    try:
        text = RUNTIME_ENV.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return ""
    logical = _env_logical_lines(text)
    if "\0" in text or logical is None:
        return ""
    value = None
    for line in logical:
        key, sep, rest = line.strip(" \t").partition("=")
        if not sep:
            continue
        assigned = rest.strip(" \t")
        if assigned.startswith(("'", '"')):
            inner = assigned[1:-1]
            if len(assigned) < 2 or assigned[-1] != assigned[0] or assigned[0] in inner or "\\" in inner:
                return ""
            assigned = inner
        if key.strip(" \t") == name:
            value = assigned
    return value


@dataclass(frozen=True)
class Component:
    name: str
    kind: str  # http | unit | container
    target: str  # url | unit name | container name
    unit: str | None = None  # systemd unit to restart when kind == http
    container: str | None = None  # container to restart when kind == http|container
    tcp_port: int | None = None  # extra TCP check on the container's published host IP
    every: int = 1  # probe on every Nth tick only (kit: its service logs each probe; others: subprocess cost)
    timeout: float | None = None  # http probe timeout; None = the watchdog's --http-timeout (asr: a whole transcription)
    fails_on: int | None = None  # http: 200 = healthy, this status = failed, any other answer (gateway down, 202 busy, 501, route missing) = no verdict
    background: bool = False  # probe off the tick (a slow probe must not delay the quasar failover decision), result read once done
    restart_cooldown_s: float | None = None  # minimum gap between two restarts of this component (None = RESTART_COOLDOWN_S)
    needs_port: int | None = None  # http: TCP port on 127.0.0.1 the unit cannot run without; while nothing listens there a failed probe is no verdict (a restart cannot help)
    needs_port_env: str | None = None  # http: runtime.env setting (an exact http://127.0.0.1:<port> endpoint) that overrides needs_port, so a moved browser is followed
    label: str | None = None  # how alerts name the component (None = its name)


COMPONENTS = (
    Component("adapter", "http", "http://127.0.0.1:31117/healthz", unit="aisis-station-adapter.service"),
    Component("stt", "http", "http://127.0.0.1:31121/healthz", unit="aisis-station-stt.service"),
    # A real transcription of silence through the gateway (stt_wyoming.py /healthz/asr), roughly once a minute: the
    # recognizer once kept answering while every transcription died on a full temp directory (2026-11-17).
    # The recognizer is restarted only on the gateway's explicit 503 (the recognizer answered wrongly); a dead gateway, one
    # that predates the route, a timeout or a probe skipped for live traffic gives no verdict, and restarting Wyoming (shared with Home
    # Assistant) could not fix any of those. At most one restart per 10 minutes: a restart cannot fix a full disk or a bad
    # language setting either, and then the incident alert is what matters, not a model reload every minute.
    Component("asr", "http", "http://127.0.0.1:31121/healthz/asr", unit="wyoming-whisper.service", every=40, timeout=8.0,
              fails_on=503, background=True, restart_cooldown_s=600.0),
    # The ChatGPT reserve bridge, the brain's last resort behind Claude. It attaches to the browser the pm product owns on its
    # debugging port, exits when that browser disconnects and refuses to start without it. While nothing listens on that port a
    # restart cannot help, so the failed probe is no verdict. 2026-11-27: the Guardian stack, browser included, was stopped on
    # purpose; the watchdog alerted «pm не отвечает» and restarted the unit every 30 s for 399 s, then again, and every restart
    # put the unit's restart counter back to 0.
    Component("pm", "http", "http://127.0.0.1:31118/health", unit="aisis-station-pm.service", needs_port=9226,
              needs_port_env="AISIS_PM_CDP",
              label="pm (aisis-station-pm: мост к браузеру ChatGPT, резервный мозг ответов)"),
    Component("postgres", "container", "aisis-yandex-postgres-1", container="aisis-yandex-postgres-1", every=3),
    Component("tls-voice", "container", "aisis-yandex-network-tls-voice-1",
              container="aisis-yandex-network-tls-voice-1", tcp_port=443, every=3),
    Component("dns-relay", "container", "aisis-yandex-network-dns-relay-1", container="aisis-yandex-network-dns-relay-1", every=3),
    # Speaker's quasar.yandex.net/.ru + scbh requests land here via tls-voice (kit.txt); sync info answers 200.
    Component("kit", "http", "http://192.168.0.56:18080/get_sync_info?device_id=aisis-failover-probe&platform=yandexmini_2",
              unit="aisis-station-kit.service", every=10),
    Component("lan-proxy", "unit", "aisis-station-lan-proxy.service", unit="aisis-station-lan-proxy.service", every=3),
    Component("embed", "unit", "aisis-station-embed.service", unit="aisis-station-embed.service", every=3),
)


@dataclass
class Tracker:
    fails: int = 0
    first_fail: float = 0.0
    incident: bool = False
    incident_start: float = 0.0
    last_action: float = 0.0
    detail: str = ""
    no_verdict: str = ""  # detail of the last probe without a verdict, so a long run of the same one is journaled once


@dataclass
class Watchdog:
    env: str
    interval: float
    threshold: int
    component_threshold: int
    http_timeout: float
    components: bool
    alert: bool
    state_dir: Path
    trackers: dict[str, Tracker] = field(default_factory=dict)
    ticks: int = 0
    clock: Future | None = None
    pending: dict[str, Future] = field(default_factory=dict)   # background probes in flight, by component name
    next_clock: float = 0.0
    log_lock: threading.Lock = field(default_factory=threading.Lock)

    # ── output ───────────────────────────────────────────────────────────
    def event(self, kind: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "env": self.env, "event": kind, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self.log_lock:
            print(line, flush=True)
            try:
                with open(self.state_dir / "failover-events.jsonl", "a") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def notify(self, text: str) -> None:
        if not self.alert:
            return
        try:
            subprocess.Popen(["tg", "--subagent", "aisis-failover", text], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            self.event("alert_failed", error=str(exc))

    # ── probes ───────────────────────────────────────────────────────────
    def probe_component(self, comp: Component) -> tuple[bool | None, str]:
        """(healthy, detail); healthy None = no verdict this time (the watchdog neither counts nor clears a failure)."""
        if comp.kind == "http":
            status = smoke.http_status(comp.target, comp.timeout or self.http_timeout)
            if comp.fails_on is not None:
                verdict = True if status == 200 else False if status == comp.fails_on else None
            else:
                verdict = status == 200
            port, unchecked = self._needed_port(comp) if verdict is False else (None, "")
            if port is not None and self._refused(port):
                return None, f"http {status}; nothing listens on 127.0.0.1:{port}, a restart cannot help"
            return verdict, f"http {status}{unchecked}"
        if comp.kind == "unit":
            result = subprocess.run(["systemctl", "--user", "is-active", comp.target], capture_output=True, text=True)
            state = result.stdout.strip()
            return state == "active", state
        running = bgctl.container_running(comp.target)
        if not running:
            return False, "not running"
        if comp.tcp_port:
            host = self._published_host(comp)
            if host is None:
                return False, "no published port"
            try:
                with socket.create_connection((host, comp.tcp_port), timeout=self.http_timeout):
                    pass
            except OSError as exc:
                return False, f"tcp {host}:{comp.tcp_port} {type(exc).__name__}"
        return True, "running"

    @staticmethod
    def _needed_port(comp: Component) -> tuple[int | None, str]:
        """(port, note): the port the component cannot run without, the bridge's own setting when it names one, else the declared
        default. The bridge gets the setting from the env file, else from the user manager's environment (environment.d), which
        the watchdog inherits too. A setting that is there but no usable endpoint, in the bridge's own canonical form (an empty
        one, or one this watchdog cannot read exactly, too), claims no prerequisite: the bridge cannot start on it either, or the
        watchdog cannot tell, and that is a fault to report and restart, not a browser to wait for. The note says so, for the
        alert; it is empty otherwise."""
        setting = None
        if comp.needs_port_env:
            setting = runtime_setting(comp.needs_port_env)
            if setting is None:
                setting = os.environ.get(comp.needs_port_env)
        if setting is None:
            return comp.needs_port, ""
        match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", setting)
        if match and 1 <= int(match[1]) <= 65535 and str(int(match[1])) == match[1]:
            return int(match[1]), ""
        return None, f"; browser port not checked ({comp.needs_port_env} unusable or {RUNTIME_ENV.name} not readable exactly)"

    @staticmethod
    def _refused(port: int) -> bool:
        """True only when a connection to 127.0.0.1:port is refused, i.e. nothing listens. A hung browser whose accept queue is
        full times out instead, and that is no proof it is gone."""
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=PREREQUISITE_TIMEOUT_S):
                return False
        except ConnectionRefusedError:
            return True
        except OSError:
            return False

    @staticmethod
    def _published_host(comp: Component) -> str | None:
        result = bgctl._docker("port", comp.target, f"{comp.tcp_port}/tcp", timeout=5)
        line = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else ""
        host = line.rpartition(":")[0]
        return host.replace("0.0.0.0", "127.0.0.1") or None

    # ── actions ──────────────────────────────────────────────────────────
    def maintenance(self) -> bool:
        flag = self.state_dir / "maintenance"
        try:
            return time.time() - flag.stat().st_mtime < MAINTENANCE_MAX_AGE_S
        except OSError:
            return False

    def _cooldown_ok(self, key: str, cooldown: float = RESTART_COOLDOWN_S) -> bool:
        tracker = self.trackers[key]
        if time.time() - tracker.last_action < cooldown:
            return False
        tracker.last_action = time.time()
        return True

    def restart_container(self, name: str, key: str) -> None:
        if not self._cooldown_ok(key):
            return

        def work() -> None:
            started = time.monotonic()
            bgctl._docker("unpause", name, timeout=10)
            result = bgctl._docker("restart", "-t", "3", name, timeout=90)
            self.event("restart_container", name=name, ok=result.returncode == 0,
                       seconds=round(time.monotonic() - started, 2), error=result.stderr.strip()[-200:])

        threading.Thread(target=work, daemon=True).start()

    def restart_unit(self, unit: str, key: str, cooldown: float = RESTART_COOLDOWN_S) -> None:
        if not self._cooldown_ok(key, cooldown):
            return

        def work() -> None:
            started = time.monotonic()
            result = subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True, text=True, timeout=60)
            self.event("restart_unit", unit=unit, ok=result.returncode == 0,
                       seconds=round(time.monotonic() - started, 2), error=result.stderr.strip()[-200:])

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def label(key: str) -> str:
        """How an alert names a target: the component's label when it has one, else the key itself."""
        return next((comp.label for comp in COMPONENTS if comp.name == key and comp.label), key)

    # ── state machine ────────────────────────────────────────────────────
    def observe(self, key: str, ok: bool, detail: str) -> Tracker:
        tracker = self.trackers.setdefault(key, Tracker())
        tracker.detail, tracker.no_verdict = detail, ""
        if ok:
            if tracker.incident:
                took = time.time() - tracker.incident_start
                self.event("recovered", target=key, outage_s=round(took, 1))
                self.notify(f"aisis-failover [{self.env}]: {self.label(key)} восстановлен (инцидент {took:.0f} с)")
            tracker.fails, tracker.incident = 0, False
        else:
            if tracker.fails == 0:
                tracker.first_fail = time.time()
                self.event("probe_failed", target=key, detail=detail)
            tracker.fails += 1
        return tracker

    def open_incident(self, key: str, tracker: Tracker, message: str) -> None:
        if tracker.incident:
            return
        tracker.incident, tracker.incident_start = True, tracker.first_fail
        self.event("incident", target=key, detail=tracker.detail, message=message)
        self.notify(f"aisis-failover [{self.env}]: {message}")

    def failed(self, tracker: Tracker) -> bool:
        return tracker.fails >= self.threshold

    def tick(self, pool: ThreadPoolExecutor) -> None:
        env = self.env
        active = bgctl.active_color(env)
        standby = bgctl.other(active)
        jobs = {
            f"quasar-{active}": pool.submit(bgctl.health, env, active, self.http_timeout),
            f"quasar-{standby}": pool.submit(bgctl.health, env, standby, self.http_timeout),
            "switch": pool.submit(self._probe_switch),
        }
        if self.components:
            for comp in COMPONENTS:
                if comp.background:
                    waiting = self.pending.get(comp.name)
                    if waiting is None and self.ticks % comp.every == 0:
                        self.pending[comp.name] = pool.submit(self.probe_component, comp)
                    elif waiting is not None and waiting.done():
                        jobs[comp.name] = self.pending.pop(comp.name)
                elif self.ticks % comp.every == 0:
                    jobs[comp.name] = pool.submit(self.probe_component, comp)
        self.ticks += 1
        if self.components and self.clock is None and time.monotonic() >= self.next_clock:
            self.next_clock = time.monotonic() + CLOCK_CHECK_EVERY_S
            self.clock = pool.submit(speaker_clock.probe)
        if self.clock is not None and self.clock.done():
            finished, self.clock = self.clock, None
            self.check_speaker_clock(finished)
        results = {key: job.result() for key, job in jobs.items()}

        active_h, standby_h = results[f"quasar-{active}"], results[f"quasar-{standby}"]
        t_active = self.observe(f"quasar-{active}", active_h["ok"], json.dumps(active_h))
        t_standby = self.observe(f"quasar-{standby}", standby_h["ok"], json.dumps(standby_h))
        switch_ok, switch_detail = results["switch"]
        t_switch = self.observe("switch", switch_ok or not active_h["ok"], switch_detail)
        quiet = self.maintenance()

        if self.failed(t_active) and not quiet:
            self._handle_active_failure(active, standby, t_active, t_standby, standby_h["ok"])
        elif self.failed(t_standby) and not quiet:
            self.open_incident(f"quasar-{standby}", t_standby,
                               f"standby {standby} не отвечает ({t_standby.detail}), перезапускаю; active {active} жив")
            self.restart_container(bgctl.container(env, standby), f"quasar-{standby}")
        if self.failed(t_switch) and not quiet:
            self.open_incident("switch", t_switch, f"switch не отвечает ({t_switch.detail}), перезапускаю")
            self.restart_container(bgctl.ENVS[env]["switch_container"], "switch")

        if not self.components:
            return
        for comp in COMPONENTS:
            if comp.name not in jobs:
                continue  # not probed this tick: only fresh probes count toward the threshold
            ok, detail = results[comp.name]
            if ok is None:
                tracker = self.trackers.setdefault(comp.name, Tracker())
                if comp.needs_port is not None or comp.needs_port_env is not None:
                    # A gone prerequisite ends the failure run (what failed before it is another episode) and holds the count below
                    # zero, so that a returning one gives the unit PREREQUISITE_GRACE_S to start by itself before failures count.
                    tracker.fails = -math.ceil(PREREQUISITE_GRACE_S / self.interval)
                    if tracker.incident:  # the failure the alert announced is over with the prerequisite: the next one is a new incident
                        tracker.incident = False
                        self.event("incident_suspended", target=comp.name, outage_s=round(time.time() - tracker.incident_start, 1))
                        self.notify(f"aisis-failover [{self.env}]: {self.label(comp.name)}: браузер остановлен, мост не перезапускаю, "
                                    "инцидент закрыт; при возвращении браузера и неисправном мосте будет новый")
                if tracker.no_verdict != detail:  # once per change: a long absence must not write a line per tick
                    tracker.no_verdict = detail
                    self.event("probe_no_verdict", target=comp.name, detail=detail)
                continue
            tracker = self.observe(comp.name, ok, detail)
            # Threshold is in time (component_threshold ticks); slower probes need fewer failures.
            needed = max(1 if comp.every == 1 else 2, -(-self.component_threshold // comp.every))
            if tracker.fails < needed or quiet:
                continue
            self.open_incident(comp.name, tracker, f"{self.label(comp.name)} не отвечает ({detail}), перезапускаю")
            if comp.unit:
                self.restart_unit(comp.unit, comp.name, comp.restart_cooldown_s or RESTART_COOLDOWN_S)
            elif comp.container:
                self.restart_container(comp.container, comp.name)

    def check_speaker_clock(self, job: Future) -> None:
        """Collect a finished clock probe: alert once per incident, recovery alert comes from observe()."""
        ok, detail = job.result()
        if ok is None:
            self.event("speaker_clock_unknown", detail=detail)
            return
        tracker = self.observe("speaker-clock", ok, detail)
        if tracker.fails:
            self.open_incident("speaker-clock", tracker, f"часы колонки: {detail}")

    def _probe_switch(self) -> tuple[bool, str]:
        health = bgctl.switch_health(self.env, self.http_timeout)
        running = bgctl.container_running(bgctl.ENVS[self.env]["switch_container"])
        return bool(health["ok"] and running), json.dumps({**health, "running": running})

    def _handle_active_failure(self, active: str, standby: str, t_active: Tracker, t_standby: Tracker,
                               standby_ok: bool) -> None:
        key = f"quasar-{active}"
        if standby_ok:
            detected = time.time()
            try:
                old, new, took = bgctl.flip(self.env, standby)
            except RuntimeError as exc:
                self.event("flip_failed", error=str(exc))
                self.restart_container(bgctl.container(self.env, active), key)
                return
            self.event("failover", target=key, to=new, detect_s=round(detected - t_active.first_fail, 2),
                       flip_s=round(took, 3), since_first_fail_s=round(time.time() - t_active.first_fail, 2))
            self.open_incident(key, t_active, f"{old} упал ({t_active.detail}); трафик переключён на {new} "
                               f"за {time.time() - t_active.first_fail:.1f} с, перезапускаю {old} как standby")
            self.restart_container(bgctl.container(self.env, old), key)
            return
        self.open_incident(key, t_active, f"active {active} и standby {standby} оба не отвечают; перезапускаю {active}")
        self.restart_container(bgctl.container(self.env, active), key)
        if self.failed(t_standby):
            self.restart_container(bgctl.container(self.env, standby), f"quasar-{standby}")

    def run(self) -> None:
        self.event("start", interval=self.interval, threshold=self.threshold, components=self.components,
                   active=bgctl.active_color(self.env))
        with ThreadPoolExecutor(max_workers=16) as pool:
            while True:
                started = time.monotonic()
                try:
                    self.tick(pool)
                except Exception as exc:  # the watchdog itself must not die on a bad tick
                    self.event("tick_error", error=f"{type(exc).__name__}: {exc}")
                time.sleep(max(0.0, self.interval - (time.monotonic() - started)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=list(bgctl.ENVS), default="prod")
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--threshold", type=int, default=3)
    parser.add_argument("--component-threshold", type=int, default=6,
                        help="failed ticks before a non-quasar component is restarted (default 6 ≈ 9 s: "
                             "tolerates a manual `systemctl restart`)")
    parser.add_argument("--http-timeout", type=float, default=1.2)
    parser.add_argument("--components", action=argparse.BooleanOptionalAction, default=None,
                        help="also watch adapter/stt/pm/... (default: on for prod, off for stage)")
    parser.add_argument("--alert", action=argparse.BooleanOptionalAction, default=None,
                        help="send Telegram alerts (default: on for prod, off for stage)")
    args = parser.parse_args()
    prod = args.env == "prod"
    bgctl.STATE_DIR.mkdir(parents=True, exist_ok=True)
    Watchdog(
        env=args.env,
        interval=args.interval,
        threshold=args.threshold,
        component_threshold=args.component_threshold,
        http_timeout=args.http_timeout,
        components=prod if args.components is None else args.components,
        alert=prod if args.alert is None else args.alert,
        state_dir=bgctl.STATE_DIR,
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
