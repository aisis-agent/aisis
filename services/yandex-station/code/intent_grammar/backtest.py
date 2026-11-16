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

from .grammar import ACTING_TIERS, TIERS, Decision, Grammar

DEFAULT_CORPUS_DIR = "~/.local/share/aisis/corpus"
ENV_CORPUS_DIR = "AISIS_INTENT_CORPUS_DIR"
# Version of the split definition below. Bump it with ANY change of the canonical form, the salt or the hash: rows then
# no longer belong to the split of older reports, and the comparison only trusts rows whose side did not change.
SPLIT_VERSION = 1
SPLIT_SALT = f"aisis-intents-split-v{SPLIT_VERSION}|"
REPORT_SCHEMA = 2
GOOD_OUTCOMES = frozenset(("correct", "dropped", "brain_ok", "no_action"))
COMPLETING_TIERS = ("execute", "query", "canned")


@dataclass(frozen=True)
class Row:
    id: str
    text: str
    truth: str
    source: str
    split: str
    expect: dict[str, Any] | None = field(default=None, compare=False)   # the corpus' own tier/action, when it has one


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


def load_rows(directory: Path, grammar: Grammar) -> tuple[list[Row], list[dict[str, Any]]]:
    """All rows of every known corpus file under `directory`, plus the file manifest (name, sha256, rows)."""
    percent = holdout_percent(grammar)
    rows: list[Row] = []
    manifest: list[dict[str, Any]] = []
    occurrences: dict[str, int] = {}
    for path in sorted(directory.glob("*.jsonl")):
        name = path.name
        if name == "intents-ru.jsonl":
            source = "synthetic"
        elif name.startswith("real-voice-") or name.endswith("-labelled.jsonl"):
            source = "real"
        else:
            continue
        data = path.read_bytes()
        count = 0
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            text = str(record.get("text", "")).strip()
            truth = _truth(source, record)
            if not text or not truth:
                continue
            basis = f"{path.stem}|{record.get('ts', '')}|{split_key(text)}"
            occurrences[basis] = occurrences.get(basis, 0) + 1
            expect = record.get("expect") if source == "synthetic" and isinstance(record.get("expect"), dict) else None
            rows.append(Row(_row_id(basis, occurrences[basis]), text, truth, source, split_of(text, percent), expect))
            count += 1
        manifest.append({"file": name, "source": source, "rows": count, "sha256": hashlib.sha256(data).hexdigest()})
    return rows, manifest


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
    mapping above, over every part of a compound; a form without an exact mapping makes the whole row label-only."""
    spec = expect.get("action")
    matcher = _action_matcher(str(spec)) if spec else None
    if spec and matcher is None:
        return True
    tier = expect.get("tier")
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
    if decision.tier == "drop" or row.truth not in decision.labels:
        return "wrong"
    return "correct" if row.expect is None or meets_expectation(row.expect, decision) else "wrong"


def evaluate(grammar: Grammar, rows: list[Row]) -> list[Outcome]:
    grammar.decide("прогрев")  # first call pays lazy costs; it must not count into the latency numbers
    results: list[Outcome] = []
    for row in rows:
        started = time.perf_counter_ns()
        decision = grammar.decide(row.text)
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


def build_report(grammar: Grammar, rows: list[Row], manifest: list[dict[str, Any]], *, include_text: bool = True,
                 previous: dict[str, Any] | None = None) -> dict[str, Any]:
    outcomes = evaluate(grammar, rows)
    unaddressed = set(grammar.backtest.get("unaddressed") or {})
    splits: dict[str, dict[str, Any]] = {}
    for source in ("real", "synthetic", "all"):
        for split in ("dev", "holdout", "all"):
            chosen = [o for o in outcomes if source in (o.row.source, "all") and split in (o.row.split, "all")]
            if chosen:
                splits[f"{source}/{split}"] = metrics(chosen, grammar)
    dev = [o for o in outcomes if o.row.split == "dev"]
    misses = [{"id": o.row.id, "split": "dev", "source": o.row.source, "truth": o.row.truth, "outcome": o.outcome,
               "intent": o.decision.intent if o.decision else None,
               **({"text": o.row.text} if include_text else {})}
              for o in dev if o.outcome not in GOOD_OUTCOMES]
    holdout_misses = [{"id": o.row.id, "split": "holdout", "source": o.row.source, "truth": o.row.truth,
                       "outcome": o.outcome, "intent": o.decision.intent if o.decision else None}
                      for o in outcomes if o.row.split == "holdout" and o.outcome not in GOOD_OUTCOMES]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "split": {"version": SPLIT_VERSION, "holdout_percent": holdout_percent(grammar)},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "grammar": {"sha256": grammar.sha256, "intents": len(grammar.intents),
                    "patterns": sum(len(i.patterns) for i in grammar.intents)},
        "corpus": manifest,
        "splits": splits,
        "per_truth": per_truth(outcomes),
        "per_intent": per_intent(outcomes),
        "dev_misses": misses,
        "holdout_misses": holdout_misses,
        "rows": {o.row.id: {"outcome": o.outcome, "split": o.row.split, "source": o.row.source,
                            "intent": o.decision.intent if o.decision else None,
                            "addressed": o.row.truth not in unaddressed,
                            "completed": o.outcome == "correct" and o.decision.tier in COMPLETING_TIERS}
                 for o in outcomes},
        "self_check_failures": grammar.self_check(),
    }
    report["compare"] = compare(report, previous)
    report["gate"] = check_gate(report, grammar)
    return report


def _rates(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    addressed = [row for row in rows if row["addressed"]]
    return {"coverage": _pct(sum(row["intent"] is not None for row in addressed), len(addressed)),
            "correct_rate": _pct(sum(row["outcome"] == "correct" for row in addressed), len(addressed)),
            "completed_rate": _pct(sum(row["completed"] for row in addressed), len(addressed))}


def compare(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """Regressions against an earlier report: rows that were good and are not, and the headline deltas.

    Both are computed over the rows present in BOTH reports (and on the same side of the split), so adding unhandled
    rows can never fail the gate and removing rows can never hide a drop. A previous report of another schema has no
    comparable rows: there is nothing to compare with."""
    if not previous or previous.get("schema") != REPORT_SCHEMA or "rows" not in previous:
        return None
    regressed, fixed = [], []
    shared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for row_id, now in current["rows"].items():
        before = previous["rows"].get(row_id)
        if before is None:
            continue
        shared.append((row_id, now, before))
        was, is_ = before["outcome"] in GOOD_OUTCOMES, now["outcome"] in GOOD_OUTCOMES
        if was and not is_:
            regressed.append({"id": row_id, "split": now["split"], "from": before["outcome"], "to": now["outcome"]})
        elif is_ and not was:
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
    return {"previous_grammar": (previous.get("grammar") or {}).get("sha256"), "regressed": regressed,
            "fixed": len(fixed), "deltas": deltas}


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
    compare_ = report.get("compare")
    if compare_:
        if compare_["regressed"]:
            failures.append(f"{len(compare_['regressed'])} previously handled rows regressed")
        holdout = (compare_["deltas"].get("all/holdout") or {}).get("coverage")
        if holdout is not None and holdout < 0:
            failures.append(f"held-out coverage dropped by {-holdout:.4f}")
    return {"ok": not failures, "failures": failures}


# ── persistence ─────────────────────────────────────────────────────────────

def reports_dir(corpus: Path) -> Path:
    return corpus / "reports"


def archive_path(corpus: Path) -> Path:
    """A report name no other run can share: the clock in nanoseconds and the process id, not just the second."""
    return reports_dir(corpus) / f"backtest-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}-{os.getpid()}.json"


def load_previous(path: Path | None, corpus: Path) -> dict[str, Any] | None:
    candidate = path or reports_dir(corpus) / "latest.json"
    if not candidate.exists():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


@contextlib.contextmanager
def baseline_lock(corpus: Path) -> Iterator[None]:
    """Serialises «read the baseline, compare, promote» between concurrent back-tests (advisory lock on the folder)."""
    directory = reports_dir(corpus)
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
    if report.get("compare"):
        compare_ = report["compare"]
        lines.append(f"vs previous: {len(compare_['regressed'])} regressed, {compare_['fixed']} fixed")
    lines.append("gate: " + ("ok" if report["gate"]["ok"] else "FAIL " + "; ".join(report["gate"]["failures"])))
    return lines


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value * 100:5.1f}%"
