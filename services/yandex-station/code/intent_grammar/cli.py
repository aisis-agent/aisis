"""Command line: `python3 -m intent_grammar {check,match,backtest}` (run from the station `code/` directory).

    compile [--check]          write/verify the JSON bundle (grammar/ru.json) the adapter loads without PyYAML
    check                      validate the grammar, run its pinned examples/negatives, list conflicting readings
    match TEXT...              the decision for each utterance, as JSON
    backtest                   run the labelled corpora, print the numbers, write a JSON report, gate on it

Exit codes: 0 ok, 1 the gate or a pinned check failed, 2 usage / unreadable grammar or corpus.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import backtest
from .grammar import Grammar, GrammarError, compile_bundle, load

DEFAULT_GRAMMAR_PATH = Path(__file__).parent / "grammar" / "ru.yaml"
ENV_GRAMMAR = "AISIS_INTENT_GRAMMAR_FILE"   # not AISIS_INTENT_GRAMMAR: that one is the adapter's on|shadow|off switch


def _grammar(path: str | None) -> Grammar:
    import os
    return load(path or os.environ.get(ENV_GRAMMAR) or DEFAULT_GRAMMAR_PATH)


def _conflicts(grammar: Grammar) -> list[str]:
    """Pinned examples that two different intents read equally well: the grammar is ambiguous there."""
    found: list[str] = []
    for intent in grammar.intents:
        for example in intent.examples:
            top = grammar.candidates(example.say)
            if len(top) > 1 and top[0].intent == intent.id:
                rivals = [c.intent for c in top[1:] if c.intent != intent.id]
                if rivals:
                    priority = {i.id: i.priority for i in grammar.intents}
                    if priority[rivals[0]] == priority[intent.id]:
                        found.append(f"{example.say!r}: {intent.id} vs {', '.join(rivals)}")
    return found


def cmd_check(args: argparse.Namespace) -> int:
    grammar = _grammar(args.grammar)
    failures = grammar.self_check()
    conflicts = _conflicts(grammar)
    print(f"{len(grammar.intents)} intents, {sum(len(i.patterns) for i in grammar.intents)} patterns, "
          f"{sum(len(i.examples) for i in grammar.intents)} examples, {sum(len(i.negatives) for i in grammar.intents)} negatives")
    for line in failures:
        print("FAIL", line)
    for line in conflicts:
        print("CONFLICT", line)
    return 1 if failures or conflicts else 0


def cmd_compile(args: argparse.Namespace) -> int:
    source = Path(args.grammar or DEFAULT_GRAMMAR_PATH)
    target = source.with_suffix(".json")
    bundle = compile_bundle(source.read_text(encoding="utf-8"))
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != bundle:
            print(f"{target} is stale: run `python3 -m intent_grammar compile`", file=sys.stderr)
            return 1
        return 0
    target.write_text(bundle, encoding="utf-8")
    print(target)
    return 0


def cmd_match(args: argparse.Namespace) -> int:
    grammar = _grammar(args.grammar)
    for text in args.text:
        decision = grammar.decide(text)
        print(json.dumps({"text": text, "decision": None if decision is None else _plain(decision)}, ensure_ascii=False))
    return 0


def _plain(decision) -> dict:
    data = asdict(decision)
    data["parts"] = [_plain(part) for part in decision.parts]
    return data


def cmd_backtest(args: argparse.Namespace) -> int:
    grammar = _grammar(args.grammar)
    corpus = backtest.corpus_dir(args.corpus)
    if not corpus.is_dir():
        print(f"corpus directory {corpus} does not exist (set {backtest.ENV_CORPUS_DIR} or --corpus)", file=sys.stderr)
        return 2
    rows, manifest = backtest.load_rows(corpus, grammar)
    if not rows:
        print(f"no labelled rows under {corpus}", file=sys.stderr)
        return 2
    previous_path = Path(args.previous).expanduser() if args.previous else None
    if previous_path is not None and not previous_path.exists():
        print(f"--previous {previous_path} does not exist", file=sys.stderr)
        return 2
    decide = None
    if args.route != "grammar":
        import intent_route  # the adapter's own route (grammar + arithmetic + weather skills); lives in code/
        decide = intent_route.measured_route(intent_route.measured_router(grammar), mode=args.route)
    destination = Path(args.out).expanduser() if args.out else backtest.archive_path()
    # compare -> save -> promote is one step under the baseline lock, so two runs cannot interleave their baselines
    with contextlib.nullcontext() if args.no_latest else backtest.baseline_lock():
        previous = backtest.load_previous(previous_path)
        report = backtest.build_report(grammar, rows, manifest, include_text=not args.no_text, previous=previous,
                                       decide=decide, route=args.route, require_baseline=args.gate and not args.init_baseline)
        if args.init_baseline:
            if report["baseline"]["usable"]:
                # a compatible baseline: only the owner's rebaseline of lost or relabelled rows is accepted (regressions still fail)
                if not backtest.accept_rebaseline(report, grammar, args.init_baseline):
                    print("--init-baseline refused: a compatible baseline exists and no row was lost or relabelled; "
                          "every row is compared with it", file=sys.stderr)
                    return 2
            else:
                # an explicit start (first run of a route) or rebaseline: absolute checks only, the reason travels with the report
                report = backtest.build_report(grammar, rows, manifest, include_text=not args.no_text, previous=None,
                                               decide=decide, route=args.route)
                report["baseline"]["init_reason"] = args.init_baseline
        promote = not args.no_latest and report["gate"]["ok"]
        backtest.save_report(report, destination, latest=backtest.reports_dir() / "latest.json" if promote else None)
    for line in backtest.summary_lines(report):
        print(line)
    goal = report.get("goal")
    if goal:
        for part in ("all", "holdout"):
            entry = goal.get(part)
            if entry:
                cells = ", ".join(f"{key} {entry[key]['value']:.3f} (95% {entry[key]['ci95'][0]:.2f}-{entry[key]['ci95'][1]:.2f}, gap {entry[key]['gap']:+.3f})"
                                  for key in ("coverage", "correct") if key in entry)
                print(f"goal {goal['split'].split('/')[0]}/{part} n={entry['addressed']}: {cells}  target {goal['target']} {'MET' if all(entry[k]['met'] for k in goal['target'] if k in entry) else 'not met'}")
    if args.misses:
        for miss in report["dev_misses"]:
            print(f"  {miss['outcome']:13} {miss['truth']:18} {miss.get('text', miss['id'])!r} -> {miss['intent']}")
    if not args.no_latest and not promote:
        print("baseline not updated: the gate failed, latest.json stays the last passing report")
    print(f"report: {destination}")
    return 0 if report["gate"]["ok"] or not args.gate else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intent_grammar", description=__doc__.splitlines()[0])
    parser.add_argument("--grammar", help=f"grammar YAML (default: ${ENV_GRAMMAR} or the packaged ru.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate the grammar and its pinned examples").set_defaults(run=cmd_check)
    comp = sub.add_parser("compile", help="write the JSON bundle the adapter loads next to the YAML grammar")
    comp.add_argument("--check", action="store_true", help="exit 1 when the committed bundle is stale")
    comp.set_defaults(run=cmd_compile)
    match = sub.add_parser("match", help="print the decision for each utterance")
    match.add_argument("text", nargs="+")
    match.set_defaults(run=cmd_match)
    bt = sub.add_parser("backtest", help="run the labelled corpora")
    bt.add_argument("--corpus", help=f"corpus directory (default: ${backtest.ENV_CORPUS_DIR} or {backtest.DEFAULT_CORPUS_DIR})")
    bt.add_argument("--out", help=f"report file (default: ${backtest.ENV_REPORT_DIR} or ~/.local/share/aisis/intent-reports, backtest-<time>-<ns>-<pid>.json)")
    bt.add_argument("--previous", help="report to compare with (default: latest.json in the report directory)")
    bt.add_argument("--route", choices=("grammar", "adapter", "quasar", "combined"), default="grammar",
                    help="what to measure: the bare grammar; the adapter route (grammar + arithmetic + weather skills); "
                         "quasar (only what the quasar container answered before: the baseline); or combined: "
                         "pre-phase grammar, then the quasar container, then the rest (the production route)")
    bt.add_argument("--no-latest", action="store_true",
                    help="never update latest.json in the report directory (only a run whose gate held updates it)")
    bt.add_argument("--no-text", action="store_true", help="leave utterance texts out of the report")
    bt.add_argument("--misses", action="store_true", help="print the dev-split rows the grammar does not handle")
    bt.add_argument("--gate", action="store_true", help="exit 1 when the KPI gate or a regression check fails; with no usable baseline "
                    "(none yet, other route or split definition) that is itself a failure unless --init-baseline is given")
    bt.add_argument("--init-baseline", metavar="REASON", help="the owner's explicit start or rebaseline, with its reason (recorded in the "
                    "report): the first run of a route, an unusable baseline, or a compatible baseline whose rows were lost or relabelled "
                    "(the counts are recorded too; regressions still fail). Refused when a compatible baseline lost and relabelled nothing. "
                    "The nightly job never passes it")
    bt.set_defaults(run=cmd_backtest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (GrammarError, OSError, json.JSONDecodeError) as err:
        print(f"intent_grammar: {err}", file=sys.stderr)
        return 2
