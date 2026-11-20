#!/usr/bin/env python3
"""aisis-ha-cli: always-on local Home Assistant proxy with state cache, and the place where a command is VERIFIED.

Serves 127.0.0.1:8795. Proxies REST calls to Home Assistant while it is up, answers state reads from a persistent cache while
it is down, and queues service calls while Home Assistant is observed to be gone. Keeps the voice stack (quasar container
ha-intents layer, the adapter) working across HA restarts without its own restarts.

Endpoints (auth header optional; the real HA token is added here):
  GET  /health                 -> {ha_up, status up|slow|down, last_error, cache_age_s, cached_states, queued,
                                   recovery_eta_s, outages_measured}
  GET  /api/states             -> live, or cache with X-Aisis-Cache: stale
  GET  /api/states/<entity_id> -> live, or cache with X-Aisis-Cache: stale
  POST /api/template           -> live, or cached rendering with stale header
  POST /api/services/...       -> the call, verified against the device state (below)
Anything else: proxied when HA is up, 503/504 otherwise.

A service call is done when the DEVICE says so (ha_verify.py: what each domain's service must leave in the state). While the call
is in flight ha-cli polls the target entities' states and answers as soon as they are as expected; when Home Assistant's own
answer is an error or never comes, the state is checked all the same: the command may have run (2026-11-19 21:21: the light was
off 0.85 s after the command, Home Assistant's answer took longer than the 2 s ha-cli then allowed, and the speaker announced a
reboot nobody observed). Answers, all within CALL_BUDGET_S (the callers abort at 3 s):
  200  X-Aisis-Verify: ok       the state is as expected (Home Assistant's body, or the verified states when its answer failed)
  502  X-Aisis-Verify: failed   Home Assistant answered, the device state is not as expected after the budget (X-Aisis-Ha-Status)
  504  X-Aisis-Verify: unknown  no state could be read: Home Assistant did not answer. NOT a reboot: nothing is claimed
  202  X-Aisis-Queue: 1, X-Aisis-Eta, X-Aisis-Down: <kind>   Home Assistant was OBSERVED gone (connection refused/reset, 502-504,
       «starting» on the call AND on a second look); the call waits for its return
  4xx  Home Assistant's own rejection, passed through (X-Aisis-Verify: skipped, reason rejected)
  other calls (nothing in the state tells): Home Assistant's answer as it is (X-Aisis-Verify: skipped)
Headers of every answer: X-Aisis-Action (journal id), X-Aisis-Verify[-Reason|-Ms|-Attempts], X-Aisis-Verified-State, X-Aisis-Ha-Status.
Every action, availability change and replay is one line in the journal (ha_action_log.py): `journalctl --user -u aisis-ha-cli`
or ~/.local/share/aisis/ha-actions.jsonl.

Config: ~/.local/share/aisis/ha.config.json ({url, token}).
Cache:   ~/.local/share/aisis/ha-state-cache.json
Outages: ~/.local/share/aisis/ha-outages.json (measured down->up windows)
Journal: ~/.local/share/aisis/ha-actions.jsonl
Env overrides for testing: AISIS_HACLI_HA_URL, AISIS_HACLI_PORT, AISIS_HACLI_CACHE, AISIS_HACLI_OUTAGES, AISIS_HACLI_JOURNAL.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import socket
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import ha_action_log
import ha_verify

BASE = Path.home() / '.local' / 'share' / 'aisis'
CONFIG_FILE = BASE / 'ha.config.json'
CACHE_FILE = Path(os.environ.get('AISIS_HACLI_CACHE', BASE / 'ha-state-cache.json'))
OUTAGES_FILE = Path(os.environ.get('AISIS_HACLI_OUTAGES', BASE / 'ha-outages.json'))
JOURNAL = ha_action_log.Journal(os.environ.get('AISIS_HACLI_JOURNAL', BASE / 'ha-actions.jsonl'))
PORT = int(os.environ.get('AISIS_HACLI_PORT', '8795'))
REFRESH_S = 30.0
# While HA is down, probe fast so the measured outage ends within seconds of the
# real recovery and queued service calls replay promptly.
DOWN_POLL_S = 3.0
HA_TIMEOUT_S = 5.0
# A proxied READ must answer inside the caller's own timeout: the quasar container's haFetch aborts at 3 s (FETCH_TIMEOUT_MS in
# ha-intents.ts), the adapter's session at 3 s. Background probing keeps the longer HA_TIMEOUT_S.
CLIENT_TIMEOUT_S = 2.0
# A service call is answered inside CALL_BUDGET_S, state checks included; Home Assistant gets CALL_TIMEOUT_S for its own answer
# and the call thread goes on after the caller was answered (the late result is journalled).
CALL_BUDGET_S = 2.5
CALL_TIMEOUT_S = 2.3
LATE_VERIFY_TOTAL_S = 6.0   # a call the budget could not confirm is looked at again this long after it was sent (background)
LATE_POLL_S = 0.5
LATE_VERIFY_MAX = 4         # at most this many background looks at once
POLL_FIRST_S = 0.2      # the first state check comes this long after the call left
POLL_EVERY_S = 0.25
STATE_READ_S = 0.6      # one state read must not eat the budget
FINAL_PROBE_S = 0.2     # the last look at Home Assistant before an answer without a verdict: it must fit between CALL_BUDGET_S and the callers' 3 s
BEFORE_READ_S = 0.5    # the state read before the call: it may not delay the call by more than this
DOWN_CONFIRM_S = 0.25   # a second look this long after a call that found Home Assistant gone
UNRESPONSIVE_S = 5.0   # no answer to anything for this long: Home Assistant is down, cause unknown (less: slow)
# Recovery time of a Home Assistant restart: median of the last OUTAGE_WINDOW
# down->up durations. Seeded with the 38 outages the ha-ble-vhci service logged
# ("Home Assistant is back after N s", journal 2026-10-23..11-07: median 62 s,
# nightly 03:00 restarts ~33 s, manual reloads 61-91 s, 6 outliers 145-434 s);
# real outages measured here are appended to OUTAGES_FILE and replace the seed
# oldest-first.
SEED_OUTAGES_S = [
    61, 37, 241, 91, 91, 33, 151, 91, 34, 62, 33, 91, 62, 63, 61, 91, 61, 63, 91,
    13, 78, 434, 7, 179, 145, 33, 269, 8, 17, 7, 6, 91, 91, 61, 12, 91, 91, 1,
]
OUTAGE_WINDOW = 40
# Transport errors that say nobody is listening (versus a timeout, which says nothing about why).
DOWN_KINDS = frozenset({'refused', 'reset', 'dns', 'unreachable', 'starting'})

_lock = threading.Lock()
_late_slots = threading.BoundedSemaphore(LATE_VERIFY_MAX)
# Only calls that never connected (refused/dns/unreachable) are queued, so none of them ran, and they replay first in, first out.
# Known limitation: nothing orders a replay against a NEWER live command on the same device that reached Home Assistant first;
# the older queued write would land after it (tracked as a hardening ticket). Dropping queued entries by entity is not the answer:
# it would drop prerequisites (set_hvac_mode before set_temperature) and targets the newer command did not touch.
_queue: list[tuple[str, dict | None, str]] = []  # (path, body, action id) service calls waiting for Home Assistant
_wake = threading.Event()  # set when a request saw trouble: probe now, then fast


def load_config() -> tuple[str, str | None]:
    url = os.environ.get('AISIS_HACLI_HA_URL')
    token = None
    if not url:
        cfg = json.loads(CONFIG_FILE.read_text())
        url = cfg['url']
        token = cfg.get('token')
    return url.rstrip('/'), token


HA_URL, HA_TOKEN = load_config()


# -- one request to Home Assistant -----------------------------------------------------------------------------------------

@dataclass
class Reply:
    status: int              # HTTP status, 0 when nothing came back
    raw: bytes = b''
    error: str | None = None  # why nothing came back (timeout|refused|reset|dns|unreachable|ssl|other), or `starting` on a 503 «starting»
    ms: int = 0
    done_at: float = 0.0     # time.monotonic() when the request ended (0: not known)

    @property
    def down_evidence(self) -> bool:
        """Home Assistant is observed gone: nobody listens, or its front says it is not there (not: it was merely slow)."""
        return self.error in DOWN_KINDS or self.status in (502, 503, 504)

    @property
    def answered(self) -> bool:
        return self.status > 0 and not self.down_evidence

    @property
    def never_sent(self) -> bool:
        """The call certainly did not reach Home Assistant: no connection was made (refused, no such host, no route), so it is safe
        to send again. Anything after the connection (a timeout, a reset, a gateway answer whatever its text) leaves it unknown
        whether the service ran."""
        return self.error in ('refused', 'dns', 'unreachable')

    @property
    def what(self) -> str:
        """The outcome as a short token for logs and headers: the HTTP status, or the error kind."""
        return self.error if self.status == 0 and self.error else str(self.status)


def _classify(err: BaseException) -> str:
    reason = err.reason if isinstance(err, urllib.error.URLError) else err
    if isinstance(reason, TimeoutError):
        return 'timeout'
    if isinstance(reason, ConnectionRefusedError):
        return 'refused'
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return 'reset'
    if isinstance(reason, socket.gaierror):
        return 'dns'
    if isinstance(reason, ssl.SSLError):
        return 'ssl'
    if isinstance(reason, OSError) and reason.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN):
        return 'unreachable'
    return 'other'


def _finished(reply: Reply, started: float) -> Reply:
    """Stamp the reply with when its request ended (monotonic), so that a state read can be told to have started before or after it."""
    reply.done_at = time.monotonic()
    reply.ms = int((reply.done_at - started) * 1000)
    return reply


def ha_call(path: str, body: dict | None = None, timeout: float = HA_TIMEOUT_S) -> Reply:
    started = time.monotonic()
    req = urllib.request.Request(HA_URL + path, method='GET' if body is None else 'POST')
    if HA_TOKEN:
        req.add_header('Authorization', f'Bearer {HA_TOKEN}')
    data = None
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    except Exception as err:
        return _finished(Reply(0, b'', _classify(err)), started)
    starting = status == 503 and b'starting' in raw[:400].lower()
    return _finished(Reply(status, raw, 'starting' if starting else None), started)


def read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


# -- outage statistics and availability ------------------------------------------------------------------------------------

def _load_measured_outages() -> list[dict]:
    try:
        data = json.loads(OUTAGES_FILE.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [o for o in data if isinstance(o, dict) and isinstance(o.get('s'), (int, float))]


_measured_outages = _load_measured_outages()[-OUTAGE_WINDOW:]


def recovery_eta_s() -> int:
    """Typical HA restart time: median of recent down->up durations, rounded to 5 s."""
    window = (SEED_OUTAGES_S + [o['s'] for o in _measured_outages])[-OUTAGE_WINDOW:]
    return max(5, int(round(statistics.median(window) / 5.0)) * 5)


def _record_outage(seconds: float) -> None:
    """Keep one measured down->up window (caller holds _lock)."""
    _measured_outages.append({'at': round(time.time()), 's': round(seconds)})
    del _measured_outages[:-OUTAGE_WINDOW]
    try:
        tmp = OUTAGES_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(_measured_outages))
        tmp.replace(OUTAGES_FILE)
    except OSError:
        pass  # read-only stage mount: keep the in-memory window


class Availability:
    """What ha-cli has OBSERVED about Home Assistant: up, slow (requests got no answer, for less than UNRESPONSIVE_S) or down
    (nobody listens, a proxy in front says so, or nothing has answered for UNRESPONSIVE_S). A slow answer is not an outage: it is
    not timed, queued for, or announced as one.

    A down window is only measured when its start was observed (HA seen up first), so a daemon started during an outage does not
    record a truncated duration."""

    def __init__(self) -> None:
        self.state = 'unknown'
        self.since = time.time()
        self.first_failure = 0.0     # when the current run of unanswered requests began
        self.last_error: str | None = None
        self.last_ok = 0.0
        self.seen_up = False
        self.down_since: float | None = None

    @property
    def up(self) -> bool:
        return self.state in ('up', 'slow')

    def note(self, reply: Reply, now: float) -> list[dict]:
        """Fold one request's outcome in; returns the journal records of the state change (caller holds _lock)."""
        if reply.answered:
            self.first_failure = 0.0
            self.last_ok = now
            self.seen_up = True
            return self._move('up', now, None)
        if not self.first_failure:
            self.first_failure = now
        self.last_error = reply.error or f'http_{reply.status}'
        silent_long = now - self.first_failure >= UNRESPONSIVE_S
        return self._move('down' if reply.down_evidence or silent_long else 'slow', now, self.last_error)

    def _move(self, new: str, now: float, error: str | None) -> list[dict]:
        old = self.state
        if new == old:
            return []
        record: dict[str, Any] = {'kind': 'ha_status', 'ts': round(now, 3), 'from': old, 'to': new}
        if error:
            record['error'] = error
        if new == 'down' and self.down_since is None and self.seen_up:
            self.down_since = now
        if new == 'up' and self.down_since is not None:
            record['down_s'] = round(now - self.down_since, 1)
            _record_outage(now - self.down_since)
            self.down_since = None
        self.state, self.since = new, now
        return [record]


_avail = Availability()


def observe(reply: Reply) -> None:
    """Every answer (or silence) of Home Assistant to any request teaches ha-cli how it is doing."""
    with _lock:
        records = _avail.note(reply, time.time())
    for record in records:
        JOURNAL.write(record)
    if not reply.answered:
        _wake.set()


# -- state reads for verification ------------------------------------------------------------------------------------------

def _read_state(entity: str, timeout: float) -> tuple[dict | None, Reply]:
    reply = ha_call('/api/states/' + urllib.parse.quote(entity, safe=''), timeout=timeout)
    observe(reply)
    if reply.status == 200:
        try:
            state = json.loads(reply.raw)
        except ValueError:
            state = None
        return (state if isinstance(state, dict) else None), reply
    return None, reply


def read_states(entities: tuple[str, ...], timeout: float) -> tuple[dict[str, dict | None], list[Reply]]:
    """Fresh states (never the cache: a stale state would verify a command by the past) of the entities, read in parallel."""
    if not entities:
        return {}, []
    if len(entities) == 1:
        state, reply = _read_state(entities[0], timeout)
        return {entities[0]: state}, [reply]
    with ThreadPoolExecutor(max_workers=len(entities)) as pool:
        got = list(pool.map(lambda entity: _read_state(entity, timeout), entities))
    return {entity: state for entity, (state, _) in zip(entities, got)}, [reply for _, reply in got]


# -- one verified service call ---------------------------------------------------------------------------------------------

class _Call:
    """Home Assistant's own answer to a service call, awaited in a thread so the device state can be watched meanwhile."""

    def __init__(self, path: str, body: dict | None, action_id: str) -> None:
        self.path, self.body, self.action_id = path, body, action_id
        self.reply: Reply | None = None
        self.done = threading.Event()
        self.sent = time.monotonic()
        self._caller_answered = False
        self._late_journaled = False
        self._late_lock = threading.Lock()
        threading.Thread(target=self._run, name='ha-call', daemon=True).start()

    def _run(self) -> None:
        self.reply = ha_call(self.path, self.body, CALL_TIMEOUT_S)
        observe(self.reply)
        self.done.set()
        self._journal_late()

    def caller_answered(self) -> None:
        """The caller has its answer while this call is still in flight: how it ends goes to the journal."""
        with self._late_lock:
            self._caller_answered = True
        self._journal_late()

    def _journal_late(self) -> None:
        with self._late_lock:
            late = self._caller_answered and self.done.is_set() and not self._late_journaled
            self._late_journaled = self._late_journaled or late
        if late:
            JOURNAL.write({'kind': 'ha_response', 'ts': round(time.time(), 3), 'id': self.action_id, 'call': _call_record(self.reply)})


def _call_record(reply: Reply | None) -> dict:
    if reply is None:
        return {'pending': True}
    record: dict[str, Any] = {'ms': reply.ms}
    if reply.status:
        record['status'] = reply.status
    if reply.error:
        record['error'] = reply.error
    return record


def _ascii(text: str) -> str:
    return text.encode('ascii', 'replace').decode('ascii')


@dataclass
class Answer:
    status: int
    body: bytes
    headers: dict
    record: dict


def _probe(timeout: float) -> Reply:
    reply = ha_call('/api/', timeout=timeout)
    observe(reply)
    return reply


def _check(expect: ha_verify.Expectation, entities: tuple[str, ...], timeout: float
           ) -> tuple[ha_verify.Verdict, dict[str, dict | None], list[Reply]]:
    """One look at the device state against the expectation: (verdict, the states read, the replies of the reads)."""
    if expect.whole_domain:
        reply = ha_call('/api/template', {'template': ha_verify.whole_domain_template(expect.whole_domain, expect.whole_state or '')}, timeout)
        observe(reply)
        verdict = (ha_verify.whole_domain_verdict(reply.raw.decode('utf-8', 'replace')) if reply.status == 200
                   else ha_verify.Verdict('pending', 'state_unreadable'))
        return verdict, {}, [reply]
    states, replies = read_states(entities, timeout)
    return expect.check(states), states, replies


class ServiceCall:
    """One `POST /api/services/<domain>/<service>`: state before, the call, state checks until the device agrees or the budget ends."""

    def __init__(self, path: str, body: dict | None, query: str = '') -> None:
        self.path, self.body, self.query = path, body, query
        self.started = time.monotonic()
        self.wall = time.time()
        self.id = secrets.token_hex(4)
        parts = path.split('/')
        self.domain, self.service = (parts[3], parts[4]) if len(parts) >= 5 else ('', '')
        found = ha_verify.targets(body)
        self.entities: tuple[str, ...] = found[0] if found else ()
        self.before: dict[str, dict | None] = {}
        self.expect: ha_verify.Expectation | None = None
        self.already = False             # the state read before the call was already the expected one
        self.call: _Call | None = None
        self.verdict: ha_verify.Verdict | None = None
        self.states: dict[str, dict | None] = {}
        self.attempts = 0
        self.reads_answered = 0
        self.down_look: Reply | None = None   # the second look that confirmed Home Assistant gone
        self.rejected = False
        self.verified_ms: int | None = None
        self.read_at = 0.0               # when the state read behind `verdict` started
        self.read_down = 0               # polls in a row whose every state read found Home Assistant gone

    def _left(self) -> float:
        return self.started + CALL_BUDGET_S - time.monotonic()

    def run(self) -> Answer:
        self.expect = ha_verify.expectation(self.domain, self.service, self.body) if self.domain else None
        if self.entities and (self.expect is not None or self.service == 'toggle'):
            # The state before the call tells a toggle's goal and whether the device already was where the call leads; a call with
            # nothing to check is not delayed by it.
            self.before, before_replies = read_states(self.entities, BEFORE_READ_S)
            self.reads_answered = sum(1 for reply in before_replies if reply.answered)
            self.expect = ha_verify.expectation(self.domain, self.service, self.body, self.before)
        self.already = bool(self.expect and self.entities and self.expect.check(self.before).result == 'ok')
        self.call = _Call(self.path + self.query, self.body, self.id)
        self._watch()
        return self._answer()

    def _watch(self) -> None:
        call = self.call
        assert call is not None
        while self._left() > 0:
            reply = call.reply
            if reply is not None and 400 <= reply.status < 500:
                self.rejected = True             # Home Assistant refused the call itself: nothing was done
                return
            if reply is not None and self.expect is None and not reply.down_evidence:
                return                           # nothing in the state to check: its answer is the answer
            first = self.attempts == 0 and self.down_look is None
            pause = DOWN_CONFIRM_S if reply is not None and reply.down_evidence else (POLL_FIRST_S if first else POLL_EVERY_S)
            if call.done.is_set():
                time.sleep(max(0.0, min(pause, self._left())))
            else:
                call.done.wait(max(0.0, min(pause, self._left())))     # a quick answer is looked at at once
            reply = call.reply
            if reply is not None and 400 <= reply.status < 500:
                self.rejected = True             # its refusal came while we waited: a state that matches does not make it a success
                return
            if reply is not None and reply.down_evidence:
                look = _probe(min(STATE_READ_S, max(0.1, self._left())))
                if look.down_evidence:
                    self.down_look, self.verdict = look, None     # the call found Home Assistant gone and so does this second look
                    return
            if self.expect is None:
                if call.done.is_set():
                    return                       # Home Assistant answered like a gateway but is there, and there is no state to look at
                continue
            read_at = time.monotonic()
            verdict, states, replies = _check(self.expect, self.entities, min(STATE_READ_S, max(0.1, self._left())))
            self.verdict, self.states, self.read_at = verdict, states, read_at
            self.attempts += 1
            self.reads_answered += sum(1 for r in replies if r.answered)
            # Home Assistant may go away while the call is in flight: two polls in a row where nothing but «gone» came back say so.
            # Counted on every poll (a successful read breaks the streak, a matching state included) and only while Home Assistant
            # has not answered the call: one that took it and then went away (a restart) is not queued again, or a toggle would
            # run twice. A call that merely timed out has not answered.
            gone = replies and all(r.down_evidence for r in replies) and not (call.reply is not None and call.reply.answered)
            self.read_down = self.read_down + 1 if gone else 0
            if self.read_down >= 2:
                self.down_look = replies[-1]
                return
            if verdict.result == 'ok':
                if self.already and not (call.reply is not None and call.reply.answered):
                    # The device already was in the wanted state, so the state cannot show that Home Assistant took the call:
                    # only its own answer can say whether it was refused. Keep watching (a refusal, an outage) until it comes.
                    continue
                self.verified_ms = int((time.monotonic() - call.sent) * 1000)
                return

    def _judge(self, reply: Reply | None, verdict: ha_verify.Verdict | None, ms: int, read_at: float,
               late_open: bool | None = None) -> dict[str, Any] | None:
        """The verify record for a state read (`read_at`: when it started) and the latest reply of Home Assistant; None when the state
        could not be read. The foreground answer and the late background look both judge through this."""
        if reply is not None and 400 <= reply.status < 500:
            return {'result': 'skipped', 'reason': 'rejected'}
        if verdict is None or verdict.result not in ('ok', 'failed'):
            return None
        if verdict.result == 'ok':
            if self.already and not (reply is not None and reply.answered):
                # The device was in the wanted state before the call, so its state proves nothing: only Home Assistant's answer does.
                return {'result': 'unknown', 'reason': 'already_in_state_unanswered', 'ms': ms, 'after': verdict.after}
            verify: dict[str, Any] = {'result': 'ok', 'reason': 'already_in_state' if self.already else 'state_matched',
                                      'ms': self.verified_ms if self.verified_ms is not None else ms, 'after': verdict.after}
            if verdict.unchecked:
                verify['unchecked'] = list(verdict.unchecked)
            return verify
        if not self._mismatch_is_final(verdict, reply, read_at, late_open):
            # A climate unit takes seconds: the old mode shows on the state while Home Assistant's call is still running, and the new
            # one a moment after the budget (2026-11-15, 2026-11-20). A mismatch that Home Assistant had not answered is no failure.
            return {'result': 'unknown', 'reason': 'not_yet_confirmed', 'ms': ms, 'after': verdict.after}
        return {'result': 'failed', 'reason': verdict.reason, 'ms': ms, 'after': verdict.after, 'wrong': verdict.wrong}

    def _deferrable(self, verdict: ha_verify.Verdict) -> bool:
        """A mismatch that may still resolve: every mismatched entity is a climate unit (a lamp obeys at once, a unit takes seconds)."""
        return (verdict.reason != 'device_unavailable' and bool(verdict.wrong)
                and all(entity.startswith('climate.') for entity in verdict.wrong))

    def _mismatch_is_final(self, verdict: ha_verify.Verdict, reply: Reply | None, read_at: float, late_open: bool | None = None) -> bool:
        """Whether a state that is not the wanted one is a failure. `late_open`: None in the answer to the caller (a mismatch that
        may still resolve, a climate unit's, is deferred; any other is final), True while the background look has time left (never
        final), False at its deadline. A deferred mismatch is a failure only once Home Assistant answered the call ok and the state
        was read after that answer completed."""
        if late_open:
            return False
        if late_open is None and not self._deferrable(verdict):
            return True
        return reply is not None and reply.answered and read_at >= reply.done_at

    def _answer(self) -> Answer:
        call, expect, verdict = self.call, self.expect, self.verdict
        assert call is not None
        reply = call.reply
        since_sent = int((time.monotonic() - call.sent) * 1000)
        headers: dict[str, str] = {'X-Aisis-Action': self.id}
        verify: dict[str, Any] = {'result': 'skipped', 'reason': 'not_verifiable'}
        status, payload, why = 200, b'', 'passed_through'

        judged = self._judge(reply, verdict, since_sent, self.read_at)
        if judged is not None and judged['reason'] == 'rejected':      # judged on the latest answer: a refusal can arrive while the state is read
            verify = judged
            status, payload, why = reply.status, reply.raw, 'ha_rejected'
        elif self.down_look is not None and reply is not None and reply.answered:
            # Home Assistant took the call (its answer arrived after the outage was seen): it ran, so it is never queued again.
            verify = {'result': 'unknown', 'reason': 'ha_down_after_answer', 'ms': since_sent}
            status, payload, why = 504, json.dumps({'error': 'ha_down_after_answer', 'verify': verify}).encode(), 'unverified_in_flight'
        elif self.down_look is not None and not (reply is not None and reply.never_sent):
            # It is not known whether the call ran: still in flight, or it ended in a timeout, a reset or a gateway answer. Sending it
            # again could undo or repeat it, whatever the service: only a call that never connected is queued.
            verify = {'result': 'unknown', 'reason': 'ha_down_in_flight', 'ms': since_sent}
            status, payload, why = 504, json.dumps({'error': 'ha_down_in_flight', 'verify': verify}).encode(), 'unverified_in_flight'
        elif self.down_look is not None:
            evidence = self.down_look.error or f'http_{self.down_look.status}'
            verify = {'result': 'unknown', 'reason': 'ha_down_observed', 'ms': since_sent}
            with _lock:
                _queue.append((self.path + self.query, self.body, self.id))
                position = len(_queue)
            eta = recovery_eta_s()
            status = 202
            payload = json.dumps({'queued': True, 'position': position, 'recovery_eta_s': eta, 'down': evidence}).encode()
            headers.update({'X-Aisis-Queue': '1', 'X-Aisis-Eta': str(eta), 'X-Aisis-Down': evidence})
            why = 'queued_ha_down'
        elif expect is None and reply is not None and reply.status and not reply.down_evidence:
            status, payload = reply.status, reply.raw    # Home Assistant answered and nothing in the state could say otherwise
        elif expect is None:
            reason = 'no_answer_unverifiable' if _probe(FINAL_PROBE_S).answered else 'ha_no_answer'
            verify = {'result': 'unknown', 'reason': reason, 'ms': since_sent}
            status, payload, why = 504, json.dumps({'error': reason, 'verify': verify}).encode(), 'unverified_no_answer'
        elif judged is not None:
            verify = judged
            if judged['result'] == 'ok':
                payload = reply.raw if reply is not None and 200 <= reply.status < 300 else json.dumps([s for s in self.states.values() if s]).encode()
                why = 'verified_state'
            else:
                failed = judged['result'] == 'failed'
                status, why = (502, 'state_not_as_expected') if failed else (504, 'unverified_no_answer')
                payload = json.dumps({'error': judged['reason'], 'verify': judged}).encode()
        else:
            reason = 'ha_no_answer' if self.reads_answered == 0 else 'state_unreadable'
            verify = {'result': 'unknown', 'reason': reason, 'ms': since_sent}
            status, payload, why = 504, json.dumps({'error': reason, 'verify': verify}).encode(), 'unverified_no_answer'

        verify['attempts'] = self.attempts
        headers['X-Aisis-Verify'] = verify['result']
        headers['X-Aisis-Verify-Reason'] = verify['reason']
        if verify.get('ms') is not None:
            headers['X-Aisis-Verify-Ms'] = str(verify['ms'])
        headers['X-Aisis-Verify-Attempts'] = str(self.attempts)
        if verify.get('unchecked'):
            headers['X-Aisis-Verify-Unchecked'] = ','.join(verify['unchecked'])
        if verify.get('after'):
            headers['X-Aisis-Verified-State'] = _ascii(','.join(f'{k}={v}' for k, v in list(verify['after'].items())[:5]))[:240]
        headers['X-Aisis-Ha-Status'] = reply.what if reply is not None else 'pending'

        record: dict[str, Any] = {
            'kind': 'action', 'ts': round(self.wall, 3), 'id': self.id, 'service': f'{self.domain}.{self.service}',
            'entities': [f'{expect.whole_domain}.*'] if expect is not None and expect.whole_domain else list(self.entities),
            'data': self.body if isinstance(self.body, dict) else {}, 'expect': expect.want if expect is not None else None,
            'before': {k: ha_verify.state_text(v) for k, v in self.before.items()}, 'call': _call_record(reply),
            'verify': verify, 'reply': {'status': status, 'why': why}, 'ms': int((time.monotonic() - self.started) * 1000)}
        JOURNAL.write(record)
        if verify['result'] == 'unknown' and status != 202 and self.down_look is None and expect is not None:
            self._start_late_verification()
        if reply is None:
            call.caller_answered()
        return Answer(status, payload, headers, record)

    def _start_late_verification(self) -> None:
        """A call the budget could not confirm is looked at again for a few seconds in the background and what the device did is
        journalled. At most LATE_VERIFY_MAX of these run at once: a busy moment drops the look, it never queues one."""
        if not _late_slots.acquire(blocking=False):
            return
        try:
            threading.Thread(target=self._late_verification, name='ha-late', daemon=True).start()
        except BaseException:
            _late_slots.release()
            raise

    def _late_verification(self) -> None:
        expect, call = self.expect, self.call
        assert expect is not None and call is not None
        try:
            deadline = self.started + LATE_VERIFY_TOTAL_S
            while True:
                at_deadline = time.monotonic() >= deadline
                read_at = time.monotonic()
                verdict, states, _ = _check(expect, self.entities, STATE_READ_S)
                ms = int((time.monotonic() - call.sent) * 1000)
                verify = self._judge(call.reply, verdict, ms, read_at, late_open=not at_deadline)
                if (verify is not None and verify['result'] != 'unknown') or at_deadline:
                    break           # any mismatch, a missing answer or an unreadable state is waited out until the deadline
                time.sleep(LATE_POLL_S)
            record: dict[str, Any] = {
                'kind': 'late_verify', 'ts': round(self.wall, 3), 'done_ts': round(time.time(), 3), 'id': self.id,
                'service': f'{self.domain}.{self.service}',
                'entities': [f'{expect.whole_domain}.*'] if expect.whole_domain else list(self.entities),
                'data': self.body if isinstance(self.body, dict) else {}, 'expect': expect.want,
                'before': {k: ha_verify.state_text(v) for k, v in self.before.items()}, 'call': _call_record(call.reply),
                'verify': verify or {'result': 'unknown', 'reason': 'state_unreadable', 'ms': ms}, 'ms': ms}
            if any(entity.startswith('climate.') for entity in self.entities):
                record['climate'] = {entity: {'hvac_mode': state.get('state'), 'temperature': (state.get('attributes') or {}).get('temperature')}
                                     for entity, state in states.items() if isinstance(state, dict)}
            JOURNAL.write(record)
        finally:
            _late_slots.release()


def service_call(path: str, body: dict | None, query: str = '') -> Answer:
    return ServiceCall(path, body, query).run()


# -- the background probe, the queue replay --------------------------------------------------------------------------------

def probe_once() -> None:
    """One look at Home Assistant: refresh the state cache, learn its availability, and send what waited for it."""
    reply = ha_call('/api/states')
    cache = {}
    if reply.status == 200:
        try:
            states = json.loads(reply.raw)
        except Exception:
            states = None
        if isinstance(states, list):
            cache['states'] = states
            # Areas template needed by the container registry builder.
            template = ha_call('/api/template', {'template': AREA_TEMPLATE})
            if template.status == 200:
                cache['areas'] = template.raw.decode('utf-8', 'replace')
            cache['saved_at'] = time.time()
            tmp = CACHE_FILE.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(cache, ensure_ascii=False))
            tmp.replace(CACHE_FILE)
    observe(reply)
    pending: list[tuple[str, dict | None, str]] = []
    with _lock:
        if reply.status == 200 and _queue:
            pending, _queue[:] = _queue[:], []
    for path, body, action_id in pending:
        _replay(path, body, action_id)


def _replay(path: str, body: dict | None, action_id: str) -> None:
    """A call that waited for Home Assistant's return: sent, then checked against the state like any other."""
    sent = ha_call(path, body, HA_TIMEOUT_S)
    observe(sent)
    parts = path.partition('?')[0].split('/')
    found = ha_verify.targets(body)
    entities = found[0] if found else ()
    expect = ha_verify.expectation(parts[3], parts[4], body) if len(parts) >= 5 else None
    record: dict[str, Any] = {'kind': 'replay', 'ts': round(time.time(), 3), 'id': action_id,
                              'service': '.'.join(parts[3:5]), 'entities': list(entities), 'call': _call_record(sent)}
    if expect is not None and sent.answered:
        deadline = time.monotonic() + 2.0
        verdict = _check(expect, entities, STATE_READ_S)[0]
        while verdict.result != 'ok' and time.monotonic() < deadline:
            time.sleep(POLL_EVERY_S)
            verdict = _check(expect, entities, STATE_READ_S)[0]
        record['verify'] = {'result': verdict.result, 'reason': verdict.reason, 'after': verdict.after}
        if verdict.unchecked:
            record['verify']['unchecked'] = list(verdict.unchecked)
    JOURNAL.write(record)


def refresh_loop() -> None:
    while True:
        probe_once()
        _wake.wait(REFRESH_S if _avail.state == 'up' else DOWN_POLL_S)
        _wake.clear()


# Keep in sync with backend/ha-intents.ts AREA_TEMPLATE.
AREA_TEMPLATE = (
    '{% for e in states %}{% set d = device_id(e.entity_id) %}'
    '{% if d %}{{ e.entity_id }}\t{{ e.attributes.friendly_name }}\t'
    '{{ area_name(d) }}\n{% endif %}{% endfor %}'
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status: int, body: bytes, headers: dict | None = None,
              content_type: str = 'application/json'):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unreachable(self, reply: Reply):
        """Home Assistant gave no answer to a proxied request: 503 when it is observed gone, 504 when it merely did not answer."""
        self._send(503 if reply.down_evidence else 504, json.dumps({'error': reply.error or f'http_{reply.status}'}).encode())

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/health':
            with _lock:
                body = json.dumps({
                    'ha_up': _avail.up,
                    'status': _avail.state,
                    'last_error': _avail.last_error,
                    'cache_age_s': None if not _avail.up else round(time.time() - _avail.last_ok),
                    'cached_states': len(read_cache().get('states', [])),
                    'queued': len(_queue),
                    'recovery_eta_s': recovery_eta_s(),
                    'outages_measured': len(_measured_outages),
                }).encode()
            self._send(200, body)
            return
        if path == '/api/states' or path.startswith('/api/states/'):
            reply = ha_call(path, timeout=CLIENT_TIMEOUT_S)
            observe(reply)
            if reply.status == 200:
                self._send(200, reply.raw, {'X-Aisis-Cache': 'live'})
                return
            # HA down or silent: serve cache (the header says it is stale).
            cache = read_cache()
            states = cache.get('states', [])
            if path == '/api/states':
                self._send(200, json.dumps(states).encode(), {'X-Aisis-Cache': 'stale'})
                return
            entity = path.rsplit('/', 1)[1]
            for s in states:
                if s.get('entity_id') == entity:
                    self._send(200, json.dumps(s).encode(), {'X-Aisis-Cache': 'stale'})
                    return
            self._send(404, b'{"error": "not in cache"}', {'X-Aisis-Cache': 'stale'})
            return
        # Other GETs: straight proxy, the query string (`/api/history/period/...?filter_entity_id=`, the logbook's `entity=`) included.
        reply = ha_call(self.path, timeout=CLIENT_TIMEOUT_S)
        observe(reply)
        if reply.status:
            self._send(reply.status, reply.raw)
        else:
            self._unreachable(reply)

    def do_POST(self):
        path = self.path.split('?')[0]
        query = self.path[len(path):]  # `?return_response` selects a service call that answers with data
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'null')
        except Exception:
            body = None
        if path == '/api/template':
            reply = ha_call(path, body, CLIENT_TIMEOUT_S)
            observe(reply)
            if reply.status == 200:
                self._send(200, reply.raw, {'X-Aisis-Cache': 'live'})
                return
            cache = read_cache()
            if cache.get('areas') is not None:
                self._send(200, cache['areas'].encode(), {'X-Aisis-Cache': 'stale'})
            else:
                self._send(503, b'{"error": "ha down, no cache"}')
            return
        if path.startswith('/api/services/'):
            if 'return_response' in urllib.parse.parse_qs(query.lstrip('?'), keep_blank_values=True):
                # The caller waits for data: nothing to verify, and queueing it would only hide the outage.
                reply = ha_call(path + query, body, CLIENT_TIMEOUT_S)
                observe(reply)
                if reply.status:
                    self._send(reply.status, reply.raw)
                else:
                    self._unreachable(reply)
                return
            answer = service_call(path, body, query)
            self._send(answer.status, answer.body, answer.headers)
            return
        reply = ha_call(path + query, body, CLIENT_TIMEOUT_S)
        observe(reply)
        if reply.status:
            self._send(reply.status, reply.raw)
        else:
            self._unreachable(reply)


def main() -> int:
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'aisis-ha-cli on 127.0.0.1:{PORT}, upstream {HA_URL}', file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
