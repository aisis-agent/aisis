"""The adapter's route before the brain: grammar, arithmetic, weather, then the grammar's catch-alls.

`IntentRouter.classify(text)` is pure (no I/O) and is what the corpus back-test measures
(`python3 -m intent_grammar backtest --route adapter`), so the numbers describe the code that answers a turn. `route()`
executes the decision: canned lines, reprompts, the clock, timers, Home Assistant climate and colour, weather, music.

Decisions for executors the quasar container owns (volume, media keys, light and device switches) are not executed
here; the quasar overlay asks `/intent/decide` first and runs `volume` itself, everything else stays with its own
handlers. A turn that reaches the adapter with such a decision still goes to the brain, as before.

Fail-open: a grammar that cannot be loaded, an executor that raises, or a mode of `off` leave the old path
(calc -> skills -> brain) untouched.
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

LOG = logging.getLogger("aisis.intent")

BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
REPROMPT_COOLDOWN_S = 60.0     # a second miss right after «не расслышал» goes to the brain: noise cannot loop the speaker
TIMERS_OFF = "Таймеры и будильники я пока не умею."
TIMER_CANCEL_REMINDER_HINT = "Таймеров нет, есть напоминание. Чтобы отменить его, скажи: отмени напоминание."

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

# Executors the adapter runs itself; everything else belongs to the quasar container (or is only a skill label).
ADAPTER_EXECUTORS = frozenset(("clock.say", "clock.date", "dialog.repeat_last", "timer.start", "timer.cancel",
                               "timer.remaining", "alarm.set", "alarm.cancel", "reminder.cancel", "ha.climate", "ha.climate_delta",
                               "ha.light_color", "ha.light_brightness", "skill.math", "skill.weather"))
TIMER_PREFIXES = ("timer.", "alarm.", "reminder.")   # the executors that need the TimerBook
VOLUME_EXECUTORS = frozenset(("speaker.volume", "speaker.volume_delta"))
FALLBACK_BAND = -1             # intents with priority below zero are the catch-alls: they read what the skills left
CLARIFY_WINDOW_S = 30.0        # how long a clarification question waits for its answer
BRAIN_QUESTION_WINDOW_S = 30.0  # how long a question the brain asked stays the thing a short answer replies to
DEDUPE_S = 5.0                 # the same timer/alarm start or cancel inside this window is not run twice (hook and turn can both carry it)
START_EXECUTORS = frozenset(("timer.start", "alarm.set"))
CANCEL_EXECUTORS = frozenset(("timer.cancel", "alarm.cancel", "reminder.cancel"))
DEDUPED_EXECUTORS = START_EXECUTORS | CANCEL_EXECUTORS   # the only actions a second delivery of one phrase can repeat


@dataclass
class Route:
    """What to do with a turn. kind: reply | silent | volume | dialog_end | none (the old path continues)."""
    kind: str
    text: str | None = None
    listen: bool = False
    volume: dict[str, float] | None = None
    intent: str | None = None
    source: str = "none"
    ms: int = 0

    def as_json(self) -> dict[str, Any]:
        return {"route": self.kind, "text": self.text, "listen": self.listen, "volume": self.volume,
                "intent": self.intent, "ms": self.ms}


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
    brain_asked: dict[str, float] = field(default_factory=dict)                     # station -> when the brain last asked it a question
    recent_actions: dict[tuple[str, str], tuple[float, str, int]] = field(default_factory=dict)   # start/cancel -> (when, reply, generation)
    running: dict[tuple[str, str], asyncio.Task[str | None]] = field(default_factory=dict)       # starts and cancels in flight
    generation: int = 0    # counts the timer/alarm starts and cancels that ran: a remembered reply is good only within its generation

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

    async def _once(self, executor: str, args: dict, run: Callable[[], Awaitable[str | None]]) -> str | None:
        """A timer or alarm start or cancel runs once per DEDUPE_S: the quasar hook and the speaker's own turn can carry the same
        phrase, so a second «таймер на пять минут» must not start a second timer and a second «отмени таймер» must not answer
        «Таймеров нет» to a cancel that worked. A duplicate that arrives while the first is running waits for it. Only a reply
        that was spoken is remembered (a failure is never replayed), and only within its generation: any start or cancel that
        ran since changed what the reply is about."""
        key = (executor, json.dumps(args, sort_keys=True, default=str))
        done = self.recent_actions.get(key)
        if done is not None and self.monotonic() - done[0] <= DEDUPE_S and done[2] == self.generation:
            return done[1]
        task = self.running.get(key)
        if task is None or task.done():   # a finished task whose cleanup has not run yet is no one to wait for
            task = asyncio.ensure_future(self._remember(key, executor, run))
            self.running[key] = task
            task.add_done_callback(lambda finished, k=key: self._finished(k, finished))
        return await asyncio.shield(task)

    async def _remember(self, key: tuple[str, str], executor: str, run: Callable[[], Awaitable[str | None]]) -> str | None:
        if executor in CANCEL_EXECUTORS:
            await self._settle_starts()   # a cancel sees the timer a start in flight is creating
        self.generation += 1
        generation = self.generation
        text = await run()
        if text is not None:
            now = self.monotonic()
            self.recent_actions = {k: v for k, v in self.recent_actions.items() if now - v[0] <= DEDUPE_S}
            self.recent_actions[key] = (now, text, generation)
        return text

    async def _settle_starts(self) -> None:
        pending = [task for key, task in self.running.items() if key[0] in START_EXECUTORS and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _finished(self, key: tuple[str, str], task: asyncio.Task[str | None]) -> None:
        if self.running.get(key) is task:   # a newer task under the same key is not ours to drop
            del self.running[key]
        if not task.cancelled():
            task.exception()   # the awaiters report it; a task nobody awaits any more must not log «never retrieved»

    async def route(self, station: str, text: str, *, phase: str | None = None, via_quasar: bool = False) -> Route:
        """Decide and execute one turn; Route("none") hands it on. `via_quasar`: the caller (quasar) runs volume itself."""
        started = time.perf_counter_ns()
        try:
            decision = self.classify(text, phase, fallback=not self._asked_a_question(station))
            joined = self._joined_answer(station, text, phase)
            if joined is not None:
                decision, text = joined
            route = NONE if decision is None else await self._run(station, decision, via_quasar)
            if decision is not None and decision.tier == "clarify":
                self.pending[station] = (text, self.monotonic())
        except Exception as err:  # fail open: the old path answers
            LOG.warning("intent_route_failed class=%s", type(err).__name__)
            route = NONE
        route = dataclasses.replace(route, ms=(time.perf_counter_ns() - started) // 1_000_000)   # a copy: NONE is shared
        self.stats[route.source if route.kind != "none" else "none"] = self.stats.get(
            route.source if route.kind != "none" else "none", 0) + 1
        return route

    def note_reply(self, station: str, text: str, *, brain: bool = False) -> None:
        """The last thing the speaker said to this station, for «повтори». `brain`: the brain said it. A question of the brain's
        is remembered for BRAIN_QUESTION_WINDOW_S (a short answer to it is the brain's to continue); any other line replaces it."""
        if text:
            self.last_reply[station] = text
            if brain and text.rstrip().endswith("?"):
                self.brain_asked[station] = self.monotonic()
            else:
                self.brain_asked.pop(station, None)

    def _asked_a_question(self, station: str) -> bool:
        """The brain's last line to this station was a question it is still waiting on: a short answer to it («да», «про собак»)
        is the brain's to continue, never a catch-all's «не расслышал». The grammar's own questions («Да?», «На сколько
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
            if tier == "reprompt" and not self._reprompt_allowed(station):
                return NONE
            line = render_reply(decision, self.pick)
            if not line:
                return NONE
            return Route("reply", line, listen=tier in ("reprompt", "clarify"), intent=intent, source="grammar")
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

    def _reprompt_allowed(self, station: str) -> bool:
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
                and any(((part.action or {}).get("executor") or "").startswith(TIMER_PREFIXES) for part in decision.parts):
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
        run = lambda: self._adapter_executor(station, decision, executor, args)
        text = await (self._once(executor, args, run) if executor in DEDUPED_EXECUTORS else run())
        if text is None:
            return NONE
        return Route("reply", text, intent=intent, source="skill" if intent.startswith("skill.") else "grammar")

    async def _adapter_executor(self, station: str, decision: Decision, executor: str, args: dict) -> str | None:
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
            return await self._timer(executor, args)
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
