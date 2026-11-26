"""The adapter's route before the brain: grammar, arithmetic, weather, then the grammar's catch-alls.

`IntentRouter.classify(text)` is pure (no I/O) and is what the corpus back-test measures
(`python3 -m intent_grammar backtest --route adapter`), so the numbers describe the code that answers a turn. `route()`
executes the decision: canned lines, reprompts, the clock, timers, Home Assistant climate and colour, weather, music.

Decisions for executors the quasar container owns (volume, media keys, light and device switches) are not executed
here; the quasar overlay asks `/intent/decide` first and runs `volume` itself, everything else stays with its own
handlers. A turn that reaches the adapter with such a decision still goes to the brain, as before.

Fail-open: a grammar that cannot be loaded, an executor that raises, or a mode of `off` leave the old path
(calc -> skills -> brain) untouched.

The repair dialog (#182, correction.py): before the normal classification `route` first reads the utterance as the answer to a
question the speaker asked («Мне послышалось «включи свет»…», grammar contexts `repair.*`), then consults the correction engine for a
new question (a stored second opinion or the quasar layer's no-op cue). `AISIS_CORRECTION=off|shadow|on` (default off). An accepted
alternative is run here when this adapter owns its executor, handed to the overlay as route `rerun` when it announced support
(`rerun: true` in the request) and the action is not risky, and otherwise asked for again («Скажите ещё раз: …»).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from calc import evaluate as calc_evaluate
from intent_grammar import Decision, Grammar
from intent_grammar.grammar import load, render_reply
from intent_grammar.timewords import plural
from timers import duration_accusative, left_text
import voice_skills
import capabilities
import clarify
import correction
import utterance_trace

LOG = logging.getLogger("aisis.intent")

BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
REPROMPT_COOLDOWN_S = 60.0     # the «what was heard, what can be asked for» line asks the microphone open once a minute per station: noise cannot loop the speaker
TIMERS_OFF = "Таймеры и будильники я пока не умею."
UNSUPPORTED_TEXT = "Этого я пока не умею."              # the plain refusal of a command nobody implements (Jev)
TIMER_CANCEL_REMINDER_HINT = "Таймеров нет, есть напоминание. Чтобы отменить его, скажи: отмени напоминание."

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

# Executors the adapter runs itself; everything else belongs to the quasar container (or is only a skill label).
ADAPTER_EXECUTORS = frozenset(("clock.say", "clock.date", "dialog.repeat_last", "timer.start", "timer.cancel",
                               "timer.remaining", "alarm.set", "alarm.cancel", "reminder.cancel", "ha.climate", "ha.climate_delta",
                               "ha.light_color", "ha.light_brightness", "ha.climate_status", "skill.math", "skill.weather", "skill.capabilities"))
TIMER_PREFIXES = ("timer.", "alarm.", "reminder.")   # the executors that need the TimerBook
VOLUME_EXECUTORS = frozenset(("speaker.volume", "speaker.volume_delta"))
FALLBACK_BAND = -1             # intents with priority below zero are the catch-alls: they read what the skills left
CLARIFY_WINDOW_S = 30.0        # how long a clarification question waits for its answer
BRAIN_QUESTION_WINDOW_S = 300.0   # a question of the brain's is the brain's to continue for this long; hook-answered or quasar-answered lines never reach note_reply, so no line can be relied on to clear it
STATION_BOUND_PREFIXES = ("ha.", "dialog.repeat_last")   # clauses whose answer depends on the speaking station: a compound with one is never replayed to another utterance's delivery
DEDUPE_S = 5.0                 # the same utterance that started or cancelled a timer/alarm, delivered again inside this window, is not run twice
TIMER_ACTION_EXECUTORS = frozenset(("timer.start", "alarm.set", "timer.cancel", "alarm.cancel", "reminder.cancel"))   # what a second delivery could repeat


@dataclass
class Route:
    """What to do with a turn. kind: reply | silent | volume | dialog_end | rerun | none (the old path continues).
    `rerun`: `text` is a phrase the overlay matches as if it had been spoken (the corrected reading of a repair dialog; only for a caller that
    announced `rerun: true`). `context`: the station's repair dialog is open after this answer (the overlay's «повтори» defers to the hook)."""
    kind: str
    text: str | None = None
    listen: bool = False
    volume: dict[str, float] | None = None
    intent: str | None = None
    source: str = "none"
    ms: int = 0
    context: bool = False

    def as_json(self) -> dict[str, Any]:
        data = {"route": self.kind, "text": self.text, "listen": self.listen, "volume": self.volume,
                "intent": self.intent, "ms": self.ms}
        if self.context:
            data["context"] = True
        return data


NONE = Route("none")


def clock_text(now: datetime) -> str:
    """«Сейчас 17 часов 45 минут.» / «Сейчас ровно 9 часов.»"""
    hours = f"{now.hour} {plural(now.hour, 'час', 'часа', 'часов')}"
    if now.minute == 0:
        return f"Сейчас ровно {hours}."
    return f"Сейчас {hours} {now.minute} {plural(now.minute, 'минута', 'минуты', 'минут')}."


def date_text(now: datetime) -> str:
    return f"Сегодня {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}."


def volume_from(decision: Decision) -> dict[str, float] | None:
    """The overlay's volume contract (`target` 0..1 of full scale, signed `steps`, signed `deltaLevels` on the 0..10 scale)
    from the grammar's volume args. `speaker.volume`: exactly one of `step` (+-1), `level` (0..10) or `percent` (0..100).
    `speaker.volume_delta`: `direction` (+-1) and exactly one of `levels` (1..10) or `percent` (1..100). The unit is never
    guessed from the size of a number; an action that breaks the contract does nothing."""
    args = (decision.action or {}).get("args") or {}
    if decision.action and decision.action["executor"] == "speaker.volume":
        given = {key: args[key] for key in ("step", "level", "percent") if args.get(key) is not None}
        if len(given) != 1:
            return None
        (key, value), = given.items()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if key == "step":
            return {"steps": float(value)}
        scale = 10 if key == "level" else 100
        return {"target": value / scale} if 0 <= value <= scale else None
    given = {key: args[key] for key in ("levels", "percent") if args.get(key) is not None}
    if len(given) != 1 or args.get("direction") not in (-1, 1):
        return None
    (key, amount), = given.items()
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if not 1 <= amount <= (10 if key == "levels" else 100):
        return None
    levels = amount / 10 if key == "percent" else amount
    return {"deltaLevels": args["direction"] * float(levels)}


@dataclass
class IntentRouter:
    grammar: Grammar
    skills: voice_skills.Skills | None = None
    timers: Any | None = None           # timers.TimerBook, or None when voice timers are not configured
    ha: Any | None = None               # ha_actions.HaActions, or None
    catchall: bool = True               # the grammar's catch-all reprompts and refusals (priority < 0)
    now: Callable[[], datetime] = lambda: datetime.now().astimezone()
    pick: Callable[[list[str]], str] = random.choice
    monotonic: Callable[[], float] = time.monotonic
    last_reply: dict[str, str] = field(default_factory=dict)
    last_reprompt: dict[str, float] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    pending: dict[str, tuple[str, float]] = field(default_factory=dict)             # station -> (clarification phrase, when)
    brain_asked: dict[str, float] = field(default_factory=dict)                     # station -> when the brain last spoke to it with a question
    memo: tuple[str, "Route", float] | None = None        # the one remembered utterance: (canonical text, its route, when it ran)
    running: dict[str, asyncio.Task["Route"]] = field(default_factory=dict)   # timer/alarm utterances in flight, by canonical text
    hypotheses: Any | None = None       # hypothesis_store.HypothesisStore (`get(station, text, now)` -> the gateway's second opinion payload), or None
    correction_mode: str = "off"        # AISIS_CORRECTION: off | shadow (decide and trace, never speak) | on
    correction_core: Any | None = None  # the stt_cli.hypotheses module (tests pass a fake); None imports the real one
    extra_dict_mode: str = "off"        # AISIS_EXTRA_DICT: off | shadow (log the extra confusables' candidates) | on (the engine decides with them)
    status_bare: str = "shadow"         # AISIS_STATUS_BARE: shadow (traced, declined; anything but `on`) | on: «кондиционер на втором этаже» alone is answered with the status
    clarify_mode: str = "on"            # AISIS_CLARIFY: on (a phrase nobody read gets the likeliest actions as a question) | off (only the line that says what was heard)
    home: clarify.Home | None = None    # the catalog and the one service call of a clarification option (clarify.HaHome over the adapter's ha-cli access)
    capabilities: capabilities.Capabilities | None = field(default=None, init=False)
    correction: correction.Correction | None = field(default=None, init=False)
    clarifier: clarify.Clarifier | None = field(default=None, init=False)
    _engine_failed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.capabilities = capabilities.Capabilities(ha=self.ha, timers=self.timers, skills=self.skills, now=self.now,
                                                      pick=self.pick, monotonic=self.monotonic)
        self.clarifier = clarify.Clarifier(self.grammar, lambda text: self.classify(text, fallback=False), self._runs_here, self.home,
                                           now=lambda: self.now(), monotonic=lambda: self.monotonic(), abilities=self._abilities)
        if self.correction_mode in ("shadow", "on"):
            try:
                self.correction = correction.Correction(self.classify, self._decide_in, self.hypotheses, mode=self.correction_mode,
                                                        core=self.correction_core, extra_dict_mode=self.extra_dict_mode,
                                                        describe=self.clarifier.describe)
            except Exception as err:   # no core installed, bad data: the speaker runs exactly as before
                LOG.warning("correction_unavailable class=%s", type(err).__name__)

    def _guessable(self, decision: Decision | None, phase: str | None, via_quasar: bool, station: str) -> bool:
        """A turn nothing answered that may still be a command misheard: no reading at all, or only a guess that needs a «да». Not an answer to the
        brain's own question, not the quasar hook's call (it has 250 ms and no catalog), and not a phrase the grammar read and declined."""
        return (self.clarify_mode == "on" and not via_quasar and phase is None and not self.asked_a_question(station)
                and (decision is None or decision.fuzzy or decision.tier == "confirm"))

    def _decide_in(self, text: str, context: Any) -> Decision | None:
        return self.grammar.decide(text, context=context)

    # ── pure classification (the back-test measures this) ───────────────────
    def classify(self, text: str, phase: str | None = None, *, fallback: bool = True) -> Decision | None:
        """phase="pre": only what may answer before the quasar container's own handlers; None: everything.
        `fallback=False` keeps the catch-alls (priority < 0) out, whatever the router's `catchall` switch says."""
        if phase == "pre":
            return self.grammar.decide(text, phase="pre")
        decision = self.grammar.decide(text, min_priority=0)
        if decision is not None:
            return decision
        plain = self.grammar.normalise(text).core_text()
        answer = calc_evaluate(plain)
        if answer is not None:
            return Decision(intent="skill.math", tier="query", slots={"answer": answer},
                            action={"executor": "skill.math", "args": {}}, reply=None, labels=("MATH", "math"),
                            confidence=1.0, fuzzy=False, pattern=-1, text=text)
        mode = voice_skills.parse_weather_ask(text)
        if mode is not None:
            return Decision(intent="skill.weather", tier="query", slots={"mode": mode},
                            action={"executor": "skill.weather", "args": {"mode": mode}}, reply=None,
                            labels=("WEATHER", "weather_time"), confidence=1.0, fuzzy=False, pattern=-1, text=text)
        return self.grammar.decide(text, max_priority=FALLBACK_BAND) if self.catchall and fallback else None

    def executable(self, decision: Decision, *, via_quasar: bool, station: str | None = None) -> bool:
        """Whether `route()` acts on the decision (speaks, runs an executor, hands a volume change to the overlay) instead of
        declining it with `none`. The one check the runtime and the back-test share, so a decision the runtime would decline
        (a quasar-owned executor, a guess that needs the owner's «да», an executor that is not configured) is not counted
        as handled. A reprompt inside its cooldown and an executor that fails at run time are not known here."""
        if decision.parts:
            return all(self._clause_executable(part, station) for part in decision.parts)
        if decision.tier in ("drop", "canned", "reprompt", "clarify"):
            return True
        if decision.tier == "confirm" or decision.fuzzy:
            return False
        return self._executes(decision, via_quasar, station)

    def _clause_executable(self, part: Decision, station: str | None, timers_bound: bool = False) -> bool:
        """One clause of a compound: the adapter must be able to answer it itself, the quasar layer takes the whole utterance otherwise.
        `timers_bound`: judge it as if timers were configured (to tell a compound that only lacks timers from one that is the quasar's)."""
        if part.tier not in ("execute", "query", "canned") or part.fuzzy:
            return False
        if part.tier == "canned":
            return True
        return (part.action or {}).get("executor") in ADAPTER_EXECUTORS and self._executes(part, False, station, timers_bound)

    def _executes(self, decision: Decision, via_quasar: bool, station: str | None, timers_bound: bool = False) -> bool:
        action = decision.action or {}
        executor = action.get("executor")
        if executor in VOLUME_EXECUTORS:
            return via_quasar and volume_from(decision) is not None
        if executor == "dialog.end":
            return via_quasar
        if executor == "music.play":
            return not (action.get("args") or {}).get("query") and self.skills is not None
        if executor not in ADAPTER_EXECUTORS:
            return False
        if executor == "ha.climate_status" and (action.get("args") or {}).get("bare") and self.status_bare != "on":
            return False   # «кондиционер на втором этаже» alone: shadow first (AISIS_STATUS_BARE), traced and declined
        if executor.startswith(TIMER_PREFIXES):
            return timers_bound or self.timers is not None
        if executor.startswith("ha."):
            return self.ha is not None
        if executor == "skill.weather":
            return self.skills is not None
        if executor == "dialog.repeat_last":
            return station is None or station in self.last_reply
        return True

    # ── execution ───────────────────────────────────────────────────────────
    def shadow(self, text: str, phase: str | None = None) -> Route:
        """What the route would do, without doing it: no timer, Home Assistant call, reprompt cooldown or pending question.
        Always kind `none`; `intent` carries the verdict for the overlay's log line."""
        decision = self.classify(text, phase)
        self.stats["shadow"] = self.stats.get("shadow", 0) + 1
        return Route("none", intent=decision.intent if decision else None, source="shadow")

    def _joined_answer(self, station: str, text: str, phase: str | None) -> tuple[Decision, str] | None:
        """The answer to a clarification question («поставь таймер» → «На сколько?» → «на пять минут»), joined to the question."""
        asked = self.pending.pop(station, None)
        if asked is None or self.monotonic() - asked[1] > CLARIFY_WINDOW_S:
            return None
        joined = f"{asked[0]} {text}"
        decision = self.classify(joined, phase)
        if decision is None or decision.tier not in ("execute", "query"):
            return None
        return decision, joined

    def _timer_action(self, decision: Decision, via_quasar: bool, station: str) -> bool:
        """The decision runs a timer or alarm start, set or cancel (alone or as a clause of a compound) and the adapter can run it."""
        return any((part.action or {}).get("executor") in TIMER_ACTION_EXECUTORS for part in decision.parts or (decision,)) \
            and self.executable(decision, via_quasar=via_quasar, station=station)

    async def _timer_action_once(self, station: str, text: str, decision: Decision, via_quasar: bool) -> Route:
        """Timer and alarm actions run in arrival order, and one utterance delivered twice inside DEDUPE_S runs once: the quasar hook
        and the speaker's own turn can carry the same phrase (a hook timeout), and a second «таймер на пять минут» must not start a
        second timer nor a second «отмени таймер» answer «Таймеров нет» to a cancel that worked.

        One slot remembers the last utterance that ran: its route and when. It is replayed only for the SAME utterance (a compound
        as a unit), and only after every action in flight has finished, so a cancel is never answered from the slot ahead of a start
        that is registered but has not run. Any other utterance that runs an action empties the slot first (start → cancel → start →
        cancel never replays a stale reply); a failure, a decline or a queued result is never stored, and neither is a compound with a Home Assistant or
        «повтори» clause (the answer depends on the speaking station, and Home Assistant may fail or queue behind a reply that still
        says «готово»; a repeat runs it again, timer clause included). A duplicate that arrives
        while the first delivery is running waits for it."""
        key = self.grammar.normalise(text).text
        if self._station_bound(decision):   # another station's identical phrase is its own action, in flight or finished
            key = f"{key}\0{station}"
        while True:
            task = self.running.get(key)
            if task is not None and not task.done():
                return await asyncio.shield(task)
            earlier = [other for other in self.running.values() if not other.done()]
            if earlier:   # arrival order: what was registered before this utterance finishes first (no cycle: only earlier ones are waited for)
                await asyncio.wait(earlier)   # not gather: a cancelled waiter must not cancel another utterance's action
                continue
            memo = self.memo
            if memo is not None and memo[0] == key and self.monotonic() - memo[2] <= DEDUPE_S:
                return memo[1]
            task = asyncio.ensure_future(self._run_timer_action(key, station, decision, via_quasar))
            self.running[key] = task
            task.add_done_callback(lambda finished, k=key: self._finished(k, finished))
            return await asyncio.shield(task)

    @staticmethod
    def _station_bound(decision: Decision) -> bool:
        return any(((part.action or {}).get("executor") or "").startswith(STATION_BOUND_PREFIXES) for part in decision.parts or (decision,))

    async def _run_timer_action(self, key: str, station: str, decision: Decision, via_quasar: bool) -> Route:
        self.memo = None   # whatever ran before is no longer the last thing that happened
        route = await self._run(station, decision, via_quasar)
        if route.kind == "reply" and not self._station_bound(decision):
            self.memo = (key, route, self.monotonic())
        return route

    def _finished(self, key: str, task: asyncio.Task[Route]) -> None:
        if self.running.get(key) is task:   # a newer task under the same key is not ours to drop
            del self.running[key]
        if not task.cancelled():
            task.exception()   # the awaiters report it; a task nobody awaits any more must not log «never retrieved»

    def trace_fields(self, decision: Decision) -> dict[str, Any]:
        """The decision as utterance-trace fields, with the intent's priority band from the grammar."""
        priority = next((intent.priority for intent in self.grammar.intents if intent.id == decision.intent), None)
        return utterance_trace.decision_fields(decision, priority)

    def _why_declined(self, decision: Decision, via_quasar: bool, station: str) -> str:
        """Why a decision the grammar made is not run by the adapter (the trace shows it; the old path answers instead)."""
        if decision.tier == "reprompt":
            return "reprompt cooldown: one «не расслышал» a minute per station"
        if decision.parts:
            return "compound: a clause is not run by the adapter"
        if decision.tier == "confirm" or decision.fuzzy:
            return "fuzzy guess: needs the owner's «да»"
        executor = (decision.action or {}).get("executor") or ""
        if executor in VOLUME_EXECUTORS:
            return "volume change is run by the quasar layer" if not via_quasar else "volume arguments break the contract"
        if executor == "dialog.end" or executor not in ADAPTER_EXECUTORS:
            return f"executor {executor or '—'} belongs to the quasar layer"
        if executor.startswith(TIMER_PREFIXES) and self.timers is None:
            return "timers are not configured"
        if executor.startswith("ha.") and self.ha is None:
            return "Home Assistant actions are off"
        if executor == "ha.climate_status" and (decision.action or {}).get("args", {}).get("bare") and self.status_bare != "on":
            return "bare «кондиционер …» is in shadow (AISIS_STATUS_BARE): the quasar layer's question stands"
        if executor == "dialog.repeat_last":
            return "nothing to repeat"
        return f"executor {executor} gave no answer"

    async def route(self, station: str, text: str, *, phase: str | None = None, via_quasar: bool = False, rerun: bool = False,
                    noop: dict[str, Any] | None = None, device: str | None = None) -> Route:
        """Decide and execute one turn; Route("none") hands it on. `via_quasar`: the caller (quasar) runs volume itself.
        `rerun`: the caller supports the route `rerun`. `noop`: the quasar layer's cue «the device is already in the state the text asks
        for» ({desired, label, targets}). `device`: the speaker's own id when the caller knows it (the repair dialog is kept per device)."""
        started = time.perf_counter_ns()
        decision = None
        engine, key = self.correction, device or (None if via_quasar else station)   # a hook that names no device owns no dialog
        try:
            route = await self._dialog_turn(key, text, via_quasar, rerun, noop)
            if route is not None:
                self.pending.pop(station, None)   # the dialog took the turn: a clarification asked before is superseded, a bare answer joins nothing
            if route is None:
                decision = self.classify(text, phase, fallback=not self.asked_a_question(station))
                decision, text = self._with_joined_answer(station, text, phase, decision)
                route = await self._run_decision(station, text, decision, via_quasar)
                if decision is not None and decision.tier == "clarify":
                    self.pending[station] = (text, self.monotonic())
                if route.kind == "none" and self._guessable(decision, phase, via_quasar, station):
                    route = await self._clarify(station, key, text, floor=clarify.ASK_AT, always=False) or route
                elif route.kind != "none" and decision is not None and decision.tier in ("execute", "query"):
                    self.clarifier.remember(station, text)
        except Exception as err:  # fail open: the old path answers
            LOG.warning("intent_route_failed class=%s", type(err).__name__)
            utterance_trace.error("route: " + type(err).__name__)
            if engine is not None:
                engine.close(key)   # a dialog step that crashed leaves nothing open
            route = NONE
        route = self._mark_open_dialog(key, route)
        route = dataclasses.replace(route, ms=(time.perf_counter_ns() - started) // 1_000_000)   # a copy: NONE is shared
        source = route.source if route.kind != "none" else "none"
        self.stats[source] = self.stats.get(source, 0) + 1
        self._trace(decision, route, via_quasar, station)
        return route

    async def _dialog_turn(self, key: str | None, text: str, via_quasar: bool, rerun: bool, noop: dict[str, Any] | None) -> Route | None:
        """The repair dialog's route for the turn, or None (no dialog, no device to keep one for, or it did not take the turn)."""
        engine = self.correction
        if engine is None or key is None:
            return None
        try:
            return await self._correct(key, text, via_quasar, rerun, noop)
        except Exception as err:   # the dialog is an extra: whatever it breaks on, the normal route below still answers
            LOG.warning("correction_failed class=%s", type(err).__name__)
            utterance_trace.error("correction: " + type(err).__name__)
            engine.close(key)
            return None

    def _with_joined_answer(self, station: str, text: str, phase: str | None, decision: Decision | None) -> tuple[Decision | None, str]:
        """The decision and text of the turn, replaced by the clarification question joined to this answer when it completes one."""
        joined = self._joined_answer(station, text, phase)
        if joined is None:
            return decision, text
        utterance_trace.note(joined=joined[1])
        return joined

    async def _run_decision(self, station: str, text: str, decision: Decision | None, via_quasar: bool) -> Route:
        if decision is None:
            return NONE
        if self._timer_action(decision, via_quasar, station):
            return await self._timer_action_once(station, text, decision, via_quasar)
        return await self._run(station, decision, via_quasar)

    def _mark_open_dialog(self, key: str | None, route: Route) -> Route:
        """A turn answered here is no longer the brain's question to ask; `context`: the repair dialog is still open after it."""
        engine = self.correction
        if engine is None or key is None:
            return route
        if route.kind != "none":
            engine.drop_hint(key)   # answered: the question is not the brain's business
        if engine.active(key, self.monotonic()) is not None:
            return dataclasses.replace(route, context=True)
        return route

    # ── the repair dialog (correction.py) ───────────────────────────────────
    def take_question(self, station: str) -> str | None:
        """The question the speaker asked that the last reply did not answer, once, for the brain's turn text; None without one."""
        if self.correction is None:
            return None
        return self.correction.take_question(station, self.monotonic())

    async def _correct(self, key: str, text: str, via_quasar: bool, rerun: bool, noop: dict[str, Any] | None) -> Route | None:
        """The turn as the repair dialog sees it: an answer to the open question, then a new question. None: not the dialog's, the normal
        route reads it. Emergency text is never the dialog's: it is no reply (the grammar reads it above every context) and the engine vetoes it."""
        engine = self.correction
        now = self.monotonic()
        dialog = engine.active(key, now)
        expired = engine.take_expired(key)
        if expired is not None:
            engine.note_expired(expired)
        if dialog is not None:
            step = engine.reply(key, text, now=now, via_quasar=via_quasar, probe=noop is not None)
            if step is not None:
                return await self._dialog_route(key, step, via_quasar, rerun)
        offer = engine.consult(key, text, now=now, via_quasar=via_quasar, noop=noop) if self.correction_mode in ("shadow", "on") else None
        if offer is None or offer.shadow:
            return None
        return Route("reply", offer.say, listen=offer.listen, intent="repair.give_up" if offer.outcome else "repair.ask", source="correction")

    async def _dialog_route(self, station: str, step: correction.Step, via_quasar: bool, rerun: bool) -> Route:
        if step.end:   # «отмена»: nothing happens; the overlay closes its dialog, a turn answers with nothing
            return Route("dialog_end" if via_quasar else "silent", intent="repair.cancel", source="correction")
        if step.run is not None:
            return await self._run_reading(station, step, via_quasar, rerun)
        return Route("reply", step.say, listen=step.listen, intent="repair.say", source="correction")

    async def _run_reading(self, station: str, step: correction.Step, via_quasar: bool, rerun: bool) -> Route:
        """Run the phrase a spoken «да» (or the reading said back) chose. The normal route decides whether it runs here: only a decision this
        adapter executes (never a guess that needs a «да», never a risky action the overlay would run) is. A quasar-owned action goes back to
        the overlay as `rerun` when it asked for it and the action is not risky; every other case asks the owner to say it again.
        `station` is the speaker's own key (the device id of the hook, else the turn's station): a roomless command is resolved against
        the room of that speaker, never against the hook's generic `quasar` label. A clarification option that is a Home Assistant call is
        made as that one standard service call and answered from the state that was observed."""
        if step.call is not None:
            try:
                done = await self.clarifier.home.act(step.call)
            except Exception as err:   # ha-cli unreachable: said, never silently dropped
                LOG.warning("clarify_act_failed class=%s", type(err).__name__)
                utterance_trace.error("clarify act: " + type(err).__name__)
                done = clarify.ACT_FAILED
            return Route("reply", done, intent="clarify.act", source="correction")
        phrase = step.run
        decision = self.classify(phrase, fallback=False)
        if decision is not None and decision.tier in ("execute", "query") and not decision.fuzzy \
                and self.executable(decision, via_quasar=via_quasar, station=station):
            route = await self._run_decision(station, phrase, decision, via_quasar)
            if route.kind == "reply":
                lead = correction.DONE_TEXT.format(phrase=correction.shown(phrase))
                return dataclasses.replace(route, text=f"{lead} {route.text}".strip() if route.text else lead, source="correction")
            if route.kind != "none":
                return dataclasses.replace(route, source="correction")
        if via_quasar and rerun and not step.risky:
            return Route("rerun", phrase, intent="repair.rerun", source="correction")
        return Route("reply", correction.SAY_AGAIN_TEXT.format(phrase=correction.shown(phrase)), listen=True, intent="repair.say_again",
                     source="correction")

    # ── proactive clarification (clarify.py, #307) ──────────────────────────
    def _runs_here(self, decision: Decision, station: str) -> bool:
        """The speaker's own turn can run the decision itself (a clarification option must be one a «да» really does)."""
        return self.executable(decision, via_quasar=False, station=station)

    def _abilities(self) -> tuple[str, ...]:
        """Kinds of action the adapter has besides the home's own: what is configured, in the owner's words."""
        return (("музыка",) if self.skills is not None else ()) + (("таймер",) if self.timers is not None else ())

    def _engine(self) -> correction.Correction | None:
        """The dialog engine; a clarification needs one even when AISIS_CORRECTION is off (then it never consults a second opinion)."""
        if self.correction is None and not self._engine_failed:
            try:
                self.correction = correction.Correction(self.classify, self._decide_in, self.hypotheses, mode="shadow", core=self.correction_core,
                                                        describe=self.clarifier.describe)
            except Exception as err:   # no core installed: a question cannot be answered, so none is asked
                self._engine_failed = True
                LOG.warning("clarify_engine_unavailable class=%s", type(err).__name__)
        return self.correction

    async def _clarify(self, station: str, key: str | None, text: str, *, floor: float, always: bool, cooldown: bool = False,
                       intent: str = "clarify.nothing", via_quasar: bool = False) -> Route | None:
        """The likeliest actions for a phrase nobody read as ONE short question (dialog opened, microphone kept open), or, when there is none and
        `always`, a line that says what was heard and which kinds of action exist. Never a request to repeat. None: nothing to say (not `always`,
        or `cooldown` and a line was already said to this station inside REPROMPT_COOLDOWN_S: the brain takes the phrase)."""
        clarifier = self.clarifier
        listen = self.allow_reprompt(station) if always else True   # one open-microphone line a minute per station: noise cannot loop the speaker
        if always and cooldown and not listen:
            return None
        options = await clarifier.options(station, text, floor=floor) if self.clarify_mode == "on" else []
        if options:
            engine = self._engine()
            offer = None if engine is None or key is None else engine.ask(key, text, options, clarifier.question(options),
                                                                      now=self.monotonic(), via_quasar=via_quasar)
            if offer is not None:
                utterance_trace.note(asked="clarify")
                return Route("reply", offer.say, listen=True, intent="clarify.ask", source="correction")
        if not always:
            return None
        await clarifier.refresh()   # the kinds of action for the line (a read younger than a few seconds is reused)
        utterance_trace.note(asked="reprompt")
        return Route("reply", clarifier.describe(text), listen=listen, intent=intent, source="correction")

    async def unheard(self, station: str, text: str, *, cooldown: bool = False, intent: str = "clarify.nothing") -> Route | None:
        """A turn nobody could read and the brain (or Jev) gave up on («Повтори?», «Не расслышал»), or one without speech: what to say instead.
        None only with `cooldown` (see `_clarify`). `intent` names the line in the trace."""
        route = await self._clarify(station, station, text, floor=clarify.FALLBACK_AT, always=True, cooldown=cooldown, intent=intent)
        return None if route is None else self._mark_open_dialog(station, route)

    def _trace(self, decision: Decision | None, route: Route, via_quasar: bool, station: str) -> None:
        trace = utterance_trace.current()
        if trace is None:
            return
        trace.stage_ms("route", route.ms)
        if decision is None:
            if route.source == "correction":
                trace.note(intent=route.intent)   # the repair dialog's own line («repair.ask», «repair.rerun»): the trace names it
            return
        trace.note(**self.trace_fields(decision))
        trace.stage_ms("grammar", decision.elapsed_us // 1000)
        if route.kind == "none":
            trace.note(declined=self._why_declined(decision, via_quasar, station))
        elif decision.tier in ("clarify", "reprompt"):
            trace.note(asked=decision.tier)

    def note_reply(self, station: str, text: str, *, brain: bool = False) -> None:
        """The last thing the speaker said to this station, for «повтори». `brain`: the brain said it. A question of the brain's
        is remembered until the next line of any kind to that station or BRAIN_QUESTION_WINDOW_S (a short answer to it is the brain's to continue)."""
        if text:
            self.capabilities.forget(station, text)
            self.last_reply[station] = text
            if brain and text.rstrip().endswith("?"):
                self.brain_asked[station] = self.monotonic()
            else:
                self.brain_asked.pop(station, None)

    def asked_a_question(self, station: str) -> bool:
        """The brain's last line to this station was a question: a short answer to it («да», «про собак») is the brain's to
        continue, never a catch-all's «не расслышал». The window is long (the brain's text is ready long before it is spoken and a
        long story ends with its question) but bounded: lines answered by the hook or the quasar layer never reach `note_reply`, so
        without a bound a question would suppress the catch-alls for hours. The grammar's own questions («Да?», «На сколько
        поставить таймер?») are not it: the brain has no context for them."""
        asked = self.brain_asked.get(station)
        return asked is not None and self.monotonic() - asked <= BRAIN_QUESTION_WINDOW_S

    async def _run(self, station: str, decision: Decision, via_quasar: bool) -> Route:
        if decision.parts:
            return await self._compound(station, decision, via_quasar)
        tier = decision.tier
        intent = decision.intent
        if tier == "drop":
            return Route("silent", intent=intent, source="grammar")
        if tier in ("canned", "reprompt", "clarify"):
            if tier == "reprompt":   # nothing was read: the likeliest actions as a question, else what was heard and what can be asked for
                return await self._clarify(station, station, decision.text, floor=clarify.FALLBACK_AT, always=True, cooldown=True, intent=intent) or NONE
            line = render_reply(decision, self.pick)
            if not line:
                return NONE
            return Route("reply", line, listen=tier == "clarify", intent=intent, source="grammar")
        if not self.executable(decision, via_quasar=via_quasar, station=station):
            return self._declined(decision)
        return await self._execute(station, decision, via_quasar)

    def _declined(self, decision: Decision) -> Route:
        """A decision the adapter does not run. Unconfigured timers still answer honestly instead of leaving the brain to
        pretend; everything else is the old path's (quasar container, brain)."""
        executor = (decision.action or {}).get("executor") or ""
        if executor.startswith(TIMER_PREFIXES) and self.timers is None and decision.tier in ("execute", "query") \
                and not decision.fuzzy:
            return Route("reply", TIMERS_OFF, intent=decision.intent, source="grammar")
        return NONE

    def allow_reprompt(self, station: str) -> bool:
        """One reprompt a minute per station, shared by the grammar's reprompts and Jev's: True (and the clock restarts) when one is due."""
        now = self.monotonic()
        if now - self.last_reprompt.get(station, float("-inf")) < REPROMPT_COOLDOWN_S:
            return False
        self.last_reprompt[station] = now
        return True

    async def _compound(self, station: str, decision: Decision, via_quasar: bool) -> Route:
        """Every clause must be something the adapter runs, checked BEFORE the first one runs: a compound that ends in `none`
        has changed nothing (a clause that fails while running can still leave the earlier ones done)."""
        if not self.executable(decision, via_quasar=via_quasar, station=station):
            return self._declined_compound(decision, station)
        lines: list[str] = []
        for part in decision.parts:
            done = await self._run(station, part, via_quasar)
            if done.kind == "none":
                return NONE
            if done.text:
                lines.append(done.text)
        return Route("reply", " ".join(lines) or None, intent="compound", source="grammar")

    def _declined_compound(self, decision: Decision, station: str) -> Route:
        """A compound the adapter cannot run. When only the missing timer configuration stops it, it answers «не умею» as a
        single timer phrase does, instead of leaving the brain to pretend; any other clause is the quasar layer's."""
        if self.timers is None and all(self._clause_executable(part, station, timers_bound=True) for part in decision.parts) \
                and all(((part.action or {}).get("executor") or "").startswith(TIMER_PREFIXES) for part in decision.parts):
            return Route("reply", TIMERS_OFF, intent="compound", source="grammar")
        return NONE

    async def _execute(self, station: str, decision: Decision, via_quasar: bool) -> Route:
        action = decision.action or {}
        executor = action["executor"]
        args = action.get("args") or {}
        intent = decision.intent
        if executor in VOLUME_EXECUTORS:
            return Route("volume", volume=volume_from(decision), intent=intent, source="grammar")
        if executor == "dialog.end":
            return Route("dialog_end", intent=intent, source="grammar")
        text = await self._adapter_executor(station, decision, executor, args)
        if text is None:
            return NONE
        return Route("reply", text, intent=intent, source="skill" if intent.startswith("skill.") else "grammar")

    async def _adapter_executor(self, station: str, decision: Decision, executor: str, args: dict) -> str | None:
        if executor == "skill.capabilities":
            return await self.capabilities.answer(station, topic=args.get("topic"), more=bool(args.get("more")))
        if executor == "skill.math":
            return decision.slots["answer"]
        if executor == "skill.weather":
            return await self.skills.weather(args["mode"])
        if executor == "music.play":
            return await self.skills.music()
        if executor == "clock.say":
            return clock_text(self.now())
        if executor == "clock.date":
            return date_text(self.now())
        if executor == "dialog.repeat_last":
            return self.last_reply.get(station)
        if executor.startswith(TIMER_PREFIXES):
            started = time.monotonic()
            text = await self._timer(executor, args)
            utterance_trace.call("timer", executor=executor, args=utterance_trace.plain(args) or None, ok=text is not None,
                                 ms=int((time.monotonic() - started) * 1000))
            return text
        if executor.startswith("ha."):
            return await self.ha.run(executor, args, station)
        return None

    async def _timer(self, executor: str, args: dict) -> str | None:
        if executor == "timer.start":
            started = await self.timers.start(int(args["seconds"]), args.get("note"))
            if args.get("note"):
                return f"Напомню через {duration_accusative(started['seconds'])}."
            return f"Таймер на {duration_accusative(started['seconds'])}."
        if executor == "timer.cancel":
            if await self.timers.cancel("timer"):
                return "Таймер отменён."
            if any(item["note"] for item in self.timers.remaining()):   # a reminder is not a timer, but the speaker may mean it
                return TIMER_CANCEL_REMINDER_HINT
            return "Таймеров нет."
        if executor == "reminder.cancel":
            count = await self.timers.cancel("reminder")
            return ("Напоминание отменено." if count == 1 else "Напоминания отменены.") if count else "Напоминаний нет."
        if executor == "timer.remaining":
            left = self.timers.remaining()
            if not left:
                return "Таймеров нет."
            return left_text(left[0]["left"]).capitalize() + "."
        if executor == "alarm.set":
            time_slot = args["time"]
            alarm = await self.timers.set_alarm(time_slot["hour"], time_slot["minute"], time_slot.get("part"),
                                                bool(time_slot.get("ambiguous")))
            return f"Будильник на {alarm['text']}."
        if executor == "alarm.cancel":
            count = await self.timers.cancel("alarm")
            return "Будильник отменён." if count else "Будильников нет."
        return None


def load_grammar(path: str | Path | None = None) -> Grammar | None:
    """The compiled bundle, or None (logged) when it cannot be read: the adapter then runs without the grammar."""
    try:
        return load(path or BUNDLE)
    except Exception as err:
        LOG.warning("intent_grammar_unavailable class=%s", type(err).__name__)
        return None


class _Bound:
    """Stands for an executor binding (timers, Home Assistant, skills) when the back-test measures the route; never called."""
    __slots__ = ()


def measured_router(grammar: Grammar) -> IntentRouter:
    """The router as a fully configured adapter runs it, for measurement only: timers, Home Assistant actions and the
    skills are bound, so what the back-test counts as handled is what production answers when its configuration is complete."""
    bound = _Bound()
    return IntentRouter(grammar, skills=bound, timers=bound, ha=bound)


def measured_route(router: IntentRouter, *, mode: str) -> Callable[[Any], Decision | None]:
    """The decision function of the back-test, by what is measured:

    adapter   the adapter route alone (grammar, arithmetic, weather, catch-alls)
    quasar    only what the quasar container already answered (the baseline before this work)
    combined  the production route: grammar pre-phase -> quasar handlers -> grammar rest. A grammar decision counts only
              when the runtime would act on it there (`IntentRouter.executable`): a volume change in the turn path, an
              unconfigured timer or a guess the owner has to confirm is not an answer. A phrase the quasar container
              answered keeps the tier it answered with (the replay's `via`); its label and arguments are not observed, so
              the row is judged on the tier alone.
    """
    def handled_by_quasar(row: Any) -> Decision:
        return Decision(intent="quasar.handled", tier=row.quasar_tier, slots={}, action=None, reply=None,
                        labels=(), confidence=1.0, fuzzy=False, pattern=-1, text=row.text)

    def decide(row: Any) -> Decision | None:
        if mode == "adapter":
            return router.classify(row.text)
        if mode == "quasar":
            return handled_by_quasar(row) if row.quasar else None
        early = router.classify(row.text, "pre")
        if early is not None and router.executable(early, via_quasar=True):
            return early
        if row.quasar:
            return handled_by_quasar(row)
        late = router.classify(row.text)
        return late if late is not None and router.executable(late, via_quasar=False) else None
    return decide
