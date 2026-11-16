"""Corpus back-test of the intent grammar.

Reads the private labelled corpora (real spoken utterances, hand-written synthetic phrases), runs every row through
`Grammar.decide` and reports coverage, accuracy, false triggers and latency, per source, per split and per intent.
A report can be compared with the previous one: every row that used to be handled correctly and no longer is
counts as a regression, and the gate (grammar `gate:`) turns that into an exit code.

The held-out split is frozen by a hash of the canonical text (all copies of one phrase land on the same side), so a
grammar edited by looking at the dev rows cannot see the held-out ones. `dev` is what you tune on; `holdout` is the
honest number. Texts of held-out rows never appear in a report: only their ids and outcomes.

Corpus files (JSON lines) under the corpus directory:
  real-voice-*.jsonl   {text, truth, ts?, verdict?, labeller?}        source "real"
  *-labelled.jsonl     the same schema                                source "real"
  intents-ru.jsonl     {text, expect: {kind, domain, ...}}            source "synthetic"
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .grammar import ACTING_TIERS, Decision, Grammar
from .normalize import normalise

DEFAULT_CORPUS_DIR = "~/.local/share/aisis/corpus"
ENV_CORPUS_DIR = "AISIS_INTENT_CORPUS_DIR"
SPLIT_SALT = "aisis-intents-v1"
GOOD_OUTCOMES = frozenset(("correct", "dropped", "brain_ok", "no_action"))
COMPLETING_TIERS = ("execute", "query", "canned")


@dataclass(frozen=True)
class Row:
    id: str
    text: str
    truth: str
    source: str
    split: str


def corpus_dir(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get(ENV_CORPUS_DIR) or DEFAULT_CORPUS_DIR).expanduser()


def split_of(text: str, holdout_percent: int, grammar: Grammar) -> str:
    """Stable dev/holdout assignment from the canonical text, so repeated phrases stay on one side."""
    key = normalise(text, grammar.canon).text or text.strip().lower()
    digest = hashlib.sha256((SPLIT_SALT + key).encode("utf-8")).digest()
    return "holdout" if int.from_bytes(digest[:4], "big") % 100 < holdout_percent else "dev"


def _row_id(source: str, index: int, record: dict[str, Any], text: str) -> str:
    basis = f"{source}|{record.get('ts', index)}|{text}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def load_rows(directory: Path, grammar: Grammar) -> tuple[list[Row], list[dict[str, Any]]]:
    """All rows of every known corpus file under `directory`, plus the file manifest (name, sha256, rows)."""
    percent = int(grammar.backtest.get("holdout_percent", 20))
    rows: list[Row] = []
    manifest: list[dict[str, Any]] = []
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
        for index, line in enumerate(data.decode("utf-8").splitlines()):
            if not line.strip():
                continue
            record = json.loads(line)
            text = str(record.get("text", "")).strip()
            truth = _truth(source, record)
            if not text or not truth:
                continue
            rows.append(Row(_row_id(source, index, record, text), text, truth, source, split_of(text, percent, grammar)))
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
    if decision.tier == "drop":
        return "wrong"
    return "correct" if row.truth in decision.labels else "wrong"


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


def metrics(outcomes: list[Outcome], grammar: Grammar) -> dict[str, Any]:
    unaddressed = set((grammar.backtest.get("unaddressed") or {}))
    addressed = [o for o in outcomes if o.row.truth not in unaddressed]
    quiet = [o for o in outcomes if o.row.truth in unaddressed]
    answered = [o for o in addressed if o.decision is not None]
    correct = [o for o in addressed if o.outcome == "correct"]
    completed = [o for o in correct if o.decision.tier in COMPLETING_TIERS]
    wrong = [o for o in addressed if o.outcome in ("wrong", "overreach")]
    wrong_actions = [o for o in wrong if o.decision is not None and o.decision.tier in ACTING_TIERS]
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
        "schema": 1,
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
                            "intent": o.decision.intent if o.decision else None} for o in outcomes},
        "self_check_failures": grammar.self_check(),
    }
    report["compare"] = compare(report, previous)
    report["gate"] = check_gate(report, grammar)
    return report


def compare(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    """Regressions against an earlier report: rows that were good and are not, and the headline deltas."""
    if not previous or "rows" not in previous:
        return None
    regressed, fixed = [], []
    for row_id, now in current["rows"].items():
        before = previous["rows"].get(row_id)
        if before is None:
            continue
        was, is_ = before["outcome"] in GOOD_OUTCOMES, now["outcome"] in GOOD_OUTCOMES
        if was and not is_:
            regressed.append({"id": row_id, "split": now["split"], "from": before["outcome"], "to": now["outcome"]})
        elif is_ and not was:
            fixed.append(row_id)
    deltas: dict[str, Any] = {}
    for name, now in current["splits"].items():
        before = (previous.get("splits") or {}).get(name)
        if before:
            deltas[name] = {key: _delta(now.get(key), before.get(key)) for key in ("coverage", "correct_rate", "completed_rate")}
    return {"previous_grammar": (previous.get("grammar") or {}).get("sha256"), "regressed": regressed,
            "fixed": len(fixed), "deltas": deltas}


def _delta(now: float | None, before: float | None) -> float | None:
    return None if now is None or before is None else round(now - before, 4)


def check_gate(report: dict[str, Any], grammar: Grammar) -> dict[str, Any]:
    """The KPI gate of the grammar: the rules a change must keep. `failures` is empty when it holds."""
    rules: dict[str, Any] = grammar.gate or {}
    failures: list[str] = []
    for name, split in report["splits"].items():
        if name.endswith("/all") and name.startswith("all/"):
            continue
        if rules.get("max_wrong_actions") is not None and split["wrong_actions"] > rules["max_wrong_actions"]:
            failures.append(f"{name}: {split['wrong_actions']} wrong actions (max {rules['max_wrong_actions']})")
        if rules.get("max_false_triggers") is not None and split["false_triggers"] > rules["max_false_triggers"]:
            failures.append(f"{name}: {split['false_triggers']} false triggers (max {rules['max_false_triggers']})")
        if rules.get("max_p95_us") is not None and split["latency_us"]["p95"] > rules["max_p95_us"]:
            failures.append(f"{name}: p95 {split['latency_us']['p95']} us (max {rules['max_p95_us']})")
    floor = rules.get("min_coverage") or {}
    for name, minimum in floor.items():
        split = report["splits"].get(name)
        if split and split["coverage"] is not None and split["coverage"] < minimum:
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


def load_previous(path: Path | None, corpus: Path) -> dict[str, Any] | None:
    candidate = path or reports_dir(corpus) / "latest.json"
    if not candidate.exists():
        return None
    return json.loads(candidate.read_text(encoding="utf-8"))


def save_report(report: dict[str, Any], destination: Path, *, latest: Path | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    destination.write_text(payload, encoding="utf-8")
    if latest is not None:
        latest.write_text(payload, encoding="utf-8")


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
