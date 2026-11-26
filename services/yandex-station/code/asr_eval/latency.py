"""End of speech -> transcript through an ISOLATED gateway instance (ticket #59, Tier C latency bar: p95 < 1 s).

`serve` starts the real gateway (`stt_wyoming.STTGateway`) on its own port against one recogniser URI, with everything that has side
effects switched off: no clip archive, no speaker check, a throw-away dialogs DB and config dir. The live gateway and
its voice archive are never touched; stored audio is never replayed through the live gateway (ticket #163).

`measure` plays clips into it over the same websocket protocol the station uses, in REAL time (20 ms chunks every 20 ms), followed by
silence. Latency = wall time from the moment the last voiced 20 ms frame of the clip was sent to the moment the final JSON arrived:
that is the endpoint wait (`AISIS_STT_SILENCE_MS`, live value 800 ms) plus the recogniser plus the gateway. Both parts are reported:
`endpoint_ms` is the configured wait, `recognizer_ms` the rest.
"""
from __future__ import annotations

import argparse
import array
import asyncio
import json
import math
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asr_eval import private  # noqa: E402

RATE = 16000
FRAME = RATE // 50          # 20 ms
TAIL_S = 2.5                # silence played after the clip: longer than the endpoint wait + the slowest recogniser we accept
ENDPOINT_MARGIN_S = 1.7    # what the tail keeps beyond the gateway's endpoint wait (`silence_ms`), so the gateway always ends the stream itself


def isolated_env(uri: str, work: Path, silence_ms: int = 800) -> dict:
    """The environment of the isolated gateway: live knobs that matter for timing, a private place for everything it writes."""
    work.mkdir(parents=True, exist_ok=True)
    config = work / "config"
    config.mkdir(exist_ok=True)
    (config / "runtime.env").write_text(f"AISIS_STT_SILENCE_MS={silence_ms}\n")
    (work / "voiceprint.json").write_text('{"mode": "off"}')
    env = dict(os.environ)
    env.update({"AISIS_WYOMING_URI": uri, "AISIS_CONFIG_DIR": str(config), "AISIS_DIALOGS_DB": str(work / "dialogs.sqlite3"),
                "AISIS_VOICE_ARCHIVE": str(work / "archive"), "AISIS_VOICEPRINT_SETTINGS": str(work / "voiceprint.json"),
                "AISIS_VOICEPRINT_DIR": str(work / "voiceprint"), "AISIS_STT_SILENCE_MS": str(silence_ms),
                "AISIS_STT_WAKE_WINDOW": "0", "AISIS_STT_AB_PERCENT": "0", "TMPDIR": str(work),
                "AISIS_STT_TUNING": str(config / "stt_tuning.json")})   # never the live tuning file an exported variable points at
    return env


def serve(uri: str, port: int, work: Path, silence_ms: int) -> None:
    from aiohttp import web
    import stt_wyoming
    env = isolated_env(uri, work, silence_ms)
    os.environ.update(env)
    gateway = stt_wyoming.STTGateway(env=env, archive=False)
    web.run_app(gateway.app(), host="127.0.0.1", port=port, access_log=None)


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getsampwidth() != 2 or handle.getnchannels() != 1:
            raise ValueError("expected 16 kHz mono 16-bit WAV")
        pcm = handle.readframes(handle.getnframes())
        if len(pcm) != 2 * handle.getnframes():
            raise ValueError("truncated WAV data")
        return pcm


def gateway_vad_gain() -> float:
    """The quiet-frame boost the gateway applies before its VAD (AISIS_STT_VAD_GAIN, default as in `stt_wyoming.Tuning`)."""
    import stt_wyoming
    return stt_wyoming.Tuning.from_env(os.environ).vad_gain


def last_voiced_end(pcm: bytes, vad_gain: float, vad=None) -> int:
    """Byte offset after the last 20 ms frame the gateway's own VAD calls speech: webrtcvad aggressiveness 2 fed the endpointer's
    boosted frames (`Endpoint.boosted`, so quiet far-field speech the gateway hears is heard here too). 0 when it hears none."""
    import stt_wyoming
    if vad is None:
        import webrtcvad
        vad = webrtcvad.Vad(2)
    boosted = stt_wyoming.Endpoint(vad, RATE, vad_gain=vad_gain).boosted
    last = 0
    for offset in range(0, len(pcm) - FRAME * 2 + 1, FRAME * 2):
        frame = pcm[offset:offset + FRAME * 2]
        samples = array.array("h")
        samples.frombytes(frame)
        if vad.is_speech(boosted(frame, samples, max(map(abs, samples))), RATE):
            last = offset + FRAME * 2
    return last


def tail_seconds(silence_ms: int) -> float:
    """Silence played after a clip: the gateway's own endpoint wait has to run out before the finish message, or the wait is not measured."""
    return max(TAIL_S, silence_ms / 1000 + ENDPOINT_MARGIN_S)


async def play(url: str, pcm: bytes, session, vad_gain: float, vad=None, silence_ms: int = 800) -> dict:
    """One utterance, real time. Returns {text, latency_ms, rejected} or {err}; a clip the VAD hears no speech in has no latency
    (`no_speech`: there is no end of speech to count from). `silence_ms` is the isolated gateway's endpoint wait."""
    end_of_speech = last_voiced_end(pcm, vad_gain, vad)
    stream = pcm + bytes(int(tail_seconds(silence_ms) * RATE) * 2)
    async with session.ws_connect(url, max_msg_size=1 << 20) as ws:
        await ws.send_json({"sample_rate": RATE})
        start = time.perf_counter()
        sent_speech_end = None
        final: dict | None = None

        async def reader():
            nonlocal final
            async for message in ws:
                if message.type == 1:  # aiohttp WSMsgType.TEXT
                    data = json.loads(message.data)
                    if data.get("end_of_utt"):
                        final = {"data": data, "at": time.perf_counter()}
                        return
        task = asyncio.ensure_future(reader())
        step = FRAME * 2
        for index, offset in enumerate(range(0, len(stream), step)):
            if task.done():
                break
            await ws.send_bytes(stream[offset:offset + step])
            if sent_speech_end is None and offset + step >= end_of_speech:
                sent_speech_end = time.perf_counter()
            await asyncio.sleep(max(0.0, start + (index + 1) * 0.02 - time.perf_counter()))
        if not task.done():   # silence, gated noise and a rejected caption keep the gateway listening: end the stream the way the station does
            await ws.send_json({"type": "finish"})
        await asyncio.wait_for(task, 15)
        if final is None:
            return {"err": "no_final"}
        out = {"text": final["data"].get("text", ""), "rejected": final["data"].get("rejected")}
        if not end_of_speech:
            out["no_speech"] = True
        elif sent_speech_end is None:   # the gateway answered before the clip's speech ended (a long pause in it): no end-of-speech wait to measure
            out["early_final"] = True
        else:
            out["latency_ms"] = int((final["at"] - sent_speech_end) * 1000)
        return out


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def select(rows: list[dict], filters: list[str], limit: int | None) -> list[dict]:
    """Rows whose fields equal every KEY=VALUE filter, then the first `limit` of them."""
    wanted = [item.split("=", 1) for item in filters]
    rows = [row for row in rows if all(str(row.get(key)) == value for key, value in wanted)]
    return rows[:limit] if limit else rows


async def measure(url: str, manifest: Path, out: Path, limit: int | None, engine: str, filters: list[str] | None = None,
                  vad_gain: float | None = None, silence_ms: int = 800) -> dict:
    import aiohttp
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = select(rows, filters or [], limit)
    results = []
    vad_gain = gateway_vad_gain() if vad_gain is None else vad_gain
    async with aiohttp.ClientSession() as session:
        with private.open_text(out) as handle:
            for row in rows:
                try:
                    result = await play(url, read_pcm(manifest.parent / row["path"]), session, vad_gain, silence_ms=silence_ms)
                except Exception as exc:
                    result = {"err": f"{type(exc).__name__}: {str(exc)[:100]}"}
                result.update({"id": row["id"], "engine": engine})
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
                results.append(result)
    return summarize(results, engine)


def summarize(results: list[dict], engine: str) -> dict:
    good = [r["latency_ms"] for r in results if "latency_ms" in r and r.get("text")]
    return {"engine": engine, "clips": len(results), "with_text": len(good), "errors": sum(1 for r in results if "err" in r),
            "p50_ms": percentile(good, 0.5), "p95_ms": percentile(good, 0.95), "max_ms": max(good, default=None)}


def default_work_dir(home: Path) -> Path:
    """A fresh private directory for the isolated gateway; the parent is created on first use."""
    parent = home / "asr-eval"
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="gw-", dir=parent))


@private.owner_only_cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--uri", required=True)
    p_serve.add_argument("--port", type=int, required=True)
    p_serve.add_argument("--work", default=None, help="throw-away directory (default: a fresh gw-* one under ~/asr-eval)")
    p_serve.add_argument("--silence-ms", type=int, default=800)
    p_measure = sub.add_parser("measure")
    p_measure.add_argument("--url", required=True, help="ws://127.0.0.1:PORT/")
    p_measure.add_argument("--manifest", required=True)
    p_measure.add_argument("--out", required=True)
    p_measure.add_argument("--engine", required=True)
    p_measure.add_argument("--limit", type=int)
    p_measure.add_argument("--filter", action="append", default=[], metavar="KEY=VALUE", help="only manifest rows with this field value (repeatable)")
    p_measure.add_argument("--vad-gain", type=float, default=None, help="quiet-frame boost for finding the end of speech (default: the gateway's)")
    p_measure.add_argument("--silence-ms", type=int, default=800, help="the isolated gateway's endpoint wait (`serve --silence-ms`): the clip's silent tail outlasts it")
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        work = Path(args.work) if args.work else default_work_dir(Path.home())
        serve(args.uri, args.port, work, args.silence_ms)
        return 0
    print(json.dumps(asyncio.run(measure(args.url, Path(args.manifest), Path(args.out), args.limit, args.engine, args.filter, args.vad_gain, args.silence_ms))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
