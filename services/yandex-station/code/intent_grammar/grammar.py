"""Declarative intent grammar: schema, validation and the deterministic decision.

A grammar file (YAML, see grammar/ru.yaml and README.md) lists intents with patterns (pattern.py), slots, a tier,
guards, an action, a reply, and pinned examples/negatives. `Grammar.decide(text)` canonicalises the utterance, matches
every intent and returns ONE decision or `None` (abstain: the brain takes the turn). The contract is the one of
HyperCalendarBot's rules layer: whole-utterance match, two different readings abstain, a decision is never a guess
that moves hardware (execute-tier intents are not negated, and a fuzzy match is flagged for the caller).

Pure Python, no I/O except `load`; the matcher is allocation-light and runs in well under a millisecond per intent
list on an Odroid-class CPU.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


from .normalize import Canon, Normalised, normalise
from .pattern import Alt, Matcher, Node, Opt, PatternError, Resolver, SLOT_TYPES, Value, Word, compile_forms, parse_pattern
from .timewords import duration_ru, parse_duration

PHASES = ("pre", "post")   # pre: safe to answer before the quasar container's own handlers; post: only when they found nothing
MAX_CLAUSES = 8           # a compound command has at most this many clauses; a longer chain is never split (the search is quadratic)
TIERS = ("execute", "confirm", "clarify", "query", "canned", "reprompt", "drop")
# Tiers in which the speaker acts on the world: guarded against negation by default.
ACTING_TIERS = ("execute", "confirm")
# What a clause of a compound command asks of the caller, weakest first: the compound takes the strongest.
_COMPOUND_RANK = {"canned": 0, "query": 1, "execute": 2, "confirm": 3}
GUARDS = ("not_negated", "not_question", "is_question")
SET_GUARDS = ("has:", "not_has:")   # has:<lexicon set> / not_has:<lexicon set>: any token of the utterance is / is not in it
REPLY_KINDS = ("canned", "template", "earcon", "none")
_INTENT_KEYS = {"id", "tier", "labels", "priority", "patterns", "slots", "guards", "allow_negation", "fuzzy_ok", "max_tokens", "phase",
                "action", "reply", "examples", "negatives", "note", "context"}
_CONTEXT_KEYS = {"id", "values", "sample", "note"}
CONTEXT_PRIORITY = 900        # a context intent without an explicit priority sits here: above every normal intent, below the emergency one
EMERGENCY_PRIORITY = 1000     # nothing a context intent can be given reaches it
_SLOT_KEYS = {"type", "range", "required", "default", "ask", "values", "no_time", "reject"}
_TOP_KEYS = {"version", "locale", "lexicon", "contexts", "intents", "backtest", "gate"}


class GrammarError(ValueError):
    pass


@dataclass(frozen=True)
class SlotSpec:
    name: str
    type: str
    range: tuple[float, float] | None = None
    required: bool = False
    default: Any = None
    ask: str | None = None
    no_time: bool = False          # a text value holding «через <duration>» is no reading: it is a second time, two readings of «когда»
    reject: tuple[str, ...] = ()   # a text value made only of these words is no reading («нам всем» as the whole note)


@dataclass(frozen=True)
class Example:
    say: str
    slots: dict[str, Any]


@dataclass(frozen=True)
class Intent:
    id: str
    tier: str
    labels: tuple[str, ...]
    priority: int
    patterns: tuple[tuple[Node, ...], ...]
    pattern_text: tuple[str, ...]
    slots: dict[str, SlotSpec]
    guards: tuple[str, ...]
    fuzzy_ok: bool
    max_tokens: int | None
    phase: str
    action: dict[str, Any] | None
    reply: dict[str, Any] | None
    examples: tuple[Example, ...]
    negatives: tuple[str, ...]
    required_stems: tuple[frozenset[str], ...]
    contexts: tuple[str, ...] = ()   # the dialog contexts the intent lives in; empty: a normal intent, which never sees a context


@dataclass(frozen=True)
class Context:
    """A dialog context declared by the grammar: the names of the per-turn values its intents may reference as `{@name}`,
    and sample phrases for them (what the pinned examples and the back-test pass when a row brings no values of its own)."""
    id: str
    values: tuple[str, ...] = ()
    sample: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveContext:
    """A context that is open for one decision: its id and the per-turn phrases (`values[name]`) the caller supplies.
    The YAML stays static; the dialog's turn-specific words (the proposed alternative, the heard verb) ride here."""
    id: str
    values: Mapping[str, Sequence[str]] = field(default_factory=dict)


@dataclass
class Decision:
    """The deterministic answer to one utterance."""
    intent: str
    tier: str
    slots: dict[str, Any]
    action: dict[str, Any] | None
    reply: dict[str, Any] | None
    labels: tuple[str, ...]
    confidence: float                 # 1.0 exact reading, 0.8 fuzzy reading
    fuzzy: bool
    pattern: int
    text: str                          # the canonical text that was matched
    ask: str | None = None             # clarify tier: the question for the missing slot
    parts: tuple["Decision", ...] = ()  # compound command: the clauses, in order
    elapsed_us: int = 0


@dataclass
class Grammar:
    version: int
    locale: str
    intents: tuple[Intent, ...]
    canon: Canon
    connectors: tuple[tuple[str, ...], ...]
    backtest: dict[str, Any]
    gate: dict[str, Any]
    sha256: str
    lexicon_forms: dict[str, list[str]] = field(default_factory=dict)
    sets: dict[str, Alt] = field(default_factory=dict)
    contexts: dict[str, Context] = field(default_factory=dict)

    # ── decision ───────────────────────────────────────────────────────────
    def normalise(self, text: str) -> Normalised:
        return normalise(text, self.canon)

    def decide(self, text: str, *, min_priority: int | None = None, max_priority: int | None = None,
               phase: str | None = None, context: ActiveContext | str | None = None) -> Decision | None:
        """One decision or None (abstain). Whole utterance first, then a compound of clauses: the split runs only when
        the whole utterance has no reading at all, never to override two readings that tied.

        `min_priority` / `max_priority` select a band of intents: the router asks for the real intents (priority >= 0),
        lets the arithmetic and weather skills answer, and only then lets the catch-alls (priority < 0) read what is left.
        `phase="pre"` keeps only the intents that may answer before the quasar container's own handlers.

        `context`: an open dialog context (an `ActiveContext`, or just its id). The intents that live in it (priority 900 unless
        the grammar says otherwise) join the normal ones and outrank them, but never `refuse.emergency` (1000); without the
        argument, and for every intent that has no `context`, nothing changes. A compound is split into clauses of normal
        intents only: «да и громче» is never «accept» plus a volume change.
        """
        started = time.perf_counter_ns()
        utterance = self.normalise(text)
        active = self._active(context)
        if min_priority is None and max_priority is None:
            # Real intents (and compounds of them) first, the catch-alls (priority < 0) only for what is left.
            bands = ((0, None), (None, -1))
        else:
            bands = ((min_priority, max_priority),)
        decision = None
        for low, high in bands:
            decision, tied = self._pick(utterance, low, high, phase, active)
            if decision is None and not tied:
                decision = self._compound(utterance, low, high, phase)
            if decision is not None or tied:
                break   # a tie between real intents is an abstain: the catch-alls must not read what two intents disputed
        if decision is not None:
            decision.elapsed_us = (time.perf_counter_ns() - started) // 1000
        return decision

    def decide_normalised(self, utterance: Normalised, min_priority: int | None = None,
                          max_priority: int | None = None, phase: str | None = None) -> Decision | None:
        return self._pick(utterance, min_priority, max_priority, phase)[0]

    def _active(self, context: ActiveContext | str | None) -> tuple[str, dict[str, tuple[Normalised, ...]]] | None:
        """The open context as (id, its per-turn phrases normalised like utterances), or None. An id the grammar does not
        declare is a caller's bug and raises rather than silently deciding without the dialog."""
        if context is None:
            return None
        if isinstance(context, str):
            context = ActiveContext(context)
        if context.id not in self.contexts:
            raise GrammarError(f"unknown context {context.id!r}")
        values = {name: tuple(self.normalise(phrase) for phrase in phrases) for name, phrases in context.values.items()}
        return context.id, values

    def _pick(self, utterance: Normalised, min_priority: int | None = None, max_priority: int | None = None,
              phase: str | None = None, active: tuple[str, dict[str, tuple[Normalised, ...]]] | None = None) -> tuple[Decision | None, bool]:
        """The winning decision of one (sub-)utterance and whether it was withheld because two intents tied."""
        if not utterance.tokens:
            return None, False
        found = self._candidates(utterance, fuzzy=False, band=(min_priority, max_priority, phase), active=active)
        if not found:
            found = self._candidates(utterance, fuzzy=True, band=(min_priority, max_priority, phase), active=active)
        if not found:
            return None, False
        # priority first, then specificity (more literal words consumed), then ambiguity -> abstain
        found.sort(key=lambda c: (-c[0], -c[1]))
        top = found[0]
        rivals = {c[2].intent for c in found if (c[0], c[1]) == (top[0], top[1])}
        if len(rivals) > 1:
            return None, True
        return top[2], False

    def candidates(self, text: str, context: ActiveContext | str | None = None) -> list[Decision]:
        """Every intent that reads the utterance (priority/specificity order); more than one at the top is a conflict."""
        utterance = self.normalise(text)
        active = self._active(context)
        found = self._candidates(utterance, fuzzy=False, active=active) or self._candidates(utterance, fuzzy=True, active=active)
        found.sort(key=lambda c: (-c[0], -c[1]))
        return [c[2] for c in found]

    def _candidates(self, u: Normalised, *, fuzzy: bool, band: tuple[int | None, int | None, str | None] = (None, None, None),
                    active: tuple[str, dict[str, tuple[Normalised, ...]]] | None = None) -> list[tuple[int, int, Decision]]:
        out: list[tuple[int, int, Decision]] = []
        stems = set(u.stems)
        for intent in self.intents:
            if not _in_scope(intent, band, active):
                continue
            best = self._match_intent(intent, u, stems, fuzzy, active[1] if active is not None else None)
            if best is not None:
                out.append((intent.priority, best[0], best[1]))
        return out

    def _match_intent(self, intent: Intent, u: Normalised, stems: set[str], fuzzy: bool,
                      values: dict[str, tuple[Normalised, ...]] | None = None) -> tuple[int, Decision] | None:
        if intent.max_tokens is not None and len(u.tokens) > intent.max_tokens:
            return None
        for guard in intent.guards:
            if guard == "not_negated" and u.negated:
                return None
            if guard == "not_question" and u.question:
                return None
            if guard == "is_question" and not u.question:
                return None
            if guard.startswith("not_has:") and self._has(u, guard[8:]):
                return None
            if guard.startswith("has:") and not self._has(u, guard[4:]):
                return None
        best: tuple[int, Decision] | None = None
        for index, seq in enumerate(intent.patterns):
            if not fuzzy and not intent.required_stems[index] <= stems:
                continue
            matcher = Matcher(u, fuzzy=fuzzy, slot_ok=lambda name, value, i=intent: _slot_ok(i, name, value), values=values)
            for caps, score in matcher.full(seq):
                decision = self._build(intent, index, caps, u, fuzzy)
                if decision is not None and (best is None or score > best[0]):
                    best = (score, decision)
                    break
        if best is not None and u.repaired:
            best[1].fuzzy, best[1].confidence = True, 0.8
        if best is not None and best[1].fuzzy and not intent.fuzzy_ok and best[1].tier in ACTING_TIERS:
            best[1].tier = "confirm"
        return best

    def _has(self, u: Normalised, name: str) -> bool:
        """True when any token of the utterance is read by the lexicon set `name`."""
        alt = self.sets[name]
        matcher = Matcher(u)
        return any(True for pos in range(len(u.tokens)) for _ in matcher._node(alt, pos))

    def _build(self, intent: Intent, index: int, caps, u: Normalised, fuzzy: bool) -> Decision | None:
        slots: dict[str, Any] = {name: value for name, value, _span in caps}
        ask: str | None = None
        tier = intent.tier
        for name, spec in intent.slots.items():
            if name in slots:
                continue
            if spec.default is not None:
                slots[name] = spec.default
            elif spec.required:
                if spec.ask is None:
                    return None
                ask, tier = spec.ask, "clarify"
        return Decision(intent=intent.id, tier=tier, slots=slots, action=_resolve(intent.action, slots),
                        reply=intent.reply, labels=intent.labels, confidence=0.8 if fuzzy else 1.0, fuzzy=fuzzy,
                        pattern=index, text=u.text, ask=ask)

    # ── compound commands ─────────────────────────────────────────────────
    def _compound(self, u: Normalised, min_priority: int | None = None, max_priority: int | None = None,
                  phase: str | None = None) -> Decision | None:
        """«включи свет и выключи чайник»: split at connectors when every clause is read by an acting intent."""
        n = len(u.tokens)
        cuts: list[tuple[int, int]] = []   # (clause end, next clause start)
        for i in range(1, n - 1):
            for connector in self.connectors:
                if tuple(u.tokens[i:i + len(connector)]) == connector and i + len(connector) < n:
                    cuts.append((i, i + len(connector)))
        # A boundary is where the next clause starts: «а потом» registers two overlapping cuts (`а потом` and `потом`) for one boundary.
        if not cuts or len({nxt for _end, nxt in cuts}) >= MAX_CLAUSES:   # no connector, or a chain no speaker says: not worth the quadratic search
            return None
        best: list[Decision] | None = None
        dead: set[int] = set()   # clause starts that cannot reach the end of the utterance: each is searched once, not once per path to it

        def split(start: int, acc: list[Decision]) -> None:
            nonlocal best
            if best is not None or start in dead:
                return
            if start >= n:
                if len(acc) > 1:
                    best = list(acc)
                return
            for end, nxt in [(n, n)] + [c for c in cuts if c[0] > start]:
                clause = self._clause(u, start, end)
                decision = self.decide_normalised(clause, min_priority, max_priority, phase) if clause.tokens else None
                if decision is not None and decision.tier in ("execute", "query", "canned", "confirm"):
                    split(nxt, acc + [decision])
                    if best is not None:
                        return
            dead.add(start)

        split(0, [])
        if not best:
            return None
        # the compound asks as much of the caller as its most demanding part: one `execute` clause makes it `execute`,
        # a clause that needs confirmation makes the whole thing `confirm`
        tier = max((part.tier for part in best), key=_COMPOUND_RANK.__getitem__)
        return Decision(intent="compound", tier=tier, slots={}, action=None, reply=None,
                        labels=tuple(sorted({label for part in best for label in part.labels})),
                        confidence=min(part.confidence for part in best), fuzzy=any(p.fuzzy for p in best),
                        pattern=-1, text=u.text, parts=tuple(best))

    def _clause(self, u: Normalised, start: int, end: int) -> Normalised:
        clause = u.slice(start, end)
        clause.negated = any(token in self.canon.negation for token in clause.tokens)
        # a question word belongs to its own clause; the question mark ends the utterance and asks every clause
        clause.question = u.raw.rstrip().endswith("?") or any(token in self.canon.question_words for token in clause.tokens)
        return clause

    # ── replies ───────────────────────────────────────────────────────────
    def say(self, decision: Decision, pick: Callable[[list[str]], str] = lambda options: options[0],
            values: Mapping[str, Any] | None = None) -> str | None:
        """The sentence to speak for a decision, or None (earcon/none/action only, or a template the executor has to
        fill: pass what it measured as `values`)."""
        return render_reply(decision, pick, values)

    # ── pinned checks ─────────────────────────────────────────────────────
    def self_check(self) -> list[str]:
        """Every example must decide to its intent (and slots); every negative must not. Returns the failures."""
        failures: list[str] = []
        for intent in self.intents:
            for where, context in self._check_settings(intent):
                for example in intent.examples:
                    failures.extend(self._check_example(intent, example, where, context))
                failures.extend(self._check_negatives(intent, where, context))
        return failures

    def _check_settings(self, intent: Intent) -> list[tuple[str, ActiveContext | None]]:
        """Where an intent's pinned checks run: a context intent inside each of its contexts, with the context's sample values."""
        return [(f" [{cid}]", ActiveContext(cid, self.contexts[cid].sample)) for cid in intent.contexts] or [("", None)]

    def _check_example(self, intent: Intent, example: Example, where: str, context: ActiveContext | None) -> list[str]:
        decision = self.decide(example.say, context=context)
        if decision is None or decision.intent != intent.id:
            got = "abstain" if decision is None else decision.intent
            return [f"{intent.id}{where}: example {example.say!r} -> {got}"]
        return [f"{intent.id}{where}: example {example.say!r} slot {name}={decision.slots.get(name)!r}, want {want!r}"
                for name, want in example.slots.items() if decision.slots.get(name) != want]

    def _check_negatives(self, intent: Intent, where: str, context: ActiveContext | None) -> list[str]:
        failures: list[str] = []
        for negative in intent.negatives:
            decision = self.decide(negative, context=context)
            if decision is not None and decision.intent == intent.id:
                failures.append(f"{intent.id}{where}: negative {negative!r} matched")
        return failures


def _in_scope(intent: Intent, band: tuple[int | None, int | None, str | None],
              active: tuple[str, dict[str, tuple[Normalised, ...]]] | None) -> bool:
    """Whether the intent takes part in a decision: inside the priority band, and either a normal intent of the requested phase or a
    context intent of the open context (the phase does not apply to a dialog's replies)."""
    low, high, phase = band
    if (low is not None and intent.priority < low) or (high is not None and intent.priority > high):
        return False
    if intent.contexts:
        return active is not None and active[0] in intent.contexts
    return phase is None or intent.phase == phase


def _slot_ok(intent: Intent, name: str, value: object) -> bool:
    spec = intent.slots.get(name)
    if spec is not None and isinstance(value, str) and ((spec.reject and all(word in spec.reject for word in value.split())) or (spec.no_time and _holds_time(value))):
        return False
    if spec is None or spec.range is None:
        return True
    amount = value.get("amount") if isinstance(value, dict) else value
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return True
    low, high = spec.range
    return low <= amount <= high


def _resolve(action: dict[str, Any] | None, slots: dict[str, Any]) -> dict[str, Any] | None:
    """`$slot` references in the action args replaced by the matched values."""
    if action is None:
        return None
    args = {key: slots.get(value[1:]) if isinstance(value, str) and value.startswith("$") else value
            for key, value in (action.get("args") or {}).items()}
    return {"executor": action["executor"], "args": args}


def render_reply(decision: Decision, pick: Callable[[list[str]], str] = lambda options: options[0],
                 values: Mapping[str, Any] | None = None) -> str | None:
    """The line to speak. `values` are executor-supplied fields for the template (they win over matched slots); a
    template with a placeholder nobody filled yields None, never the literal «{level}»."""
    reply = decision.reply
    if not reply or reply["kind"] not in ("canned", "template"):
        return decision.ask
    options = reply["say"] if isinstance(reply["say"], list) else [reply["say"]]
    line = pick(list(options))
    if reply["kind"] != "template":
        return line
    fields: dict[str, Any] = {**decision.slots, **(values or {})}
    for name, value in list(fields.items()):
        if isinstance(value, int) and not isinstance(value, bool) and name in ("duration", "time"):
            fields[name + "_text"] = duration_ru(value)
        if isinstance(value, dict) and "text" in value:
            fields[name + "_text"] = value["text"]
    try:
        return line.format_map(fields)
    except (KeyError, IndexError, ValueError, AttributeError):
        return None


# ── loading ─────────────────────────────────────────────────────────────────

def load(path: str | Path) -> Grammar:
    """A grammar from its YAML source, or from the compiled JSON bundle (`.json`: no YAML library needed at runtime)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        bundle = json.loads(text)
        return from_data(bundle["data"], bundle["source_sha256"])
    return loads(text)


def parse_yaml(source: str) -> Any:
    import yaml  # authoring-time dependency only: the speaker loads the compiled bundle
    try:
        return yaml.safe_load(source)
    except yaml.YAMLError as err:
        raise GrammarError(f"not valid YAML: {err}") from err


def compile_bundle(source: str) -> str:
    """The JSON bundle of a YAML grammar (validated first): what the adapter loads, committed beside the YAML."""
    loads(source)
    bundle = {"source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "data": parse_yaml(source)}
    return json.dumps(bundle, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def loads(source: str) -> Grammar:
    return from_data(parse_yaml(source), hashlib.sha256(source.encode("utf-8")).hexdigest())


def from_data(data: Any, sha256: str) -> Grammar:
    if not isinstance(data, dict):
        raise GrammarError("the grammar must be a mapping")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise GrammarError(f"unknown top-level keys: {sorted(unknown)}")
    if data.get("version") != 1:
        raise GrammarError("version must be 1")
    lexicon = data.get("lexicon") or {}
    if not isinstance(lexicon, dict):
        raise GrammarError("lexicon must be a mapping")

    resolver = Resolver(sets={}, keyed={}, slot_types={})
    forms: dict[str, list[str]] = {}
    plain = Resolver(sets={}, keyed={}, slot_types={})
    for name, entry in lexicon.items():
        if isinstance(entry, list):
            forms[name] = [str(f) for f in entry]
        elif isinstance(entry, dict):
            forms[name] = [str(f) for key in entry for f in (entry[key] or [])]
        else:
            raise GrammarError(f"lexicon.{name} must be a list or a mapping of key -> forms")
    for name, entry in lexicon.items():
        try:
            if isinstance(entry, dict):
                keyed = tuple((str(key), compile_forms([str(f) for f in (entry[key] or [])], plain))
                              for key in entry)
                resolver.keyed[name] = keyed
                resolver.sets[name] = Alt(tuple(option for _, alt in keyed for option in alt.options))
            else:
                resolver.sets[name] = compile_forms(forms[name], plain)
        except PatternError as err:
            raise GrammarError(f"lexicon.{name}: {err}") from err

    def words(name: str) -> frozenset[str]:
        return frozenset(str(form).lower().replace("ё", "е") for form in forms.get(name, []))

    canon = Canon(fillers=words("fillers"), lead_in=words("lead_in") | words("wake"), wake=words("wake"),
                  negation=words("negation"), question_words=words("question_words"),
                  typo_vocabulary=tuple(sorted(words("typo_vocabulary"))))
    connectors = tuple(tuple(str(form).lower().replace("ё", "е").split()) for form in forms.get("connectors", []))

    contexts = _contexts(data.get("contexts"))
    intents: list[Intent] = []
    seen: set[str] = set()
    for raw in data.get("intents") or []:
        intent = _intent(raw, resolver, contexts)
        if intent.id in seen:
            raise GrammarError(f"duplicate intent id {intent.id}")
        seen.add(intent.id)
        intents.append(intent)
    if not intents:
        raise GrammarError("the grammar has no intents")
    return Grammar(version=1, locale=str(data.get("locale", "ru")), intents=tuple(intents), canon=canon,
                   connectors=connectors, backtest=dict(data.get("backtest") or {}), gate=dict(data.get("gate") or {}),
                   sha256=sha256, lexicon_forms=forms, sets=resolver.sets, contexts=contexts)


def _contexts(raw: Any) -> dict[str, Context]:
    """The `contexts:` list: id, the names of the per-turn values (`values`) and sample phrases for them (`sample`)."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise GrammarError("contexts must be a list")
    found: dict[str, Context] = {}
    for item in raw:
        context = _context(item, found)
        found[context.id] = context
    return found


def _context(item: Any, known: dict[str, Context]) -> Context:
    """One entry of `contexts:`, checked; `known` are the contexts declared before it (an id is declared once)."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
        raise GrammarError(f"every context is a mapping with an id, got {item!r}")
    cid = item["id"]
    unknown = set(item) - _CONTEXT_KEYS
    if unknown:
        raise GrammarError(f"context {cid}: unknown keys {sorted(unknown)}")
    if cid in known:
        raise GrammarError(f"duplicate context {cid}")
    values = _context_values(cid, item)
    return Context(cid, tuple(values), _context_sample(cid, item, values))


def _context_values(cid: str, item: dict[str, Any]) -> list[str]:
    values = item.get("values") or []
    if not isinstance(values, list) or not all(isinstance(name, str) and name for name in values):
        raise GrammarError(f"context {cid}: values must be a list of names")
    return values


def _context_sample(cid: str, item: dict[str, Any], values: list[str]) -> dict[str, tuple[str, ...]]:
    sample = item.get("sample") or {}
    if not isinstance(sample, dict):
        raise GrammarError(f"context {cid}: sample must be a mapping of value name -> phrases")
    for name, phrases in sample.items():
        if name not in values:
            raise GrammarError(f"context {cid}: sample {name} is not one of its values {values}")
        if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
            raise GrammarError(f"context {cid}: sample {name} must be a list of phrases")
    return {name: tuple(phrases) for name, phrases in sample.items()}


def _value_names(seq: tuple[Node, ...]) -> set[str]:
    """Every `{@name}` a compiled pattern references, at any depth."""
    found: set[str] = set()
    for node in seq:
        if isinstance(node, Value):
            found.add(node.name)
        elif isinstance(node, Alt):
            for option in node.options:
                found |= _value_names(option)
        elif isinstance(node, Opt):
            found |= _value_names((node.inner,))
    return found


def _holds_time(value: str) -> bool:
    tokens = value.split()
    return any(token == "через" and parse_duration(tokens, at + 1) for at, token in enumerate(tokens))


def _words(value: Any) -> tuple[str, ...]:
    return tuple(str(word).lower().replace("ё", "е") for word in value or ())


def _intent(raw: Any, resolver: Resolver, declared: dict[str, Context]) -> Intent:
    if not isinstance(raw, dict) or "id" not in raw:
        raise GrammarError("every intent is a mapping with an id")
    iid = str(raw["id"])
    unknown = set(raw) - _INTENT_KEYS
    if unknown:
        raise GrammarError(f"{iid}: unknown keys {sorted(unknown)}")
    tier = raw.get("tier")
    if tier not in TIERS:
        raise GrammarError(f"{iid}: tier must be one of {TIERS}, got {tier!r}")
    slots = _slot_specs(iid, raw, resolver)
    local = Resolver(sets=resolver.sets, keyed=resolver.keyed, slot_types={n: s.type for n, s in slots.items()})
    pattern_text = [str(p) for p in (raw.get("patterns") or [])]
    if not pattern_text:
        raise GrammarError(f"{iid}: at least one pattern is required")
    patterns: list[tuple[Node, ...]] = []
    for text in pattern_text:
        try:
            patterns.append(parse_pattern(text, local))
        except PatternError as err:
            raise GrammarError(f"{iid}: pattern {text!r}: {err}") from err
    in_contexts = _intent_contexts(iid, raw.get("context"), declared)
    _check_context_values(iid, pattern_text, patterns, in_contexts, declared)
    priority = _intent_priority(iid, raw, in_contexts)
    guards = tuple(raw.get("guards") or ())
    for guard in guards:
        named = next((g for g in SET_GUARDS if guard.startswith(g)), None)
        if named is not None and guard[len(named):] in resolver.sets:
            continue
        if guard not in GUARDS:
            raise GrammarError(f"{iid}: unknown guard {guard!r}")
    if tier in ACTING_TIERS and "not_negated" not in guards and not raw.get("allow_negation", False):
        guards = guards + ("not_negated",)
    action = raw.get("action")
    if action is not None and (not isinstance(action, dict) or "executor" not in action):
        raise GrammarError(f"{iid}: action needs an executor")
    reply = raw.get("reply")
    if reply is not None:
        if not isinstance(reply, dict) or reply.get("kind") not in REPLY_KINDS:
            raise GrammarError(f"{iid}: reply.kind must be one of {REPLY_KINDS}")
        if reply["kind"] in ("canned", "template") and not reply.get("say"):
            raise GrammarError(f"{iid}: a {reply['kind']} reply needs say")
    if tier == "canned" and (reply is None or reply["kind"] != "canned"):
        raise GrammarError(f"{iid}: a canned-tier intent needs a canned reply")
    examples: list[Example] = []
    for item in raw.get("examples") or []:
        if isinstance(item, str):
            examples.append(Example(item, {}))
        elif isinstance(item, dict) and "say" in item:
            examples.append(Example(str(item["say"]), dict(item.get("slots") or {})))
        else:
            raise GrammarError(f"{iid}: bad example {item!r}")
    return Intent(id=iid, tier=tier, labels=tuple(str(label) for label in raw.get("labels") or ()),
                  priority=priority, patterns=tuple(patterns), pattern_text=tuple(pattern_text),
                  slots=slots, guards=guards, fuzzy_ok=bool(raw.get("fuzzy_ok", False)),
                  max_tokens=int(raw["max_tokens"]) if raw.get("max_tokens") is not None else None,
                  phase=_phase(iid, raw.get("phase", "post")),
                  action=action, reply=reply,
                  examples=tuple(examples), negatives=tuple(str(n) for n in raw.get("negatives") or ()),
                  required_stems=tuple(_required_stems(seq) for seq in patterns), contexts=in_contexts)


def _slot_spec(iid: str, name: str, spec: Any, resolver: Resolver) -> SlotSpec:
    if not isinstance(spec, dict) or "type" not in spec:
        raise GrammarError(f"{iid}: slot {name} needs a type")
    bad = set(spec) - _SLOT_KEYS
    if bad:
        raise GrammarError(f"{iid}: slot {name} unknown keys {sorted(bad)}")
    if spec["type"] not in SLOT_TYPES and spec["type"] not in resolver.keyed:
        raise GrammarError(f"{iid}: slot {name} has unknown type {spec['type']!r}")
    rng = spec.get("range")
    if rng is not None and (not isinstance(rng, list) or len(rng) != 2):
        raise GrammarError(f"{iid}: slot {name} range must be [low, high]")
    return SlotSpec(name, spec["type"], tuple(rng) if rng else None, bool(spec.get("required", False)),
                    spec.get("default"), spec.get("ask"), bool(spec.get("no_time", False)), _words(spec.get("reject")))


def _slot_specs(iid: str, raw: dict[str, Any], resolver: Resolver) -> dict[str, SlotSpec]:
    return {name: _slot_spec(iid, name, spec, resolver) for name, spec in (raw.get("slots") or {}).items()}


def _check_context_values(iid: str, pattern_text: list[str], patterns: list[tuple[Node, ...]], in_contexts: tuple[str, ...],
                          declared: dict[str, Context]) -> None:
    """A pattern may reference `{@name}` only in an intent that has a context, and only names that context declares."""
    for text, seq in zip(pattern_text, patterns):
        names = _value_names(seq)
        if names and not in_contexts:
            raise GrammarError(f"{iid}: pattern {text!r} uses a context value {sorted(names)} but the intent has no context")
        for cid in in_contexts:
            missing = names - set(declared[cid].values)
            if missing:
                raise GrammarError(f"{iid}: pattern {text!r} uses {sorted(missing)}, which context {cid} does not declare in values")


def _intent_priority(iid: str, raw: dict[str, Any], in_contexts: tuple[str, ...]) -> int:
    priority = int(raw.get("priority", CONTEXT_PRIORITY if in_contexts else 0))
    if in_contexts and priority >= EMERGENCY_PRIORITY:
        raise GrammarError(f"{iid}: a context intent never reaches priority {EMERGENCY_PRIORITY} (the emergency intent's), got {priority}")
    return priority


def _intent_contexts(iid: str, raw: Any, declared: dict[str, Context]) -> tuple[str, ...]:
    """The `context:` of an intent (one id or a list), every one declared at the top level."""
    if raw is None:
        return ()
    ids = [raw] if isinstance(raw, str) else raw
    if not isinstance(ids, list) or not ids or not all(isinstance(cid, str) for cid in ids):
        raise GrammarError(f"{iid}: context must be a context id or a list of them")
    for cid in ids:
        if cid not in declared:
            raise GrammarError(f"{iid}: context {cid} is not declared under contexts")
    return tuple(dict.fromkeys(ids))


def _phase(iid: str, value: Any) -> str:
    if value not in PHASES:
        raise GrammarError(f"{iid}: phase must be one of {PHASES}, got {value!r}")
    return value


def _required_stems(seq: tuple[Node, ...]) -> frozenset[str]:
    """Stems every reading of the pattern must contain (top-level plain words): a cheap prefilter."""
    return frozenset(node.stem for node in seq if isinstance(node, Word) and node.kind == "stem")
