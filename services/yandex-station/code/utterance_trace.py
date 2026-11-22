"""One compact record per call the adapter answers: how an utterance was processed (the clip feed and the dashboard show it).

The adapter is asked twice for a phrase at most: by the quasar overlay's hook (`/intent/decide`, before the container's own
handlers; `via: "hook"`) and by the turn's text request (the processor socket; `via: "turn"`). Each call opens a Trace,
the code that decides or executes adds facts to it through the module functions (a contextvar, so executors need no extra
parameter and the functions are a no-op outside a call), and the adapter stores the finished record in dialogs.sqlite3
(`dialogstore.add_trace`). The session page and the Telegram feed read it back by time: the adapter does not know the
quasar dialog id.

Record (JSON, all keys optional except v/ts/via/text):
  v, ts (epoch start), ms (total), via turn|hook|hook-shadow, station, room, text (as routed), layer, outcome
  layer    what answered: grammar | skill | calc | jev | brain | none (the old path continues) | error
  intent, tier, conf, fuzzy, priority, pattern, slots, executor, args, labels   the grammar's decision (also when declined)
  declined why the decision was not run by the adapter («owner: quasar», «not configured», ...)
  asked    clarify | reprompt | brain: the reply is a question that waits for an answer; joined: the clarification text this phrase was joined to
  reply, listen, suppressed (reprompt_cooldown, ...)
  reply_why  the reason code of the reply of a Home Assistant command (ha_verified | ha_not_confirmed | ha_unverified | ha_down_observed | ...,
           ha_action_log.WHY_RU); the sentence names only what ha-cli observed on the device
  calls    [{"kind": "ha"|"weather-cli"|"music"|"timer", ...fields, "ok": bool, "ms": int}]; an "ha" call also carries
           verify {"result": ok|failed|unknown|skipped, "reason", "ms", "after"} (ha-cli's verdict on the device state after the call),
           aid (the ha-cli journal id of the action), ha (Home Assistant's own outcome: HTTP status or error kind), down (outage evidence)
  jev      {"mode", "decision", "reason", "confidence", "ms", "acted"}
  brain    {"provider", "slot", "model", "attempts": [{"result", "seconds"}], "ms"}
  stages_ms {"grammar": .., "skills": .., "jev": .., "brain": .., "total": ..}
  errors   [short strings]
  correction  the repair dialog (correction.py, #182), written when the engine had evidence or a dialog was open:
              {"trigger": the core policy's trigger («garbled», «contradiction», «confusable-disagreement», «low-margin») or null,
               "reason": why nothing was asked («agree», «vetoed», «below-floor», …) or null, "decision": none|ask|ask-choice|relisten,
               "heard": the primary reading, "nbest": [{"text", "percent", "source", "engine", "fit"}] (percent is the share among the listed
               readings, never spoken), "nbest_ms": the second opinion's engine time or null, "cue": [{"kind": "noop", "supports": text}],
               "shadow": true (only in shadow mode: decided and traced, never spoken),
               "dialog": {"state": awaiting_confirm|awaiting_choice|awaiting_repeat, "ask": the spoken question, "answer": the reply text or null,
                          "outcome": accepted|denied|kept|chose:<text>|cancelled|timeout|fallthrough|gave_up|repeat or null while open, "retries": int}}
              The turn that asks carries the evidence and `dialog` with a null answer and outcome; every later turn that reads or closes the
              dialog carries the same evidence with its own `answer` and `outcome`.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import time
from typing import Any, Iterator

VERSION = 1
TEXT_CHARS = 400
REPLY_CHARS = 300
MAX_CALLS = 20
MAX_ERRORS = 8

_current: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar("utterance_trace", default=None)


class Trace:
    def __init__(self, via: str, text: str, station: str | None = None, clock=time.time, perf=time.perf_counter):
        self.perf = perf
        self.started = perf()
        self.data: dict[str, Any] = {"v": VERSION, "ts": clock(), "via": via, "text": text[:TEXT_CHARS]}
        if station:
            self.data["station"] = station
        self.token: contextvars.Token | None = None
        self.closed = False

    def note(self, **fields: Any) -> None:
        for key, value in fields.items():
            if value is not None:
                self.data[key] = value

    def call(self, kind: str, **fields: Any) -> None:
        calls = self.data.setdefault("calls", [])
        if len(calls) < MAX_CALLS:
            calls.append({"kind": kind, **{k: v for k, v in fields.items() if v is not None}})

    def error(self, message: str) -> None:
        errors = self.data.setdefault("errors", [])
        if len(errors) < MAX_ERRORS and message not in errors:
            errors.append(message[:160])

    def stage_ms(self, name: str, ms: int) -> None:
        stages = self.data.setdefault("stages_ms", {})
        stages[name] = stages.get(name, 0) + ms

    def finish(self) -> dict[str, Any]:
        total = int((self.perf() - self.started) * 1000)
        self.data["ms"] = total
        self.data.setdefault("stages_ms", {})["total"] = total
        reply = self.data.get("reply")
        if isinstance(reply, str) and len(reply) > REPLY_CHARS:
            self.data["reply"] = reply[:REPLY_CHARS - 1] + "…"
        return self.data


def start(via: str, text: str, station: str | None = None) -> Trace:
    """Open a trace for the call this task is answering; `end` closes it. Each call runs in its own task, so the contextvar never leaks."""
    trace = Trace(via, text, station)
    trace.token = _current.set(trace)
    return trace


def end(trace: Trace) -> dict[str, Any]:
    """The finished record; the contextvar is released."""
    record = trace.finish()
    trace.closed = True
    if trace.token is not None:
        try:
            _current.reset(trace.token)
        except ValueError:   # closed from another context than it was opened in
            _current.set(None)
        trace.token = None
    return record


def current() -> Trace | None:
    return _current.get()


def note(**fields: Any) -> None:
    trace = _current.get()
    if trace is not None:
        trace.note(**fields)


def call(kind: str, **fields: Any) -> None:
    trace = _current.get()
    if trace is not None:
        trace.call(kind, **fields)


def error(message: str) -> None:
    trace = _current.get()
    if trace is not None:
        trace.error(message)


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    """Time a block into stages_ms[name] (also when it raises)."""
    trace = _current.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if trace is not None:
            trace.stage_ms(name, int((time.perf_counter() - started) * 1000))


def decision_fields(decision: Any, priority: int | None = None) -> dict[str, Any]:
    """The grammar Decision as trace fields (also for a decision the adapter then declines to run)."""
    action = decision.action or {}
    fields: dict[str, Any] = {"intent": decision.intent, "tier": decision.tier, "conf": round(decision.confidence, 2),
                              "fuzzy": decision.fuzzy, "pattern": decision.pattern, "slots": plain(decision.slots),
                              "executor": action.get("executor"), "args": plain(action.get("args") or {}) or None,
                              "labels": list(decision.labels) or None, "priority": priority}
    if decision.parts:
        fields["parts"] = [part.intent for part in decision.parts]
    return {k: v for k, v in fields.items() if v is not None}


def plain(value: Any) -> Any:
    """JSON-safe copy (slots and args hold only plain data, but a bug elsewhere must not break the trace)."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)[:200]


def dumps(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))
