"""Tier C of ticket #59: the always-on voice archive (real household audio) through several recognisers, aggregate numbers only.

`build` decodes every archived clip (activation-only clips excluded) into a working directory twice: lifted like the gateway lifts it for
the recogniser (`bench_asr_archive.lift`) and as stored. `run` sends the lifted copies to a Wyoming recogniser (`wyoming_batch`). `score`
compares the engines.

Ground truth. Human verdicts (`clip_truth`) exist for only a handful of clips, so most references here are PROVISIONAL: for each
evaluated engine the reference of a clip is the consensus of all the OTHER engines (leave-one-out, plus the transcript the live gateway
stored and the speaker's own on-device recognition when there is one). A clip is `speech` when at least two other voters wrote text and
their medoid is backed by a second voter within AGREE_WER; `nonspeech` when at most one other voter wrote anything (captions of the
known subtitle-credit kind count as silence); everything else is `uncertain` and left out of both tables. A consensus is only as good as
the engines in it: this is a stand-in for hand labels, never a substitute, and the report says how many clips each class holds.

Nothing printed or written by this module (other than the clip audio under the working directory) carries transcript text.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asr_eval import private  # noqa: E402

AGREE_WER = 0.34   # two voters agree on a clip when their word error rate against each other is at most this
RATE = 16000
MIN_VOTERS = 2    # fewer completed voters than this cannot call a clip speech or nonspeech: it stays uncertain


def norm(text: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е")).split()


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(ref: list[str], hyp: list[str]) -> float:
    if not ref:
        return 0.0 if not hyp else float(len(hyp))
    return edit_distance(ref, hyp) / len(ref)


def voiced_words(text: str, is_caption) -> list[str]:
    """The words a voter contributes: none for an empty text or a known subtitle-credit caption."""
    return [] if not text or is_caption(text) else norm(text)


def consensus(voters: list[list[str]]) -> tuple[str, list[str] | None]:
    """('speech', medoid words) | ('nonspeech', None) | ('uncertain', None) from the other engines' words (empty list = silent)."""
    spoken = [v for v in voters if v]
    if len(spoken) <= 1:
        return "nonspeech", None
    best, best_cost = None, None
    for candidate in spoken:
        cost = sum(wer(candidate, other) for other in spoken if other is not candidate)
        if best_cost is None or cost < best_cost:
            best, best_cost = candidate, cost
    backers = sum(1 for other in spoken if other is not best and wer(best, other) <= AGREE_WER)
    return ("speech", best) if backers >= 1 else ("uncertain", None)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def write_wav(path: Path, wave_f32) -> None:
    import numpy as np
    pcm = (np.clip(wave_f32, -1, 1) * 32767).astype("<i2")
    with private.open_binary(path) as raw, wave.open(raw, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(pcm.tobytes())


def build(out: Path, db_path: str) -> dict:
    """Copies of the archive's clips, lifted and as stored, under `out`: household audio, so every directory and file is owner-only."""
    from asr_eval import private
    with private.owner_only():
        return build_private(out, db_path)


def build_private(out: Path, db_path: str) -> dict:
    import bench_asr_archive as bench
    import voiceclips
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, path, voiced_ms, status, reason, trigger FROM voice_clips WHERE status<>? ORDER BY ts", (voiceclips.ACTIVATION,)).fetchall()
    (out / "lifted").mkdir(parents=True, exist_ok=True)
    (out / "orig").mkdir(exist_ok=True)
    counts: dict[str, int] = {}
    with private.open_text(out / "manifest.jsonl") as lifted_m, private.open_text(out / "manifest-orig.jsonl") as orig_m:
        for row in rows:
            try:
                wave_f32 = bench.decode(str(voiceclips.root() / row["path"]))
            except Exception:
                counts["undecodable"] = counts.get("undecodable", 0) + 1
                continue
            write_wav(out / "orig" / f"{row['id']}.wav", wave_f32)
            write_wav(out / "lifted" / f"{row['id']}.wav", bench.lift(wave_f32, row["voiced_ms"]))
            meta = {"status": row["status"], "reason": row["reason"], "trigger": row["trigger"], "seconds": round(len(wave_f32) / RATE, 2)}
            lifted_m.write(json.dumps({"id": row["id"], "path": f"lifted/{row['id']}.wav", **meta}) + "\n")
            orig_m.write(json.dumps({"id": row["id"], "path": f"orig/{row['id']}.wav", **meta}) + "\n")
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def routed_false_accepts(out: Path, engines: list[str], results: dict[str, dict], meta: dict[str, dict], image: str | None) -> list[str]:
    """What production would DO with each engine's text on the clips that held no request (status gate or blacklist: the live gateway
    dropped them, or its recogniser wrote a known caption). Every non-empty text is routed like production (`combined`: grammar,
    the deployed quasar container, grammar rest) as an ambient row; an answer that is not a drop is a false trigger (the speaker acts or
    replies to something nobody asked). Quiet real speech may hide among these clips: a label is only as sure as the clip, so the
    numbers compare engines on the same clips, they do not prove a clip empty."""
    import intent_route
    from asr_eval import tier_b
    from intent_grammar import backtest
    clips = [c for c, m in meta.items() if m["status"] in ("gate", "blacklist")]
    texts = {row["text"] for e in engines for c in clips for row in [results[e].get(c)] if row and not row.get("err") and row["text"]}
    replay = tier_b.replay_texts(out / "replay", texts, image)
    grammar = intent_route.load_grammar()
    decide = intent_route.measured_route(intent_route.measured_router(grammar), mode="combined")
    lines = ["", "non-addressed clips routed like production as ambient rows (blacklist: reached the live recogniser, which wrote a known caption; "
                 "gate: the live gateway dropped them before any recogniser, so a text here is a what-if): "
                 "engine | status | n | empty | unavailable (missing or failed: counted in no other column) | routed to nothing or a drop (good) | false trigger (acts or answers)"]
    for engine in engines:
        for status in ("blacklist", "gate"):
            counts: dict[str, int] = {}
            members = [c for c in clips if meta[c]["status"] == status]
            for clip in members:
                row = results[engine].get(clip)
                if row is None or row.get("err"):
                    counts["unavailable"] = counts.get("unavailable", 0) + 1
                    continue
                text = row["text"]
                if not text:
                    counts["empty"] = counts.get("empty", 0) + 1
                    continue
                quasar = replay.get(text.strip(), tier_b.NOT_HANDLED)
                ambient = backtest.Row(clip, text, "AMBIENT", "real", "dev", quasar.handled, quasar_via=quasar.via)
                outcome = backtest.classify(ambient, decide(ambient), grammar)
                counts[outcome] = counts.get(outcome, 0) + 1
            good = counts.get("dropped", 0) + counts.get("no_action", 0)
            lines.append(f"{engine} | {status} | {len(members)} | {counts.get('empty', 0)} | {counts.get('unavailable', 0)} | {good} | {counts.get('false_trigger', 0)}")
    return lines


def score(out: Path, engines: list[str], db_path: str, route: bool = False, image: str | None = None) -> str:
    from asr_eval import wyoming_batch
    from stt_wyoming import is_silence_hallucination
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    meta = {json.loads(line)["id"]: json.loads(line) for line in (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}
    stored = {r["id"]: r["transcript"] for r in conn.execute("SELECT id, transcript FROM voice_clips")}   # NULL: the gateway never transcribed it, no vote
    has_truth = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clip_truth'").fetchone() is not None
    truth = {r["clip_id"]: r["truth"] for r in conn.execute("SELECT clip_id, truth FROM clip_truth")} if has_truth else {}
    results = {name: wyoming_batch.load(out / f"results-{name}.jsonl") for name in engines}
    lines = [f"Tier C archive (aggregate only): {len(meta)} clips; verified human verdicts among them: {sum(1 for c in meta if c in truth)}"]
    by_status: dict[str, int] = {}
    for m in meta.values():
        by_status[m["status"]] = by_status.get(m["status"], 0) + 1
    lines.append("clips by archive status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))

    def words_of(engine: str, clip: str) -> list[str] | None:
        """The words an engine wrote for a clip; None when it has no usable result (missing or an error): it does not vote, and a clip it
        could not do is not counted as one it left silent."""
        row = results[engine].get(clip)
        return None if row is None or row.get("err") else voiced_words(row["text"], is_silence_hallucination)

    classes: dict[str, dict[str, str]] = {}
    references: dict[str, dict[str, list[str]]] = {}
    for engine in engines:
        classes[engine], references[engine] = {}, {}
        for clip, m in meta.items():
            if words_of(engine, clip) is None:
                continue
            voters = [words for other in engines if other != engine for words in [words_of(other, clip)] if words is not None]
            if m["trigger"] != "spotter" and stored.get(clip) is not None:
                voters.append(voiced_words(stored[clip], is_silence_hallucination))   # the live gateway's stored text
            kind, medoid = consensus(voters) if len(voters) >= MIN_VOTERS else ("uncertain", None)
            classes[engine][clip] = kind
            if medoid is not None:
                references[engine][clip] = medoid
    lines.append("")
    lines.append("engine | speech n | WER vs LOO consensus (mean / median / p95) | empty on speech | nonspeech n | text on nonspeech (false-accept proxy) | caption hits (all clips) | uncertain n | errors | clips without a usable result | ms p50/p95")
    for engine in engines:
        speech = [c for c, k in classes[engine].items() if k == "speech"]
        nonspeech = [c for c, k in classes[engine].items() if k == "nonspeech"]
        uncertain = sum(1 for k in classes[engine].values() if k == "uncertain")
        wers = [wer(references[engine][c], words_of(engine, c)) for c in speech]
        empties = sum(1 for c in speech if not words_of(engine, c))
        accepts = sum(1 for c in nonspeech if words_of(engine, c))
        captions = sum(1 for row in results[engine].values() if row["text"] and is_silence_hallucination(row["text"]))
        errors = sum(1 for row in results[engine].values() if row.get("err"))
        unusable = sum(1 for c in meta if words_of(engine, c) is None)
        ms = [row["ms"] for row in results[engine].values() if not row.get("err")]
        mean = sum(wers) / len(wers) if wers else float("nan")
        lines.append(f"{engine} | {len(speech)} | {mean:.3f} / {percentile(wers, 0.5) or 0:.3f} / {percentile(wers, 0.95) or 0:.3f} | {empties} | "
                     f"{len(nonspeech)} | {accepts} | {captions} | {uncertain} | {errors} | {unusable} | {percentile(ms, 0.5)}/{percentile(ms, 0.95)}")
    verified = [c for c in meta if c in truth]
    lines.append("")
    lines.append(f"verified clips only (human verdicts, n={len(verified)}): WER per engine")
    for engine in engines:
        values = [wer(norm(truth[c]), words_of(engine, c)) for c in verified if words_of(engine, c) is not None]
        lines.append(f"{engine} | n={len(values)} | " + (f"mean WER {sum(values) / len(values):.3f}" if values else "n/a"))
    lines.append("")
    lines.append("by archive status (what the live gateway did with the clip: sent = a request, blacklist = text dropped as a known caption, "
                 "gate = dropped before any recogniser): engine | status | n | non-empty | caption hits | ms p50/p95")
    live = {c: stored.get(c) or "" for c in meta}
    for engine in [*engines, "live-gateway(stored)"]:
        for status in sorted(by_status):
            clips = [c for c, m in meta.items() if m["status"] == status]
            if engine == "live-gateway(stored)":
                texts = [live[c] for c in clips]
                times = []
            else:
                done = [results[engine][c] for c in clips if c in results[engine] and not results[engine][c].get("err")]
                texts = [row["text"] for row in done]
                times = [row["ms"] for row in done]
            nonempty = sum(1 for t in texts if t and not is_silence_hallucination(t))
            captions = sum(1 for t in texts if t and is_silence_hallucination(t))
            lines.append(f"{engine} | {status} | {len(clips)} | {nonempty} | {captions} | {percentile(times, 0.5)}/{percentile(times, 0.95)}")
    if route:
        lines += routed_false_accepts(out, engines, results, meta, image)
    return "\n".join(lines)


@private.owner_only_cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--out", required=True)
    p_build.add_argument("--db", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--dir", required=True)
    p_run.add_argument("--engine", required=True)
    p_run.add_argument("--uri", required=True)
    p_run.add_argument("--concurrency", type=int, default=2)
    p_score = sub.add_parser("score")
    p_score.add_argument("--dir", required=True)
    p_score.add_argument("--engines", required=True)
    p_score.add_argument("--db", required=True)
    p_score.add_argument("--route", action="store_true", help="also route the text of non-addressed clips like production (needs docker for the quasar replay)")
    p_score.add_argument("--image", default=None, help="quasar image for the replay (default: the active prod colour's)")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        print(json.dumps(build(out, args.db)))
    elif args.cmd == "run":
        from asr_eval import wyoming_batch
        out = Path(args.dir)
        files = [(row["id"], out / row["path"]) for row in map(json.loads, (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines())]
        print(asyncio.run(wyoming_batch.run(args.uri, args.engine, files, out / f"results-{args.engine}.jsonl", args.concurrency)))
    else:
        report = score(Path(args.dir), args.engines.split(","), args.db, args.route, args.image)
        (Path(args.dir) / "report.txt").write_text(report + "\n", encoding="utf-8")
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
