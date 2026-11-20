#!/usr/bin/env python3
"""Calibration and measurement of the correction policy (`stt_cli.hypotheses.repair.decide`) on stored data (aisis ticket #182, core #153).

The policy decides whether the speaker asks «Мне послышалось «включи свет». Вы имели в виду «выключи свет»?» instead of acting. Its thresholds
(ask_floor, primary_weak, margin_max, garbled_fit) are chosen here from numbers, on the labelled synthetic set (confusables_corpus.py, TTS clips:
Piper voices, NOT the owner's voice) and checked on the stored real clips (88, only 2 with an owner truth: no rate is claimed there).

  nbest    for every clip build the Evidence the gateway will: primary reading + the GigaAM CTC n-best (decode, then the confusable rivals of the
           primary and of GigaAM's own top priced on the same audio, `GigaCtc.score_alternatives`). One JSONL line per clip in
           <corpus>/nbest.jsonl (resumable), so every later step is instant. Three primaries per synthetic clip:
             whisper       what the live Wyoming Whisper said (results.jsonl)
             giga          GigaAM's own top as the primary (a robustness split: a primary that agrees with the second opinion)
             constructed   the OPPOSITE member of the clip's pair as the primary: a simulated mis-hearing of the recogniser. It measures whether
                           the second opinion can recover the truth. NEVER pooled with the real error rates (7 Whisper flips of 4032)
  report   the policy's decisions per split (all / augmentation / pair set / voice / grammar-reachable / risky): n in every cell; the real clips;
           the junk-primary case (a dropped Whisper caption while GigaAM hears a command) and the two rule candidates measured (`RULES`)
  grid     grid search over the Policy fields, the selection criterion, the voice hold-out check, the plateau around the optimum

The decision runs EXACTLY as the correction engine will run it (correction.py of the correction-fsm worktree, mirrored in `engine_decide`):
`action_of` = IntentRouter.classify -> executor + canonical sorted args (None for no decision / drop / reprompt / clarify); an emergency or
dropped reading (primary or ANY listed hypothesis, or the proposed option) vetoes; an action outside SAFE_EXECUTORS makes the engine use
ask_floor=cue_floor, primary_weak=1, margin_max=1 (asked on ANY disagreement), so the thresholds calibrated here steer the safe executors only.
No caller cue (no-op / device state) exists offline: the `contradiction` trigger is not measured here.

Run with the gateway's venv from services/yandex-station/code; stt_cli on PYTHONPATH (until aisis pins it) and the GigaAM worker's interpreter:

    PYTHONPATH=<stt-cli checkout> ~/.local/share/aisis/venv/bin/python bench_policy.py nbest [--corpus DIR] [--limit N]
    ... bench_policy.py report [--corpus DIR] [--policy '{"ask_floor": 0.3}']
    ... bench_policy.py grid [--corpus DIR]

CPU only. Nothing here touches a speaker, the gateway, Home Assistant or the Wyoming server (the Whisper texts are read from results.jsonl).
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import fcntl
import itertools
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import confusables_corpus as corpus
from bench_confusables import cell_text, read_results, write_private
from stt_cli.hypotheses.giga import GigaCtc, GigaError
from stt_cli.hypotheses.model import NBest, same_text
from stt_cli.hypotheses.repair import Decision, Evidence, Policy, decide

DEFAULT_GIGA_PYTHON = Path.home() / ".local/share/stt-cli/engines/onnx-asr/bin/python"
KINDS = ("whisper", "giga", "constructed")          # the primaries of a clip; see the module docstring
EMERGENCY_INTENT = "refuse.emergency"
# Mirrors correction.py (correction-fsm worktree): executors whose mistaken run is a light, a volume step, a media key or the end of a dialog.
SAFE_EXECUTORS = frozenset(("ha.light", "ha.light_color", "ha.light_brightness", "speaker.volume", "speaker.volume_delta",
                            "speaker.volume_query", "media.key", "music.play", "dialog.end", "dialog.repeat_last"))
ASKED = ("ask", "ask-choice", "relisten")
GRID = {"ask_floor": (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7), "primary_weak": (0.3, 0.5, 0.6, 0.7, 0.9),
        "margin_max": (0.1, 0.2, 0.35, 0.5, 0.7), "garbled_fit": (0.1, 0.2, 0.35, 0.5, 0.7)}
FALSE_ASK_MAX = 0.01     # the selection constraint, see `select`
VOICE_HALVES = (("denis", "irina"), ("dmitri", "ruslan"))


# ---------------------------------------------------------------- statistics

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval of k successes in n trials (95 % by default); (0, 1) for n = 0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def frac(k: int, n: int, interval: bool = False) -> str:
    """`k/n (p%)`: the n is in every cell; with `interval` the Wilson 95 % interval follows."""
    if not n:
        return "0/0 (-)"
    text = f"{k}/{n} ({100 * k / n:.1f}%)"
    if interval:
        low, high = wilson(k, n)
        text += f" [{100 * low:.1f}-{100 * high:.1f}]"
    return text


# ---------------------------------------------------------------- the grammar's action_of

def _parts_key(parts: Iterable[Any]) -> str | None:
    keys = [action_key(part) for part in parts]
    return None if any(key is None for key in keys) else "+".join(key for key in keys if key)


def action_key(decision: Any) -> str | None:
    """The executable action of a grammar decision, canonical (correction.py's rule): None for no decision, no action, drop/reprompt/clarify."""
    if decision is None:
        return None
    if decision.parts:
        return _parts_key(decision.parts)
    if decision.action is None or decision.tier in ("drop", "reprompt", "clarify"):
        return None
    action = decision.action
    return f"{action['executor']}|{json.dumps(action.get('args') or {}, sort_keys=True, ensure_ascii=False)}"


def executors_of(decision: Any) -> frozenset[str]:
    if decision is None:
        return frozenset()
    if decision.parts:
        return frozenset(executor for part in decision.parts for executor in executors_of(part))
    return frozenset((decision.action["executor"],)) if decision.action else frozenset()


def execute_query_key(decision: Any) -> str | None:
    """The stricter reading of an action: only an execute/query decision is an action (a `confirm` guess is none). Sensitivity run only."""
    return action_key(decision) if decision is not None and decision.tier in ("execute", "query") else None


class Actions:
    """What the engine learns from the grammar about a text: its action key, whether it is protected (an emergency, or speech the grammar drops:
    hallucinated captions, fillers), the executors it would run. Memoised: the grid asks about the same few hundred texts millions of times."""

    def __init__(self, classify: Callable[[str], Any], key: Callable[[Any], str | None] = action_key) -> None:
        self._classify = classify
        self._key = key
        self._memo: dict[str, tuple[str | None, bool, bool, frozenset[str]]] = {}

    def _read(self, text: str) -> tuple[str | None, bool, bool, frozenset[str]]:
        found = self._memo.get(text)
        if found is None:
            decision = self._classify(text)
            parts = () if decision is None else (decision, *decision.parts)
            found = self._memo[text] = (self._key(decision), any(part.intent == EMERGENCY_INTENT for part in parts),
                                        any(part.tier == "drop" for part in parts), executors_of(decision))
        return found

    def key(self, text: str) -> str | None:
        return self._read(text)[0]

    def emergency(self, text: str) -> bool:
        return self._read(text)[1]

    def dropped(self, text: str) -> bool:
        return self._read(text)[2]

    def protected(self, text: str) -> bool:
        _, emergency, dropped, _ = self._read(text)
        return emergency or dropped

    def risky(self, text: str) -> bool:
        return not self._read(text)[3] <= SAFE_EXECUTORS


def grammar_actions(key: Callable[[Any], str | None] = action_key) -> Actions:
    """The real aisis grammar, as the correction engine classifies (IntentRouter.classify of the shipped bundle)."""
    import intent_route
    grammar = intent_route.load_grammar()
    if grammar is None:
        raise SystemExit("the intent grammar bundle cannot be loaded")
    return Actions(intent_route.IntentRouter(grammar).classify, key)


# ---------------------------------------------------------------- cases and the decision

@dataclass(frozen=True)
class Case:
    """One decision to evaluate: a primary reading of a clip with the GigaAM n-best priced on the clip's audio."""
    id: str
    split: str                      # synthetic | real
    kind: str                       # whisper | giga | constructed
    primary: str
    nbest: NBest
    pair_set: str | None = None
    polarity: str | None = None     # the truth member ("a" | "b"); None: unlabelled
    voice: str | None = None
    augmentation: str | None = None
    truth: str | None = None
    reachable: bool = False         # the grammar maps the truth and the opposite member to different actions: the policy can tell them apart
    vetoed: bool = False
    risky: bool = False

    @property
    def seen(self) -> str:
        """What the primary reading says about the truth: right (the truth member) | flip (the opposite) | neither (no marker or both) | unlabelled."""
        if self.polarity is None or self.pair_set is None:
            return "unlabelled"
        heard = corpus.heard_polarity(self.pair_set, self.primary)
        return "right" if heard == self.polarity else "flip" if heard == corpus.opposite(self.polarity) else "neither"

    @property
    def exact(self) -> bool:
        """The primary is the truth phrase itself (same text after normalisation): a question about it is a plain false ask."""
        return self.truth is not None and self.polarity is not None and corpus.same_text(self.primary, self.truth)

    def classes(self) -> tuple[str, ...]:
        """The classes of primary this case counts in: its `seen`, and `exact` as well when the primary is the truth phrase."""
        return (self.seen, "exact") if self.exact else (self.seen,)

    def groups(self) -> tuple[str, ...]:
        if self.split == "real":
            return ("real",)
        every = ("all", f"aug:{self.augmentation}", f"pair:{self.pair_set}", f"voice:{self.voice}", f"reach:{'yes' if self.reachable else 'no'}",
                 f"risk:{'risky' if self.risky else 'safe'}")
        if not self.reachable:
            return every
        # the clips the grammar can tell apart: the population a threshold can act on (prefix r)
        return every + ("rall", f"raug:{self.augmentation}", f"rpair:{self.pair_set}", f"rvoice:{self.voice}", f"rrisk:{'risky' if self.risky else 'safe'}")


def engine_policy(policy: Policy, risky: bool) -> Policy:
    """Policy as the engine applies it: a reading outside SAFE_EXECUTORS is asked about on ANY disagreement (correction.py `_policy`)."""
    return dataclasses.replace(policy, ask_floor=policy.cue_floor, primary_weak=1.0, margin_max=1.0) if risky else policy


def engine_decide(case: Case, policy: Policy, actions: Actions) -> Decision:
    """`repair.decide` with the engine's veto and risk handling around it (correction.py `consult`, without cues)."""
    evidence = Evidence(primary=case.primary, nbest=case.nbest, action_of=actions.key, vetoed=case.vetoed)
    decision = decide(evidence, engine_policy(policy, case.risky))
    if decision.kind != "none" and any(actions.protected(option.text) for option in decision.options):
        return Decision("none", "", "vetoed", decision.heard)
    return decision


def arm(case: Case, actions: Actions) -> Case:
    """The policy-independent facts of a case, from the grammar: the veto and the risk of the primary and every listed hypothesis."""
    texts = [case.primary] + [h.text for h in case.nbest.hypotheses]
    return dataclasses.replace(case, vetoed=any(actions.protected(t) for t in texts), risky=any(actions.risky(t) for t in texts))


@dataclass(frozen=True)
class Outcome:
    seen: str
    kind: str          # none | ask | ask-choice | relisten
    trigger: str
    reason: str
    proposal: str      # "" for none/relisten | truth | other (ask: the proposed option's member) | choice-truth | choice-other
    same_text: bool    # an ask proposing the truth member whose text is the truth text itself

    @property
    def label(self) -> str:
        if self.kind == "none":
            return "quiet:" + (self.reason or "?")
        return self.proposal or self.kind


def outcome(case: Case, policy: Policy, actions: Actions) -> Outcome:
    decision = engine_decide(case, policy, actions)
    proposal, same = "", False
    if case.pair_set is not None and case.polarity is not None:
        members = [corpus.heard_polarity(case.pair_set, option.text) for option in decision.options]
        if decision.kind == "ask":
            proposal = "truth" if members[0] == case.polarity else "other"
            same = proposal == "truth" and corpus.same_text(decision.options[0].text, case.truth or "")
        elif decision.kind == "ask-choice":
            proposal = "choice-truth" if case.polarity in members[1:] else "choice-other"   # options[0] is the heard reading
    return Outcome(case.seen, decision.kind, decision.trigger, decision.reason, proposal, same)


def _count_outcome(counts: Counter[tuple[str, ...]], result: Outcome, classes: Iterable[str]) -> None:
    for seen in classes:
        counts[(seen, "n")] += 1
        counts[(seen, "out:" + result.label)] += 1
        if result.kind in ASKED:
            counts[(seen, "asked")] += 1
            counts[(seen, "trig:" + result.trigger)] += 1
        if result.proposal in ("truth", "choice-truth"):
            counts[(seen, "offered")] += 1
        if result.same_text:
            counts[(seen, "same-text")] += 1


def tally(cases: Iterable[Case], policy: Policy, actions: Actions) -> dict[tuple[str, str], Counter[tuple[str, ...]]]:
    """Counts per (kind, group): ("*", "n"), ("*", "asked"), and per class of primary (right | flip | neither | exact | unlabelled; `exact` is also
    counted in its member class): (class, "n"), (class, "asked"), (class, "out:<label>"), (class, "trig:<trigger>"), (class, "offered"), (class, "same-text")."""
    table: dict[tuple[str, str], Counter[tuple[str, ...]]] = {}
    for case in cases:
        result = outcome(case, policy, actions)
        for group in case.groups():
            counts = table.setdefault((case.kind, group), Counter())
            counts[("*", "n")] += 1
            counts[("*", "asked")] += result.kind in ASKED
            _count_outcome(counts, result, case.classes())
    return table


# ---------------------------------------------------------------- the n-best cache

@dataclass(frozen=True)
class Clip:
    id: str
    split: str
    whisper: str | None
    opposite: str | None                 # the opposite member's text (synthetic): the constructed primary
    load: Callable[[], bytes]            # 16 kHz s16le PCM exactly as the gateway hands its recogniser


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def opposite_id(row: Mapping[str, Any]) -> str:
    """The clip id of the opposite member with the same phrasing index, voice and augmentation (`<pair>-<pol><variant>-<voice>-<aug>`)."""
    head = f"{row['pair_set']}-{row['polarity']}"
    return f"{row['pair_set']}-{corpus.opposite(row['polarity'])}" + row["id"][len(head):]


def synthetic_clips(corpus_dir: Path) -> list[Clip]:
    import bench_confusables

    def pcm(row: Mapping[str, Any]) -> bytes:
        return bench_confusables.recognizer_pcm(bench_confusables.read_clip(corpus_dir / row["path"]), row.get("trailing_ms", round(corpus.TAIL_S * 1000)))
    rows = list(first_per_id(read_results(corpus_dir / "results.jsonl")).values())
    texts = {row["id"]: row["text"] for row in rows}
    return [Clip(row["id"], "synthetic", (row.get("whisper") or {}).get("text") or None, texts.get(opposite_id(row)), lambda row=row: pcm(row)) for row in rows]


def real_clips(corpus_dir: Path, archive: Path | None = None) -> list[Clip]:
    import bench_confusables

    import bench_asr_archive
    root = archive or bench_confusables.ARCHIVE

    def pcm(row: Mapping[str, Any]) -> bytes:
        wave = bench_asr_archive.decode(str(root / row["path"]))
        return (bench_asr_archive.lift(wave, row["voiced_ms"]) * 32768).clip(-32768, 32767).astype("<i2").tobytes()

    return [Clip(row["id"], "real", (row.get("whisper") or {}).get("text") or None, None, lambda row=row: pcm(row))
            for row in read_jsonl(corpus_dir / "real.jsonl")]


async def _decode_clip(giga: Any, clip: Clip) -> tuple[Any, dict[str, dict[str, Any]]]:
    decoded = await giga.decode(clip.load())
    primaries: dict[str, dict[str, Any]] = {}
    if clip.whisper:
        primaries["whisper"] = {"text": clip.whisper, "nbest": (await giga.score_alternatives(decoded.handle, clip.whisper)).as_dict()}
    if decoded.nbest.top is not None:
        primaries["giga"] = {"text": decoded.nbest.top.text, "nbest": (await giga.score_alternatives(decoded.handle)).as_dict()}
    if clip.opposite:
        primaries["constructed"] = {"text": clip.opposite, "nbest": (await giga.score_alternatives(decoded.handle, clip.opposite)).as_dict()}
    return decoded, primaries


def cache_rows(path: Path, repair: bool = False) -> dict[str, dict[str, Any]]:
    """The cache by clip id. A clip decoded again (a primary that was unknown, or whose text changed) appends a later line: its primaries are merged,
    the latest line winning per primary."""
    merged: dict[str, dict[str, Any]] = {}
    for row in read_results(path, repair=repair):
        known = merged.setdefault(row["id"], {**row, "primaries": {}})
        known["primaries"].update(row["primaries"])
    return merged


def _is_complete(clip: Clip, cached: Mapping[str, Any] | None) -> bool:
    """Every primary the clip has text for is cached for THAT text: a rerun of the corpus step can replace a transcript under the same id, and the
    stale primary and its alternatives must be decoded again (the decode itself always yields the GigaAM one when it decodes anything)."""
    if cached is None:
        return False
    have = cached["primaries"]
    return (not clip.whisper or have.get("whisper", {}).get("text") == clip.whisper) and (not clip.opposite or have.get("constructed", {}).get("text") == clip.opposite)


def _open_private_append(path: Path) -> Any:
    """The cache holds household transcripts: created 0600, and an existing one is tightened to 0600 before anything is appended."""
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a", encoding="utf-8")


async def build_cache(clips: list[Clip], giga: Any, cache: Path, *, limit: int | None = None, log: Callable[[str], None] = print) -> dict[str, int]:
    """Decode every clip not yet fully in `cache` and append one line per clip; a clip the worker fails on is left out (the next run retries it)."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    # The lock spans reading the done-set and every append, so a concurrent run waits and then sees these clips as done instead of repeating them.
    with cache.with_name(cache.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = cache_rows(cache, repair=True)
        todo = [clip for clip in clips if not _is_complete(clip, cached.get(clip.id))][:limit]
        stats = {"new": 0, "failed": 0, "skipped": len(clips) - len(todo) if limit is None else len(cached)}
        started = time.monotonic()
        with _open_private_append(cache) as out:
            for index, clip in enumerate(todo, 1):
                try:
                    decoded, primaries = await _decode_clip(giga, clip)
                except GigaError as error:
                    stats["failed"] += 1
                    log(f"{clip.id}: {error}")
                    await giga.start()
                    continue
                out.write(json.dumps({"id": clip.id, "split": clip.split, "decode_ms": decoded.ms, "primaries": primaries}, ensure_ascii=False) + "\n")
                out.flush()
                stats["new"] += 1
                if index % 100 == 0:
                    log(f"{index}/{len(todo)} clips, {time.monotonic() - started:.0f} s")
    return stats


def first_per_id(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """A clip id repeated by concurrent appends counts once: the first row."""
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row["id"], row)
    return unique


def load_cases(corpus_dir: Path, actions: Actions, cache: Path | None = None) -> list[Case]:
    """Every (clip, primary) of the cache with its truth and the policy-independent facts from the grammar."""
    manifest = first_per_id(read_results(corpus_dir / "results.jsonl"))
    real = {row["id"]: row for row in read_jsonl(corpus_dir / "real.jsonl")}
    cases: list[Case] = []
    for line in cache_rows(cache or corpus_dir / "nbest.jsonl").values():
        row = manifest.get(line["id"]) if line["split"] == "synthetic" else real.get(line["id"])
        if row is None:
            continue
        for kind, entry in line["primaries"].items():
            nbest = NBest.from_dict(entry["nbest"])
            if line["split"] == "real":
                case = Case(line["id"], "real", kind, entry["text"], nbest, truth=row.get("truth"))
            else:
                other = manifest.get(opposite_id(row))
                reachable = other is not None and actions.key(row["text"]) is not None and actions.key(other["text"]) is not None \
                    and actions.key(row["text"]) != actions.key(other["text"])
                case = Case(line["id"], "synthetic", kind, entry["text"], nbest, row["pair_set"], row["polarity"], row["voice"], row["augmentation"],
                            row["text"], reachable)
            cases.append(arm(case, actions))
    return cases


# ---------------------------------------------------------------- the report

def cell(counts: Counter[tuple[str, ...]], seen: str, key: str, interval: bool = False) -> str:
    return frac(counts[(seen, key)], counts[(seen, "n")], interval)


def group_rows(table: Mapping[tuple[str, str], Counter[tuple[str, ...]]], kind: str, prefix: str) -> list[tuple[str, Counter[tuple[str, ...]]]]:
    return [(group.split(":", 1)[-1], counts) for (k, group), counts in sorted(table.items()) if k == kind and group.startswith(prefix)]


def render_groups(title: str, rows: list[tuple[str, Counter[tuple[str, ...]]]], interval: bool = False) -> list[str]:
    head = ("| group | n | asked (any) | primary exactly right: asked (false-ask) | primary says the right member: asked | ... proposing the OPPOSITE | "
            "primary flipped: ask→truth | choice→truth | neither member: asked | ... ask→truth |")
    lines = ["", f"#### {title}", "", head, "|---|---|---|---|---|---|---|---|---|---|"]
    for name, c in rows:
        lines.append(f"| {name} | {c[('*', 'n')]} | {frac(c[('*', 'asked')], c[('*', 'n')], interval)} | {cell(c, 'exact', 'asked', interval)} | {cell(c, 'right', 'asked', interval)} | "
                     f"{frac(c[('right', 'out:other')] + c[('right', 'out:choice-other')], c[('right', 'n')])} | "
                     f"{frac(c[('flip', 'out:truth')], c[('flip', 'n')], interval)} | {frac(c[('flip', 'out:choice-truth')], c[('flip', 'n')])} | "
                     f"{cell(c, 'neither', 'asked', interval)} | {frac(c[('neither', 'out:truth')], c[('neither', 'n')], interval)} |")
    return lines


def render_constructed(rows: list[tuple[str, Counter[tuple[str, ...]]]]) -> list[str]:
    lines = ["", "| group | n | ask→truth member | ... its text equals the truth text | choice offers truth | truth offered (ask or choice) | ask→other | choice→other | relisten | quiet |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, c in rows:
        n = c[("flip", "n")]
        quiet = sum(v for (seen, key), v in c.items() if seen == "flip" and key.startswith("out:quiet:"))
        lines.append(f"| {name} | {n} | {frac(c[('flip', 'out:truth')], n, True)} | {frac(c[('flip', 'same-text')], n)} | {frac(c[('flip', 'out:choice-truth')], n)} | "
                     f"{frac(c[('flip', 'offered')], n, True)} | {frac(c[('flip', 'out:other')], n)} | {frac(c[('flip', 'out:choice-other')], n)} | "
                     f"{frac(c[('flip', 'out:relisten')], n)} | {frac(quiet, n)} |")
    return lines


def _suffixes(c: Counter[tuple[str, ...]], prefix: str) -> list[str]:
    return sorted({key[1][len(prefix):] for key in c if key[1].startswith(prefix)})


def render_triggers(table: Mapping[tuple[str, str], Counter[tuple[str, ...]]], kind: str, group: str) -> list[str]:
    """What each primary class got: every outcome label and trigger with its count (one row per class, n in the first column)."""
    c = table.get((kind, group), Counter())
    labels = _suffixes(c, "out:")
    triggers = _suffixes(c, "trig:")
    lines = ["", "| primary says | n | " + " | ".join(labels) + " | " + " | ".join("trigger " + t for t in triggers) + " |", "|---|---|" + "---|" * (len(labels) + len(triggers))]
    for seen in ("exact", "right", "flip", "neither"):
        n = c[(seen, "n")]
        if n:
            lines.append(f"| {seen} | {n} | " + " | ".join(str(c[(seen, 'out:' + label)]) for label in labels) + " | " + " | ".join(str(c[(seen, 'trig:' + t)]) for t in triggers) + " |")
    return lines


def report_synthetic(cases: list[Case], policy: Policy, actions: Actions) -> list[str]:
    table = tally([c for c in cases if c.split == "synthetic"], policy, actions)
    legend = ("Classes: `exactly right` = the primary is the truth phrase (a question there is a plain false ask); `says the right member` includes it plus readings with "
              "other words wrong («в спании» for «в спальне»), where a question can be a useful repair. Proposal labels: `truth` = the proposed option says the truth member (for a primary "
              "that already says it, the question is about other words: a room, an ending), `other` = it says the opposite member; `choice-*` = a question with two options, offering "
              "the truth member or not.")
    lines = [f"## Synthetic split (TTS, Piper voices; not the owner's voice), policy {policy}", "", legend]
    for kind, blurb in (("whisper", "primary = Whisper's text"), ("giga", "primary = GigaAM's own top (robustness split)")):
        lines += ["", f"### primary: {kind} ({blurb})", ""]
        lines += render_groups("all clips (95 % Wilson intervals)", group_rows(table, kind, "all"), True)
        lines += render_triggers(table, kind, "all")
        for title, prefix in (("by augmentation", "aug:"), ("by pair set", "pair:"), ("by voice", "voice:"), ("by grammar reachability (does the grammar tell the pair apart?)", "reach:"),
                              ("by risk (risky executor = asked on any disagreement, thresholds unused)", "risk:")):
            lines += render_groups(title, group_rows(table, kind, prefix))
    lines += ["", "### constructed-flips: primary = the OPPOSITE member (simulated mis-hearing); a recovery test of the second opinion, NOT an error rate", ""]
    lines += render_constructed(group_rows(table, "constructed", "all") + [("reachable clips only", table.get(("constructed", "rall"), Counter()))])
    lines += ["", "#### constructed-flips by grammar reachability"] + render_constructed(group_rows(table, "constructed", "reach:"))
    for title, prefix in (("by augmentation", "raug:"), ("by pair set", "rpair:"), ("by voice", "rvoice:"), ("by risk", "rrisk:")):
        lines += ["", f"#### constructed-flips {title}, grammar-reachable clips only (the others cannot be told apart by any threshold)"]
        lines += render_constructed(group_rows(table, "constructed", prefix))
    lines += ["", "#### constructed-flips: what became of them", ""] + render_triggers(table, "constructed", "all")
    return lines + false_asks(cases, policy, actions)


def _false_ask_counts(cases: list[Case], policy: Policy, actions: Actions, kind: str) -> Counter[tuple[str, str, str, str]]:
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for case in cases:
        if case.split == "synthetic" and case.kind == kind and case.seen == "right":   # the member is right; the text may not be
            decision = engine_decide(case, policy, actions)
            if decision.kind in ASKED:
                counts[(corpus.normalize(case.primary), "yes" if case.exact else "no", decision.kind, " / ".join(option.text for option in decision.options))] += 1
    return counts


def false_asks(cases: list[Case], policy: Policy, actions: Actions, kind: str = "whisper", limit: int = 15) -> list[str]:
    """The most frequent questions asked about a primary that already said the right member: what the false asks actually are."""
    counts = _false_ask_counts(cases, policy, actions, kind)
    lines = ["", f"#### the most frequent false asks (primary {kind} says the right member; {sum(counts.values())} in all; a question about a misheard room or ending is a repair, not a false ask)", "", "| times | primary | primary is the truth phrase | question | options |", "|---|---|---|---|---|"]
    lines += [f"| {times} | {cell_text(primary)} | {exact} | {question} | {cell_text(options)} |" for (primary, exact, question, options), times in counts.most_common(limit)]
    return lines


def real_decisions(cases: list[Case], policy: Policy, actions: Actions) -> list[tuple[Case, Decision]]:
    return [(case, engine_decide(case, policy, actions)) for case in cases if case.split == "real"]


def truth_cell(truth: str | None) -> str:
    """None: no owner verdict (empty cell); '': the owner heard no speech (cliptruth's noise verdict), shown, never dropped."""
    return "" if truth is None else truth or "(noise: the owner heard no speech)"


def _real_summary_lines(cases: list[Case], policy: Policy, actions: Actions) -> list[str]:
    lines = []
    for kind in ("whisper", "giga"):
        decided = [(case, decision) for case, decision in real_decisions(cases, policy, actions) if case.kind == kind]
        kinds = Counter(decision.kind for _, decision in decided)
        triggers = Counter(decision.trigger or f"none:{decision.reason}" for _, decision in decided)
        lines.append(f"- primary {kind}: n={len(decided)} clips; decisions {dict(kinds)}; trigger/reason {dict(triggers)}")
    return lines


def _asked_real_lines(cases: list[Case], policy: Policy, actions: Actions) -> list[str]:
    lines = []
    for case, decision in real_decisions(cases, policy, actions):
        if case.kind == "whisper" and decision.kind in ASKED:
            options = " / ".join(f"{o.text} {o.percent if hasattr(o, 'percent') else ''}%".replace(" %", "") for o in decision.options)
            lines.append(f"| {cell_text(case.id)} | {cell_text(case.primary)} | {decision.kind}: {cell_text(options)} | {decision.trigger} | {cell_text(truth_cell(case.truth))} |")
    return lines


def _labelled_real_lines(labelled: list[tuple[Case, Decision]]) -> list[str]:
    return [f"- {case.id}: truth «{truth_cell(case.truth)}»; Whisper «{case.primary}»; decision {decision.kind} {decision.trigger or decision.reason}"
            + (f" -> {[o.text for o in decision.options]}" if decision.options else "")
            for case, decision in labelled]


def report_real(cases: list[Case], policy: Policy, actions: Actions) -> list[str]:
    lines = ["", f"## Real clips (n small, mostly no truth: counts, no rates), policy {policy}", ""]
    lines += _real_summary_lines(cases, policy, actions)
    lines += ["", "Every asked clip (primary whisper); the stored owner truth where there is one:", "", "| clip | Whisper (primary) | question | trigger | truth |", "|---|---|---|---|---|"]
    lines += _asked_real_lines(cases, policy, actions)
    labelled = [(case, decision) for case, decision in real_decisions(cases, policy, actions) if case.kind == "whisper" and case.truth is not None]
    lines += ["", f"Clips with an owner truth (n={len(labelled)}, too few for any rate):", ""]
    return lines + _labelled_real_lines(labelled)


# ---------------------------------------------------------------- the junk primary

def without_dropped_primary_veto(case: Case, actions: Actions) -> Case:
    """The case as if only an EMERGENCY primary vetoed: a dropped primary (a hallucinated caption, a filler) is no longer a veto by itself.
    Every listed hypothesis still vetoes when protected. This is the counterfactual of the junk-primary rule, not what the engine does."""
    vetoed = actions.emergency(case.primary) or any(actions.protected(h.text) for h in case.nbest.hypotheses)
    return dataclasses.replace(case, vetoed=vetoed)


@dataclass(frozen=True)
class JunkRow:
    split: str
    name: str                       # dropped | unread
    n: int
    hears: int                      # the second opinion's top is an action
    today: Counter[str]             # decisions of the engine as it is, over all n
    lifted: Counter[str]            # if a dropped primary never vetoed, over all n
    lifted_when_hears: Counter[str]  # if a dropped primary stopped vetoing only where the second opinion's top is an action, over all n


def junk_primary_rows(cases: list[Case], policy: Policy, actions: Actions) -> list[JunkRow]:
    """Per (split, class of a Whisper primary that is no action): n, how many the second opinion reads as an action, and the decisions today and under
    the two counterfactual veto rules. Classes: `dropped` (the grammar drops it: a caption or filler) and `unread` (no action, not dropped)."""
    groups: dict[tuple[str, str], list[Case]] = {}
    for case in cases:
        if case.kind != "whisper" or actions.key(case.primary) is not None or actions.emergency(case.primary):
            continue
        groups.setdefault((case.split, "dropped" if actions.dropped(case.primary) else "unread"), []).append(case)
    rows = []
    for (split, name), members in sorted(groups.items()):
        today, lifted, lifted_when_hears, hears = Counter[str](), Counter[str](), Counter[str](), 0
        for case in members:
            top = case.nbest.top
            hearing = top is not None and actions.key(top.text) is not None
            hears += hearing
            label = outcome(case, policy, actions).label
            freed = outcome(without_dropped_primary_veto(case, actions), policy, actions).label
            today[label] += 1
            lifted[freed] += 1
            lifted_when_hears[freed if hearing else label] += 1
        rows.append(JunkRow(split, name, len(members), hears, today, lifted, lifted_when_hears))
    return rows


def report_junk(cases: list[Case], policy: Policy, actions: Actions) -> list[str]:
    legend = ("`dropped` = the grammar drops the primary (a caption like «Продолжение следует.», a filler): the engine vetoes it today. `unread` = no action and not dropped. "
              "Decisions over all n of the row under three veto rules: today / a dropped primary never vetoes / a dropped primary stops vetoing only where the second opinion's top "
              "is an action.")
    head = ("| split | Whisper primary | n | second opinion's top is an action | decisions today | ... dropped primary never vetoes | "
            "... stops vetoing only when the second opinion hears an action |")
    lines = ["", "## Junk primary: Whisper's text is no action", "", legend, "", head, "|---|---|---|---|---|---|---|"]
    for row in junk_primary_rows(cases, policy, actions):
        lines.append(f"| {row.split} | {row.name} | {row.n} | {row.hears} | {dict(sorted(row.today.items()))} | {dict(sorted(row.lifted.items()))} | {dict(sorted(row.lifted_when_hears.items()))} |")
    lines += ["", "Real clips whose primary is dropped while the second opinion hears an action:", "", "| clip | Whisper | second opinion's top | share | owner truth |", "|---|---|---|---|---|"]
    for case in cases:
        if case.split == "real" and case.kind == "whisper" and actions.dropped(case.primary) and not actions.emergency(case.primary):
            top = case.nbest.top
            if top is not None and actions.key(top.text) is not None:
                lines.append(f"| {cell_text(case.id)} | {cell_text(case.primary)} | {cell_text(top.text)} | {top.percent}% (fit {top.fit if top.fit is None else round(top.fit, 2)}) | {cell_text(truth_cell(case.truth))} |")
    return lines


# ---------------------------------------------------------------- rule candidates

def hears_action(case: Case, actions: Actions) -> bool:
    top = case.nbest.top
    return top is not None and actions.key(top.text) is not None


def rule_dropped_primary(case: Case, actions: Actions) -> Case:
    """Rule A (the ENGINE's veto, aisis correction.py): a dropped primary (a hallucinated caption, a filler) stops being a veto when the second
    opinion's top is an action; an emergency primary and a protected hypothesis still veto."""
    if actions.dropped(case.primary) and not actions.emergency(case.primary) and hears_action(case, actions):
        return without_dropped_primary_veto(case, actions)
    return case


def _led_by(nbest: NBest, lead: Any, primary: str, actions: Actions) -> NBest:
    """The list without the readings that are no action and rank above `lead` (the primary's own reading stays): `lead` becomes the best listed
    alternative by the engine's own ranking. Every posterior and fit stays as measured, so the ask floor and the other thresholds judge `lead` on
    its real share."""
    ahead = nbest.hypotheses[:nbest.hypotheses.index(lead)]
    kept = tuple(h for h in ahead if actions.key(h.text) is not None or same_text(h.text, primary))
    return NBest(kept + nbest.hypotheses[len(ahead):], nbest.engine, nbest.ms)


def rule_lead_alternative(case: Case, actions: Actions) -> Case:
    """Rule B (the CORE's `decide`, stt-cli repair.py): when the second opinion's top is not the primary and is no action (a mangled word, `научи`),
    the question is about the best LISTED reading that is an action different from the primary's, not about nothing. The readings that are no action
    and outrank that one are taken out of the list; no posterior changes, so the experiment is "the engine chose among the listed actions" and an
    alternative below the ask floor stays below it."""
    top = case.nbest.top
    if top is None or same_text(top.text, case.primary) or actions.key(top.text) is not None:
        return case
    primary_key = actions.key(case.primary)
    for hypothesis in case.nbest.hypotheses:
        key = actions.key(hypothesis.text)
        if key is not None and key != primary_key and not same_text(hypothesis.text, case.primary):
            return dataclasses.replace(case, nbest=_led_by(case.nbest, hypothesis, case.primary, actions))
    return case


RULES: tuple[tuple[str, Callable[[Case, Actions], Case]], ...] = (
    ("today", lambda case, actions: case),
    ("A: dropped primary stops vetoing when the second opinion hears an action", rule_dropped_primary),
    ("B: ask about the best listed action when the second opinion's top is no action", rule_lead_alternative),
    ("A+B", lambda case, actions: rule_lead_alternative(rule_dropped_primary(case, actions), actions)),
)


def _count_real_whisper(counts: Counter[str], real: list[str], case: Case, result: Outcome) -> None:
    if case.kind == "whisper":
        counts["real-n"] += 1
        counts["real-ask"] += result.kind in ASKED
        real.append(result.label)


def _count_synthetic_whisper(counts: Counter[str], case: Case, result: Outcome, actions: Actions) -> None:
    offered = result.proposal in ("truth", "choice-truth")
    if case.exact:
        counts["exact-n"] += 1
        counts["exact-ask"] += result.kind in ASKED
    if case.seen == "right":
        counts["right-n"] += 1
        counts["opposite"] += result.proposal in ("other", "choice-other")
    if case.seen == "neither":
        counts["neither-n"] += 1
        counts["neither-hit"] += offered
    if actions.dropped(case.primary) and actions.key(case.primary) is None:
        counts["dropped-n"] += 1
        counts["dropped-hit"] += offered


def rule_row(label: str, rule: Callable[[Case, Actions], Case], cases: list[Case], policy: Policy, actions: Actions) -> tuple[str, list[str]]:
    """The measured effect of one rule: a table row and the real clips whose question it changes."""
    counts: Counter[str] = Counter()
    real: list[str] = []
    for case in cases:
        result = outcome(rule(case, actions), policy, actions)
        if case.split == "real":
            _count_real_whisper(counts, real, case, result)
        elif case.kind == "constructed" and case.reachable:
            counts["built-n"] += 1
            counts["built-hit"] += result.proposal in ("truth", "choice-truth")
        elif case.kind == "whisper":
            _count_synthetic_whisper(counts, case, result, actions)
    row = (f"| {label} | {frac(counts['built-hit'], counts['built-n'], True)} | {frac(counts['exact-ask'], counts['exact-n'], True)} | "
           f"{frac(counts['opposite'], counts['right-n'])} | {frac(counts['neither-hit'], counts['neither-n'])} | {frac(counts['dropped-hit'], counts['dropped-n'])} | "
           f"{counts['real-ask']}/{counts['real-n']} |")
    return row, real


def _changed_real_row(cases: list[Case], today: list[str], real: list[str]) -> str:
    names = [case.id for case in cases if case.split == "real" and case.kind == "whisper"]
    changed = [f"{names[i]}: {today[i]} -> {real[i]}" for i in range(len(real)) if real[i] != today[i]]
    return f"|  ... real clips changed vs today ({len(changed)}) | " + "; ".join(changed) + " | | | | | |"


def report_rules(cases: list[Case], policies: list[Policy], actions: Actions) -> list[str]:
    legend = ("A lives in the engine (aisis correction.py: the veto), B in the core (stt-cli repair.py: which reading `_disagreement` looks at). Columns: constructed flips "
              "(grammar-reachable, synthetic) where the truth member is offered; false asks on Whisper primaries that are the truth phrase; Whisper primaries that say the right member "
              "and got a question proposing the OPPOSITE; primaries that say neither member where the truth is offered; synthetic clips whose Whisper primary is dropped, truth offered; "
              "real clips (n=88, no truth, a count not a rate) that got a question.")
    head = ("| rule | constructed flips: truth offered | false ask, primary exact | right member, opposite proposed | neither member: truth offered | "
            "dropped primary: truth offered | real clips asked |")
    lines = ["", "## Rule candidates, measured (the engine's decision with each rule applied before `decide`)", "", legend]
    for policy in policies:
        lines += ["", f"Policy {policy}:", "", head, "|---|---|---|---|---|---|---|"]
        today: list[str] = []
        for label, rule in RULES:
            row, real = rule_row(label, rule, cases, policy, actions)
            lines.append(row)
            today = today or real
            if real != today:
                lines.append(_changed_real_row(cases, today, real))
    return lines


# ---------------------------------------------------------------- the grid

@dataclass(frozen=True)
class Point:
    policy: Policy
    right: tuple[int, int]            # Whisper primary says the right member: (asked, n); informational (a question about «в спании» is a repair)
    exact: tuple[int, int]            # Whisper primary is the truth phrase: (asked, n)  -> the false-ask the criterion limits
    flip: tuple[int, int]             # Whisper primary flipped (real): (truth offered, n)
    giga_exact: tuple[int, int]       # GigaAM-top primary is the truth phrase: (asked, n)
    built: tuple[int, int]            # constructed flips, grammar-reachable: (truth offered, n)  -> the recall the criterion maximises
    built_all: tuple[int, int]        # constructed flips, all clips

    @property
    def false_ask(self) -> float:
        return self.exact[0] / self.exact[1] if self.exact[1] else 0.0

    @property
    def recall(self) -> float:
        return self.built[0] / self.built[1] if self.built[1] else 0.0


def evaluate_point(cases: list[Case], policy: Policy, actions: Actions) -> Point:
    counts: Counter[tuple[str, str]] = Counter()
    for case in cases:
        result = outcome(case, policy, actions)
        offered = result.proposal in ("truth", "choice-truth")
        if case.kind == "constructed":
            counts["built_all", "n"] += 1
            counts["built_all", "hit"] += offered
            if case.reachable:
                counts["built", "n"] += 1
                counts["built", "hit"] += offered
            continue
        for seen in case.classes():
            if seen in ("right", "exact"):
                counts[f"{case.kind}-{seen}", "n"] += 1
                counts[f"{case.kind}-{seen}", "hit"] += result.kind in ASKED
        if result.seen == "flip" and case.kind == "whisper":
            counts["flip", "n"] += 1
            counts["flip", "hit"] += offered

    def pair(name: str) -> tuple[int, int]:
        return counts[name, "hit"], counts[name, "n"]
    return Point(policy, pair("whisper-right"), pair("whisper-exact"), pair("flip"), pair("giga-exact"), pair("built"), pair("built_all"))


def grid_policies(base: Policy) -> list[Policy]:
    names = list(GRID)
    return [dataclasses.replace(base, **dict(zip(names, values))) for values in itertools.product(*GRID.values())]


_WORK: tuple[list[Case], Actions] | None = None


def _evaluate_shared(policy: Policy) -> Point:
    assert _WORK is not None
    return evaluate_point(_WORK[0], policy, _WORK[1])


def run_grid(cases: list[Case], actions: Actions, policies: list[Policy], workers: int = 1) -> list[Point]:
    """Every policy on the synthetic cases. Several workers fork the (large, read-only) case list instead of copying it."""
    global _WORK
    synthetic = [case for case in cases if case.split == "synthetic"]
    _WORK = (synthetic, actions)
    try:
        if workers <= 1:
            return [_evaluate_shared(policy) for policy in policies]
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(workers) as pool:
            return pool.map(_evaluate_shared, policies, chunksize=8)
    finally:
        _WORK = None


def has_data(point: Point) -> bool:
    """False-ask and recall are measured: there are exact Whisper primaries and grammar-reachable constructed flips to divide by."""
    return bool(point.exact[1] and point.built[1])


def select(points: list[Point], limit: float = FALSE_ASK_MAX) -> Point | None:
    """The criterion: maximise constructed-flip recall (truth offered, grammar-reachable clips) subject to a false-ask rate <= `limit` on the clips
    whose Whisper primary is exactly the truth phrase (synthetic). Ties: fewer false asks, then the more conservative thresholds (higher
    ask_floor, higher margin_max, lower primary_weak, lower garbled_fit) so that a flat optimum is resolved towards asking less.
    A point without either denominator has no measurement: never selected."""
    feasible = [p for p in points if has_data(p) and p.false_ask <= limit]
    if not feasible:
        return None
    return min(feasible, key=lambda p: (-p.recall, p.false_ask, -p.policy.ask_floor, p.policy.margin_max, p.policy.primary_weak, p.policy.garbled_fit))


def frontier(points: list[Point]) -> list[Point]:
    """Points no other point beats on both axes (higher recall, lower false-ask), by recall."""
    best: list[Point] = []
    for p in sorted(points, key=lambda p: (p.false_ask, -p.recall)):
        if not best or p.recall > best[-1].recall:
            best.append(p)
    return best


def plateau(points: list[Point], chosen: Point, slack: float = 0.01) -> list[Point]:
    """The feasible points within `slack` of the chosen recall: how flat the optimum is."""
    return [p for p in points if p.false_ask <= FALSE_ASK_MAX and p.recall >= chosen.recall - slack]


def recommend(points: list[Point], chosen: Point, base: Policy | None = None) -> Point:
    """The plateau point nearest to the shipped policy (sum over the four fields of |value - shipped| over the field's grid span). The arg max of a
    flat plateau is noise (every point within 1 pp of it is statistically the same); a change from the shipped values has to buy a measurable gain, so
    the shipped policy itself wins whenever it is on the plateau."""
    base = base or Policy()

    def distance(p: Point) -> tuple[float, float]:
        far = sum(abs(getattr(p.policy, name) - getattr(base, name)) / (max(values) - min(values)) for name, values in GRID.items())
        return far, -p.recall
    return min(plateau(points, chosen), key=distance)


def sensitivity(points: list[Point], base: Policy | None = None) -> list[str]:
    """One field at a time around the shipped policy: what each threshold buys on its own."""
    base = base or Policy()
    by_policy = {p.policy: p for p in points}
    lines = ["", "### One field at a time around the shipped policy", ""] + POINT_HEAD
    for name, values in GRID.items():
        for value in values:
            found = by_policy.get(dataclasses.replace(base, **{name: value}))
            if found is not None:
                lines.append(render_point_row(found, f"{name}={value}" + (" (shipped)" if getattr(base, name) == value else "")))
    return lines


def render_point_row(p: Point, label: str = "") -> str:
    pol = p.policy
    return (f"| {label} | {pol.ask_floor} | {pol.primary_weak} | {pol.margin_max} | {pol.garbled_fit} | {frac(*p.exact, True)} | {frac(*p.right)} | {frac(*p.giga_exact)} | "
            f"{frac(*p.built, True)} | {frac(*p.built_all)} | {frac(*p.flip, True)} |")


POINT_HEAD = [("| | ask_floor | primary_weak | margin_max | garbled_fit | false-ask, Whisper primary exactly right (asked/n) | ... Whisper primary says the right member | ... GigaAM-top primary exactly right | "
               "constructed-flip recall, reachable (truth offered/n) | constructed-flip recall, all clips | real Whisper flips: truth offered |"),
              "|---|---|---|---|---|---|---|---|---|---|---|"]


def voice_holdout(cases: list[Case], actions: Actions, policies: list[Policy]) -> list[str]:
    """Choose on two voices, test on the other two (and back): does the choice survive voices it was not fitted on?"""
    lines = ["", "### Voice hold-out (selection on one half of the voices, the same criterion measured on the other half)", "",
             "| fitted on | chosen policy (ask_floor / primary_weak / margin_max / garbled_fit) | fit: false-ask / recall | held out: false-ask | held out: recall | shipped defaults held out: false-ask / recall |", "|---|---|---|---|---|---|"]
    for fit_voices, test_voices in (VOICE_HALVES, VOICE_HALVES[::-1]):
        fit = [c for c in cases if c.voice in fit_voices]
        test = [c for c in cases if c.voice in test_voices]
        points = [evaluate_point(fit, policy, actions) for policy in policies]
        chosen = select(points)
        if chosen is None:
            lines.append(f"| {', '.join(fit_voices)} | no policy meets the constraint | | | | |")
            continue
        held = evaluate_point(test, chosen.policy, actions)
        shipped = evaluate_point(test, Policy(), actions)
        p = chosen.policy
        lines.append(f"| {', '.join(fit_voices)} (n={len(fit)}) | {p.ask_floor} / {p.primary_weak} / {p.margin_max} / {p.garbled_fit} | {frac(*chosen.exact)} / {frac(*chosen.built)} | "
                     f"{frac(*held.exact, True)} | {frac(*held.built, True)} | {frac(*shipped.exact)} / {frac(*shipped.built)} |")
    return lines


def report_grid(cases: list[Case], actions: Actions, workers: int = 1) -> tuple[list[str], list[str], Point | None]:
    """(markdown summary, the full grid table, the selected point)."""
    policies = grid_policies(Policy())
    points = run_grid(cases, actions, policies, workers)
    chosen = select(points)
    shipped = run_grid(cases, actions, [Policy()], 1)[0]
    criterion = (f"{len(points)} grid points {GRID}, max_words fixed at {Policy().max_words}. Criterion: maximise the constructed-flip recall (truth offered as the "
                 f"proposed or a chosen option, grammar-reachable clips) subject to a false-ask rate <= {100 * FALSE_ASK_MAX:g}% on the synthetic clips whose Whisper primary "
                 "is exactly the truth phrase. Wilson 95 % intervals in brackets.")
    lines = ["## Grid search", "", criterion, "", "Shipped defaults:", ""] + POINT_HEAD + [render_point_row(shipped, "defaults")]
    if chosen is None:
        lines += ["", "No grid point meets the constraint." if has_data(shipped) else
                  "Insufficient data: the corpus has no exact Whisper primaries or no grammar-reachable constructed flips, so no false-ask rate or recall is measured; nothing is selected or recommended."]
        return lines, full_table(points), None
    flat = plateau(points, chosen)
    recommended = recommend(points, chosen)
    lines += ["", "Selected:", ""] + POINT_HEAD + [render_point_row(chosen, "selected")]
    lines += ["", f"Plateau: {len(flat)} of {len(points)} points are feasible and within 1 pp of the selected recall; their ranges: "
              + ", ".join(f"{name} {min(getattr(p.policy, name) for p in flat)}-{max(getattr(p.policy, name) for p in flat)}" for name in GRID) + "."]
    lines += ["", "Recommended (the plateau point nearest to the shipped values):", ""] + POINT_HEAD + [render_point_row(recommended, "recommended")]
    lines += sensitivity(points)
    lines += ["", "Pareto frontier (false-ask against constructed-flip recall):", ""] + POINT_HEAD + [render_point_row(p, "frontier") for p in frontier(points)]
    lines += voice_holdout([c for c in cases if c.split == "synthetic"], actions, policies)
    return lines, full_table(points), chosen


def full_table(points: list[Point]) -> list[str]:
    return POINT_HEAD + [render_point_row(p, str(index)) for index, p in enumerate(sorted(points, key=lambda p: (p.false_ask, -p.recall)), 1)]


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("nbest", "report", "grid"))
    parser.add_argument("--corpus", type=Path, default=corpus.DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--policy", default="{}", help="JSON of Policy fields for `report`")
    parser.add_argument("--workers", type=int, default=1, help="processes for `grid`")
    parser.add_argument("--strict-tiers", action="store_true", help="only execute/query decisions are actions (sensitivity run)")
    args = parser.parse_args(argv)
    if args.mode == "nbest":
        python = os.environ.get("AISIS_GIGA_PYTHON", DEFAULT_GIGA_PYTHON)

        async def run() -> dict[str, int]:
            args.corpus.mkdir(parents=True, exist_ok=True)
            # The corpus lock (the one a corpus build or `bench_confusables.py real` takes) spans the metadata, the audio reads and every decode: the
            # transcripts and constructed primaries cached here always belong to the audio they were scored on.
            with (args.corpus / "results.jsonl.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                async with GigaCtc(python, threads=4, request_timeout=60.0) as giga:
                    return await build_cache(synthetic_clips(args.corpus) + real_clips(args.corpus), giga, args.corpus / "nbest.jsonl", limit=args.limit)
        print(asyncio.run(run()))
        return 0
    args.corpus.mkdir(parents=True, exist_ok=True)   # before the grid: a missing directory must not cost its result
    actions = grammar_actions(execute_query_key if args.strict_tiers else action_key)
    cases = load_cases(args.corpus, actions)
    if args.mode == "report":
        policy = Policy.from_dict(json.loads(args.policy))
        body = (report_synthetic(cases, policy, actions) + report_real(cases, policy, actions) + report_junk(cases, policy, actions)
                + report_rules(cases, [policy, dataclasses.replace(policy, ask_floor=0.1)], actions))
        text = "\n".join(["# Correction policy: decisions on stored clips", ""] + body) + "\n"
        write_private(args.corpus / "policy-report.md", text)
        print(text)
        return 0
    lines, table, _ = report_grid(cases, actions, args.workers)
    write_private(args.corpus / "policy-grid.md", "\n".join(["# Policy grid: every point", ""] + table) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
