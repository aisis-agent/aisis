"""Test doubles of the stt-cli core's contract (stt_cli.hypotheses: GigaCtc, Confusables, NBest, same_text) for the gateway tests.

The glue under test (second_opinion.py, stt_wyoming.py) must not need a model or the real core: these fakes implement exactly the
calls the glue makes (decode, score, alive, close, candidates, groups_in, same_text) and let a test script their behaviour.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace


def normalize(text):
    return " ".join(text.lower().replace("ё", "е").replace(".", " ").replace(",", " ").replace("?", " ").split())


def same_text(a, b):
    return normalize(a) == normalize(b)


@dataclass
class FakeHypothesis:
    text: str
    percent: int


class FakeNBest:
    def __init__(self, items, engine="gigaam-v3-ctc", ms=None):
        self.hypotheses = tuple(FakeHypothesis(text, percent) for text, percent in items)
        self.engine, self.ms = engine, ms

    @property
    def top(self):
        return self.hypotheses[0] if self.hypotheses else None

    def as_dict(self):
        return {"engine": self.engine, "ms": self.ms, "hypotheses": [{"text": h.text, "percent": h.percent} for h in self.hypotheses]}


@dataclass
class FakeCandidate:
    text: str


class FakeConfusables:
    """«включи» <-> «выключи»: the one set the tests need."""
    PAIRS = {"включи": "выключи", "выключи": "включи"}

    def groups_in(self, text):
        return ["on-off"] if any(word in self.PAIRS for word in normalize(text).split()) else []

    def candidates(self, text, *, limit=6):
        words = normalize(text).split()
        return [FakeCandidate(" ".join(self.PAIRS.get(w, w) if i == index else w for i, w in enumerate(words)))
                for index, w in enumerate(words) if w in self.PAIRS][:limit]


class FakeWorker:
    """A GigaCtc stand-in. `top` is what the second recogniser hears; `delay` makes decode slow, `score_delay` the scoring; `hang_start`
    never finishes starting; `fail_*` raise."""
    instances: list = []
    script = staticmethod(lambda worker: None)   # a test replaces it (see core()) to configure every worker the glue creates

    def __init__(self, python, *, model="gigaam-v3-ctc", threads=4, **extra):
        self.python, self.model, self.threads = python, model, threads
        self.started = self.closed = False
        self.alive = False
        self.decodes, self.scores = [], []
        self.top = "выключи свет"
        self.delay = self.score_delay = 0.0
        self.hang_start = False
        self.fail_start = self.fail_decode = self.fail_score = None
        FakeWorker.instances.append(self)
        self.script(self)

    async def start(self):
        if self.hang_start:
            await asyncio.Event().wait()
        if self.fail_start is not None:
            raise self.fail_start
        self.started = self.alive = True

    async def decode(self, pcm, *, rate=16000, beam=8):
        self.decodes.append((len(pcm), rate))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_decode is not None:
            self.alive = False
            raise self.fail_decode
        return SimpleNamespace(handle=len(self.decodes), ms=70, nbest=FakeNBest([(self.top, 100)], ms=70))

    async def score_alternatives(self, handle, *phrases):
        """What the core does: the phrases and the decode's own top, each with its confusable swaps, priced as one NBest."""
        seeds = list(phrases) + [self.top]
        confusables = FakeConfusables()
        texts = list(seeds)
        for seed in seeds:
            texts += [candidate.text for candidate in confusables.candidates(seed)]
        kept = []
        for text in texts:
            if not any(same_text(text, other) for other in kept):
                kept.append(text)
        self.scores.append((handle, kept))
        if self.score_delay:
            await asyncio.sleep(self.score_delay)
        if self.fail_score is not None:
            raise self.fail_score
        return FakeNBest([(text, 100 // len(kept)) for text in kept], ms=3)

    async def close(self):
        self.closed, self.alive = True, False


def core(script=None):
    """A fresh fake core; `script(worker)` configures every worker the glue creates."""
    FakeWorker.instances = []
    FakeWorker.script = staticmethod(script or (lambda worker: None))
    return SimpleNamespace(GigaCtc=FakeWorker, confusables=FakeConfusables(), same_text=same_text)
