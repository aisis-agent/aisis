"""Tier B of the recogniser switch (ticket #59): does a recogniser keep the INTENT of the owner's Russian command set.

Every row of the intent corpus (`intents-ru.jsonl`, 469 phrases with the expected kind/domain/tier/action) is synthesised with Piper in
two rotating voices and two speaking rates, at a clean level (voiced RMS about -26 dBFS, where the gateway lifts the station's audio to)
and with pink noise at 5 dB SNR (more conditions on request). Each isolated recogniser transcribes every clip; the transcript is then
routed exactly like production (`intent_route.measured_route` mode `combined`: grammar pre-phase -> the deployed quasar container ->
grammar rest) and judged with the grammar back-test's own `classify`, against the row's expectation. The quasar container's answer is
opaque to `classify` (its tier only), so the replay also records what the handler resolved the phrase to (`action`): a transcript the
container handles counts as correct only when it resolves to what the ORIGINAL text resolves to («включи свет» is a fast execute for
an expected «выключи свет», and a wrong one).

Per engine x condition it reports, with n and Wilson 95 % intervals:
  intent_correct_abs  share of clips whose routed outcome is a good class (correct, brain_ok, dropped, no_action); the absolute 95 % / 85 %
                      figures are REPORTED metrics (owner decision tg#31291), not the acceptance bar: the router's own ceiling on the exact text is below them
  RELATIVE BAR        per condition, over the clips production routes well from the exact text: not worse than the current recogniser AND
                      measurably better, i.e. more clips that only the candidate gets right than clips only the baseline gets right, with a
                      two-sided exact sign test p < 0.05 on those paired clips (`paired` and `relative_bar` in summary.json)
  intent_preserved    share whose outcome equals the outcome of routing the exact ORIGINAL text: what the recogniser changed, apart from
                      what the grammar never covered
  text_baseline_abs   intent_correct_abs of the original text: the ceiling no recogniser can beat
plus normalised exact-match rate, pooled WER and CER. The corpus is private: `per-phrase.jsonl` carries no phrase or transcript (it may be
attached to a ticket), `per-phrase-with-text.jsonl` stays on the host.

  python -m asr_eval.tier_b build --out DIR          # needs numpy, piper-tts and ffmpeg (the tts-venv python)
  python -m asr_eval.tier_b run --dir DIR --engine NAME --uri tcp://127.0.0.1:PORT
  python -m asr_eval.tier_b score --dir DIR --engines a,b,c
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import wave
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asr_eval import private  # noqa: E402

RATE = 16000
FRAME = RATE // 50
LEAD_S = 0.3          # the gateway's endpointer keeps ~200 ms of pre-roll
TAIL_S = 0.6
VOICES = ("denis", "dmitri", "irina", "ruslan")
LENGTH_SCALES = (1.0, 1.1)
CLEAN_DBFS = -26.0    # voiced speech level; the gateway lifts the station's quiet audio to about -30
STATION_FLOOR = 1.0   # the pauses of the real station clip sit at about 1 LSB RMS
DEFAULT_CONDITIONS = ("clean", "snr5")
DEFAULT_CORPUS = Path.home() / ".local/share/aisis/corpus/intents-ru.jsonl"
DEFAULT_MODELS = Path.home() / ".local/share/aisis/corpus/confusables/tts-models"
STATE_ENV = Path.home() / ".local/share/aisis/bluegreen-state/state.env"
REPLAY_TOOL = Path(__file__).resolve().parent.parent / "intent_grammar/tools/replay_quasar.mts"
GOOD_OUTCOMES = frozenset(("correct", "brain_ok", "dropped", "no_action"))
BAR = {"clean": 0.95, "snr5": 0.85}


# ---------------------------------------------------------------- plan (pure text)

def voice_plan(index: int) -> list[tuple[str, float]]:
    """The two (voice, length_scale) clips of corpus row `index`. The first voice walks the four voices, the second sits 1..3 places
    after it (the step changes every four rows) and the rate flips every four rows, so over the corpus each voice and each rate carry
    about the same share and a row never meets the same voice twice."""
    block = index // 4
    first = index % 4
    second = (first + 1 + block % 3) % 4
    return [(VOICES[first], LENGTH_SCALES[block % 2]), (VOICES[second], LENGTH_SCALES[(block + 1) % 2])]


def snr_of(condition: str) -> float | None:
    """`clean` -> None (no added noise), `snr5` -> 5.0."""
    if condition == "clean":
        return None
    match = re.fullmatch(r"snr(\d+)", condition)
    if not match:
        raise ValueError(f"unknown condition {condition!r} (clean, snr<dB>)")
    return float(match.group(1))


def clip_id(row: int, voice: str, scale: float, condition: str) -> str:
    return f"r{row:03d}-{voice}-ls{round(scale * 10)}-{condition}"


def manifest_rows(corpus: Sequence[dict], conditions: Sequence[str] = DEFAULT_CONDITIONS) -> list[dict]:
    rows = []
    for index, record in enumerate(corpus):
        for voice, scale in voice_plan(index):
            for condition in conditions:
                snr_of(condition)
                cid = clip_id(index, voice, scale, condition)
                rows.append({"id": cid, "path": f"{condition}/{cid}.wav", "row": index, "voice": voice, "condition": condition,
                             "speed": scale, "text": record["text"]})
    return rows


def load_corpus(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- text metrics (pure text)

def normalize(text: str) -> str:
    """Lower case, ё as е, everything that is not a letter or digit is a space: GigaAM/T-one speak lower case without punctuation,
    Whisper/NeMo with both."""
    return " ".join(re.sub(r"[^\w]+|_", " ", (text or "").lower().replace("ё", "е")).split())


def edit_distance(a: Sequence, b: Sequence) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def word_errors(ref: str, hyp: str) -> tuple[int, int]:
    """(word edits, reference words) of two normalised texts."""
    ref_words = ref.split()
    return edit_distance(ref_words, hyp.split()), len(ref_words)


def char_errors(ref: str, hyp: str) -> tuple[int, int]:
    return edit_distance(ref, hyp), len(ref)


def wilson(part: int, whole: int, z: float = 1.96) -> tuple[float, float] | None:
    if not whole:
        return None
    p = part / whole
    denominator = 1 + z * z / whole
    centre = (p + z * z / (2 * whole)) / denominator
    margin = z * math.sqrt(p * (1 - p) / whole + z * z / (4 * whole * whole)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


# ---------------------------------------------------------------- audio (numpy, piper and ffmpeg are imported or run here only)

def voiced_rms(x) -> float:
    """RMS over the 20 ms frames whose own RMS is at least 30 % of the loudest frame's: the level of the speech, not of the pauses."""
    import numpy as np
    count = len(x) // FRAME
    if count == 0:
        return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0
    frames = x[:count * FRAME].astype("float64").reshape(count, FRAME)
    energy = np.sqrt((frames ** 2).mean(axis=1))
    loud = energy >= 0.3 * energy.max()
    return float(np.sqrt((frames[loud] ** 2).mean())) if loud.any() else 0.0


def pink_noise(samples: int, seed: int):
    import numpy as np
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.standard_normal(samples))
    freqs = np.arange(len(spectrum), dtype="float64")
    freqs[0] = 1.0
    shaped = np.fft.irfft(spectrum / np.sqrt(freqs), n=samples)
    return shaped / np.sqrt(np.mean(np.square(shaped)))


def condition_pcm(speech, snr_db: float | None, seed: int):
    """int16 clip of `speech` (float, 16 kHz, padded) levelled to CLEAN_DBFS voiced RMS, a station-like floor and, for `snr_db`, pink noise
    whose RMS is the voiced speech RMS divided by that many dB. The noise runs over the lead and tail too, as in a real room."""
    import numpy as np
    speech = speech * (32768 * 10 ** (CLEAN_DBFS / 20) / max(1e-9, voiced_rms(speech)))
    mixed = speech + np.random.default_rng(seed ^ 0x5EED).standard_normal(len(speech)) * STATION_FLOOR
    if snr_db is not None:
        mixed = mixed + pink_noise(len(speech), seed) * voiced_rms(speech) / (10 ** (snr_db / 20))
    return np.clip(np.rint(mixed), -32768, 32767).astype("<i2")


def to_16k(samples, rate: int):
    """float samples at `rate` -> float64 at 16 kHz through ffmpeg."""
    import numpy as np
    raw = np.asarray(samples, dtype="<f4").tobytes()
    out = subprocess.run([os.environ.get("FFMPEG", "ffmpeg"), "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                          "-f", "f32le", "-ar", str(RATE), "-ac", "1", "pipe:1"], input=raw, capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(out, dtype="<f4").astype("float64")


def write_wav(path: Path, pcm) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with private.open_binary(path) as raw, wave.open(raw, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


def synthesize(voice, text: str, length_scale: float):
    import numpy as np
    from piper import SynthesisConfig
    chunks = list(voice.synthesize(text, SynthesisConfig(length_scale=length_scale)))
    return np.concatenate([chunk.audio_float_array for chunk in chunks]), chunks[0].sample_rate


def build(out: Path, corpus: Path, models: Path, conditions: Sequence[str], limit: int | None = None) -> list[dict]:
    """build_private with every directory and file created owner-only (the corpus is the owner's private command set)."""
    from asr_eval import private
    with private.owner_only():
        return build_private(out, corpus, models, conditions, limit)


def build_private(out: Path, corpus: Path, models: Path, conditions: Sequence[str], limit: int | None = None) -> list[dict]:
    """Synthesise each (voice, text, rate) once, write every condition's clip and `manifest.jsonl`."""
    import numpy as np
    from piper import PiperVoice
    records = load_corpus(corpus)[:limit]
    rows = manifest_rows(records, conditions)
    loaded: dict[str, Any] = {}
    speech: dict[tuple[str, str, float], Any] = {}
    for row in rows:
        key = (row["voice"], row["text"], row["speed"])
        if key not in speech:
            if row["voice"] not in loaded:
                loaded[row["voice"]] = PiperVoice.load(next((models / f"vits-piper-ru_RU-{row['voice']}-medium").glob("*.onnx")))
            samples, rate = synthesize(loaded[row["voice"]], row["text"], row["speed"])
            speech[key] = to_16k(np.concatenate([np.zeros(int(LEAD_S * rate)), samples, np.zeros(int(TAIL_S * rate))]), rate)
        pcm = condition_pcm(speech[key], snr_of(row["condition"]), zlib.crc32(row["id"].encode()))
        write_wav(out / row["path"], pcm)
        row.update({"seconds": round(len(pcm) / RATE, 3), "rms_dbfs": round(20 * math.log10(max(1e-9, voiced_rms(pcm.astype("float64")) / 32768)), 1)})
    private.write_text(out / "manifest.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return rows


# ---------------------------------------------------------------- routing (the production route and the quasar replay)

def active_quasar_image() -> str:
    """Image of the container serving prod now (`PROD_ACTIVE` of the blue/green state; only that line is read)."""
    colour = next((line.split("=", 1)[1].strip() for line in STATE_ENV.read_text().splitlines() if line.startswith("PROD_ACTIVE=")), "green")
    return subprocess.run(["docker", "inspect", "--format", "{{.Config.Image}}", f"aisis-quasar-prod-{colour}"],
                          capture_output=True, text=True, check=True, timeout=60).stdout.strip()


def image_identity(image: str) -> str:
    """What a cached replay answer depends on: docker's immutable id of `image` plus a fingerprint of the replay tool."""
    image_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                              capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    return f"{image_id}+{hashlib.sha256(REPLAY_TOOL.read_bytes()).hexdigest()[:16]}"


class Replayed(NamedTuple):
    """What the deployed quasar container does with a phrase without a model."""
    handled: bool
    via: str | None
    action: str | None   # canonical JSON of what the handler resolved the phrase to (device and state, volume step, media key, ...)


NOT_HANDLED = Replayed(False, None, None)


def replay_texts(directory: Path, texts: set[str], image: str | None = None) -> dict[str, Replayed]:
    """text -> what the quasar container does with it without a model, from `directory/quasar-replay.jsonl`. Texts it lacks are added to
    `directory/transcripts.jsonl` and ONE throwaway container (no network, pure text) replays all of them again. The cached answers
    belong to one image and one replay tool (`quasar-replay.identity`): another image, a rebuilt tag or an edited tool replays everything."""
    directory = directory.resolve()   # docker -v wants an absolute source
    directory.mkdir(parents=True, exist_ok=True)
    replay = directory / "quasar-replay.jsonl"
    marker = directory / "quasar-replay.identity"

    def read() -> dict[str, Replayed]:
        if not replay.exists():
            return {}
        found = {}
        for line in replay.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                action = record.get("action")
                found[str(record["text"]).strip()] = Replayed(bool(record["handled"]), record.get("via"),
                                                              None if action is None else json.dumps(action, sort_keys=True, ensure_ascii=False))
        return found

    resolved = image or active_quasar_image()
    identity = image_identity(resolved)
    cached = marker.exists() and marker.read_text(encoding="utf-8").strip() == identity
    wanted = {text.strip() for text in texts if text.strip()}
    known = read() if cached else {}
    if wanted - set(known) or not cached:
        every = sorted(wanted | set(known))
        private.write_text(directory / "transcripts.jsonl", "".join(json.dumps({"text": t}, ensure_ascii=False) + "\n" for t in every))
        partial = replay.with_name(replay.name + ".tmp")   # published only after the replay finished: an interrupted write never replaces a good cache
        with private.open_text(partial) as out:
            subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/node_modules/.bin/tsx",
                            "-v", f"{REPLAY_TOOL}:/src/backend/replay_quasar.mts:ro", "-v", f"{directory}:/corpus:ro",
                            resolved, "/src/backend/replay_quasar.mts", "/corpus"],
                           stdout=out, check=True, timeout=1800)
        os.replace(partial, replay)
        marker.write_text(identity + "\n", encoding="utf-8")
        known = read()
    return known


# ---------------------------------------------------------------- scoring

def truth_of(expect: dict) -> str:
    """The back-test's truth of a synthetic row (`backtest._truth`)."""
    return str(expect.get("domain") or expect.get("kind") or "")


def judge(grammar: Any, decide: Callable[[Any], Any], record: dict, row_index: int, label: str, text: str,
          replay: dict[str, Replayed]) -> str:
    """Routing outcome of `text` against the corpus row `record`. The quasar container's answer is opaque to the back-test (tier only),
    so a transcript it handled counts as correct only when it resolves to what the ORIGINAL text resolves to: «включи свет» for an expected
    «выключи свет» is a fast execute too, and a wrong one."""
    from intent_grammar import backtest
    quasar = replay.get(text.strip(), NOT_HANDLED)
    row = backtest.Row(f"{row_index}-{label}", text, truth_of(record["expect"]), "synthetic", "dev", quasar.handled, expect=record["expect"], quasar_via=quasar.via)
    decision = decide(row)
    outcome = backtest.classify(row, decision, grammar)
    if outcome == "correct" and decision is not None and decision.intent in backtest.OPAQUE_INTENTS and text.strip() != record["text"].strip():
        original = replay.get(record["text"].strip(), NOT_HANDLED)
        if (quasar.via, quasar.action) != (original.via, original.action):
            return "wrong"
    return outcome


def score_clips(manifest: list[dict], corpus: list[dict], results: dict[str, dict[str, dict]], engines: Sequence[str],
                grammar: Any, decide: Callable[[Any], Any], replay: dict[str, Replayed], route_text: str = "raw") -> list[dict]:
    """One dict per engine x clip: transcript, wer/cer counts, outcome, outcome of the original text, preserved. `route_text` `raw` routes
    the transcript as the recogniser wrote it (as production does), `normalised` the lower-case unpunctuated form."""
    base = {row_index: judge(grammar, decide, record, row_index, "text", record["text"], replay) for row_index, record in enumerate(corpus)}
    rows = []
    for engine in engines:
        for clip in manifest:
            result = results[engine].get(clip["id"])
            if result is None:
                continue
            record = corpus[clip["row"]]
            hyp_raw = result.get("text") or ""
            hyp, ref = normalize(hyp_raw), normalize(record["text"])
            routed = hyp_raw if route_text == "raw" else hyp
            outcome = judge(grammar, decide, record, clip["row"], clip["id"], routed, replay)
            word_edits, words = word_errors(ref, hyp)
            char_edits, chars = char_errors(ref, hyp)
            rows.append({"id": clip["id"], "row": clip["row"], "voice": clip["voice"], "condition": clip["condition"], "engine": engine,
                         "domain": record["expect"].get("domain"), "outcome": outcome, "baseline_outcome": base[clip["row"]],
                         "preserved": outcome == base[clip["row"]], "exact": hyp == ref, "empty": not hyp, "err": bool(result.get("err")),
                         "word_edits": word_edits, "words": words, "char_edits": char_edits, "chars": chars,
                         "wer": round(word_edits / words, 4) if words else None, "ref": record["text"], "hyp": hyp_raw})
    return rows


def rate(part: int, whole: int) -> dict[str, Any]:
    interval = wilson(part, whole)
    return {"rate": round(part / whole, 4) if whole else None, "lo": round(interval[0], 4) if interval else None,
            "hi": round(interval[1], 4) if interval else None}


def summarise(scored: list[dict]) -> dict[str, dict[str, dict[str, Any]]]:
    """engine -> condition -> the table cell (n, exact, wer, cer, the intent rates, empties, errors).

    `intent_correct_routable` is the ticket bar: the corpus also holds rows that production routing does not handle even when it is handed
    the exact text (typos, half-words, aspirational negatives), so `intent_correct_abs` can never reach the bar however good the recogniser
    is (`text_baseline_abs` is that ceiling). The routable rate asks the question a recogniser can answer: of the rows production handles
    from the exact text, how many does it still handle from the transcript."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in scored:
        groups[(row["engine"], row["condition"])].append(row)
    table: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (engine, cond), rows in groups.items():
        n = len(rows)
        routable = [r for r in rows if r["baseline_outcome"] in GOOD_OUTCOMES]
        table[engine][cond] = {
            "n": n, "exact": rate(sum(r["exact"] for r in rows), n),
            "wer": round(sum(r["word_edits"] for r in rows) / max(1, sum(r["words"] for r in rows)), 4),
            "cer": round(sum(r["char_edits"] for r in rows) / max(1, sum(r["chars"] for r in rows)), 4),
            "intent_correct_abs": rate(sum(r["outcome"] in GOOD_OUTCOMES for r in rows), n),
            "intent_correct_routable": rate(sum(r["outcome"] in GOOD_OUTCOMES for r in routable), len(routable)),
            "routable_n": len(routable),
            "intent_preserved": rate(sum(r["preserved"] for r in rows), n),
            "text_baseline_abs": rate(sum(r["baseline_outcome"] in GOOD_OUTCOMES for r in rows), n),
            "empty": sum(r["empty"] for r in rows), "errors": sum(r["err"] for r in rows)}
    return table


def weakest_domains(scored: list[dict], engine: str, condition: str, count: int = 5) -> list[tuple[str, float, int]]:
    by_domain: dict[str, list[bool]] = defaultdict(list)
    for row in scored:
        if row["engine"] == engine and row["condition"] == condition:
            by_domain[row["domain"]].append(row["outcome"] in GOOD_OUTCOMES)
    ranked = sorted(((name, sum(good) / len(good), len(good)) for name, good in by_domain.items()), key=lambda item: (item[1], item[0]))
    return ranked[:count]


def bar_verdicts(table: dict[str, dict[str, dict[str, Any]]], baseline: str) -> dict[str, dict[str, Any]]:
    """Per engine: does each bar hold (point estimate of intent_correct_routable), and is the engine at least as good as `baseline`."""
    verdicts: dict[str, dict[str, Any]] = {}
    for engine, cells in table.items():
        verdict: dict[str, Any] = {}
        for cond, floor in BAR.items():
            cell = cells.get(cond)
            if cell:
                value = cell["intent_correct_routable"]["rate"]
                verdict[f"{cond}_ge_{round(floor * 100)}"] = value is not None and value >= floor
                reference = table.get(baseline, {}).get(cond)
                reference = reference["intent_correct_routable"]["rate"] if reference else None   # None: the baseline has no routable clip (an interrupted run)
                if reference is not None:
                    verdict[f"{cond}_not_worse_than_{baseline}"] = value is not None and value >= reference
        verdicts[engine] = verdict
    return verdicts


PAIRED_ALPHA = 0.05


def sign_test_p(better: int, worse: int) -> float:
    """Two-sided exact binomial (sign) test p-value of `better` vs `worse` discordant pairs (1.0 when there are none)."""
    pairs = better + worse
    if not pairs:
        return 1.0
    return min(1.0, 2 * sum(math.comb(pairs, k) for k in range(min(better, worse) + 1)) / 2 ** pairs)


def paired_comparisons(scored: list[dict], engines: Sequence[str], baseline: str) -> dict[str, dict[str, dict[str, Any]]]:
    """engine -> condition -> {better, worse, pairs, p}: on the same clips (routable ones: production handles the exact text), the clips
    only the engine gets right (`better`) and only the baseline gets right (`worse`). The baseline itself has no entry."""
    good: dict[tuple[str, str], bool] = {}
    condition_of: dict[str, str] = {}
    for row in scored:
        if row["baseline_outcome"] in GOOD_OUTCOMES:
            good[(row["engine"], row["id"])] = row["outcome"] in GOOD_OUTCOMES
            condition_of[row["id"]] = row["condition"]
    paired: dict[str, dict[str, dict[str, Any]]] = {}
    for engine in engines:
        if engine == baseline:
            continue
        counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for (name, clip), engine_good in good.items():
            if name != engine or (baseline, clip) not in good:
                continue
            base_good = good[(baseline, clip)]
            cell = counts[condition_of[clip]]
            cell[0] += engine_good and not base_good
            cell[1] += base_good and not engine_good
            cell[2] += 1
        paired[engine] = {cond: {"better": better, "worse": worse, "pairs": pairs, "p": round(sign_test_p(better, worse), 6)}
                          for cond, (better, worse, pairs) in sorted(counts.items())}
    return paired


def relative_bars(paired: dict[str, dict[str, dict[str, Any]]], conditions: Sequence[str] = tuple(BAR)) -> dict[str, dict[str, bool]]:
    """The acceptance bar (owner decision tg#31291): in every condition the engine is not worse than the baseline and measurably better."""
    bars: dict[str, dict[str, bool]] = {}
    for engine, cells in paired.items():
        verdict = {f"{cond}_improves_on_baseline": cond in cells and cells[cond]["better"] > cells[cond]["worse"] and cells[cond]["p"] < PAIRED_ALPHA
                   for cond in conditions}
        verdict["relative_bar"] = all(verdict.values())
        bars[engine] = verdict
    return bars


def per_phrase_rows(scored: list[dict], with_text: bool) -> list[dict]:
    rows = []
    for item in scored:
        row = {"row": item["row"], "voice": item["voice"], "condition": item["condition"], "engine": item["engine"], "domain": item["domain"],
               "outcome": item["outcome"], "preserved": item["preserved"], "wer": item["wer"]}
        if with_text:
            row.update({"id": item["id"], "text": item["ref"], "transcript": item["hyp"], "baseline_outcome": item["baseline_outcome"]})
        rows.append(row)
    return rows


def format_table(table: dict[str, dict[str, dict[str, Any]]], engines: Sequence[str]) -> str:
    def cell(value: dict[str, Any]) -> str:
        return "  n/a            " if value["rate"] is None else f"{value['rate'] * 100:5.1f} [{value['lo'] * 100:4.1f}-{value['hi'] * 100:5.1f}]"
    lines = [f"{'engine':14} {'cond':6} {'n':>5} {'exact%':>7} {'WER':>6} {'CER':>6}  {'intent_correct_routable':24} {'routable_n':>10} {'intent_correct_abs':18} "
             f"{'intent_preserved':18} {'text_baseline_abs':18} empty err"]
    for engine in engines:
        for cond in sorted(table.get(engine, {}), key=lambda name: (name != "clean", name)):
            c = table[engine][cond]
            lines.append(f"{engine:14} {cond:6} {c['n']:5d} {c['exact']['rate'] * 100:7.1f} {c['wer']:6.3f} {c['cer']:6.3f}  "
                         f"{cell(c['intent_correct_routable']):24} {c['routable_n']:10d} {cell(c['intent_correct_abs']):18} {cell(c['intent_preserved']):18} "
                         f"{cell(c['text_baseline_abs']):18} {c['empty']:5d} {c['errors']:3d}")
    return "\n".join(lines)


def withhold_incomplete(bars: dict[str, dict[str, Any]], missing: dict[str, int], baseline: str) -> dict[str, dict[str, Any]]:
    """A bar is only issued on complete evidence: an engine that lacks results (or has failed ones) for part of the manifest, and every
    engine when the baseline lacks some, gets `incomplete: true` and every bar false (a rate over the few clips done proves nothing)."""
    blocked = set(missing) if missing.get(baseline) else {engine for engine, count in missing.items() if count}
    return {engine: ({**{name: False for name in verdict}, "incomplete": True} if engine in blocked else verdict) for engine, verdict in bars.items()}


def check_fixtures(manifest: list[dict], corpus: list[dict]) -> None:
    """The clips were recorded from the corpus as it was at build time: every clip's phrase must still be that row of the corpus now (an
    inserted, removed or reordered row would score recordings against another phrase's text and expectation)."""
    for clip in manifest:
        row = clip["row"]
        if not 0 <= row < len(corpus) or corpus[row]["text"] != clip["text"]:
            raise ValueError(f"corpus changed since the clips were built (row {row}, clip {clip['id']}): rebuild the fixtures")


def score(directory: Path, engines: Sequence[str], corpus_path: Path, baseline: str, route_text: str, image: str | None) -> dict[str, Any]:
    import intent_route
    from asr_eval import wyoming_batch
    corpus = load_corpus(corpus_path)
    manifest = [json.loads(line) for line in (directory / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    check_fixtures(manifest, corpus)
    results = {engine: wyoming_batch.load(directory / f"results-{engine}.jsonl") for engine in engines}
    missing = {engine: sum(clip["id"] not in results[engine] or bool(results[engine][clip["id"]].get("err")) for clip in manifest) for engine in engines}
    texts = {record["text"] for record in corpus}
    for engine in engines:
        for result in results[engine].values():
            hyp = result.get("text") or ""
            texts.add(hyp if route_text == "raw" else normalize(hyp))
    replay = replay_texts(directory / "replay", texts, image)
    grammar = intent_route.load_grammar()
    decide = intent_route.measured_route(intent_route.measured_router(grammar), mode="combined")
    scored = score_clips(manifest, corpus, results, engines, grammar, decide, replay, route_text)
    table = summarise(scored)
    (directory / "per-phrase.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in per_phrase_rows(scored, False)), encoding="utf-8")
    private.write_text(directory / "per-phrase-with-text.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in per_phrase_rows(scored, True)))
    paired = paired_comparisons(scored, engines, baseline)
    bars = bar_verdicts(table, baseline)
    for engine, verdict in relative_bars(paired).items():
        bars.setdefault(engine, {}).update(verdict)
    summary = {"route_text": route_text, "baseline": baseline, "missing_clips": missing, "table": table, "paired": paired,
               "bars": withhold_incomplete(bars, missing, baseline)}
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"route_text={route_text}  clips in manifest={len(manifest)}  missing results per engine={missing}")
    print(format_table(table, engines))
    for engine in engines:
        for cond in sorted(table.get(engine, {})):
            weak = ", ".join(f"{name} {share * 100:.0f}% (n={n})" for name, share, n in weakest_domains(scored, engine, cond))
            print(f"weakest domains {engine}/{cond}: {weak}")
    print("paired vs baseline on routable clips (better = only the engine is right, worse = only the baseline; two-sided sign test):")
    for engine, cells in paired.items():
        print("  " + engine + ": " + "; ".join(f"{cond} better={c['better']} worse={c['worse']} pairs={c['pairs']} p={c['p']}" for cond, c in cells.items()))
    print("bars (point estimates; absolute bars are reported metrics, `relative_bar` is the acceptance bar; an engine with missing or failed clips, or any engine when the baseline has them, gets none):", json.dumps(summary["bars"]))
    return summary


# ---------------------------------------------------------------- commands

@private.owner_only_cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    built = sub.add_parser("build", help="synthesise the clips and write manifest.jsonl")
    built.add_argument("--out", type=Path, required=True)
    built.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    built.add_argument("--models", type=Path, default=DEFAULT_MODELS)
    built.add_argument("--conditions", default=",".join(DEFAULT_CONDITIONS), help="clean and snr<dB>, comma separated")
    built.add_argument("--limit", type=int, default=None, help="only the first N corpus rows (a smoke run)")
    ran = sub.add_parser("run", help="transcribe every clip of DIR through one Wyoming recogniser")
    ran.add_argument("--dir", type=Path, required=True)
    ran.add_argument("--engine", required=True)
    ran.add_argument("--uri", required=True)
    ran.add_argument("--concurrency", type=int, default=2)
    scored = sub.add_parser("score", help="tables per engine and condition, per-phrase files")
    scored.add_argument("--dir", type=Path, required=True)
    scored.add_argument("--engines", required=True)
    scored.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    scored.add_argument("--baseline", default="whisper-turbo", help="the current recogniser the others must not be worse than")
    scored.add_argument("--route-text", choices=("raw", "normalised"), default="raw", help="route the transcript as written (production) or normalised")
    scored.add_argument("--image", default=None, help="quasar image for the replay (default: the active prod colour's)")
    args = parser.parse_args(argv)
    if args.mode == "build":
        rows = build(args.out, args.corpus, args.models, args.conditions.split(","), args.limit)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row["condition"]] += 1
        print(json.dumps({"clips": len(rows), "per_condition": counts, "rows": len({row["row"] for row in rows}), "out": str(args.out)}))
        return 0
    if args.mode == "run":
        from asr_eval import wyoming_batch
        manifest = [json.loads(line) for line in (args.dir / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        files = [(row["id"], args.dir / row["path"]) for row in manifest]
        count = asyncio.run(wyoming_batch.run(args.uri, args.engine, files, args.dir / f"results-{args.engine}.jsonl", args.concurrency))
        print(json.dumps({"engine": args.engine, "transcribed": count, "files": len(files)}))
        return 0
    score(args.dir, args.engines.split(","), args.corpus, args.baseline, args.route_text, args.image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
