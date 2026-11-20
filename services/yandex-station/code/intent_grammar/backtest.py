"""Corpus back-test of the intent grammar.

Reads the private labelled corpora (real spoken utterances, hand-written synthetic phrases), runs every row through
`Grammar.decide` and reports coverage, accuracy, false triggers and latency, per source, per split and per intent.
A report can be compared with the previous one: every row that used to be handled correctly and no longer is
counts as a regression, and the gate (grammar `gate:`) turns that into an exit code.

The held-out split is frozen by a hash of a grammar-independent canonical form of the text (lower case, ё -> е,
punctuation removed, whitespace collapsed; see `SPLIT_VERSION`), so editing the grammar's vocabulary can never move a
row between dev and holdout, and all copies of one phrase land on the same side. `dev` is what you tune on; `holdout`
is the honest number. Texts of held-out rows never appear in a report: only their ids and outcomes. Row ids come from
the content (file, `ts`, canonical text, n-th exact duplicate), never from line numbers, so inserting or reordering
rows keeps every id and the comparison with the previous report stays valid.

Corpus files (JSON lines) under the corpus directory:
  real-voice-*.jsonl   {text, truth, ts?, verdict?, labeller?}        source "real"
  *-labelled.jsonl     the same schema                                source "real"
  intents-ru.jsonl     {text, expect: {kind, domain, tier?, action?}} source "synthetic"
  <any of the above> a row with `context` is a DIALOG row (below), not part of the splits and the gate numbers
  quasar-replay.jsonl  {text, handled, via} from tools/replay_quasar.mts: which phrases the deployed quasar container
                       already answers without a model. Without it the flag falls back to the corpus' own record
                       (synthetic: expect.kind != llm; real: answered by layer L1 and judged ok/clarify).

Dialog rows (`context`, optional `values`, `expect: {intent}` | `expect: {outside: true}`, or `truth` for a real row) say how the text
reads INSIDE an open dialog context (`Grammar.decide(text, context=...)`): the intent it must decide to (`null`: abstain), or that it is
no reply of the dialog (`outside`: it leaves the context and takes the normal route). They are measured apart from the coverage numbers
(a reply is meaningless outside its dialog, and the real/synthetic splits must stay comparable), live in the committed
`grammar/dialog-ru.jsonl` (kind `synthetic` for the generated matrix, `authored` for phrases written from the owner's wording; no real
recorded reply exists yet) plus any `context` row of the private corpus (kind by source), and gate with `gate.dialog` (every row decided as
labelled) and the emergency phrases of `gate.safety` inside every context.

A synthetic row that carries `expect.tier` / `expect.action` is only correct when the decision's tier and the
essential arguments of its action match too (see `meets_expectation`); every other row is judged by its label alone.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .grammar import ACTING_TIERS, TIERS, ActiveContext, Decision, Grammar

DEFAULT_CORPUS_DIR = "~/.local/share/aisis/corpus"
DIALOG_FIXTURE = Path(__file__).parent / "grammar" / "dialog-ru.jsonl"
ENV_CORPUS_DIR = "AISIS_INTENT_CORPUS_DIR"
# Version of the split definition below. Bump it with ANY change of the canonical form, the salt or the hash: rows then
# no longer belong to the split of older reports, and the comparison only trusts rows whose side did not change.
SPLIT_VERSION = 1
SPLIT_SALT = f"aisis-intents-split-v{SPLIT_VERSION}|"
# Version of the report format AND of how rows are scored. Bump it with any change of either: an older baseline is then
# `incompatible` and only the owner's `--init-baseline REASON` starts a new one (comparing rows scored by another rule
# would turn the scoring change itself into «regressions»). 3: only decisions the runtime acts on count; quasar replay
# hits keep their observed tier and carry no label.
REPORT_SCHEMA = 3
GOOD_OUTCOMES = frozenset(("correct", "dropped", "brain_ok", "no_action"))
COMPLETING_TIERS = ("execute", "query", "canned")


@dataclass(frozen=True)
class Row:
    id: str
    text: str
    truth: str
    source: str
    split: str
    quasar: bool = False   # the quasar container already answered this phrase without a model (synthetic: expect.kind != llm; real: layer L1, verdict ok/clarify)
    expect: dict[str, Any] | None = field(default=None, compare=False)   # the corpus' own tier/action, when it has one
    quasar_via: str | None = None   # how it answered: the replay's `via` («fast-confirm», «media-key», …); None when not observed

    @property
    def quasar_tier(self) -> str:
        """The tier the quasar container answered this row with (never the row's expected one)."""
        return replay_tier(self.quasar_via)


def corpus_dir(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get(ENV_CORPUS_DIR) or DEFAULT_CORPUS_DIR).expanduser()


_PUNCTUATION = re.compile(r"[^\w\s]|_")


def split_key(text: str) -> str:
    """Grammar-independent canonical form of a phrase: lower case, ё -> е, punctuation removed, whitespace collapsed.
    The held-out split and the row ids hash this and nothing the grammar defines (fillers, wake words, typo list)."""
    folded = _PUNCTUATION.sub(" ", text.lower().replace("ё", "е"))
    return " ".join(folded.split()) or text.strip().lower()


def holdout_percent(grammar: Grammar) -> int:
    return int(grammar.backtest.get("holdout_percent", 20))


def split_of(text: str, percent: int) -> str:
    """Stable dev/holdout assignment from the canonical text, so repeated phrases stay on one side."""
    digest = hashlib.sha256((SPLIT_SALT + split_key(text)).encode("utf-8")).digest()
    return "holdout" if int.from_bytes(digest[:4], "big") % 100 < percent else "dev"


def _row_id(basis: str, occurrence: int) -> str:
    return hashlib.sha1(f"{basis}|{occurrence}".encode("utf-8")).hexdigest()[:12]


def corpus_source(name: str) -> str | None:
    """The source (`synthetic` / `real`) of a corpus file by its name; None for a file the back-test does not read."""
    if name == "intents-ru.jsonl":
        return "synthetic"
    if name.startswith("real-voice-") or name.endswith("-labelled.jsonl"):
        return "real"
    return None


def _jsonl_records(text: str) -> Iterator[dict[str, Any]]:
    """The JSON record of every non-blank line of a corpus file."""
    for line in text.splitlines():
        if line.strip():
            yield json.loads(line)


def load_rows(directory: Path, grammar: Grammar) -> tuple[list[Row], list[dict[str, Any]]]:
    """All rows of every known corpus file under `directory`, plus the file manifest (name, sha256, rows)."""
    percent = holdout_percent(grammar)
    rows: list[Row] = []
    manifest: list[dict[str, Any]] = []
    replay = _load_replay(directory / REPLAY_FILE)
    occurrences: dict[str, int] = {}
    for path in sorted(directory.glob("*.jsonl")):
        name = path.name
        if name == REPLAY_FILE:
            manifest.append({"file": name, "source": "replay", "rows": len(replay or {}),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            continue
        source = corpus_source(name)
        if source is None:
            continue
        data = path.read_bytes()
        count = 0
        for record in _jsonl_records(data.decode("utf-8")):
            if "context" in record:
                continue   # a dialog row: `load_dialog_rows` reads it
            text = str(record.get("text", "")).strip()
            truth = _truth(source, record)
            if not text or not truth:
                continue
            basis = f"{path.stem}|{record.get('ts', '')}|{split_key(text)}"
            occurrences[basis] = occurrences.get(basis, 0) + 1
            quasar, via = replay[text] if replay is not None and text in replay else _quasar_handled(source, record)
            expect = record.get("expect") if source == "synthetic" and isinstance(record.get("expect"), dict) else None
            rows.append(Row(_row_id(basis, occurrences[basis]), text, truth, source, split_of(text, percent),
                            quasar=quasar, expect=expect, quasar_via=via))
            count += 1
        manifest.append({"file": name, "source": source, "rows": count, "sha256": hashlib.sha256(data).hexdigest()})
    return rows, manifest


REPLAY_FILE = "quasar-replay.jsonl"


@dataclass(frozen=True)
class DialogRow:
    """One reading inside a dialog context: `intent` is what `decide(text, context=...)` must return (None: abstain), or `outside`:
    it must be no reply of the dialog (abstain or a normal intent: the utterance leaves the context)."""
    id: str
    text: str
    context: str
    values: dict[str, tuple[str, ...]] | None   # None: the context's sample values
    kind: str                                    # synthetic | authored | real
    intent: str | None = None
    outside: bool = False


def _dialog_row(record: dict[str, Any], kind: str, basis: str, occurrence: int) -> DialogRow | None:
    text = str(record.get("text", "")).strip()
    context = record.get("context")
    expect = record.get("expect")
    if not text or not isinstance(context, str):
        return None
    if isinstance(expect, dict):
        outside, intent = bool(expect.get("outside")), expect.get("intent")
    else:   # a real row: `truth` is the intent id, or «outside»
        truth = record.get("truth")
        outside, intent = truth == "outside", None if truth in (None, "", "outside", "abstain") else truth
    values = record.get("values")
    return DialogRow(_row_id(basis, occurrence), text, context,
                     None if not isinstance(values, dict) else {str(k): tuple(str(p) for p in v) for k, v in values.items()},
                     kind, None if outside else intent, outside)


def _dialog_sources(directory: Path | None, fixture: bool) -> list[tuple[Path, str | None]]:
    """The files with dialog records: the dialog fixture (when `fixture`; it must exist, or the gate would measure nothing) and the
    corpus files under `directory`, each with its source (None for the fixture)."""
    sources: list[tuple[Path, str | None]] = []
    if fixture:
        if not DIALOG_FIXTURE.exists():
            raise FileNotFoundError(f"the dialog fixture {DIALOG_FIXTURE} is missing: the dialog gate would measure nothing")
        sources.append((DIALOG_FIXTURE, None))
    if directory is not None:
        sources += [(path, corpus_source(path.name)) for path in sorted(directory.glob("*.jsonl")) if corpus_source(path.name)]
    return sources


def _dialog_records(directory: Path | None, fixture: bool) -> Iterator[tuple[Path, str | None, dict[str, Any]]]:
    """Every `context` record of the dialog fixture (when `fixture`) and of the corpus files under `directory`, with its file and
    that file's source (None for the fixture)."""
    for path, source in _dialog_sources(directory, fixture):
        for record in _jsonl_records(path.read_text(encoding="utf-8")) if path.exists() else ():
            if "context" in record:
                yield path, source, record


def gates_dialog(grammar: Grammar) -> bool:
    """The grammar asks for the dialog rows (`gate.dialog`): only then is the committed fixture, the shipped grammar's, read."""
    return "dialog" in (grammar.gate or {})


def load_dialog_rows(directory: Path | None, *, fixture: bool = True) -> list[DialogRow]:
    """The dialog rows: the committed fixture (`fixture`: only a grammar that asks for it with `gate.dialog` should read it, the fixture
    is the shipped grammar's), then every `context` row of the corpus files under `directory` (kind by file source)."""
    rows: list[DialogRow] = []
    occurrences: dict[str, int] = {}
    for path, source, record in _dialog_records(directory, fixture):
        kind = str(record.get("kind") or source or "synthetic")
        basis = f"{path.stem}|{record.get('ts', '')}|{record['context']}|{split_key(str(record.get('text', '')))}|{json.dumps(record.get('values'), sort_keys=True, ensure_ascii=False)}"
        occurrences[basis] = occurrences.get(basis, 0) + 1
        row = _dialog_row(record, kind, basis, occurrences[basis])
        if row is not None:
            rows.append(row)
    return rows


def _judge_dialog_row(grammar: Grammar, row: DialogRow) -> tuple[bool, str | None]:
    """Whether the row's text is decided inside its context as labelled, and the intent it was decided to (None: abstained)."""
    declared = grammar.contexts.get(row.context)
    if declared is None:   # a row for a context the grammar does not declare is a wrong row, never a skipped one
        return False, f"unknown context {row.context}"
    decision = grammar.decide(row.text, context=ActiveContext(row.context, row.values if row.values is not None else declared.sample))
    got = decision.intent if decision else None
    in_dialog = got is not None and any(intent.contexts and intent.id == got for intent in grammar.intents)
    return ((not in_dialog) if row.outside else got == row.intent), got


def _safety_reading(grammar: Grammar, context_id: str, phrase: str, intent: str) -> str | None:
    """The failure message when an emergency phrase does not decide to the safety intent inside the context, else None."""
    decision = grammar.decide(phrase, min_priority=0, context=ActiveContext(context_id, grammar.contexts[context_id].sample))
    if decision is not None and decision.intent == intent:
        return None
    return f"SAFETY: «{phrase}» inside {context_id} reads as {decision.intent if decision else 'nothing'}, not {intent}"


def _dialog_safety(grammar: Grammar) -> dict[str, Any]:
    """The safety set of the grammar (`gate.safety`) decided inside every context."""
    safety = (grammar.gate or {}).get("safety") or {}
    safety_failures: list[str] = []
    checked = 0
    for context_id in grammar.contexts:
        for phrase in safety.get("phrases") or ():
            checked += 1
            failure = _safety_reading(grammar, context_id, phrase, safety["intent"])
            if failure is not None:
                safety_failures.append(failure)
    return {"checked": checked, "failures": safety_failures}


def dialog_metrics(grammar: Grammar, rows: list[DialogRow], *, include_text: bool = True) -> dict[str, Any]:
    """Every dialog row decided inside its context, and the safety set of the grammar (`gate.safety`) decided inside EVERY
    context: no context may keep an emergency phrase from `refuse.emergency`."""
    failures: list[dict[str, Any]] = []
    kinds: dict[str, dict[str, int]] = {}
    contexts: dict[str, dict[str, int]] = {}
    for row in rows:
        ok, got = _judge_dialog_row(grammar, row)
        for table, key in ((kinds, row.kind), (contexts, row.context)):
            entry = table.setdefault(key, {"n": 0, "correct": 0})
            entry["n"] += 1
            entry["correct"] += ok
        if not ok:
            failures.append({"id": row.id, "kind": row.kind, "context": row.context, "intent": row.intent, "got": got,
                             **({"text": row.text} if include_text else {})})
    return {"rows": len(rows), "kinds": dict(sorted(kinds.items())), "contexts": dict(sorted(contexts.items())), "failures": failures,
            "safety": _dialog_safety(grammar)}


def _load_replay(path: Path) -> dict[str, tuple[bool, str | None]] | None:
    """What the deployed quasar layer answers without a model, per phrase: (handled, via). tools/replay_quasar.mts writes
    it; None when the file is absent."""
    if not path.exists():
        return None
    handled: dict[str, tuple[bool, str | None]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            via = record.get("via")
            handled[str(record["text"]).strip()] = (bool(record["handled"]), via if isinstance(via, str) else None)
    return handled


def replay_tier(via: str | None) -> str:
    """The tier of the quasar container's answer from the replay's `via`: `fast-confirm` asked for a «да», `fast-clarify`
    asked a question, `fast-query` answered; the other handlers (media keys, apps, music, end of dialog) acted, arithmetic
    answered. An unknown route is taken for an action."""
    if via and via.startswith("fast-") and via[5:] in TIERS:
        return via[5:]
    return "query" if via == "math" else "execute"


def _quasar_handled(source: str, record: dict[str, Any]) -> tuple[bool, str | None]:
    """Without a replay file: the corpus' own record of whether the container answered, and the tier it recorded (a real
    row's `verdict`; a synthetic row's expected tier, the only trace of one)."""
    if source == "synthetic":
        expect = record.get("expect") or {}
        tier = expect.get("tier")
        return expect.get("kind") not in (None, "llm"), f"fast-{tier}" if tier in TIERS else None
    handled = record.get("layer") == "L1" and record.get("verdict") in ("ok", "clarify")
    return handled, "fast-clarify" if record.get("verdict") == "clarify" else None


def _truth(source: str, record: dict[str, Any]) -> str:
    if source == "synthetic":
        expect = record.get("expect") or {}
        return str(expect.get("domain") or expect.get("kind") or "")
    return str(record.get("truth") or "")


# ── evaluation ──────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    row: Row
    decision: Decision | None
    outcome: str
    micros: int


# The corpus writes the action it expects as «kind:value». The forms below map to the executor and the argument that
# carry them in a grammar decision; `on:`/`off:` name a household entity the grammar cannot know, so only the state is
# compared. Any other form (`brain:…`) has no exact mapping and leaves the row label-only.
_ACTION_ARGUMENT = {"volume": ("speaker.volume", "level"), "setpoint": ("ha.climate", "temperature"),
                    "brightness": ("ha.light_brightness", "percent")}
_SWITCH_EXECUTORS = ("ha.light", "ha.device")
# Decisions of the quasar container, not of the grammar (`intent_route.measured_route`). The replay records that the
# container answered and how (`via`, so the tier), never with which label or arguments: such a decision carries no labels
# and is judged on its observed tier alone, not on a label copied from the row's expectation.
OPAQUE_INTENTS = frozenset(("quasar.handled",))


def _action_matcher(spec: str) -> Callable[[dict[str, Any]], bool] | None:
    kind, _, value = spec.partition(":")
    if kind in _ACTION_ARGUMENT:
        executor, argument = _ACTION_ARGUMENT[kind]
        try:
            wanted = float(value)
        except ValueError:
            return None
        if not math.isfinite(wanted):
            return None

        def number_matches(action: dict[str, Any]) -> bool:
            got = action["args"].get(argument)
            return action["executor"] == executor and isinstance(got, (int, float)) and not isinstance(got, bool) and got == wanted
        return number_matches
    if kind in ("on", "off"):
        return lambda action: action["executor"] in _SWITCH_EXECUTORS and action["args"].get("state") == kind
    return None


def meets_expectation(expect: dict[str, Any], decision: Decision) -> bool:
    """Whether a decision with the right label also has the tier and action the corpus row expects.

    The tier is compared when it names a grammar tier (`llm` is no decision: label-only). The action is compared by the
    mapping above, over every part of a compound; a form without an exact mapping makes the whole row label-only. A
    decision of the quasar container (`OPAQUE_INTENTS`) has an observed tier but no observed arguments: only the tier is compared."""
    tier = expect.get("tier")
    if decision.intent in OPAQUE_INTENTS:
        return tier not in TIERS or decision.tier == tier
    spec = expect.get("action")
    matcher = _action_matcher(str(spec)) if spec else None
    if spec and matcher is None:
        return True
    if tier in TIERS and decision.tier != tier:
        return False
    if matcher is None:
        return True
    return any(part.action is not None and matcher(part.action) for part in (decision.parts or (decision,)))


def classify(row: Row, decision: Decision | None, grammar: Grammar) -> str:
    cfg = grammar.backtest
    unaddressed: dict[str, str] = cfg.get("unaddressed") or {}
    brain: set[str] = set(cfg.get("brain_labels") or ())
    answered = decision is not None
    if row.truth in unaddressed:
        if answered and decision.tier == "drop":
            return "dropped"
        if answered:
            return "false_trigger"
        return "no_action" if unaddressed[row.truth] == "no_action" else "leak"
    if row.truth in brain:
        return "overreach" if answered else "brain_ok"
    if not answered:
        return "missed"
    if decision.tier == "drop" or (decision.intent not in OPAQUE_INTENTS and row.truth not in decision.labels):
        return "wrong"
    return "correct" if row.expect is None or meets_expectation(row.expect, decision) else "wrong"


Decider = Callable[[Row], Decision | None]


def evaluate(grammar: Grammar, rows: list[Row], decide: Decider | None = None) -> list[Outcome]:
    """Run every row through `decide` (default: the bare grammar; the adapter routes live in cli.py)."""
    decide = decide or (lambda row: grammar.decide(row.text))
    decide(Row("warmup", "прогрев", "", "real", "dev"))  # the first call pays lazy costs; keep them out of the latency numbers
    results: list[Outcome] = []
    for row in rows:
        started = time.perf_counter_ns()
        decision = decide(row)
        micros = (time.perf_counter_ns() - started) // 1000
        results.append(Outcome(row, decision, classify(row, decision, grammar), micros))
    return results


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _acts(decision: Decision) -> bool:
    """Whether a decision moves something: its own tier, or any clause of a compound, is an acting one."""
    return decision.tier in ACTING_TIERS or any(_acts(part) for part in decision.parts)


def metrics(outcomes: list[Outcome], grammar: Grammar) -> dict[str, Any]:
    unaddressed = set((grammar.backtest.get("unaddressed") or {}))
    addressed = [o for o in outcomes if o.row.truth not in unaddressed]
    quiet = [o for o in outcomes if o.row.truth in unaddressed]
    answered = [o for o in addressed if o.decision is not None]
    correct = [o for o in addressed if o.outcome == "correct"]
    completed = [o for o in correct if o.decision.tier in COMPLETING_TIERS]
    wrong = [o for o in addressed if o.outcome in ("wrong", "overreach")]
    wrong_actions = [o for o in wrong if o.decision is not None and _acts(o.decision)]
    micros = [o.micros for o in outcomes]
    by_tier: dict[str, int] = {}
    for o in answered:
        by_tier[o.decision.tier] = by_tier.get(o.decision.tier, 0) + 1
    return {
        "rows": len(outcomes),
        "addressed": len(addressed),
        "answered_without_llm": len(answered),
        "coverage": _pct(len(answered), len(addressed)),
        "correct": len(correct),
        "correct_rate": _pct(len(correct), len(addressed)),
        "accuracy_of_answered": _pct(len(correct), len(answered)),
        "completed": len(completed),
        "completed_rate": _pct(len(completed), len(addressed)),
        "wrong": len(wrong),
        "wrong_actions": len(wrong_actions),
        "by_tier": by_tier,
        "unaddressed": len(quiet),
        "unaddressed_handled": sum(1 for o in quiet if o.outcome in ("dropped", "no_action")),
        "false_triggers": sum(1 for o in quiet if o.outcome == "false_trigger"),
        "leaks_to_llm": sum(1 for o in quiet if o.outcome == "leak"),
        "latency_us": {"p50": int(statistics.median(micros)) if micros else 0, "p95": _percentile(micros, 0.95),
                       "max": max(micros, default=0)},
    }


def per_truth(outcomes: list[Outcome]) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {}
    for o in outcomes:
        entry = table.setdefault(o.row.truth, {"n": 0, "answered": 0, "correct": 0})
        entry["n"] += 1
        entry["answered"] += o.decision is not None
        entry["correct"] += o.outcome in GOOD_OUTCOMES
    return dict(sorted(table.items()))


def per_intent(outcomes: list[Outcome]) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {}
    for o in outcomes:
        if o.decision is None:
            continue
        entry = table.setdefault(o.decision.intent, {"matched": 0, "correct": 0, "wrong": 0})
        entry["matched"] += 1
        entry["correct"] += o.outcome in GOOD_OUTCOMES
        entry["wrong"] += o.outcome in ("wrong", "overreach", "false_trigger")
    return dict(sorted(table.items()))


def _split_tables(grammar: Grammar, outcomes: list[Outcome]) -> dict[str, dict[str, Any]]:
    """The metrics of every non-empty source/split combination."""
    splits: dict[str, dict[str, Any]] = {}
    for source in ("real", "synthetic", "all"):
        for split in ("dev", "holdout", "all"):
            chosen = [o for o in outcomes if source in (o.row.source, "all") and split in (o.row.split, "all")]
            if chosen:
                splits[f"{source}/{split}"] = metrics(chosen, grammar)
    return splits


def _dev_misses(outcomes: list[Outcome], include_text: bool) -> list[dict[str, Any]]:
    return [{"id": o.row.id, "split": "dev", "source": o.row.source, "truth": o.row.truth, "outcome": o.outcome,
             "intent": o.decision.intent if o.decision else None,
             **({"text": o.row.text} if include_text else {})}
            for o in outcomes if o.row.split == "dev" and o.outcome not in GOOD_OUTCOMES]


def _holdout_misses(outcomes: list[Outcome]) -> list[dict[str, Any]]:
    return [{"id": o.row.id, "split": "holdout", "source": o.row.source, "truth": o.row.truth,
             "outcome": o.outcome, "intent": o.decision.intent if o.decision else None}
            for o in outcomes if o.row.split == "holdout" and o.outcome not in GOOD_OUTCOMES]


def build_report(grammar: Grammar, rows: list[Row], manifest: list[dict[str, Any]], *, include_text: bool = True,
                 previous: dict[str, Any] | None = None, decide: Decider | None = None,
                 route: str = "grammar", require_baseline: bool = False,
                 dialog_rows: list[DialogRow] | None = None) -> dict[str, Any]:
    outcomes = evaluate(grammar, rows, decide)
    unaddressed = set(grammar.backtest.get("unaddressed") or {})
    splits = _split_tables(grammar, outcomes)
    misses = _dev_misses(outcomes, include_text)
    holdout_misses = _holdout_misses(outcomes)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "split": {"version": SPLIT_VERSION, "holdout_percent": holdout_percent(grammar)},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "route": route,
        "grammar": {"sha256": grammar.sha256, "intents": len(grammar.intents),
                    "patterns": sum(len(i.patterns) for i in grammar.intents)},
        "corpus": manifest,
        "splits": splits,
        "per_truth": per_truth(outcomes),
        "per_intent": per_intent(outcomes),
        "dev_misses": misses,
        "holdout_misses": holdout_misses,
        "rows": {o.row.id: {"outcome": o.outcome, "split": o.row.split, "source": o.row.source, "truth": o.row.truth,
                            "intent": o.decision.intent if o.decision else None,
                            "addressed": o.row.truth not in unaddressed,
                            "completed": o.outcome == "correct" and o.decision.tier in COMPLETING_TIERS}
                 for o in outcomes},
        "self_check_failures": grammar.self_check(),
    }
    if dialog_rows is not None:
        report["dialog"] = dialog_metrics(grammar, dialog_rows, include_text=include_text)
    report["compare"] = compare(report, previous)
    report["baseline"] = {"required": require_baseline, "usable": report["compare"] is not None
                          and not report["compare"].get("incompatible")}
    report["goal"] = goal_status(report, grammar)
    report["legacy_floor"] = legacy_floor_status(report, grammar)
    report["gate"] = check_gate(report, grammar)
    return report


def _rates(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    addressed = [row for row in rows if row["addressed"]]
    return {"coverage": _pct(sum(row["intent"] is not None for row in addressed), len(addressed)),
            "correct_rate": _pct(sum(row["outcome"] == "correct" for row in addressed), len(addressed)),
            "completed_rate": _pct(sum(row["completed"] for row in addressed), len(addressed))}


# How bad an outcome is, for the per-row ratchet: a row may get better, never worse. Answering a row wrongly is worse than
# missing it, and missing it is worse than handling it; turning a wrong answer into an abstention is an improvement.
_BAD_OUTCOMES = frozenset(("wrong", "overreach", "false_trigger"))


def _rank(outcome: str) -> int:
    if outcome in GOOD_OUTCOMES:
        return 2
    return 0 if outcome in _BAD_OUTCOMES else 1


def compare(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """The per-row ratchet against an earlier report, over the rows present in BOTH (same id, same label, same split side).

    `regressed`: rows whose outcome got worse (handled -> missed/wrong, missed -> wrong). Nothing is sampled: the same phrases
    meet the same deterministic engine, so any worsening is a real change and no tolerance applies. New rows only add to
    n. `lost_rows` (a baseline row gone from the corpus) and `relabelled` (same row, other label) are reported because
    deleting or relabelling a row would otherwise hide a regression; they fail the gate. A previous report of another
    schema, route or split definition cannot be compared: `incompatible` says why (a rebaseline is then an explicit act)."""
    if not previous:
        return None
    if previous.get("schema") != REPORT_SCHEMA or "rows" not in previous:
        return {"incompatible": "the previous report has another schema", "previous_grammar": None, "regressed": [],
                "fixed": 0, "lost_rows": {}, "relabelled": [], "deltas": {}}
    problems = [what for what, same in (("route", previous.get("route") == current.get("route")),
                                        ("split definition", previous.get("split") == current.get("split"))) if not same]
    if problems:
        return {"incompatible": "the previous report differs in " + " and ".join(problems),
                "previous_grammar": (previous.get("grammar") or {}).get("sha256"), "regressed": [], "fixed": 0,
                "lost_rows": {}, "relabelled": [], "deltas": {}}
    regressed, fixed, relabelled = [], [], []
    shared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for row_id, now in current["rows"].items():
        before = previous["rows"].get(row_id)
        if before is None:
            continue
        if before.get("truth") is not None and before["truth"] != now["truth"]:
            relabelled.append(row_id)
            continue
        shared.append((row_id, now, before))
        was, is_ = _rank(before["outcome"]), _rank(now["outcome"])
        if is_ < was:
            regressed.append({"id": row_id, "split": now["split"], "from": before["outcome"], "to": now["outcome"]})
        elif is_ > was:
            fixed.append(row_id)
    deltas: dict[str, Any] = {}
    for name in current["splits"]:
        source, split = name.split("/")
        chosen = [(now, before) for _id, now, before in shared
                  if before["split"] == now["split"] and source in (now["source"], "all") and split in (now["split"], "all")]
        if not chosen:
            continue
        now_rates = _rates([now for now, _ in chosen])
        before_rates = _rates([before for _, before in chosen])
        deltas[name] = {"rows": len(chosen), **{key: _delta(now_rates[key], before_rates[key]) for key in now_rates}}
    lost: dict[str, int] = {}
    for row_id, before in previous["rows"].items():
        if row_id not in current["rows"]:
            lost[before["source"]] = lost.get(before["source"], 0) + 1
    return {"previous_grammar": (previous.get("grammar") or {}).get("sha256"), "regressed": regressed,
            "fixed": len(fixed), "lost_rows": lost, "relabelled": relabelled, "deltas": deltas}


def _delta(now: float | None, before: float | None) -> float | None:
    return None if now is None or before is None else round(now - before, 4)


def check_gate(report: dict[str, Any], grammar: Grammar) -> dict[str, Any]:
    """The KPI gate of the grammar: the rules a change must keep. `failures` is empty when it holds.

    The limits are totals over the whole corpus (`all/all`); a coverage floor names its own split and fails when that
    split is absent or has nothing addressed, so a corpus without real rows cannot pass a `real/holdout` floor."""
    rules: dict[str, Any] = grammar.gate or {}
    failures: list[str] = []
    total = report["splits"].get("all/all")
    if total is not None:
        if rules.get("max_wrong_actions") is not None and total["wrong_actions"] > rules["max_wrong_actions"]:
            failures.append(f"{total['wrong_actions']} wrong actions (max {rules['max_wrong_actions']})")
        if rules.get("max_false_triggers") is not None and total["false_triggers"] > rules["max_false_triggers"]:
            failures.append(f"{total['false_triggers']} false triggers (max {rules['max_false_triggers']})")
        if rules.get("max_p95_us") is not None and total["latency_us"]["p95"] > rules["max_p95_us"]:
            failures.append(f"p95 {total['latency_us']['p95']} us (max {rules['max_p95_us']})")
    for name, minimum in (rules.get("min_coverage") or {}).items():
        split = report["splits"].get(name)
        if split is None:
            failures.append(f"{name}: no such split in this corpus (coverage floor {minimum})")
        elif split["coverage"] is None:
            failures.append(f"{name}: coverage not measurable, nothing addressed (coverage floor {minimum})")
        elif split["coverage"] < minimum:
            failures.append(f"{name}: coverage {split['coverage']} < {minimum}")
    if report["self_check_failures"]:
        failures.append(f"{len(report['self_check_failures'])} pinned examples/negatives fail")
    failures.extend(_safety_failures(grammar, rules.get("safety") or {}))
    failures.extend(_dialog_failures(report.get("dialog"), rules.get("dialog")))
    failures.extend(_holdout_failures(report, rules.get("holdout") or {}))
    compare_ = report.get("compare")
    baseline = report.get("baseline") or {}
    if baseline.get("required") and compare_ is None:
        failures.append("no baseline: the first run needs an explicit --init-baseline REASON, a missing latest.json is not a pass")
    if compare_:
        if compare_.get("incompatible"):
            failures.append(f"baseline unusable: {compare_['incompatible']} (rebaseline explicitly, never by deleting latest.json)")
        if compare_["regressed"]:
            failures.append(f"{len(compare_['regressed'])} rows got worse than in the previous report")
        rebaselined = baseline.get("rebaselined")   # the owner accepted the lost and relabelled rows of this run (`accept_rebaseline`)
        if compare_["lost_rows"] and not rebaselined:
            failures.append(f"{sum(compare_['lost_rows'].values())} rows of the previous report are gone from the corpus (rows are only added; "
                            "an owner's rebaseline is `--init-baseline REASON`)")
        if compare_["relabelled"] and not rebaselined:
            failures.append(f"{len(compare_['relabelled'])} rows changed their label since the previous report (an owner's rebaseline "
                            "is `--init-baseline REASON`)")
    return {"ok": not failures, "failures": failures}


def accept_rebaseline(report: dict[str, Any], grammar: Grammar, reason: str) -> bool:
    """The owner's explicit rebaseline against a COMPATIBLE baseline: the corpus lost or relabelled rows (a label fixed, a
    file renamed, a row removed), which the ratchet reports and fails for ever. The reason and the counts are written into the
    report's baseline block and the gate is evaluated again without those two checks; every other check, the per-row
    regressions included, still applies. False (and nothing changed) when there is no compatible baseline or nothing was
    lost or relabelled: then no rebaseline is needed and `--init-baseline` is refused."""
    compare_ = report.get("compare")
    if not compare_ or compare_.get("incompatible"):
        return False
    lost, relabelled = sum(compare_["lost_rows"].values()), len(compare_["relabelled"])
    if not lost and not relabelled:
        return False
    report["baseline"]["init_reason"] = reason
    report["baseline"]["rebaselined"] = {"lost_rows": lost, "relabelled": relabelled}
    report["gate"] = check_gate(report, grammar)
    return True


def _safety_failures(grammar: Grammar, rule: dict[str, Any]) -> list[str]:
    """Phrases that must always be read as the emergency intent: independent of the corpus, of coverage and of every other rule."""
    if not rule:
        return []
    wanted = rule["intent"]
    failures = []
    for phrase in rule["phrases"]:
        decision = grammar.decide(phrase, min_priority=0)
        if decision is None or decision.intent != wanted:
            failures.append(f"SAFETY: «{phrase}» reads as {decision.intent if decision else 'nothing'}, not {wanted}")
    return failures


def _dialog_failures(block: dict[str, Any] | None, rule: dict[str, Any] | None) -> list[str]:
    """`gate.dialog`: dialog rows decided as labelled (at most `max_wrong` may not be), and the emergency phrases above every context.
    A grammar that asks for the rule and a report without the dialog block fails: a run that skipped them proved nothing."""
    if rule is None:
        return []
    if block is None or not block["rows"]:
        return ["dialog: the report has no dialog rows (the gate measures the repair contexts)"]
    failures: list[str] = []
    if len(block["failures"]) > rule.get("max_wrong", 0):
        failures.append(f"dialog: {len(block['failures'])} of {block['rows']} dialog rows are not decided as labelled (max {rule.get('max_wrong', 0)})")
    failures.extend(block["safety"]["failures"])
    return failures


def _holdout_failures(report: dict[str, Any], rule: dict[str, Any]) -> list[str]:
    """Absolute checks on the held-out split, which is tiny (tens of rows) and so is NOT gated by a hand-picked number at today's n.

    The held-out regression gate is the per-row ratchet of `compare` (a row of the previous report may not get worse), applied to
    every split. On top of it, with enough addressed rows: from `ci_min_addressed` rows the split fails when the whole 95 % Wilson
    interval of coverage or correctness lies below the floor (the rate is clearly under it, one phrase cannot flip that);
    from `floor_min_addressed` rows the point floor applies too. Duplicate phrases hold identical outcomes, so n counts rows
    the corpus loader already de-duplicates by id."""
    failures: list[str] = []
    floor = rule.get("floor") or {}
    for name in rule.get("splits") or []:
        split = report["splits"].get(name)
        if split is None or split["coverage"] is None:
            failures.append(f"{name}: no addressed rows (the held-out checks need a held-out split)")
            continue
        for key, label, count in (("coverage", "coverage", split["answered_without_llm"]), ("correct_rate", "correct", split["correct"])):
            minimum = floor.get(label)
            if minimum is None:
                continue
            if split["addressed"] >= rule.get("ci_min_addressed", 10 ** 9):
                low, high = _wilson(count, split["addressed"])
                if high < minimum:
                    failures.append(f"{name}: {label} {split[key]} is clearly below {minimum} (95% interval {low}-{high}, {split['addressed']} addressed rows)")
            if split["addressed"] >= rule.get("floor_min_addressed", 10 ** 9) and split[key] < minimum:
                failures.append(f"{name}: {label} {split[key]} < {minimum} ({split['addressed']} addressed rows)")
    return failures


def legacy_floor_status(report: dict[str, Any], grammar: Grammar) -> dict[str, Any] | None:
    """The superseded absolute held-out floor, kept visible so a green gate can never be read as that floor being met."""
    legacy = ((grammar.gate or {}).get("holdout") or {}).get("legacy_floor")
    split = report["splits"].get(legacy["split"]) if legacy else None
    if not legacy or split is None or split["coverage"] is None:
        return None
    return {"split": legacy["split"], "floor": legacy["coverage"], "coverage": split["coverage"], "met": split["coverage"] >= legacy["coverage"],
            "addressed": split["addressed"]}


def _wilson(part: int, whole: int, z: float = 1.96) -> tuple[float, float] | None:
    if not whole:
        return None
    p = part / whole
    denom = 1 + z * z / whole
    centre = (p + z * z / (2 * whole)) / denom
    half = z * math.sqrt(p * (1 - p) / whole + z * z / (4 * whole * whole)) / denom
    return round(centre - half, 4), round(centre + half, 4)


def goal_status(report: dict[str, Any], grammar: Grammar) -> dict[str, Any] | None:
    """The product target (`gate.goal`: split, coverage, correct) as a tracked number with its Wilson interval and the
    gap, on the whole split and on its held-out part. Reported, never gated: n is too small to gate a target on."""
    goal = (grammar.gate or {}).get("goal")
    if not goal:
        return None
    status: dict[str, Any] = {"split": goal["split"], "target": {k: goal[k] for k in ("coverage", "correct") if k in goal}}
    for label, name in (("all", goal["split"]), ("holdout", goal["split"].split("/")[0] + "/holdout")):
        split = report["splits"].get(name)
        if split is None or not split["addressed"]:
            continue
        entry: dict[str, Any] = {"addressed": split["addressed"]}
        for key, field_name, count in (("coverage", "coverage", split["answered_without_llm"]), ("correct", "correct_rate", split["correct"])):
            if key in status["target"]:
                entry[key] = {"value": split[field_name], "ci95": _wilson(count, split["addressed"]),
                              "gap": round(status["target"][key] - split[field_name], 4), "met": split[field_name] >= status["target"][key]}
        status[label] = entry
    return status


# ── persistence ─────────────────────────────────────────────────────────────

ENV_REPORT_DIR = "AISIS_INTENT_REPORT_DIR"


def reports_dir() -> Path:
    """Reports stay out of the corpus directory: deploy.sh copies that whole directory into the quasar containers."""
    return Path(os.environ.get(ENV_REPORT_DIR) or "~/.local/share/aisis/intent-reports").expanduser()


def archive_path() -> Path:
    """A report name no other run can share: the clock in nanoseconds and the process id, not just the second."""
    return reports_dir() / f"backtest-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}-{os.getpid()}.json"


def load_previous(path: Path | None) -> dict[str, Any] | None:
    candidate = path or reports_dir() / "latest.json"
    if not candidate.exists():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


@contextlib.contextmanager
def baseline_lock() -> Iterator[None]:
    """Serialises «read the baseline, compare, promote» between concurrent back-tests (advisory lock on the folder)."""
    directory = reports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_atomically(path: Path, payload: str) -> None:
    """Temp file beside the target, then `os.replace`: a reader sees the old or the new document, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(payload)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
        raise


def save_report(report: dict[str, Any], destination: Path, *, latest: Path | None) -> None:
    """Write the report to `destination` and, when `latest` is given, make it the baseline of the next run. The caller
    passes `latest` only for a report whose gate held: a failed run is archived but never becomes the baseline."""
    payload = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    _write_atomically(destination, payload)
    if latest is not None:
        _write_atomically(latest, payload)


def summary_lines(report: dict[str, Any]) -> list[str]:
    """The numbers a person reads first (no household text)."""
    lines = [f"grammar {report['grammar']['sha256'][:10]}: {report['grammar']['intents']} intents, "
             f"{report['grammar']['patterns']} patterns"]
    for name in ("real/dev", "real/holdout", "real/all", "synthetic/all", "all/all"):
        split = report["splits"].get(name)
        if not split:
            continue
        lines.append(f"{name:14} n={split['rows']:4} addressed={split['addressed']:4} coverage={_fmt(split['coverage'])} "
                     f"correct={_fmt(split['correct_rate'])} completed={_fmt(split['completed_rate'])} "
                     f"wrong_actions={split['wrong_actions']} false_triggers={split['false_triggers']} "
                     f"p95={split['latency_us']['p95']}us")
    dialog = report.get("dialog")
    if dialog:
        kinds = ", ".join(f"{kind} {entry['correct']}/{entry['n']}" for kind, entry in dialog["kinds"].items())
        lines.append(f"dialog rows n={dialog['rows']}: {kinds}; {len(dialog['failures'])} wrong; emergency phrase above every context "
                     f"{dialog['safety']['checked'] - len(dialog['safety']['failures'])}/{dialog['safety']['checked']}")
    if report.get("compare"):
        compare_ = report["compare"]
        lines.append(f"vs previous: {len(compare_['regressed'])} worse, {compare_['fixed']} better, "
                     f"{sum(compare_['lost_rows'].values())} lost, {len(compare_['relabelled'])} relabelled")
    rebaselined = (report.get("baseline") or {}).get("rebaselined")
    if rebaselined:
        lines.append(f"rebaselined by the owner ({report['baseline']['init_reason']}): {rebaselined['lost_rows']} lost, "
                     f"{rebaselined['relabelled']} relabelled rows accepted")
    legacy = report.get("legacy_floor")
    if legacy:
        lines.append(f"legacy absolute floor {legacy['split']} coverage >= {legacy['floor']}: {legacy['coverage']} on {legacy['addressed']} addressed rows, "
                     f"{'met' if legacy['met'] else 'NOT met (superseded by the per-row ratchet, shown so green is not read as met)'}")
    lines.append("gate: " + ("ok" if report["gate"]["ok"] else "FAIL " + "; ".join(report["gate"]["failures"])))
    return lines


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value * 100:5.1f}%"
