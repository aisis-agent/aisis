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
from intent_grammar.timewords import duration_ru, plural
import voice_skills

LOG = logging.getLogger("aisis.intent")

BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
REPROMPT_COOLDOWN_S = 60.0     # a second miss right after «не расслышал» goes to the brain: noise cannot loop the speaker
TIMERS_OFF = "Таймеры и будильники я пока не умею."

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

# Executors the adapter runs itself; everything else belongs to the quasar container (or is only a skill label).
ADAPTER_EXECUTORS = frozenset(("clock.say", "clock.date", "dialog.repeat_last", "timer.start", "timer.cancel",
                               "timer.remaining", "alarm.set", "alarm.cancel", "ha.climate", "ha.climate_delta",
                               "ha.light_color", "ha.light_brightness", "skill.math", "skill.weather"))
VOLUME_EXECUTORS = frozenset(("speaker.volume", "speaker.volume_delta"))
FALLBACK_BAND = -1             # intents with priority below zero are the catch-alls: they read what the skills left
CLARIFY_WINDOW_S = 30.0        # how long a clarification question waits for its answer
ACTION_DEDUPE_S = 5.0          # the same timer or Home Assistant action inside this window is not run twice


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
    from the grammar's `speaker.volume` args: exactly one of `step` (+-1), `level` (0..10) or `percent` (0..100). The unit
    is never guessed from the size of a number; an action that breaks the contract does nothing."""
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
    delta = args.get("delta") or {}
    amount = delta.get("amount")
    if not isinstance(amount, (int, float)) or args.get("direction") not in (-1, 1):
        return None
    levels = amount / 10 if delta.get("unit") == "процент" else amount
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
    recent_actions: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)

    # ── pure classification (the back-test measures this) ───────────────────
    def classify(self, text: str, phase: str | None = None) -> Decision | None:
        """phase="pre": only what may answer before the quasar container's own handlers; None: everything."""
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
        return self.grammar.decide(text, max_priority=FALLBACK_BAND) if self.catchall else None

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

    async def _once(self, executor: str, args: dict, run: Callable[[], Awaitable[str | None]], intent: str) -> Route:
        """Timer and Home Assistant actions run once per ACTION_DEDUPE_S: the quasar hook and the speaker's own turn can carry the
        same phrase, and a second «таймер на пять минут» must not start a second timer."""
        key = (executor, json.dumps(args, sort_keys=True, default=str))
        now = self.monotonic()
        done = self.recent_actions.get(key)
        if done is not None and now - done[0] <= ACTION_DEDUPE_S:
            text = done[1]
        else:
            text = await run()
            if text is None:
                return NONE
            self.recent_actions = {k: v for k, v in self.recent_actions.items() if now - v[0] <= ACTION_DEDUPE_S}
            self.recent_actions[key] = (now, text)
        return Route("reply", text, intent=intent, source="grammar")

    async def route(self, station: str, text: str, *, phase: str | None = None, via_quasar: bool = False) -> Route:
        """Decide and execute one turn; Route("none") hands it on. `via_quasar`: the caller (quasar) runs volume itself."""
        started = time.perf_counter_ns()
        try:
            decision = self.classify(text, phase)
            joined = self._joined_answer(station, text, phase)
            if joined is not None:
                decision, text = joined
            route = NONE if decision is None else await self._run(station, decision, via_quasar)
            if decision is not None and decision.tier == "clarify":
                self.pending[station] = (text, self.monotonic())
        except Exception as err:  # fail open: the old path answers
            LOG.warning("intent_route_failed class=%s", type(err).__name__)
            route = NONE
        route.ms = (time.perf_counter_ns() - started) // 1_000_000
        self.stats[route.source if route.kind != "none" else "none"] = self.stats.get(
            route.source if route.kind != "none" else "none", 0) + 1
        return route

    def note_reply(self, station: str, text: str) -> None:
        """The last thing the speaker said to this station, for «повтори»."""
        if text:
            self.last_reply[station] = text

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
        if tier == "confirm" or decision.fuzzy:
            return NONE   # a guessed reading never acts without the owner's «да», which only the quasar layer can ask
        return await self._execute(station, decision, via_quasar)

    def _reprompt_allowed(self, station: str) -> bool:
        now = self.monotonic()
        if now - self.last_reprompt.get(station, float("-inf")) < REPROMPT_COOLDOWN_S:
            return False
        self.last_reprompt[station] = now
        return True

    async def _compound(self, station: str, decision: Decision, via_quasar: bool) -> Route:
        lines: list[str] = []
        for part in decision.parts:
            if part.tier not in ("execute", "query", "canned") or part.fuzzy:
                return NONE
            executor = (part.action or {}).get("executor")
            if part.tier != "canned" and executor not in ADAPTER_EXECUTORS:
                return NONE   # one clause belongs to the quasar layer: it takes the whole utterance, as before
            done = await self._run(station, part, via_quasar)
            if done.kind == "none":
                return NONE
            if done.text:
                lines.append(done.text)
        return Route("reply", " ".join(lines) or None, intent="compound", source="grammar")

    async def _execute(self, station: str, decision: Decision, via_quasar: bool) -> Route:
        action = decision.action or {}
        executor = action.get("executor")
        args = action.get("args") or {}
        intent = decision.intent
        if executor in VOLUME_EXECUTORS:
            volume = volume_from(decision) if via_quasar else None
            return Route("volume", volume=volume, intent=intent, source="grammar") if volume else NONE
        if executor == "dialog.end" and via_quasar:
            return Route("dialog_end", intent=intent, source="grammar")
        if executor not in ADAPTER_EXECUTORS and not (executor == "music.play" and not args.get("query")):
            return NONE
        if executor.startswith(("timer.", "alarm.", "ha.")):
            return await self._once(executor, args, lambda: self._adapter_executor(station, decision, executor, args), intent)
        text = await self._adapter_executor(station, decision, executor, args)
        if text is None:
            return NONE
        return Route("reply", text, intent=intent, source="skill" if intent.startswith("skill.") else "grammar")

    async def _adapter_executor(self, station: str, decision: Decision, executor: str, args: dict) -> str | None:
        if executor == "skill.math":
            return decision.slots["answer"]
        if executor == "skill.weather":
            return await self.skills.weather(args["mode"]) if self.skills else None
        if executor == "music.play":
            return await self.skills.music() if self.skills else None
        if executor == "clock.say":
            return clock_text(self.now())
        if executor == "clock.date":
            return date_text(self.now())
        if executor == "dialog.repeat_last":
            return self.last_reply.get(station)
        if executor.startswith(("timer.", "alarm.")):
            return await self._timer(decision, executor, args)
        if executor.startswith("ha."):
            return await self.ha.run(executor, args) if self.ha else None
        return None

    async def _timer(self, decision: Decision, executor: str, args: dict) -> str | None:
        if self.timers is None:
            return TIMERS_OFF
        if executor == "timer.start":
            started = await self.timers.start(int(args["seconds"]), args.get("note"))
            if args.get("note"):
                return f"Напомню через {duration_ru(started['seconds'])}."
            return f"Таймер на {duration_ru(started['seconds'])}."
        if executor == "timer.cancel":
            count = await self.timers.cancel("timer")
            return "Таймер отменён." if count else "Таймеров нет."
        if executor == "timer.remaining":
            left = self.timers.remaining()
            if not left:
                return "Таймеров нет."
            from timers import left_text
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


def measured_route(router: IntentRouter, *, mode: str) -> Callable[[Any], Decision | None]:
    """The decision function of the back-test, by what is measured:

    adapter   the adapter route alone (grammar, arithmetic, weather, catch-alls)
    quasar    only what the quasar container already answered (the baseline before this work)
    combined  the production route: grammar pre-phase -> quasar handlers -> grammar rest
    """
    def handled_by_quasar(row: Any) -> Decision:
        return Decision(intent="quasar.handled", tier="execute", slots={}, action=None, reply=None,
                        labels=(row.truth,), confidence=1.0, fuzzy=False, pattern=-1, text=row.text)

    def decide(row: Any) -> Decision | None:
        if mode == "adapter":
            return router.classify(row.text)
        if mode == "quasar":
            return handled_by_quasar(row) if row.quasar else None
        early = router.classify(row.text, "pre")
        if early is not None:
            return early
        return handled_by_quasar(row) if row.quasar else router.classify(row.text)
    return decide
