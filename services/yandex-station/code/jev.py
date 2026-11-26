"""Jev (TypeSafe AI's decision model, through the Vercel AI Gateway) behind a `RouteDecisionProvider`.

Jev answers typed questions about a piece of text (a choice among named options, a probability, a score) in one round
trip of ~0.25 s from the speaker host. It is an OPTIONAL decision model (spec 040): the deterministic grammar, the
fast-intent layer and the brain never wait for it beyond a hard budget, and any failure is an abstention.

Used only where it saves time, on the turns the grammar, the quasar layer and the skills could not answer (about to go to
the 2.4 s brain):
  - what the utterance is (a command nobody implements, talk for the brain, garbled recognition, not addressed to us),
  - which brain tier and reasoning effort it needs, and which of the design's four routes (deterministic / fast / reprompt /
    escalate) the phrase belongs to: advisory until a lite tier exists, logged in shadow mode,
  - nightly: which existing intent an unanswered phrase most resembles (`label_with_jev`), to propose grammar changes.

Fail-open by construction: a hard timeout, a circuit breaker, no exception escapes `assess`/`decide`, and the key never
appears in a log line or an error. Off by default (`AISIS_JEV=off|shadow|on`). In `on` mode each acting class has its own
switch (`AISIS_JEV_REFUSE`, `AISIS_JEV_REPROMPT`, both off by default) and the confidence threshold is `AISIS_JEV_THRESHOLD`;
`is_emergency` is the deterministic guard that keeps any acting decision away from an emergency phrase.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

LOG = logging.getLogger("aisis.jev")

DEFAULT_URL = "https://ai-gateway.vercel.sh/v1/decisions"
DEFAULT_MODEL = "typesafe-ai/jev"
HARD_TIMEOUT_S = 0.4         # the decision budget on a voice turn; the brain call it may shorten takes ~2.4 s
BREAKER_FAILURES = 4         # consecutive failures that open the breaker
BREAKER_OPEN_S = 30.0        # how long it stays open before one probe is let through
CONFIDENT = 0.95             # the default act threshold (below: abstain); bench_jev.py: 6 of 6 right at 0.95
THRESHOLD_RANGE = (0.5, 1.0)  # what AISIS_JEV_THRESHOLD may be set to

ModelAlias = Literal["fast", "balanced", "deep", "background"]
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]
ExecutionKind = Literal["deterministic", "model", "harness", "background"]

# (status, parsed JSON body or None); raises on transport errors. Bound to aiohttp in the adapter, faked in tests.
Post = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[tuple[int, Any]]]


@dataclass(frozen=True)
class RouteTurn:
    """What a route decision sees: the final transcript only (spec 140: no audio, no speaker identity)."""
    text: str


@dataclass(frozen=True)
class RouteDecision:
    """docs/architecture/contracts.md `RouteDecision`, plus what the speaker adds: the utterance kind and Jev's confidence."""
    kind: ExecutionKind
    reason_code: str
    model_alias: ModelAlias | None = None
    harness: str | None = None
    effort: Effort = "none"
    tool_groups: tuple[str, ...] = ()
    context_budget_tokens: int = 8000
    time_budget_ms: int = 20000
    utterance: str = "talk"          # command | talk | garbled | not_addressed
    confidence: float = 0.0


class RouteDecisionProvider(Protocol):
    async def decide(self, turn: RouteTurn) -> RouteDecision | None: ...


# ── transport and questions ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Choice:
    name: str
    instructions: str
    options: dict[str, str]          # value -> what it means

    def request(self) -> dict[str, Any]:
        return {"type": "choice", "name": self.name, "instructions": self.instructions,
                "choices": [{"value": value, "description": text} for value, text in self.options.items()]}


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    confidence: float


@dataclass(frozen=True)
class Reply:
    """The answers of one call and what the gateway says it cost (USD), if it says."""
    answers: dict[str, ChoiceAnswer]
    cost: float | None = None


class JevError(Exception):
    """Any reason a Jev call gave no usable answer. The message never carries the key or the transcript."""


class JevClient:
    """One `POST /v1/decisions` per call, several questions answered together against the same text."""

    def __init__(self, post: Post, api_key: str, *, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
                 timeout: float = HARD_TIMEOUT_S):
        self._post, self._key, self.url, self.model, self.timeout = post, api_key, url, model, timeout

    async def choose(self, text: str, questions: list[Choice]) -> dict[str, ChoiceAnswer]:
        return (await self.ask(text, questions)).answers

    async def ask(self, text: str, questions: list[Choice]) -> Reply:
        body = {"model": self.model, "input": text, "questions": [question.request() for question in questions]}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            status, data = await asyncio.wait_for(self._post(self.url, headers, body), self.timeout)
        except asyncio.TimeoutError:
            raise JevError("timeout") from None
        except Exception as err:  # transport: refused, reset, TLS, DNS … never re-raised with its message
            raise JevError(f"transport {type(err).__name__}") from None
        if status != 200 or not isinstance(data, dict):
            raise JevError(f"http {status}")
        return Reply(_parse_choices(data, questions), _gateway_cost(data))


def _gateway_cost(data: dict[str, Any]) -> float | None:
    """The call's cost in USD when the gateway reports it (`provider_metadata.gateway.cost`), else None."""
    metadata = data.get("provider_metadata")
    gateway = metadata.get("gateway") if isinstance(metadata, dict) else None
    cost = gateway.get("cost") if isinstance(gateway, dict) else None
    try:
        value = float(cost)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _parse_choices(data: dict[str, Any], questions: list[Choice]) -> dict[str, ChoiceAnswer]:
    by_name = {answer.get("name"): answer for answer in data.get("answers") or [] if isinstance(answer, dict)}
    parsed: dict[str, ChoiceAnswer] = {}
    for question in questions:
        answer = by_name.get(question.name)
        if answer is None or answer.get("choice") not in question.options:
            raise JevError("malformed answer")
        confidence = answer.get("confidence")
        if isinstance(confidence, bool):   # JSON true is a bool, which Python calls an int: a malformed answer, not 1.0
            raise JevError("malformed confidence")
        if not isinstance(confidence, (int, float)):
            probabilities = answer.get("probabilities")
            confidence = next((p.get("probability") for p in probabilities if isinstance(p, dict)
                               and p.get("value") == answer["choice"]), None) if isinstance(probabilities, list) else None
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise JevError("malformed confidence")
        parsed[question.name] = ChoiceAnswer(answer["choice"], float(confidence))
    return parsed


class CircuitBreaker:
    """Opens after `failures` consecutive failures; after `open_for` seconds one probe call is let through."""

    def __init__(self, failures: int = BREAKER_FAILURES, open_for: float = BREAKER_OPEN_S,
                 clock: Callable[[], float] = time.monotonic):
        self.failures, self.open_for, self.clock = failures, open_for, clock
        self._count = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self.clock() - self._opened_at >= self.open_for:
            self._opened_at = self.clock()   # half-open: exactly one probe per window
            return True
        return False

    def success(self) -> None:
        self._count, self._opened_at = 0, None

    def failure(self) -> None:
        self._count += 1
        if self._count >= self.failures:
            self._opened_at = self.clock()

    @property
    def open(self) -> bool:
        return self._opened_at is not None and self.clock() - self._opened_at < self.open_for


def aiohttp_post(session: Any) -> Post:
    """The `Post` of the adapter: one persistent aiohttp session, so the TLS connection stays warm (~0.25 s a call)."""
    async def post(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        async with session.post(url, headers=headers, json=body) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = None
            return response.status, data
    return post


def from_env(env: dict[str, str], session: Any, settings: JevSettings) -> "JevRouteProvider | None":
    """The provider for `AISIS_JEV=on|shadow` with a key present; None (Jev not used) otherwise.

    AISIS_JEV_URL / AISIS_JEV_MODEL override the gateway endpoint and the model id; the budget is capped at the hard timeout;
    `settings.threshold` is the confidence an answer needs to count as a decision.
    """
    if env.get("AISIS_JEV", "off").lower() not in ("on", "shadow") or not env.get("VERCEL_JEV_KEY"):
        return None
    try:
        timeout = min(HARD_TIMEOUT_S, float(env.get("AISIS_JEV_TIMEOUT", HARD_TIMEOUT_S)))
    except ValueError:
        timeout = HARD_TIMEOUT_S
    client = JevClient(aiohttp_post(session), env["VERCEL_JEV_KEY"], url=env.get("AISIS_JEV_URL", DEFAULT_URL),
                       model=env.get("AISIS_JEV_MODEL", DEFAULT_MODEL), timeout=max(0.05, timeout))
    return JevRouteProvider(client, confident=settings.threshold)


# ── the speaker's route decision ────────────────────────────────────────────

UTTERANCE = Choice("utterance", "What is this spoken phrase to a home voice speaker?", {
    "command": "a request to do something: smart home, volume, music, timers, calls, messages, shopping lists, reminders",
    "talk": "small talk, an opinion, advice, a knowledge question, or a request for an answer or an explanation",
    "garbled": "words that do not form an understandable request: a recognition error, a cut-off or meaningless phrase",
    "not_addressed": "speech that is not meant for the speaker: a person talking to someone else, an animal, a caption"})
TIER = Choice("tier", "How capable a language model does the answer need?", {
    "fast": "a greeting, banter, a one-line reply or a trivial fact the person already knows",
    "balanced": "an ordinary question or a short explanation",
    "deep": "multi-step reasoning, planning, a long explanation, or knowledge that needs care"})
EFFORT = Choice("effort", "How much reasoning does the answer need?", {
    "none": "none: a reflex answer", "low": "a little", "medium": "some careful thought", "high": "extended step-by-step thought"})

# The design's four routes (docs/research/fast-llm-routing.md §6), in the wording and options FastModelBench measured with Jev
# (docs/research/fast-llm-bench-2026-11.md §5: 92 % route accuracy, 0 of 71 escalate utterances sent to fast, on `/v1/evaluate`).
# The Russian text is the benchmark's, word for word: the wording is part of the measurement. Advisory only: no code acts on it.
ROUTE = Choice("route", "Выбери маршрут голосовой фразы, распознанной колонкой.", {
    "deterministic": "ответ даёт локальный инструмент: погода, точное время, перевод единиц измерения",
    "fast": "только болтовня без фактов: приветствие, благодарность, настроение, шутка, вопрос о тебе самом, «да/нет/ладно»",
    "reprompt": "шум распознавания, обрывок фразы, одно слово без команды: нужно переспросить",
    "escalate": "всё остальное: факты о мире, новости, цены, советы, объяснения, тексты, задачи, команды, смесь болтовни с вопросом"})

QUESTIONS = [UTTERANCE, TIER, EFFORT, ROUTE]    # one call, answered together

_TIER_EFFORT: dict[str, ModelAlias] = {"fast": "fast", "balanced": "balanced", "deep": "deep"}


@dataclass(frozen=True)
class JevSettings:
    """What `AISIS_JEV=on` may act on. Both switches default to off: `on` with neither is advisory (logs, never acts)."""
    threshold: float = CONFIDENT
    refuse: bool = False       # AISIS_JEV_REFUSE=on: a confident «command» nobody implements gets «Этого я пока не умею.»
    reprompt: bool = False     # AISIS_JEV_REPROMPT=on: a confident «garbled» gets the clarifier's answer (the likeliest action as a question, else what was heard), never a request to repeat

    @property
    def acts(self) -> bool:
        return self.refuse or self.reprompt


def settings_from_env(env: dict[str, str]) -> JevSettings:
    """AISIS_JEV_THRESHOLD (0.5..1.0, default 0.95), AISIS_JEV_REFUSE and AISIS_JEV_REPROMPT (`on`|`off`, default off).

    A value outside the contract falls back to the safe default and is logged by name: a typo must not take the speaker down,
    and it must not make Jev act more than the owner asked for."""
    def switch(name: str) -> bool:
        value = env.get(name, "off").strip().lower()
        if value not in ("on", "off"):
            LOG.warning("jev_config_invalid name=%s fallback=off", name)
        return value == "on"

    threshold = CONFIDENT
    raw = env.get("AISIS_JEV_THRESHOLD")
    if raw is not None and raw.strip():
        try:
            parsed = float(raw)
        except ValueError:
            parsed = math.nan
        if THRESHOLD_RANGE[0] <= parsed <= THRESHOLD_RANGE[1]:
            threshold = parsed
        else:
            LOG.warning("jev_config_invalid name=AISIS_JEV_THRESHOLD fallback=%s", CONFIDENT)
    return JevSettings(threshold, switch("AISIS_JEV_REFUSE"), switch("AISIS_JEV_REPROMPT"))


@dataclass(frozen=True)
class Verdict:
    """What one Jev call came to, including the cases that give no decision: the shadow log is built from this.

    `candidate` is the decision the answers read as whatever their confidence (None when the call failed or never ran);
    `decision` is `candidate` only if it reached the threshold, else None (an abstention). `reason` is `ok`, `below_threshold`,
    `breaker_open`, or the failure (`timeout`, `http 403`, `transport ClientConnectorError`, `malformed answer`, ...)."""
    candidate: RouteDecision | None
    reason: str
    answers: dict[str, ChoiceAnswer] = field(default_factory=dict)
    latency_ms: int | None = None
    cost: float | None = None        # USD, when the gateway reports it

    @property
    def decision(self) -> RouteDecision | None:
        return self.candidate if self.reason == "ok" else None


class JevRouteProvider:
    """`RouteDecisionProvider` over Jev: one call, four typed questions. Abstains (None) on any failure or low confidence."""

    def __init__(self, client: JevClient, *, breaker: CircuitBreaker | None = None, confident: float = CONFIDENT):
        self.client, self.breaker, self.confident = client, breaker or CircuitBreaker(), confident
        self.stats = {"calls": 0, "ok": 0, "failed": 0, "skipped_open": 0, "low_confidence": 0}

    async def decide(self, turn: RouteTurn) -> RouteDecision | None:
        return (await self.assess(turn)).decision

    async def assess(self, turn: RouteTurn) -> Verdict:
        """Never raises. Every outcome, abstentions included, comes back with its reason and latency."""
        if not self.breaker.allow():
            self.stats["skipped_open"] += 1
            return Verdict(None, "breaker_open")
        self.stats["calls"] += 1
        started = time.perf_counter()
        try:
            reply = await self.client.ask(turn.text, QUESTIONS)
        except JevError as err:
            self._failed()
            LOG.warning("jev_failed reason=%s breaker_open=%s", err, self.breaker.open)
            return Verdict(None, str(err), latency_ms=_ms_since(started))
        except Exception as err:  # a bug here must never reach the voice turn
            self._failed()
            LOG.warning("jev_failed class=%s", type(err).__name__)
            return Verdict(None, f"error {type(err).__name__}", latency_ms=_ms_since(started))
        self.breaker.success()
        self.stats["ok"] += 1
        answers = reply.answers
        candidate = route_from(answers["utterance"], answers["tier"], answers["effort"], self.confident)
        if answers["utterance"].confidence < self.confident:
            self.stats["low_confidence"] += 1
            return Verdict(candidate, "below_threshold", answers, _ms_since(started), reply.cost)
        return Verdict(candidate, "ok", answers, _ms_since(started), reply.cost)

    def _failed(self) -> None:
        self.breaker.failure()
        self.stats["failed"] += 1


def _ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def route_from(utterance: ChoiceAnswer, tier: ChoiceAnswer, effort: ChoiceAnswer, confident: float = CONFIDENT) -> RouteDecision:
    if utterance.choice == "garbled":
        return RouteDecision("deterministic", "jev_garbled", utterance="garbled", confidence=utterance.confidence)
    if utterance.choice == "not_addressed":
        return RouteDecision("deterministic", "jev_not_addressed", utterance="not_addressed", confidence=utterance.confidence)
    if utterance.choice == "command":
        return RouteDecision("deterministic", "jev_command_unsupported", utterance="command", confidence=utterance.confidence)
    effort_level: Effort = effort.choice  # type: ignore[assignment]  # the option keys are exactly the contract's levels
    # A tier or effort the model is unsure of degrades to the balanced middle, never upwards.
    alias = _TIER_EFFORT[tier.choice] if tier.confidence >= confident else "balanced"
    if effort.confidence < confident:
        effort_level = "medium"
    return RouteDecision("model", "jev_talk", model_alias=alias, effort=effort_level, utterance="talk",
                         confidence=min(utterance.confidence, tier.confidence, effort.confidence))


# ── the emergency guard and the shadow log line ─────────────────────────────

# Deterministic and independent of the grammar: no Jev decision may ever act on a phrase that mentions one of these. A refusal
# («Этого я пока не умею.») or a reprompt to someone who says «мне плохо, вызови скорую» is the failure this guard exists for.
# Deliberately over-inclusive (a prefix match on «скор» also takes «скоро»): a false hit only hands the turn to the brain, as without
# Jev. test_jev checks that every phrase of the grammar's must-pass safety set trips it, so the two lists cannot drift apart.
_EMERGENCY_PREFIXES = (
    "скор", "полиц", "милиц", "пожар", "помощ", "помогит", "спасит", "спасат", "неотложк", "врач", "доктор", "медик", "больниц",
    "сердц", "сердеч", "дыш", "дыха", "задых", "задох", "задуш", "удуш", "тон", "утон", "отрав", "созна", "обмор", "инсульт",
    "инфаркт", "кров", "умира", "умер", "суицид", "самоубий", "дым", "загор", "горит", "горим", "гарь", "гарью")
_EMERGENCY_WORDS = frozenset(("плохо", "нехорошо", "худо", "112", "101", "102", "103", "911", "192", "193", "194"))
_EMERGENCY_NUMBERS = re.compile(r"\b(?:сто двенадцать|один один два|ноль (?:один|два|три)|сто девяносто (?:два|три|четыре))\b")
_TOKEN = re.compile(r"[0-9a-zа-я]+")


def is_emergency(text: str) -> bool:
    """True when the phrase mentions an emergency stem (скорая, полиция, пожар, мне плохо, сердце, не могу дышать, тонет, отравился, ...)."""
    lowered = text.lower().replace("ё", "е")
    if _EMERGENCY_NUMBERS.search(lowered):
        return True
    return any(token in _EMERGENCY_WORDS or token.startswith(_EMERGENCY_PREFIXES) for token in _TOKEN.findall(lowered))


def turn_hash(text: str) -> str:
    """A short id of a turn for the shadow log: stable (the same phrase hashes the same, so a log line can be joined to the dialog
    store offline by hashing its rows) and not the text. Short phrases can be guessed from it, so the log stays on the host."""
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:10]


def describe(verdict: Verdict, turn: str, guard: str = "none") -> str:
    """`key=value` fields of a shadow or route log line: the answers with their confidences, the decision they read as, why it did or
    did not stand, latency and cost. The first four keys (utterance, tier, effort, conf) keep the order the log readers parse.
    `turn` is `turn_hash(text)`; the text itself never reaches a log. `conf` is the utterance confidence."""
    def answer(name: str) -> tuple[str, str]:
        got = verdict.answers.get(name)
        return (got.choice, f"{got.confidence:.2f}") if got else ("none", "0.00")

    utterance, conf = answer("utterance")
    tier, tier_conf = answer("tier")
    effort, effort_conf = answer("effort")
    route, route_conf = answer("route")
    cost = "-" if verdict.cost is None else f"{verdict.cost:.7f}"
    latency = "-" if verdict.latency_ms is None else str(verdict.latency_ms)
    return (f"utterance={utterance} tier={tier} effort={effort} conf={conf} tier_conf={tier_conf} effort_conf={effort_conf} "
            f"route={route} route_conf={route_conf} decision={verdict.candidate.reason_code if verdict.candidate else 'none'} "
            f"outcome={verdict.reason.replace(' ', '_')} ms={latency} cost={cost} turn={turn} guard={guard}")


# ── nightly helper: which existing intent does an unanswered phrase resemble ─

NEW_INTENT = "_none"


async def label_with_jev(client: JevClient, text: str, intents: dict[str, str]) -> ChoiceAnswer:
    """The intent id (or `_none`: a new intent is needed) Jev finds closest to `text`.

    `intents` maps intent id -> a one-line description (the pinned examples joined). Used offline by the nightly proposal
    step, where a failure simply leaves the phrase unlabelled; the caller owns the retry policy and the batching.
    """
    options = {**intents, NEW_INTENT: "none of these: the phrase asks for something no listed intent covers"}
    answer = await client.choose(text, [Choice("intent", "Which existing voice-assistant intent does this phrase belong to?", options)])
    return answer["intent"]
