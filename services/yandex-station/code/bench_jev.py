#!/usr/bin/env python3
"""Live benchmark of Jev as the speaker's route classifier (needs VERCEL_JEV_KEY in the environment).

    VERCEL_JEV_KEY=... python3 bench_jev.py [--corpus DIR] [--limit N] [--out report.json]
    VERCEL_JEV_KEY=... python3 bench_jev.py --eval docs/research/fast-llm-eval.jsonl [--out report.json]

Every row's transcript is sent once to the Gateway with the four questions the adapter asks (jev.QUESTIONS: utterance, tier,
effort, route).

  corpus mode (default)  the utterance answer is compared with the corpus label mapped to the four kinds: confusion table,
                         accuracy by confidence threshold, latency percentiles.
  --eval FILE            the 149 labelled utterances of the fast-model benchmark (`label` = fast | reprompt | escalate |
                         deterministic): the route question's confusion against the label, wrong-fast counts by threshold,
                         and the tier question's wrong-fast counts next to it. This is the result that belongs with jev.ROUTE.

Never prints a transcript or the key. The report holds no transcripts either. Needs the grammar package's corpus loader
(PyYAML or the bundle) and aiohttp.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import jev
from intent_grammar import backtest
from intent_grammar.grammar import load

BUNDLE = Path(__file__).parent / "intent_grammar" / "grammar" / "ru.json"
GARBLED = {"ASR_GARBAGE", "FRAGMENT"}
NOT_ADDRESSED = {"HALLUCINATION", "AMBIENT"}
KINDS = ("command", "talk", "garbled", "not_addressed")
ROUTES = ("deterministic", "fast", "reprompt", "escalate")
THRESHOLDS = (0.0, 0.5, 0.7, 0.9, 0.95)


def expected_class(truth: str, brain_labels: set[str]) -> str:
    """The four Jev classes a corpus label stands for."""
    if truth in GARBLED:
        return "garbled"
    if truth in NOT_ADDRESSED:
        return "not_addressed"
    if truth in brain_labels or truth in ("smalltalk", "GREETING", "THANKS", "FAREWELL", "WAKE_ONLY"):
        return "talk"
    return "command"


def corpus_rows(corpus: str | None, limit: int | None) -> list[dict]:
    grammar = load(BUNDLE)
    rows, _ = backtest.load_rows(backtest.corpus_dir(corpus), grammar)
    rows = rows[:limit] if limit else rows
    brain = set(grammar.backtest.get("brain_labels") or ())
    return [{"id": row.id, "split": row.split, "source": row.source, "text": row.text,
             "want": expected_class(row.truth, brain), "route": None} for row in rows]


def eval_rows(path: str, limit: int | None) -> list[dict]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = rows[:limit] if limit else rows
    unknown = {row["label"] for row in rows} - set(ROUTES)
    if unknown:
        raise SystemExit(f"{path}: labels outside the four routes: {sorted(unknown)}")
    return [{"id": row["id"], "split": "eval", "source": "fast-llm-eval", "text": row["text"], "want": None, "route": row["label"]}
            for row in rows]


async def run(args: argparse.Namespace) -> int:
    key = os.environ.get("VERCEL_JEV_KEY")
    if not key:
        print("VERCEL_JEV_KEY is not set", file=sys.stderr)
        return 2
    import aiohttp
    rows = eval_rows(args.eval, args.limit) if args.eval else corpus_rows(args.corpus, args.limit)
    results: list[dict] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        client = jev.JevClient(jev.aiohttp_post(session), key, timeout=args.timeout)
        semaphore = asyncio.Semaphore(args.concurrency)

        async def one(row):
            async with semaphore:
                started = time.perf_counter()
                try:
                    reply = await client.ask(row["text"], jev.QUESTIONS)
                    error = None
                except jev.JevError as err:
                    reply, error = None, str(err)
                ms = (time.perf_counter() - started) * 1000
                record = {"id": row["id"], "split": row["split"], "source": row["source"], "want": row["want"], "label": row["route"],
                          "ms": round(ms), "error": error}
                if reply is not None:
                    a = reply.answers
                    record.update(got=a["utterance"].choice, conf=a["utterance"].confidence, tier=a["tier"].choice,
                                  tier_conf=a["tier"].confidence, effort=a["effort"].choice, route=a["route"].choice,
                                  route_conf=a["route"].confidence, cost=reply.cost)
                results.append(record)

        await one(rows[0])   # warm the connection before the timed run
        results.clear()
        await asyncio.gather(*(one(row) for row in rows))
    ok = [r for r in results if r["error"] is None]
    print(f"rows {len(results)}  answered {len(ok)}  failed {len(results) - len(ok)} {dict(Counter(r['error'] for r in results if r['error']))}")
    if args.eval:
        report_route(ok)
    else:
        report_utterance(ok)
    ms = sorted(r["ms"] for r in ok)
    if ms:
        print(f"\nlatency ms: p50 {statistics.median(ms):.0f}  p95 {ms[int(0.95 * len(ms))]:.0f}  max {ms[-1]}  "
              f"over {int(jev.HARD_TIMEOUT_S * 1000)} ms: {sum(m > jev.HARD_TIMEOUT_S * 1000 for m in ms)}")
    costs = [r["cost"] for r in ok if r.get("cost") is not None]
    if costs:
        print(f"gateway cost per call (USD): mean {statistics.mean(costs):.7f} over {len(costs)} calls")
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


def report_utterance(ok: list[dict]) -> None:
    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in ok:
        confusion[r["want"]][r["got"]] += 1
    print("\nwant \\ got   " + "  ".join(f"{c:>13}" for c in KINDS))
    for want in KINDS:
        print(f"{want:13}" + "  ".join(f"{confusion[want][c]:>13}" for c in KINDS))
    print("\nact-on threshold: share of answers at/above it, and how many of those are right (utterance class)")
    for threshold in (0.8, 0.9, 0.95, 0.99):
        sure = [r for r in ok if r["conf"] >= threshold]
        right = sum(r["got"] == r["want"] for r in sure)
        print(f"  >= {threshold:.2f}: {len(sure):4}/{len(ok)}  right {right}/{len(sure)}")
    risky = [r for r in ok if r["conf"] >= jev.CONFIDENT and r["got"] in ("garbled", "command") and r["want"] == "talk"]
    print(f"\nacted-on wrong (a talk row answered as garbled/command at >= {jev.CONFIDENT}): {len(risky)}")
    talk = [r for r in ok if r["want"] == "talk"]
    print("tier on talk rows:", dict(Counter(r["tier"] for r in talk)), " effort:", dict(Counter(r["effort"] for r in talk)))


def report_route(ok: list[dict]) -> None:
    """The route question against the benchmark labels, and the tier question's wrong-fast count beside it."""
    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in ok:
        confusion[r["label"]][r["route"]] += 1
    print("\nlabel \\ route " + "  ".join(f"{c:>13}" for c in ROUTES))
    for label in ROUTES:
        print(f"{label:13} " + "  ".join(f"{confusion[label][c]:>13}" for c in ROUTES))
    right = sum(r["route"] == r["label"] for r in ok)
    print(f"\nroute accuracy: {right}/{len(ok)} = {100 * right / max(len(ok), 1):.1f} %")
    escalate = [r for r in ok if r["label"] == "escalate"]
    reprompt = [r for r in ok if r["label"] == "reprompt"]
    print(f"\nthreshold on the answer's confidence; below it the question counts as «escalate» (safe): "
          f"wrongly fast of {len(escalate)} escalate rows, reprompt sent to fast of {len(reprompt)}")
    print(f"{'':>10} {'route question':>34}   {'tier question (jev.TIER)':>34}")
    print(f"{'threshold':>10} {'escalate->fast':>16} {'reprompt->fast':>16}   {'escalate->fast':>16} {'reprompt->fast':>16}")
    for t in THRESHOLDS:
        by_route = [sum(r["route"] == "fast" and r["route_conf"] >= t for r in group) for group in (escalate, reprompt)]
        by_tier = [sum(r["tier"] == "fast" and r["tier_conf"] >= t for r in group) for group in (escalate, reprompt)]
        print(f"{t:>10.2f} {by_route[0]:>16} {by_route[1]:>16}   {by_tier[0]:>16} {by_tier[1]:>16}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus")
    parser.add_argument("--eval", help="a fast-llm-eval.jsonl (id, text, label): score the route question instead of the corpus")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=2.0, help="per-call budget for the benchmark (the adapter uses 0.4 s)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
