"""A service call that is DONE when the device says so, through Home Assistant's own API and nothing else.

`service_call` sends `call_service` over the WebSocket and, on the same connection, listens to Home Assistant's state triggers
(`subscribe_trigger`) of the entities the call targets. It answers as soon as Home Assistant's events (or its own answer) show the state the
service must leave behind (ha_verify.py: the expectation of each domain) — not when Home Assistant's HTTP answer finally arrives. No polling,
no cache, no queue: a call is sent once; when Home Assistant is unreachable nothing is sent and nothing is kept for later.

Why the confirmation is the device state and not only Home Assistant's answer (2026-11-19 21:21 «выключи свет»): the light was off 0.85 s after
the command and Home Assistant's own answer took longer than the caller waits; judged by the answer alone the speaker announced a reboot nobody
observed. The verdict (`CallResult.verify`):
  ok       the state is as expected (reason state_matched; already_in_state when it was before the call and Home Assistant accepted the call)
  failed   Home Assistant answered and the device is not as expected (state_mismatch, device_unavailable), or a lamp did not obey in the budget
  unknown  no state could be read, Home Assistant did not answer (ha_no_answer) or went away in flight, or a climate unit has not shown it YET
           (2026-11-15, 2026-11-20: an air conditioner takes seconds; its call is not judged failed before Home Assistant answered)
  skipped  nothing in the state tells (a spoken message), or Home Assistant rejected the call (rejected)
`status` is 200 ok / 502 failed / 504 unknown / Home Assistant's own 4xx for a rejection / 0 when Home Assistant could not be reached and NOTHING was
sent (`down` is then the evidence: refused, reset, dns, unreachable, starting, http_5xx).

The whole call fits CALL_BUDGET_S, under the quasar layer's 3 s fetch timeout and the adapter's 3 s session.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

import ha_client
import ha_verify
import ha_ws

CALL_BUDGET_S = 2.5     # everything: state before, connect, the call, the confirmation
BEFORE_READ_S = 0.5     # the state before the call may not delay it by more than this
CONNECT_S = 0.6         # opening the WebSocket
READ_S = 0.6            # one template read after Home Assistant's answer (a whole-domain call)
FINAL_READ_S = 0.25     # the last look at the states when the budget ends without a confirmation: 2.5 + 0.25 stays under the callers' 3 s
DOWN_KINDS = frozenset({"refused", "reset", "dns", "unreachable"})   # nobody is listening (a timeout says nothing about why)
REJECTED_CODES = frozenset({"not_found", "invalid_format", "service_validation_error", "unknown_command"})


@dataclass
class CallResult:
    status: int
    verify: dict[str, Any]
    body: Any = None
    ha: str | None = None        # Home Assistant's own outcome: its HTTP status / error code, or an error kind; None: not asked
    down: str | None = None      # evidence that nobody listens, when status is 0
    events: list[dict] = field(default_factory=list)


def _read_states(client: ha_client.HaClient, entities: tuple[str, ...], timeout: float) -> tuple[dict[str, dict | None], list[ha_client.Reply]]:
    """Fresh states (never remembered ones: a stale state would verify a command by the past), read in parallel."""
    def one(entity: str) -> ha_client.Reply:
        return client.get("/api/states/" + entity, timeout=timeout)
    with ThreadPoolExecutor(max_workers=max(1, len(entities))) as pool:
        replies = list(pool.map(one, entities))
    states: dict[str, dict | None] = {}
    for entity, reply in zip(entities, replies):
        data = reply.json() if reply.status == 200 else None
        states[entity] = data if isinstance(data, dict) else None
    return states, replies


def _down_result(kind: str, ms: int) -> CallResult:
    return CallResult(0, {"result": "unknown", "reason": "ha_down_observed", "ms": ms}, ha=kind, down=kind)


class _Call:
    def __init__(self, client: ha_client.HaClient, domain: str, service: str, data: dict, budget: float,
                 clock: Callable[[], float]) -> None:
        self.client, self.domain, self.service, self.data = client, domain, service, data
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + budget
        found = ha_verify.targets(data)
        self.entities: tuple[str, ...] = found[0] if found else ()
        self.expect: ha_verify.Expectation | None = None
        self.before: dict[str, dict | None] = {}
        self.already = False
        self.sent = False       # the call_service frame was handed to the socket (or half of it)
        self.sent_at = self.started
        self.answer: tuple[int, str] | None = None     # (HTTP-like status, Home Assistant's code) once Home Assistant answered

    def left(self) -> float:
        return self.deadline - self.clock()

    def ms(self) -> int:
        return int((self.clock() - self.sent_at) * 1000)

    def run(self) -> CallResult:
        self.expect = ha_verify.expectation(self.domain, self.service, self.data)
        if self.entities and (self.expect is not None or self.service == "toggle"):
            self.before, replies = _read_states(self.client, self.entities, min(BEFORE_READ_S, max(0.1, self.left())))
            if replies and all(r.status == 0 and r.error in DOWN_KINDS for r in replies):
                return _down_result(replies[0].error or "other", int((self.clock() - self.started) * 1000))
            self.expect = ha_verify.expectation(self.domain, self.service, self.data, self.before)
            self.already = bool(self.expect and self.expect.check(self.before).result == "ok")
        if self.expect is None:
            return self._plain()
        return self._confirmed(self.expect)

    # -- a call nothing in the state can confirm

    def _plain(self) -> CallResult:
        reply = self.client.service(self.domain, self.service, self.data, timeout=max(0.2, self.left()))
        ms = self.ms()
        if reply.status == 0:
            if reply.error in DOWN_KINDS:
                return _down_result(reply.error or "other", ms)
            return CallResult(504, {"result": "unknown", "reason": "no_answer_unverifiable", "ms": ms}, ha=reply.error)
        body = _json_or_text(reply)
        if 400 <= reply.status < 500:
            return CallResult(reply.status, {"result": "skipped", "reason": "rejected"}, body, ha=str(reply.status))
        return CallResult(reply.status, {"result": "skipped", "reason": "not_verifiable", "ms": ms}, body, ha=str(reply.status))

    # -- a call the device state confirms

    def _confirmed(self, expect: ha_verify.Expectation) -> CallResult:
        try:
            session = ha_ws.Session.open(self.client, timeout=min(CONNECT_S, max(0.2, self.left())))
        except ha_ws.WsError as err:
            return self._no_session(err.kind, err.status)
        except ha_client.Unreachable as err:
            return self._no_session(err.kind, None)
        try:
            return self._watch(session, expect)
        finally:
            session.close()

    def _no_session(self, kind: str, status: int | None) -> CallResult:
        ms = int((self.clock() - self.started) * 1000)
        if kind in DOWN_KINDS:
            return _down_result(kind, ms)
        if status is not None and status >= 500:     # a gateway or Home Assistant still starting: it is not serving yet
            return _down_result("starting" if status == 503 else f"http_{status}", ms)
        return CallResult(504, {"result": "unknown", "reason": "ha_no_answer", "ms": ms}, ha=kind)

    def _watch(self, session: ha_ws.Session, expect: ha_verify.Expectation) -> CallResult:
        states = dict(self.before)
        try:
            if self.entities and not expect.whole_domain:
                sub = session.send("subscribe_trigger", trigger={"platform": "state", "entity_id": list(self.entities)})
                self._await_result(session, sub, states)
            self.sent = True      # from here the call may have reached Home Assistant, even when the send itself fails half way
            call = session.send("call_service", domain=self.domain, service=self.service, service_data=self.data)
            self.sent_at = self.clock()
            return self._loop(session, expect, call, states)
        except ha_ws.WsError as err:
            if err.kind == "timeout":
                if not self.sent:      # the subscription never completed: the device may have moved for any other reason
                    return CallResult(504, {"result": "unknown", "reason": "ha_no_answer", "ms": self.ms()}, ha=err.kind)
                return self._judge_end(expect, states, answered_ok=False)
            return self._lost_in_flight(expect, err.kind)

    def _lost_in_flight(self, expect: ha_verify.Expectation, kind: str) -> CallResult:
        """The connection broke: the call is never sent again. When it may have been sent, the device is asked once what it says now (the
        call may have run before the socket died); only fresh readings of EVERY target that prove the call are a verdict, a reading that
        failed is never replaced by what the state was before. Anything else stays unknown."""
        if self.sent and self.entities and not expect.whole_domain and not self.already:
            fresh, _ = _read_states(self.client, self.entities, min(FINAL_READ_S, max(0.1, self.left())))
            verdict = expect.check(fresh)
            if verdict.result == "ok":
                return self._ok(verdict, "state_matched")
        return CallResult(504, {"result": "unknown", "reason": "ha_down_in_flight", "ms": self.ms()}, ha=kind)

    def _await_result(self, session: ha_ws.Session, ident: int, states: dict[str, dict | None]) -> None:
        """The answer to the subscription; events that come first are kept. A missing answer is simply a Home Assistant that is too slow."""
        while self.left() > 0:
            frame = session.recv(min(0.25, self.left()))
            if frame is None:
                continue
            if frame.get("type") == "result" and frame.get("id") == ident:
                return
            self._take_event(frame, states)
        raise ha_ws.WsError("подписка не подтверждена", "timeout")

    @staticmethod
    def _take_event(frame: dict, states: dict[str, dict | None]) -> None:
        if frame.get("type") != "event":
            return
        trigger = ((frame.get("event") or {}).get("variables") or {}).get("trigger") or {}
        new = trigger.get("to_state")
        entity = trigger.get("entity_id")
        if isinstance(entity, str) and isinstance(new, dict):
            states[entity] = new

    def _loop(self, session: ha_ws.Session, expect: ha_verify.Expectation, call: int, states: dict[str, dict | None]) -> CallResult:
        while self.left() > 0:
            frame = session.recv(min(0.25, self.left()))
            if frame is not None:
                if frame.get("type") == "result" and frame.get("id") == call:
                    self.answer = self._answered(frame)
                    if 400 <= self.answer[0] < 500:
                        return CallResult(self.answer[0], {"result": "skipped", "reason": "rejected"}, ha=self.answer[1])
                else:
                    self._take_event(frame, states)
            done = self._decide(expect, states)
            if done is not None:
                return done
        return self._judge_end(expect, states, answered_ok=self._answered_ok())

    @staticmethod
    def _answered(frame: dict) -> tuple[int, str]:
        if frame.get("success"):
            return 200, "200"
        code = str((frame.get("error") or {}).get("code") or "error")
        return (400 if code in REJECTED_CODES else 500), code

    def _answered_ok(self) -> bool:
        return self.answer is not None and self.answer[0] == 200

    def _decide(self, expect: ha_verify.Expectation, states: dict[str, dict | None]) -> CallResult | None:
        """A verdict as soon as the events show the wanted state (and Home Assistant accepted the call, when the state was already
        there); None: keep listening. A state that is NOT the wanted one is never final before the budget ends: a cloud lamp lags
        behind Home Assistant's answer, a climate unit takes seconds."""
        if expect.whole_domain:
            if not self._answered_ok():
                return None
            result = self._whole_domain(expect)
            return result if result.verify["result"] == "ok" else None
        verdict = expect.check(states)
        if verdict.result != "ok":
            return None
        if self.already and not self._answered_ok():
            return None     # the device already was there: its state proves nothing, only Home Assistant's acceptance does
        return self._ok(verdict, "already_in_state" if self.already else "state_matched")

    def _whole_domain(self, expect: ha_verify.Expectation) -> CallResult:
        reply = self.client.template(ha_verify.whole_domain_template(expect.whole_domain or "", expect.whole_state or ""), timeout=READ_S)
        verdict = ha_verify.whole_domain_verdict(reply.text()) if reply.status == 200 else ha_verify.Verdict("pending", "state_unreadable", {})
        if verdict.result == "ok":
            return self._ok(verdict, "state_matched")
        if verdict.result == "failed":
            return self._failed(verdict)
        return CallResult(504, {"result": "unknown", "reason": "state_unreadable", "ms": self.ms()}, ha=self.answer[1] if self.answer else None)

    def _ok(self, verdict: ha_verify.Verdict, reason: str) -> CallResult:
        verify: dict[str, Any] = {"result": "ok", "reason": reason, "ms": self.ms(), "after": verdict.after}
        if verdict.unchecked:
            verify["unchecked"] = list(verdict.unchecked)
        return CallResult(200, verify, ha=self.answer[1] if self.answer else None)

    def _failed(self, verdict: ha_verify.Verdict) -> CallResult:
        return CallResult(502, {"result": "failed", "reason": verdict.reason, "ms": self.ms(), "after": verdict.after, "wrong": verdict.wrong},
                          ha=self.answer[1] if self.answer else None)

    def _judge_end(self, expect: ha_verify.Expectation, states: dict[str, dict | None], answered_ok: bool) -> CallResult:
        """The budget is over without a confirmation."""
        ms = self.ms()
        ha = self.answer[1] if self.answer else None
        if expect.whole_domain:
            if answered_ok:
                return self._whole_domain(expect)
            return CallResult(504, {"result": "unknown", "reason": "ha_no_answer", "ms": ms}, ha=ha)
        verdict = expect.check(states)
        if verdict.result != "ok" and any(isinstance(v, dict) for v in states.values()):
            # One last look at what Home Assistant says now, in case an event was missed: it must fit under the callers' timeout.
            fresh, replies = _read_states(self.client, self.entities, FINAL_READ_S)
            states.update({k: v for k, v in fresh.items() if v is not None})
            verdict = expect.check(states)
            ms = self.ms()
        if verdict.result == "ok":
            if self.already and not answered_ok:
                return CallResult(504, {"result": "unknown", "reason": "already_in_state_unanswered", "ms": ms, "after": verdict.after}, ha=ha)
            return self._ok(verdict, "already_in_state" if self.already else "state_matched")
        if verdict.result == "failed":
            climate_only = bool(verdict.wrong) and all(entity.startswith("climate.") for entity in verdict.wrong) \
                and verdict.reason != "device_unavailable"
            if climate_only and not answered_ok:
                # A climate unit takes seconds: the old mode shows while Home Assistant's call is still running (2026-11-15, 2026-11-20).
                return CallResult(504, {"result": "unknown", "reason": "not_yet_confirmed", "ms": ms, "after": verdict.after}, ha=ha)
            return self._failed(verdict)
        silent = not any(isinstance(v, dict) for v in states.values())
        return CallResult(504, {"result": "unknown", "reason": "ha_no_answer" if silent else "state_unreadable", "ms": ms}, ha=ha)


def _json_or_text(reply: ha_client.Reply) -> Any:
    try:
        return reply.json()
    except ValueError:
        return reply.text()


def service_call(client: ha_client.HaClient, domain: str, service: str, data: dict | None = None,
                 budget: float = CALL_BUDGET_S, clock: Callable[[], float] = time.monotonic) -> CallResult:
    """One service call, confirmed by the device state through Home Assistant's own events. Blocking; at most `budget` seconds."""
    return _Call(client, domain, service, data or {}, budget, clock).run()
