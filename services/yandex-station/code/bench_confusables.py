#!/usr/bin/env python3
"""Measurement of the two recognisers on Russian command confusables (aisis ticket #182): where the thresholds of the correction policy come from.

  run      every clip of the labelled synthetic set (confusables_corpus.py) through (a) the live Wyoming Whisper at AISIS_WYOMING_URI and
           (b) GigaAM v3 CTC int8 on CPU in a warm worker process (onnx-asr engine env); one JSONL line per clip
  summary  exact-match and polarity-error rates per engine, per pair set and per augmentation (n in every cell) and the agreement matrix
           between the engines: how often their disagreement coincides with a polarity error of Whisper, of GigaAM, of both
  real     the stored real clips (voice_clips status `sent`) through both engines: per-clip agreement and the list of disagreements for a
           human to read. They have no truth except the rows the owner corrected in `clip_truth` (a handful): no accuracy is claimed here.

A polarity error is "heard the opposite member of the pair" (включи/выключи, ...): the transcript carries the marker of the other member and
not of the right one (confusables_corpus.heard_polarity). It is NOT a word error rate; a transcript with neither marker is `none`.

Both engines get the copy the gateway sends to its recogniser: the clip lifted by the speech front-end (bench_asr_archive.lift). Whisper is
asked directly (stt_wyoming.transcribe), never through the gateway, one request at a time, and nothing is written to the archive, the clip
feed, a speaker or Home Assistant. The synthetic split says nothing about the owner's voice.

    bench_confusables.py run [--corpus DIR] [--limit N] [--pause S]      (resumes: clips already in results.jsonl are skipped)
    bench_confusables.py summary [--corpus DIR]
    bench_confusables.py real [--out FILE] [--limit N]

Run in the gateway's venv (numpy, aiohttp); the GigaAM worker is this same file started by the onnx-asr engine's interpreter (AISIS_GIGA_PYTHON).

`policy_eval(rows, decide)` is the hook for the repair policy: it asks `decide` about every result row and tabulates what it did.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
from collections import defaultdict
from pathlib import Path

import confusables_corpus as corpus

RATE = 16000
DEFAULT_URI = "tcp://127.0.0.1:10300"
DEFAULT_GIGA_PYTHON = Path.home() / ".local/share/stt-cli/engines/onnx-asr/bin/python"
GIGA_MODEL = "gigaam-v3-ctc"
GIGA_THREADS = 4
ENGINES = ("whisper", "giga")
DB = Path(os.environ.get("AISIS_DIALOGS_DB") or Path.home() / ".local/share/aisis/dialogs.sqlite3")
ARCHIVE = Path(os.environ.get("AISIS_VOICE_ARCHIVE") or Path.home() / ".local/share/aisis/voice-archive")


# ---------------------------------------------------------------- GigaAM worker (runs in the onnx-asr engine's interpreter)

def giga_worker() -> int:
    """stdin: `<n>\\n` + n bytes of 16 kHz s16le PCM, repeated; stdout: one JSON line per request {text, ms, min_prob} (first line {ready})."""
    import numpy as np
    import onnx_asr
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = int(os.environ.get("AISIS_GIGA_THREADS", GIGA_THREADS))
    model = onnx_asr.load_model(os.environ.get("AISIS_GIGA_MODEL", GIGA_MODEL), quantization="int8", sess_options=options,
                                providers=["CPUExecutionProvider"]).with_timestamps()
    out = sys.stdout
    out.write(json.dumps({"ready": True}) + "\n")
    out.flush()
    stdin = sys.stdin.buffer
    while True:
        header = stdin.readline()
        if not header:
            return 0
        pcm = stdin.read(int(header))
        wave = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        started = time.monotonic()
        result = model.recognize(wave, sample_rate=RATE)
        probs = [float(np.exp(x)) for x in (result.logprobs or [])]
        out.write(json.dumps({"text": result.text, "ms": round((time.monotonic() - started) * 1000), "min_prob": round(min(probs), 3) if probs else None},
                             ensure_ascii=False) + "\n")
        out.flush()


class Giga:
    """The warm worker: one process, one request at a time. A worker that stalls is killed at its deadline, so no read or write waits forever."""
    START_SECONDS = 600   # model load on a loaded host
    REQUEST_SECONDS = 120

    def __init__(self, python: Path | str):
        self.proc = subprocess.Popen([str(python), os.path.abspath(__file__), "_giga-worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        try:
            ready = json.loads(self._guarded(self.proc.stdout.readline, self.START_SECONDS) or "{}")
        except ValueError:
            ready = {}
        if not ready.get("ready"):
            self.proc.terminate()
            self.close()
            raise RuntimeError("giga_worker_failed_to_start")

    def _guarded(self, action, seconds):
        """`action()` with the worker ended when it has not finished in `seconds`: the blocked call then fails or returns EOF."""
        timer = threading.Timer(seconds, self.proc.kill)
        timer.start()
        try:
            return action()
        finally:
            timer.cancel()

    def decode(self, pcm: bytes) -> dict:
        def exchange():
            self.proc.stdin.write(b"%d\n" % len(pcm) + pcm)
            self.proc.stdin.flush()
            return self.proc.stdout.readline()
        try:
            line = self._guarded(exchange, self.REQUEST_SECONDS)
        except OSError as error:   # BrokenPipeError: the worker is gone
            raise RuntimeError("giga_worker_died") from error
        if not line:
            raise RuntimeError("giga_worker_died")
        return json.loads(line)

    def close(self) -> None:
        """Stop the worker; every step is bounded and a failing one does not stop the next."""
        for step in (self.proc.stdin.close, lambda: self.proc.wait(timeout=30), self.proc.stdout.close):
            try:
                step()
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()   # ignored SIGTERM
            self.proc.wait(timeout=10)



# ---------------------------------------------------------------- audio handed to the engines

def voiced_frames(wave):
    """Frames whose RMS is at least 30 % of the loudest frame's."""
    import numpy as np
    count = len(wave) // corpus.FRAME
    if count == 0:
        return np.zeros(0, dtype=bool)
    energy = np.sqrt((wave[:count * corpus.FRAME].astype("float64").reshape(count, corpus.FRAME) ** 2).mean(axis=1))
    return energy >= 0.3 * energy.max()


def voiced_ms_of(wave) -> int:
    return int(voiced_frames(wave).sum()) * 20


def recognizer_pcm(wave, trailing_ms: int) -> bytes:
    """What the gateway sends its recogniser for this audio: the clip cut like the live utterance (Utterance.speech_pcm keeps only TAIL_MS of
    the closing silence) and THEN lifted by the speech front-end, as 16-bit PCM.
    `trailing_ms` is the closing silence the corpus built into the clip (corpus.TAIL_S): known, not guessed from noisy audio."""
    import bench_asr_archive
    import numpy as np
    import stt_wyoming
    raw = (wave * 32768).clip(-32768, 32767).astype("<i2").tobytes()
    cut = stt_wyoming.Utterance(raw, RATE, int(voiced_frames(wave).sum()), trailing_ms, 0, 0, 0.0, None).speech_pcm
    trimmed = np.frombuffer(cut, dtype="<i2").astype("float32") / 32768.0
    lifted = bench_asr_archive.lift(trimmed, voiced_ms_of(trimmed))
    return (lifted * 32768).clip(-32768, 32767).astype("<i2").tobytes()


def read_clip(path: Path):
    import wave

    import numpy as np
    with wave.open(str(path), "rb") as clip:
        if (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path} is not 16 kHz mono 16-bit")
        return np.frombuffer(clip.readframes(clip.getnframes()), dtype="<i2").astype("float32") / 32768.0


def recognizer_extras() -> dict:
    """The profile and domain context the live gateway sends with every request (STTGateway.recognizer_extras), from the same environment."""
    import stt_wyoming
    vocab = stt_wyoming.voice_vocab
    catalog = None
    if os.environ.get("AISIS_STT_DOMAIN_CONTEXT") == "1":
        loaded = vocab.fetch_catalog(os.environ.get("AISIS_HACLI_URL", "http://127.0.0.1:8795"), vocab.library_artists)   # loaded now: the live cache is warm, a cold one answers empty
        catalog = types.SimpleNamespace(get=lambda: loaded)
    return stt_wyoming.STTGateway.recognizer_extras(types.SimpleNamespace(env=os.environ, domain_catalog=catalog))


async def whisper_text(uri: str, pcm: bytes, extras: dict | None = None) -> dict:
    import stt_wyoming
    started = time.monotonic()
    try:
        text = await asyncio.wait_for(stt_wyoming.transcribe(uri, pcm, RATE, "ru", timeout=60, **(extras or {})), 70)   # the whole exchange, close included
        return {"text": text, "ms": round((time.monotonic() - started) * 1000)}
    except (RuntimeError, OSError, asyncio.TimeoutError, ValueError, KeyError, AttributeError, TypeError, asyncio.IncompleteReadError) as error:   # ValueError: a reply that is not JSON; KeyError/AttributeError/TypeError: JSON of the wrong shape; IncompleteReadError: closed mid payload
        return {"text": None, "ms": round((time.monotonic() - started) * 1000), "error": type(error).__name__ + ": " + str(error)[:80]}


# ---------------------------------------------------------------- run

def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def read_results(path: Path, repair: bool = False) -> list[dict]:
    """results.jsonl rows. An unparseable FINAL line is an interrupted write: dropped (and, with `repair`, truncated away, and a missing trailing
    newline restored so the next append starts a fresh line). An unparseable line anywhere else is real corruption and raises."""
    if not path.exists():
        return []
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    last = max((index for index, line in enumerate(lines) if line.strip()), default=-1)
    rows, offset = [], 0
    for index, line in enumerate(lines):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                if index != last:
                    raise
                if repair:
                    with path.open("r+b") as handle:
                        handle.truncate(offset)
                return rows
        offset += len(line)
    if repair and data and not data.endswith(b"\n"):
        with path.open("ab") as handle:
            handle.write(b"\n")
    return rows


def stage_private(path: Path, text: str) -> str:
    """Write `text` to a unique O_EXCL temporary file beside `path`, owner-only (0600): the exports hold household speech. Returns its name."""
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return name


def write_private(path: Path, text: str) -> None:
    os.replace(stage_private(path, text), path)


def publish_pair(results: Path, rows_text: str, report: Path, report_text: str) -> None:
    """Both files are staged before either destination changes; the old report goes first, so a failure leaves no report that contradicts the rows."""
    staged = [stage_private(results, rows_text)]
    try:
        staged.append(stage_private(report, report_text))
    except BaseException:
        Path(staged[0]).unlink(missing_ok=True)
        raise
    report.unlink(missing_ok=True)
    os.replace(staged[0], results)
    os.replace(staged[1], report)


def run(corpus_dir: Path, uri: str, giga_python: Path | str, limit: int | None, pause: float) -> int:
    results = corpus_dir / "results.jsonl"
    # The lock spans reading the manifest and done-set and every append, so a concurrent run waits and then sees these clips as done instead of
    # repeating them, and a corpus rebuild (which takes the same lock) cannot change the clips under the run.
    with (corpus_dir / "results.jsonl.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (corpus_dir / "manifest.jsonl").exists():
            raise FileNotFoundError(f"{corpus_dir / 'manifest.jsonl'}: build the corpus first (confusables_corpus.py build)")
        rows = read_jsonl(corpus_dir / "manifest.jsonl")
        done = {row["id"] for row in read_results(results, repair=True)}
        todo = [row for row in rows if row["id"] not in done][:limit]
        if not todo:
            print(f"done: nothing to run, {len(done)} in {results}")
            return 0
        extras = recognizer_extras()
        giga = Giga(giga_python)
        started = time.monotonic()
        try:
            with results.open("a", encoding="utf-8") as out:
                for index, row in enumerate(todo, 1):
                    pcm = recognizer_pcm(read_clip(corpus_dir / row["path"]), row.get("trailing_ms", round(corpus.TAIL_S * 1000)))
                    heard = asyncio.run(whisper_text(uri, pcm, extras))
                    out.write(json.dumps({**row, "whisper": heard, "giga": giga.decode(pcm)}, ensure_ascii=False) + "\n")
                    out.flush()
                    if index % 100 == 0:
                        print(f"{index}/{len(todo)} clips, {time.monotonic() - started:.0f} s", flush=True)
                    if pause:
                        time.sleep(pause)
        finally:
            giga.close()
    print(f"done: {len(todo)} new clips, {len(done) + len(todo)} in {results}")
    return 0


# ---------------------------------------------------------------- summary

def heard(row: dict, engine: str) -> str | None:
    return (row.get(engine) or {}).get("text")


def classify(row: dict, engine: str) -> str:
    """exact | right (the right member said, other words differ) | flip (the opposite member) | none (neither member) | both | error."""
    text = heard(row, engine)
    if text is None:
        return "error"
    found = corpus.verdict(row, text)
    if found["exact"]:
        return "exact"
    if found["flip"]:
        return "flip"
    return {"none": "none", "both": "both"}.get(found["heard"], "right")


def cell(rows: list[dict]) -> dict:
    """n and the share of each outcome per engine."""
    out = {"n": len(rows)}
    for engine in ENGINES:
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[classify(row, engine)] += 1
        out[engine] = dict(counts)
    return out


def grouped(rows: list[dict], key: str, order: tuple[str, ...] | None = None) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    names = [name for name in order if name in groups] if order else sorted(groups)
    return {name: cell(groups[name]) for name in names}


def engines_agree(row: dict, level: str) -> bool:
    """Whether the two transcripts agree: `text` = equal after normalisation, `polarity` = they say the same member of the pair (or neither)."""
    w, g = heard(row, "whisper"), heard(row, "giga")
    if w is None or g is None:
        return False
    if level == "text":
        return corpus.same_text(w, g)
    return corpus.heard_polarity(row["pair_set"], w) == corpus.heard_polarity(row["pair_set"], g)


def comparable(rows: list[dict]) -> list[dict]:
    """The rows where BOTH engines returned a text: an engine that failed says nothing about whether the two agree."""
    return [row for row in rows if heard(row, "whisper") is not None and heard(row, "giga") is not None]


def agreement(rows: list[dict], level: str = "text") -> dict:
    """Counts of (engines agree?, Whisper flip?, GigaAM flip?); `level` says what agreeing means (see engines_agree)."""
    counts: dict[tuple[bool, bool, bool], int] = defaultdict(int)
    known = comparable(rows)
    for row in known:
        counts[(engines_agree(row, level), classify(row, "whisper") == "flip", classify(row, "giga") == "flip")] += 1
    return {"n": len(known), "unknown": len(rows) - len(known), "level": level, "counts": {"{}|{}|{}".format(*key): value for key, value in sorted(counts.items())}, "matrix": counts}


def pct(part: int, whole: int) -> str:
    return "%.1f%%" % (100 * part / whole) if whole else "-"


def render_cells(title: str, groups: dict[str, dict]) -> list[str]:
    lines = ["", "### " + title, "",
             "| {} | n | W exact | W right | W flip | W none/both | W err | G exact | G right | G flip | G none/both | G err |".format(title.split(" by ")[-1]),
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, stats in groups.items():
        n, cols = stats["n"], []
        for engine in ENGINES:
            counts = stats[engine]
            cols += [pct(counts.get("exact", 0), n), pct(counts.get("right", 0), n), pct(counts.get("flip", 0), n), pct(counts.get("none", 0) + counts.get("both", 0), n),
                     pct(counts.get("error", 0), n)]
        lines.append(f"| {name} | {n} | {' | '.join(cols)} |")
    return lines


FLIPS = (("neither flips", (False, False)), ("Whisper flips only", (True, False)), ("GigaAM flips only", (False, True)), ("both flip", (True, True)))


def unknown_of(stats: dict) -> str:
    return f"{stats['unknown']} clip(s) left out: an engine returned no text. " if stats.get("unknown") else ""


def render_agreement(title: str, stats: dict) -> list[str]:
    matrix, noun = stats["matrix"], {"text": "texts", "polarity": "polarity readings"}[stats["level"]]
    lines = ["", "### " + title, "", "| | " + " | ".join(label for label, _ in FLIPS) + " | all |", "|---|---|---|---|---|---|"]
    for name, agree in ((f"{noun} agree", True), (f"{noun} differ", False)):
        cells = [matrix.get((agree,) + flips, 0) for _, flips in FLIPS]
        lines.append(f"| {name} | {' | '.join(str(c) for c in cells)} | {sum(cells)} |")
    total = {flips: sum(matrix.get((agree,) + flips, 0) for agree in (True, False)) for _, flips in FLIPS}
    lines.append(f"| all | {' | '.join(str(total[flips]) for _, flips in FLIPS)} | {sum(total.values())} |")

    def differ_in(*flip_sets) -> str:
        n = sum(total[f] for f in flip_sets)
        return f"n={n}, {noun} differ in {pct(sum(matrix.get((False,) + f, 0) for f in flip_sets), n)}"

    lines += ["", f"{unknown_of(stats)}{noun.capitalize()} differ: where Whisper flips ({differ_in((True, False), (True, True))}); where GigaAM flips ({differ_in((False, True), (True, True))}); where both flip ({differ_in((True, True))}); where neither flips ({differ_in((False, False))})."]
    return lines


def render_disagreement_by(title: str, groups: dict[str, list[dict]]) -> list[str]:
    """One row per group: how often the engines' texts / polarity readings differ, and how often they differ on the clips where one flips."""
    lines = ["", "### " + title, "", "| " + title.split(" by ")[-1] + " | n | W flips | G flips | texts differ | polarity differs | polarity differs on W flips | polarity differs on G flips |",
             "|---|---|---|---|---|---|---|---|"]
    for name, everything in groups.items():
        rows = comparable(everything)   # clips an engine failed on are in the cell tables as err, not here
        wf = [row for row in rows if classify(row, "whisper") == "flip"]
        gf = [row for row in rows if classify(row, "giga") == "flip"]
        text = sum(not engines_agree(row, "text") for row in rows)
        polarity = sum(not engines_agree(row, "polarity") for row in rows)
        lines.append(f"| {name} | {len(rows)} | {len(wf)} | {len(gf)} | {text} ({pct(text, len(rows))}) | {polarity} ({pct(polarity, len(rows))}) | "
                     f"{sum(not engines_agree(row, 'polarity') for row in wf)}/{len(wf)} | {sum(not engines_agree(row, 'polarity') for row in gf)}/{len(gf)} |")
    return lines


def summarize(rows: list[dict]) -> tuple[dict, str]:
    """(machine-readable summary, markdown) of a results.jsonl; a repeated clip id counts once (the first)."""
    rows = list({row["id"]: row for row in reversed(rows)}.values())[::-1]
    augs = tuple(aug.name for aug in corpus.AUGMENTATIONS)
    sets = tuple(pair.id for pair in corpus.PAIR_SETS)
    overall = cell(rows)
    tables = {"pair_set": grouped(rows, "pair_set", sets), "augmentation": grouped(rows, "augmentation", augs),
              "voice": grouped(rows, "voice", corpus.VOICES), "polarity": grouped(rows, "polarity")}
    by_aug = {name: [row for row in rows if row["augmentation"] == name] for name in augs}
    by_set = {name: [row for row in rows if row["pair_set"] == name] for name in sets}
    whole = {level: agreement(rows, level) for level in ("text", "polarity")}
    summary = {"split": "synthetic", "n": len(rows), "overall": overall, **tables,
               "agreement": {level: {k: v for k, v in stats.items() if k != "matrix"} for level, stats in whole.items()},
               "agreement_by_augmentation": {name: {level: {k: v for k, v in agreement(group, level).items() if k != "matrix"} for level in ("text", "polarity")}
                                             for name, group in by_aug.items()}}
    lines = ["# Confusables: Whisper (live Wyoming) vs GigaAM v3 CTC int8 CPU", "",
             (f"Split: synthetic only (Piper TTS voices {', '.join(corpus.VOICES)}, {len(augs)} augmentations), n={len(rows)} clips. W = Whisper, G = GigaAM. "
             "exact: normalised text equals the truth; right: the right member said but other words differ (an ending, a filler); flip: heard the OPPOSITE member of the pair; none/both: neither/both members' markers; "
             "err: the engine returned no text.")]
    lines += render_cells("Overall by all clips", {"all": overall})
    for title, key in (("Pair set", "pair_set"), ("Augmentation", "augmentation"), ("Voice", "voice"), ("Truth polarity (a = first member)", "polarity")):
        lines += render_cells("Engines by " + title, tables[key])
    lines += render_agreement("Agreement of the engines on the text (all clips)", whole["text"])
    lines += render_agreement("Agreement of the engines on the polarity (all clips)", whole["polarity"])
    lines += render_disagreement_by("Disagreement by augmentation", by_aug)
    lines += render_disagreement_by("Disagreement by pair set", by_set)
    return summary, "\n".join(lines) + "\n"


# ---------------------------------------------------------------- real clips

def real_rows(db: Path, archive: Path, limit: int | None) -> list[dict]:
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)   # as_uri escapes '#' and '?' in the path
    conn.row_factory = sqlite3.Row
    has_truth = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'clip_truth'").fetchone() is not None   # created by the owner-correction tool, not by the archive
    truth, join = ("t.truth, t.label", "LEFT JOIN clip_truth t ON t.clip_id = v.id") if has_truth else ("NULL AS truth, NULL AS label", "")
    found = conn.execute(f"SELECT v.id, v.ts, v.path, v.trigger, v.voiced_ms, v.transcript, {truth} FROM voice_clips v "
                         f"{join} WHERE v.status = 'sent' ORDER BY v.ts").fetchall()
    conn.close()
    rows = [dict(item) for item in found if (archive / item["path"]).exists()]
    return rows[:limit]


def run_real(db: Path, archive: Path, uri: str, giga_python: Path | str, out: Path, limit: int | None) -> int:
    import bench_asr_archive
    out.parent.mkdir(parents=True, exist_ok=True)   # before the recognition work: a failure afterwards must not lose every result
    rows = real_rows(db, archive, limit)
    report_path = out.with_suffix(".md")
    if report_path == out:
        report_path = out.with_name(out.name + ".md")   # never over the raw results

    def save(results):
        report = render_real(results)
        with out.with_name(out.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)   # the pair is published as one: two runs sharing --out never leave one's rows beside the other's report
            publish_pair(out, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), report_path, report)
        return report

    if not rows:
        print(save([]))
        return 0
    extras = recognizer_extras()
    giga = Giga(giga_python)
    results = []
    try:
        for row in rows:
            wave = bench_asr_archive.decode(str(archive / row["path"]))
            pcm = (bench_asr_archive.lift(wave, row["voiced_ms"]) * 32768).clip(-32768, 32767).astype("<i2").tobytes()
            results.append({**row, "whisper": asyncio.run(whisper_text(uri, pcm, extras)), "giga": giga.decode(pcm)})
    finally:
        try:
            report = save(results)   # also after a failed clip: the finished measurements are kept
        finally:
            giga.close()
    print(report)
    return 0


def cell_text(value) -> str:
    """One table cell: a pipe or a line break in a transcript or an owner correction must not split the row or the column."""
    return " ".join(str(value).split()).replace("\\", "\\\\").replace("|", "\\|")


def render_real(rows: list[dict]) -> str:
    """Agreement of the engines on real clips and the disagreements to read; truth only where the owner gave one."""
    usable = [row for row in rows if heard(row, "whisper") is not None and heard(row, "giga") is not None]
    differ = [row for row in usable if not corpus.same_text(heard(row, "whisper"), heard(row, "giga"))]
    spotter = sum(1 for row in rows if row.get("trigger") == "spotter")
    lines = ["# Real clips (split: real, NO truth except where stated): Whisper vs GigaAM", "",
             (f"n={len(usable)} clips with both texts ({len(rows)} stored, {spotter} of them recognised by the speaker's own spotter, whose stored "
             f"transcript is not Whisper's); texts equal after normalisation: {len(usable) - len(differ)} ({pct(len(usable) - len(differ), len(usable))}); "
             f"differ: {len(differ)}. Not an accuracy: neither engine is the reference."),
             "", "## Disagreements", "", "| clip | stored transcript | Whisper now | GigaAM |", "|---|---|---|---|"]
    for row in differ:
        lines.append("| {} | {} | {} | {} |".format(*(cell_text(value) for value in (row["id"], row.get("transcript") or "", heard(row, "whisper"), heard(row, "giga")))))
    truthful = [row for row in usable if row.get("truth") is not None]   # '' is the owner's explicit "no speech" verdict, not an absent one
    lines += ["", f"## Clips with an owner truth (n={len(truthful)}: too few for any rate)", "", "| clip | truth | Whisper now | GigaAM |", "|---|---|---|---|"]
    for row in truthful:
        truth = row["truth"] or "(noise: the owner heard no speech)"
        lines.append("| {} | {} | {} | {} |".format(*(cell_text(value) for value in (row["id"], truth, heard(row, "whisper"), heard(row, "giga")))))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- policy hook

def policy_eval(rows: list[dict], decide) -> dict:
    """Tabulate what a repair policy does on result rows (the synthetic set: they carry truth).

    `decide(view) -> kind` (a string, or an object with `.kind`; "none" means no question) is called once per row with the row plus
    `primary` (Whisper's text) and `alt` (GigaAM's text). Counted, per Whisper outcome (exact / right / flip / none): n and how many got a
    question, so `flip` asked/n is how many of Whisper's polarity errors the policy catches and `exact` asked/n is how many right readings it
    bothers the owner with. Rows where Whisper returned no text are skipped and counted. No thresholds live here."""
    table: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "asked": 0})
    skipped = 0
    for row in rows:
        primary = heard(row, "whisper")
        if primary is None:
            skipped += 1
            continue
        decision = decide({**row, "primary": primary, "alt": heard(row, "giga")})
        kind = getattr(decision, "kind", decision)
        for key in (classify(row, "whisper"), "all"):
            table[key]["n"] += 1
            table[key]["asked"] += kind != "none"
    return {"by_whisper_outcome": dict(table), "skipped": skipped}


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=("run", "summary", "real", "_giga-worker"))
    parser.add_argument("--corpus", type=Path, default=corpus.DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between Whisper requests")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    uri = os.environ.get("AISIS_WYOMING_URI", DEFAULT_URI)
    giga_python = os.environ.get("AISIS_GIGA_PYTHON", DEFAULT_GIGA_PYTHON)
    if args.mode == "_giga-worker":
        return giga_worker()
    if args.mode == "run":
        return run(args.corpus, uri, giga_python, args.limit, args.pause)
    if args.mode == "real":
        return run_real(DB, ARCHIVE, uri, giga_python, args.out or args.corpus / "real.jsonl", args.limit)
    summary, text = summarize(read_results(args.corpus / "results.jsonl"))
    (args.corpus / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (args.corpus / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
