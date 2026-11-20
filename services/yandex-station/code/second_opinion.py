"""A second recognition of the same audio by a different architecture (GigaAM-v3 CTC on the CPU), beside Whisper.

Whisper (the live recogniser) answers a transcript and nothing else: no alternatives, no confidence. The CTC model gives both:
the posteriors of its own readings and the exact likelihood of ANY candidate text, so the confusable reading of a command
(«выключи свет» for «включи свет») is scored instead of hoped for. The engine is the stt-cli core (`stt_cli.hypotheses`, pinned in
requirements-stt-lib.txt): the worker process, the CTC maths, the confusable sets and the NBest model all live there. This module
is only the gateway's glue: the switches, a warm worker that starts lazily and restarts at most once per RESTART_BACKOFF_S, the
decision whether a final waits for the opinion, and the guarantee that none of it can raise into a voice turn.

    task = second.begin(pcm, rate)                       # at the same moment the PCM goes to Whisper
    ...Whisper answers `text`...
    opinion = await second.resolve(task, text, wait_ms)  # Opinion(nbest, ms, status); nbest is None unless status == "ok"

Statuses of an opinion without evidence: `disabled`, `unavailable` (no engine, no core, worker inside its backoff, or the work was
abandoned after TASK_TTL_S), `busy` (MAX_OUTSTANDING decodes and scorings are already in flight: a stalled worker must not keep the audio
of every later turn), `timeout` (not ready within `wait_ms`: the decode and a scoring already started are NOT cancelled, a later
`resolve(task, ...)` waits for the same work) and `error:<ExceptionClass>`.
Settings (all optional): AISIS_HYP_ENABLED (off unless 1/true/yes/on), AISIS_HYP_PYTHON (interpreter of the onnx-asr engine
environment; default `<stt-cli home>/engines/onnx-asr/bin/python` when it exists), AISIS_HYP_MODEL (gigaam-v3-ctc), AISIS_HYP_THREADS (4),
AISIS_HYP_WAIT_MS (300: how long a final that needs the opinion may wait beyond Whisper).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from types import SimpleNamespace

LOG = logging.getLogger("aisis.stt")

DEFAULT_MODEL = "gigaam-v3-ctc"
DEFAULT_THREADS = 4
DEFAULT_WAIT_MS = 300
RESTART_BACKOFF_S = 30.0    # a worker (re)start is attempted at most this often: a model that cannot load must not be retried per utterance
WAIT_MAX_WORDS = 8          # only a short, command-shaped utterance is worth holding its final for
WORKER_RATE = 16000         # giga_worker.py decodes 16 kHz only; the station streams 16 kHz
MAX_OUTSTANDING = 8         # decodes and scorings in flight at once; the worker is serial, so more than this means it is stalled
TASK_TTL_S = 20.0           # a decode or scoring still running this long after it began is cancelled: it holds its audio, and a worker start that hangs holds the start lock
_TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Opinion:
    nbest: object | None    # stt_cli.hypotheses.model.NBest when status == "ok"
    ms: int | None          # decode wall time of the worker plus the scoring of the candidates
    status: str


@dataclass
class _Decoding:
    worker: object | None
    decoded: object | None
    status: str
    scoring: tuple | None = None   # (text, job): the scoring of `text` on this decode, started once and shared by every later `resolve`


class _Unavailable(Exception):
    """No worker can be had now (no engine, no core, or the restart backoff is running); not an error of a request."""


def load_core():
    """The stt-cli core's pieces the glue uses; ImportError when the pinned core is not installed in this environment."""
    from stt_cli.hypotheses import confusables, giga
    return SimpleNamespace(GigaCtc=giga.GigaCtc, confusables=confusables.load())


def default_engine_python(env) -> str | None:
    home = env.get("STT_HOME") or os.path.join(env.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "stt-cli")
    interpreter = os.path.join(home, "engines", "onnx-asr", "bin", "python")
    return interpreter if os.access(interpreter, os.X_OK) else None


def _positive_int(value, default, floor):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= floor else default


def _consume(future):
    """Retrieve the outcome of a future nobody awaits any more (a request the caller stopped waiting for)."""
    if not future.cancelled():
        future.exception()


class SecondOpinion:
    def __init__(self, env=None, load_core=load_core, clock=time.monotonic):
        env = dict(os.environ) if env is None else env
        self.enabled = str(env.get("AISIS_HYP_ENABLED", "")).strip().lower() in _TRUE
        self.python = env.get("AISIS_HYP_PYTHON") or default_engine_python(env)
        self.model = env.get("AISIS_HYP_MODEL") or DEFAULT_MODEL
        self.threads = _positive_int(env.get("AISIS_HYP_THREADS"), DEFAULT_THREADS, 1)
        self.wait_ms = _positive_int(env.get("AISIS_HYP_WAIT_MS"), DEFAULT_WAIT_MS, 0)
        self._load = load_core
        self._clock = clock
        self._core = None
        self._worker = None
        self._lock = asyncio.Lock()
        self._next_start_at = 0.0
        self._state = "idle" if self.enabled else "disabled"
        self._last_ms = None
        self._tasks = set()
        self._closed = False

    # --- the switches and the report -------------------------------------------------------------------------------

    def health(self):
        return {"enabled": self.enabled, "state": self._state, "model": self.model, "last_ms": self._last_ms}

    def _set_state(self, state):
        if state != self._state:
            LOG.info("hyp_state state=%s", state)
            self._state = state

    # --- the worker -------------------------------------------------------------------------------------------------

    def _core_or_down(self):
        if self._core is None:
            try:
                self._core = self._load()
            except Exception as exc:   # the pinned core is not installed (or does not load): the feature is off, nothing else changes
                self._next_start_at = self._clock() + RESTART_BACKOFF_S
                self._set_state("down:" + type(exc).__name__)
                raise _Unavailable from exc
        return self._core

    async def _client(self):
        worker = self._worker
        if worker is not None and worker.alive:
            return worker
        async with self._lock:
            worker = self._worker
            if worker is not None and worker.alive:
                return worker
            now = self._clock()
            if now < self._next_start_at:
                raise _Unavailable
            self._worker = None
            if worker is not None:   # the one that died
                await self._quiet_close(worker)
            core = self._core_or_down()
            if not self.python:
                self._next_start_at = now + RESTART_BACKOFF_S
                self._set_state("down:no-engine")
                raise _Unavailable
            self._next_start_at = now + RESTART_BACKOFF_S
            self._set_state("starting")
            worker = core.GigaCtc(self.python, model=self.model, threads=self.threads, confusables=core.confusables)
            try:
                await worker.start()
            except BaseException as exc:
                self._set_state("down:" + type(exc).__name__)
                await asyncio.shield(self._quiet_close(worker))
                raise
            self._worker = worker
            self._set_state("ready")
            return worker

    @staticmethod
    async def _quiet_close(worker):
        try:
            await worker.close()
        except Exception as exc:
            LOG.info("hyp_worker_close_failed class=%s", type(exc).__name__)

    def _failed(self, worker, exc):
        if worker is None or not worker.alive:   # a dead worker is a crash; a live one only failed this request
            self._set_state("down:" + type(exc).__name__)

    async def warm(self):
        """Start the worker off the request path so the first utterance does not pay the model load. Never raises. The start is one
        counted job under the same deadline as a decode: a hung `worker.start()` is cancelled (its worker closed, the lock released)."""
        if not self.enabled or self._closed:
            return
        job = self._track(self._client())
        try:
            await asyncio.wait({job})
        except asyncio.CancelledError:
            raise
        if job.cancelled():
            LOG.warning("hyp_warm_failed class=timeout")
            return
        exc = job.exception()
        if exc is not None and not isinstance(exc, _Unavailable):
            LOG.warning("hyp_warm_failed class=%s", type(exc).__name__)

    async def close(self):
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        worker, self._worker = self._worker, None
        if worker is not None:
            await self._quiet_close(worker)

    # --- the request path -------------------------------------------------------------------------------------------

    def begin(self, pcm, rate):
        """Start decoding `pcm` now; the awaitable for `resolve`. None when the feature is off or the audio is not 16 kHz (the worker's only rate)."""
        if not self.enabled or self._closed or rate != WORKER_RATE:
            return None
        if len(self._tasks) >= MAX_OUTSTANDING:
            LOG.warning("hyp_busy outstanding=%d", len(self._tasks))
            refused = asyncio.get_running_loop().create_future()
            refused.set_result(_Decoding(None, None, "busy"))
            return refused
        return self._track(self._decode(pcm, rate))

    def _track(self, coroutine):
        """Run `coroutine` as one counted job: close() cancels it, and so does TASK_TTL_S after it began."""
        job = asyncio.ensure_future(coroutine)
        expiry = asyncio.get_running_loop().call_later(TASK_TTL_S, job.cancel)
        self._tasks.add(job)
        job.add_done_callback(lambda finished: self._release(finished, expiry))
        return job

    def _release(self, job, expiry):
        expiry.cancel()
        self._tasks.discard(job)
        _consume(job)   # a job nobody awaits any more must not leave an unretrieved exception behind

    async def _decode(self, pcm, rate):
        worker = None
        try:
            worker = await self._client()
            decoded = await worker.decode(pcm, rate=rate)
            return _Decoding(worker, decoded, "ok")
        except asyncio.CancelledError:
            raise
        except _Unavailable:
            return _Decoding(None, None, "unavailable")
        except Exception as exc:
            self._failed(worker, exc)
            return _Decoding(None, None, "error:" + type(exc).__name__)

    def waits_for(self, text):
        """Does a final carrying `text` wait for the opinion: a short utterance holding a word of a confusable set. Never raises."""
        if not self.enabled or len(text.split()) > WAIT_MAX_WORDS:
            return False
        try:
            return bool(self._core_or_down().confusables.groups_in(text))
        except Exception:
            return False

    async def resolve(self, task, primary_text, wait_ms):
        """The opinion for `task` (from `begin`) about `primary_text`, waiting at most `wait_ms` from now for decode AND scoring: the CTC
        model prices the primary reading, its confusable swaps and the swaps of its own top reading on the same audio (core: score_alternatives).
        Never raises (a cancellation of the caller excepted); a timeout leaves the decode and the scoring running for a later call, which
        waits for the same scoring instead of asking the worker again."""
        if task is None:
            return Opinion(None, None, "disabled")
        try:
            deadline = asyncio.get_running_loop().time() + wait_ms / 1000
            if not await self._finished(task, deadline):
                return Opinion(None, None, "timeout")
            if task.cancelled():
                return Opinion(None, None, "unavailable")
            decoding = task.result()
            if decoding.status != "ok":
                return Opinion(None, None, decoding.status)
            scoring = self._scoring_of(decoding, primary_text)
            if scoring is None:
                return Opinion(None, None, "busy")
            if not await self._finished(scoring, deadline):
                return Opinion(None, None, "timeout")
            if scoring.cancelled():
                return Opinion(None, None, "unavailable")
            nbest, self._last_ms = scoring.result()
            return Opinion(nbest, self._last_ms, "ok")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed(self._worker, exc)
            return Opinion(None, None, "error:" + type(exc).__name__)

    @staticmethod
    async def _finished(job, deadline):
        """Is `job` done by `deadline` (event-loop time)? A job still running at the deadline keeps running."""
        loop = asyncio.get_running_loop()
        done, _ = await asyncio.wait({job}, timeout=max(0.0, deadline - loop.time()))
        return bool(done)

    def _scoring_of(self, decoding, text):
        """The tracked scoring of `text` on this decode, or None when the outstanding-work cap is reached (admission counts decodes AND
        scorings; a refused scoring is retried by a later call)."""
        if decoding.scoring is None or decoding.scoring[0] != text:
            if len(self._tasks) >= MAX_OUTSTANDING:
                LOG.warning("hyp_busy outstanding=%d scoring", len(self._tasks))
                return None
            decoding.scoring = (text, self._track(self._score(decoding, text)))
        return decoding.scoring[1]

    @staticmethod
    async def _score(decoding, text):
        """(NBest, ms): the worker's scoring of `text` on this decode; ms is the decode's own time plus the scoring's wall time."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        nbest = await decoding.worker.score_alternatives(decoding.decoded.handle, *([text] if text.strip() else []))
        return nbest, decoding.decoded.ms + int((loop.time() - began) * 1000)
