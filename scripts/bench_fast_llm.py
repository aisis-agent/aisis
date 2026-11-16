#!/usr/bin/env python3
"""Benchmark of fast LLM candidates and route classifiers for the voice speaker (aisis#88).

Context: docs/research/fast-llm-routing.md defines three routes (deterministic skills, fast/lite
model, strong brain) and a router prompt set; docs/research/fast-llm-bench-2026-11.md holds the
results. This harness measures, per model, speed (TTFT / total latency / tok/s), route accuracy on
the labelled eval set, the safety metric "wrong fast answer on open-world facts", slot extraction,
Russian small-talk quality inputs, paraphrase fidelity, strict-JSON adherence, a knowledge /
reasoning sanity set and token cost. It also benchmarks Laya and Jev as task-type classifiers.

Secrets: keys are read from the environment only and never printed or stored.
  CEREBRAS_API_KEY (or CEREBRAS_KEY)          Cerebras direct, models qwen, gptoss
  AI_GATEWAY_API_KEY (or VERCEL_JEV_KEY)      Vercel AI Gateway, ONLY for Jev (typesafe-ai/jev)
  ANTHROPIC_API_KEY                           optional raw Anthropic Messages API (`--claude-via anthropic`)
Sonnet/Haiku default to `--claude-via omp`: the omp CLI calling the Anthropic Messages API with its own account (nothing to export).
Run with the repo-root .env loaded into the process only, e.g.
  (set -a; . ./.env; set +a; python3 scripts/bench_fast_llm.py run --model qwen --out-dir /tmp/bench)

Subcommands: run (one model, raw results to <out-dir>/<model>.json), judge (rubric + blind
side-by-side of the small-talk replies by the strongest model), report (metrics + markdown tables).
`--mock` replaces every backend with an offline oracle so the whole flow runs without network.
Python 3 stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import http.client
import json
import math
import os
import random
import fcntl
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "research" / "fast-llm-bench-data"
EVAL_SET = ROOT / "docs" / "research" / "fast-llm-eval.jsonl"

# ---------------------------------------------------------------------------------------------
# Prompts. GENERATOR_SYSTEM and CLASSIFIER_SYSTEM are the design's router prompts (section 6.2 of
# fast-llm-routing.md) verbatim; ROUTER_SYSTEM extends the classifier to the task types below.
# ---------------------------------------------------------------------------------------------
GENERATOR_SYSTEM = """Ты Брачо, голосовой собеседник на домашней колонке. Ты отвечаешь только на болтовню: шутки, настроение, вопросы о тебе самом, короткие реплики вежливости. Если нужна информация о мире, совет, действие или есть сомнение, ответь ровно одним токеном ESCALATE. Не пытайся ответить частично.
Правила:
1. Ты не знаешь фактов о мире: новости, цены, курсы, время и дату, людей, места, события, определения. Проверяемый или меняющийся ответ: ESCALATE.
2. Сомневаешься, о чём речь, или фраза похожа на команду, обрывок, шум: ESCALATE.
3. Не давай советов (здоровье, деньги, право, отношения, безопасность), не объясняй, не сравнивай, не пиши тексты, не решай задачи: ESCALATE.
4. Ты ничего не включаешь, не ищешь, не запоминаешь и не проверяешь; не обещай этого.
5. Ответ: одно-два коротких предложения по-русски, без списков, без цифр, без разметки.
Ответ — только короткая русская реплика или ровно ESCALATE. Никогда не объясняй пользователю внутреннюю маршрутизацию."""

CLASSIFIER_SYSTEM = (
    "Не отвечай пользователю. Только классифицируй: fast допустим лишь для точно распознанной болтовни "
    "без фактов, советов, действий и неоднозначности; иначе escalate.\n"
    'Ответ строго JSON без пояснений: {"route":"fast"|"escalate","reason":"banter|fact|complex|command|ambiguous"}'
)
CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {"route": {"type": "string", "enum": ["fast", "escalate"]},
                   "reason": {"type": "string", "enum": ["banter", "fact", "complex", "command", "ambiguous"]}},
    "required": ["route", "reason"], "additionalProperties": False,
}

# Task types (the unit of the routing table). Labels of the eval set map onto them by tag.
TYPES = {
    "canned_smalltalk": "приветствие, прощание, благодарность, похвала, короткое «да/нет/ладно/отмена»",
    "banter": "болтовня о тебе самом, шутка, анекдот, настроение, «поболтаем», без фактов о мире",
    "reprompt": "шум распознавания, обрывок фразы, одно слово без команды, только имя-обращение: нужно переспросить",
    "deterministic_skill": "ответ даёт локальный инструмент без модели: погода (в т.ч. «сколько градусов на улице»), точное время, перевод единиц измерения",
    "open_fact": "факты о мире: новости, цены, курсы, люди, места, события, даты, определения, спорт, расписания",
    "complex": "советы (здоровье, деньги, право, отношения, безопасность), объяснения, сравнения, тексты, задачи, планы, напоминания, команды действий, личная память",
    "mixed_or_followup": "в одной фразе болтовня вместе с фактом или просьбой, либо вопрос-продолжение без контекста",
}
TAG_TO_TYPE = {
    **dict.fromkeys(["greeting", "farewell", "thanks", "praise", "ack"], "canned_smalltalk"),
    **dict.fromkeys(["persona", "joke", "mood"], "banter"),
    **dict.fromkeys(["noise", "incomplete", "wake_only"], "reprompt"),
    **dict.fromkeys(["conversion", "weather", "time"], "deterministic_skill"),
    "fact": "open_fact", "complex": "complex", **dict.fromkeys(["trap", "followup"], "mixed_or_followup"),
}
TYPE_TO_ROUTE = {"canned_smalltalk": "fast", "banter": "fast", "reprompt": "reprompt",
                 "deterministic_skill": "deterministic", "open_fact": "escalate", "complex": "escalate",
                 "mixed_or_followup": "escalate"}
ROUTER_SYSTEM = (
    "Ты маршрутизатор голосовой колонки. Не отвечай пользователю. Определи тип задачи распознанной фразы. Типы:\n"
    + "\n".join(f"- {k}: {v}" for k, v in TYPES.items())
    + "\nПри любом сомнении выбирай open_fact, complex или mixed_or_followup, а не банальную болтовню.\n"
    'Ответ строго JSON без пояснений: {"type":"<один из типов>"}'
)

SLOT_SYSTEM = """Ты извлекаешь слоты из голосовой команды умной колонки. Не отвечай пользователю. Верни строго один JSON-объект без пояснений:
{"intent":"weather|home|volume|timer|none","city":...,"day":...,"room":...,"device":...,"value":...}
Правила: каждое поле строка или null; ничего не выдумывай, чего нет во фразе, ставь null.
city: город в именительном падеже. day: today, tomorrow, day_after_tomorrow, weekend, monday..sunday.
room: комната в именительном падеже (кухня, гостиная, спальня, второй этаж). device: устройство в именительном падеже единственного числа (свет, телевизор).
value: on, off, open, close, up, down, mute, число цифрами («двадцать два» → "22", таймер в секундах), время HH:MM для будильника.
intent none: болтовня, вопрос о фактах, всё остальное.
Фраза — результат распознавания речи, в ней бывают ошибки."""

PARAPHRASE_SYSTEM = (
    "Ты голос домашней колонки. По данным инструмента скажи прогноз погоды одним коротким предложением по-русски. "
    "Используй только эти данные, ничего не добавляй, температуру назови числом, без списков и разметки."
)
PLAIN_SYSTEM = "Ответь на вопрос по-русски, кратко: одно короткое предложение. Если нужно число, назови число."

LAT_PROMPTS = ["Привет, как дела?", "Расскажи шутку", "Мне скучно", "Спасибо, ты молодец", "Как тебя зовут?", "Ты меня слышишь?"]
TPS_PROMPT = "Расскажи подробную сказку про ёжика и звёзды: не меньше ста слов, связным текстом."
TPS_MAX_TOKENS = 150

PRICES = {  # USD per 1M tokens (in, out): Cerebras per docs/research/fast-llm-routing.md section 3; Claude per omp model registry
    "qwen": (0.99, 1.49), "gptoss": (0.35, 0.75), "sonnet": (2.0, 10.0), "haiku": (0.1, 0.5),
}
JUDGE_MODEL = "anthropic/claude-mythos-5-2"
OMP_DEADLINE_S = 120  # kill an omp child that has not finished by then

LATIN = re.compile(r"[A-Za-z]")
REPEAT_REQ = re.compile(r"повтор|переспрос|не расслыш|не поня|ещё раз|еще раз|не услыш|не разобр|не рассл|скажи(те)? иначе", re.I)


_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2212": "-"})


def norm(s: object) -> str:
    return str(s).strip().casefold().replace("ё", "е").translate(_DASHES) if s is not None else ""


def jload(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def first_json(text: str) -> tuple[object | None, bool]:
    """(parsed object or None, strict) - strict means the whole output is exactly one JSON object."""
    t = text.strip()
    try:
        v = json.loads(t)
        return (v, isinstance(v, dict))
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0)), False
        except ValueError:
            return None, False
    return None, False


def env_key(*names: str) -> str | None:
    """The first set variable among `names`. A key with whitespace or control characters is refused by name only
    (its value is never printed): http.client escapes such characters in its errors, which would defeat redaction."""
    for n in names:
        value = os.environ.get(n)
        if value:
            value = value.strip()
            if not re.fullmatch(r"[\x21-\x7e]+", value):
                sys.exit(f"{n} contains whitespace or non-printable characters; fix the variable")
            return value
    return None


# ---------------------------------------------------------------------------------------------
# Backends. complete() returns a dict: text, ttft_ms (first answer token), ttft_any_ms (first token
# of any kind, reasoning included), total_ms, in_tok, out_tok, finish, error.
# ---------------------------------------------------------------------------------------------
class OpenAICompat:
    """Streaming chat completions over one keep-alive connection (Cerebras direct, Vercel AI Gateway)."""

    def __init__(self, name, host, path, key, model, token_param, extra, max_tokens_cap=None, sleep=0.0):
        self.name, self.host, self.path, self.key, self.model = name, host, path, key, model
        self.token_param, self.extra, self.cap, self.sleep = token_param, extra, max_tokens_cap, sleep
        self.conn: http.client.HTTPSConnection | None = None

    def _connect(self):
        self.conn = http.client.HTTPSConnection(self.host, timeout=60)
        self.conn.connect()

    def warm(self):
        self._connect()

    def complete(self, system, user, max_tokens=256, temperature=0.0, response_format=None, **_):
        if self.cap:
            max_tokens = max(max_tokens, self.cap)
        body = {"model": self.model, "stream": True, "stream_options": {"include_usage": True},
                "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}],
                self.token_param: max_tokens, "temperature": temperature, **self.extra}
        if response_format:
            body["response_format"] = response_format
        payload = json.dumps(body, ensure_ascii=False).encode()
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "aisis-bench/1"}
        out = {"text": "", "reasoning": "", "ttft_ms": None, "ttft_any_ms": None, "total_ms": None, "in_tok": None,
               "out_tok": None, "finish": None, "error": None, "retries": 0}
        for attempt in range(4):
            if self.sleep:
                time.sleep(self.sleep)
            try:
                if self.conn is None:
                    self._connect()
                t0 = time.perf_counter()
                self.conn.request("POST", self.path, payload, headers)
                resp = self.conn.getresponse()
                if resp.status != 200:
                    err = resp.read().decode("utf-8", "replace").replace(self.key, "<redacted>")[:300]
                    if resp.status in (429, 503) and attempt < 3:
                        out["retries"] += 1
                        time.sleep(2 ** attempt)
                        continue
                    out["error"] = f"HTTP {resp.status}: {err}"
                    return out
                out.update(text="", reasoning="", ttft_ms=None, ttft_any_ms=None)
                complete = False
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        complete = True
                        break
                    ev = json.loads(data)
                    now = (time.perf_counter() - t0) * 1000
                    if ev.get("error"):
                        out["error"] = f"stream error: {json.dumps(ev['error'], ensure_ascii=False)[:200]}".replace(self.key, "<redacted>")
                        break
                    if ev.get("usage"):
                        out["in_tok"], out["out_tok"] = ev["usage"].get("prompt_tokens"), ev["usage"].get("completion_tokens")
                    for ch in ev.get("choices") or []:
                        d = ch.get("delta") or {}
                        if d.get("content"):
                            if out["ttft_ms"] is None:
                                out["ttft_ms"] = now
                            out["text"] += d["content"]
                        rs = d.get("reasoning") or d.get("reasoning_content")
                        if rs:
                            out["reasoning"] += rs
                        if (d.get("content") or rs) and out["ttft_any_ms"] is None:
                            out["ttft_any_ms"] = now
                        if ch.get("finish_reason"):
                            out["finish"] = ch["finish_reason"]
                out["total_ms"] = (time.perf_counter() - t0) * 1000
                if not complete and not out["error"]:
                    out["error"] = "stream ended before [DONE]"
                resp.read()
                return out
            except (http.client.HTTPException, OSError, ValueError) as e:
                self.conn = None
                out["retries"] += 1
                if attempt == 3:
                    out["error"] = f"{type(e).__name__}: {str(e).replace(self.key, '<redacted>')[:200]}"
        return out


class OmpBackend:
    """Claude through the omp CLI: one process per call, a minimal system prompt, no tools.

    Latency here includes the CLI's own overhead and a ~22k-token cached prefix that omp adds
    (measured by calibrate()); it is an upper bound, not a raw API latency."""

    def __init__(self, name, selector):
        self.name, self.selector, self.prefix_tokens = name, selector, 0

    def warm(self):
        r = self.complete("Ответь одним словом.", ".")
        self.prefix_tokens = (r.get("raw_in") or 0) - 6  # the prompt itself is a few tokens
        return r

    def complete(self, system, user, **_):
        cmd = ["omp", "-p", "--mode", "json", "--model", self.selector, "--thinking", "off", "--no-tools", "--no-skills",
               "--no-rules", "--no-context-files", "--no-extensions", "--no-lsp", "--no-session", "--no-title",
               "--system-prompt", system or "Ты помощник.", user]
        out = {"text": "", "reasoning": "", "ttft_ms": None, "ttft_any_ms": None, "total_ms": None, "in_tok": None,
               "out_tok": None, "finish": None, "error": None, "retries": 0, "decode_ms": None}
        t_user = t_first = t_last = t_end = None
        p = None
        timer = None
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
            timer = threading.Timer(OMP_DEADLINE_S, p.kill)  # a stalled child must not hang the run: closing its pipe ends the read
            timer.start()
            for line in p.stdout:
                now = time.time() * 1000
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                typ = ev.get("type")
                if typ == "message_start" and ev["message"].get("role") == "user":
                    t_user = ev["message"].get("timestamp") or now
                elif typ == "message_update":
                    ae = ev.get("assistantMessageEvent") or {}
                    if ae.get("type") == "text_delta":
                        if t_first is None:
                            t_first = now
                        t_last = now
                        out["text"] += ae.get("delta", "")
                elif typ == "turn_end":
                    m = ev["message"]
                    u = m.get("usage") or {}
                    out["raw_in"] = (u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0))
                    out["in_tok"] = max(0, out["raw_in"] - self.prefix_tokens)
                    out["out_tok"] = u.get("output")
                    out["finish"] = m.get("stopReason")
                    if m.get("errorMessage"):
                        out["error"] = str(m["errorMessage"])[:200]
                    t_end = now
            p.wait(timeout=10)
            if p.returncode and not out["error"]:
                out["error"] = f"omp exited with status {p.returncode}"
            if t_user is not None and t_first is not None and t_end is not None:
                out["ttft_ms"] = out["ttft_any_ms"] = t_first - t_user
                out["total_ms"] = t_end - t_user
                out["decode_ms"] = t_last - t_first
            elif not out["error"]:
                out["error"] = "omp produced no complete turn"
        except (OSError, subprocess.SubprocessError, KeyError, TypeError) as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        finally:
            if timer is not None:
                timer.cancel()
            if p is not None:
                p.kill()
                p.wait()
        return out


class AnthropicDirect:
    """Anthropic Messages API streaming over one keep-alive connection (optional `--claude-via anthropic`).

    Needs ANTHROPIC_API_KEY in the environment; the omp path is the default because no raw key is provisioned."""

    def __init__(self, name, key, model, host="api.anthropic.com", tls=True):
        self.name, self.key, self.model, self.host, self.tls = name, key, model, host, tls
        self.conn = None

    def warm(self):
        cls = http.client.HTTPSConnection if self.tls else http.client.HTTPConnection
        self.conn = cls(self.host, timeout=60)
        self.conn.connect()

    def complete(self, system, user, max_tokens=256, temperature=0.0, **_):
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "stream": True,
                "messages": [{"role": "user", "content": user}], **({"system": system} if system else {})}
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json",
                   "User-Agent": "aisis-bench/1"}
        out = {"text": "", "reasoning": "", "ttft_ms": None, "ttft_any_ms": None, "total_ms": None, "in_tok": None,
               "out_tok": None, "finish": None, "error": None, "retries": 0}
        for attempt in range(3):
            try:
                if self.conn is None:
                    self.warm()
                t0 = time.perf_counter()
                self.conn.request("POST", "/v1/messages", json.dumps(body, ensure_ascii=False).encode(), headers)
                resp = self.conn.getresponse()
                if resp.status != 200:
                    err = resp.read().decode("utf-8", "replace").replace(self.key, "<redacted>")[:300]
                    if resp.status in (429, 529) and attempt < 2:
                        out["retries"] += 1
                        time.sleep(2 ** attempt)
                        continue
                    out["error"] = f"HTTP {resp.status}: {err}"
                    return out
                out.update(text="", ttft_ms=None, ttft_any_ms=None)
                complete = False
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    if not line.startswith(b"data:"):
                        continue
                    ev = json.loads(line[5:])
                    now = (time.perf_counter() - t0) * 1000
                    typ = ev.get("type")
                    if typ == "error":
                        out["error"] = f"stream error: {json.dumps(ev.get('error'), ensure_ascii=False)[:200]}".replace(self.key, "<redacted>")
                        break
                    if typ == "message_start":
                        out["in_tok"] = (ev.get("message") or {}).get("usage", {}).get("input_tokens")
                    elif typ == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                        if out["ttft_ms"] is None:
                            out["ttft_ms"] = out["ttft_any_ms"] = now
                        out["text"] += ev["delta"].get("text", "")
                    elif typ == "message_delta":
                        out["out_tok"] = (ev.get("usage") or {}).get("output_tokens")
                        out["finish"] = (ev.get("delta") or {}).get("stop_reason")
                    elif typ == "message_stop":
                        complete = True
                        break
                out["total_ms"] = (time.perf_counter() - t0) * 1000
                if not complete and not out["error"]:
                    out["error"] = "stream ended before message_stop"
                resp.read()
                return out
            except (http.client.HTTPException, OSError, ValueError) as e:
                self.conn = None
                out["retries"] += 1
                if attempt == 2:
                    out["error"] = f"{type(e).__name__}: {str(e).replace(self.key, '<redacted>')[:200]}"
        return out


class LayaBackend:
    """Local resident Laya server (task-cli classify server) over its unix socket."""

    def __init__(self, name, checkpoint):
        self.name, self.checkpoint = name, checkpoint
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "task-cli" / "classify"
        socks = sorted(base.glob("laya-*.sock"), key=lambda p: [int(x) for x in re.findall(r"\d+", p.stem)])
        self.sock = os.environ.get("LAYA_SOCKET") or (str(socks[-1]) if socks else "")

    def warm(self):
        return None

    def classify(self, text, questions):
        t0 = time.perf_counter()
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(60)
            s.connect(self.sock)
            s.sendall(json.dumps({"op": "predict", "message": text, "questions": questions, "model": self.checkpoint},
                                 ensure_ascii=False).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            s.close()
            res = json.loads(buf)
            ans = (res.get("result") or res).get("answers") or {}
            return {"answers": ans, "total_ms": (time.perf_counter() - t0) * 1000, "error": None if ans else str(res)[:200]}
        except (OSError, ValueError) as e:
            return {"answers": {}, "total_ms": (time.perf_counter() - t0) * 1000, "error": f"{type(e).__name__}: {str(e)[:150]}"}


class JevBackend:
    """TypeSafe Jev through Vercel AI Gateway POST /v1/evaluate (same question shape as Laya)."""

    def __init__(self, name, key):
        self.name, self.key = name, key
        self.conn = None

    def warm(self):
        self.conn = http.client.HTTPSConnection("ai-gateway.vercel.sh", timeout=60)
        self.conn.connect()

    def classify(self, text, questions):
        payload = json.dumps({"model": "typesafe-ai/jev", "state": text, "questions": questions}, ensure_ascii=False).encode()
        t0 = time.perf_counter()
        try:
            if self.conn is None:
                self.warm()
            self.conn.request("POST", "/v1/evaluate", payload, {"Authorization": f"Bearer {self.key}",
                                                                  "Content-Type": "application/json", "User-Agent": "aisis-bench/1"})
            r = self.conn.getresponse()
            body = r.read().decode("utf-8", "replace").replace(self.key, "<redacted>")
            ms = (time.perf_counter() - t0) * 1000
            if r.status != 200:
                return {"answers": {}, "total_ms": ms, "error": f"HTTP {r.status}: {body[:200]}"}
            ans = json.loads(body).get("answers") or {}
            return {"answers": ans, "total_ms": ms, "error": None if ans else body[:200]}
        except (http.client.HTTPException, OSError, ValueError) as e:
            self.conn = None
            return {"answers": {}, "total_ms": (time.perf_counter() - t0) * 1000, "error": f"{type(e).__name__}: {str(e)[:150]}"}


class MockBackend:
    """Offline oracle: answers from the gold passed in `oracle`, with fixed fake latencies."""

    def __init__(self, name):
        self.name = name
        self.rng = random.Random(name)

    def warm(self):
        return None

    def complete(self, system, user, oracle=None, **_):
        text = oracle if oracle is not None else "Хорошо."
        ttft = 50 + self.rng.random() * 100
        return {"text": text, "reasoning": "", "ttft_ms": ttft, "ttft_any_ms": ttft, "total_ms": ttft + 10 + len(text) * 0.5,
                "in_tok": 200, "out_tok": max(1, len(text) // 3), "finish": "stop", "error": None, "retries": 0}

    def classify(self, text, questions, oracle=None):
        name = next(iter(questions))
        labels = list(questions[name]["criteria"])
        pick = oracle if oracle in labels else labels[0]
        probs = {l: (0.8 if l == pick else 0.2 / (len(labels) - 1)) for l in labels}
        return {"answers": {name: {"choice": pick, "probabilities": probs}}, "total_ms": 5 + self.rng.random() * 5, "error": None}


def make_backend(model: str, claude_via: str, mock: bool):
    if mock:
        return MockBackend(model)
    if model in ("qwen", "gptoss"):
        key = env_key("CEREBRAS_API_KEY", "CEREBRAS_KEY")
        if not key:
            sys.exit("CEREBRAS_API_KEY (or CEREBRAS_KEY) is not set in the environment")
        if model == "qwen":
            return OpenAICompat("qwen", "api.cerebras.ai", "/v1/chat/completions", key, "qwen-3.8-27b",
                                "max_completion_tokens", {"reasoning_effort": "none"})
        # gpt-oss cannot switch reasoning off; design section 6.2 gives it a thinking budget (512 classifier / 1024 generator)
        return OpenAICompat("gptoss", "api.cerebras.ai", "/v1/chat/completions", key, "gpt-oss-120b",
                            "max_completion_tokens", {"reasoning_effort": "low"}, max_tokens_cap=1024)
    if model in ("sonnet", "haiku"):
        sel = {"sonnet": ("anthropic/claude-sonnet-5-5", "claude-sonnet-5-5"),
               "haiku": ("anthropic/claude-haiku-5-5", "claude-haiku-5-5")}[model]
        if claude_via == "omp":
            return OmpBackend(model, sel[0])
        key = env_key("ANTHROPIC_API_KEY")
        if not key:
            sys.exit("ANTHROPIC_API_KEY is not set in the environment (use --claude-via omp otherwise)")
        return AnthropicDirect(model, key, sel[1])
    if model in ("laya", "laya_en"):
        return LayaBackend(model, "multilingual" if model == "laya" else "english")
    if model == "jev":
        key = env_key("AI_GATEWAY_API_KEY", "VERCEL_JEV_KEY")
        if not key:
            sys.exit("AI_GATEWAY_API_KEY (or VERCEL_JEV_KEY) is not set in the environment")
        return JevBackend("jev", key)
    sys.exit(f"unknown model {model}")


# ---------------------------------------------------------------------------------------------
# Passes. Each returns a list of raw records; metrics are computed later by `report`.
# ---------------------------------------------------------------------------------------------
def user_turn(text: str) -> str:
    """Every model gets the utterance in the same frame. Without it Haiku via omp answered the user instead of
    classifying on 54% of route calls: omp adds its own ~22k-token prefix (MCP tool definitions, another persona)
    that a system prompt alone does not outweigh."""
    return f"Распознанная фраза пользователя: «{text}»"


def eval_items() -> list[dict]:
    items = jload(EVAL_SET)
    for it in items:
        it["type"] = TAG_TO_TYPE[it["tag"]]
        it["route"] = TYPE_TO_ROUTE[it["type"]]
    return items


def is_cerebras(be) -> bool:
    return isinstance(be, OpenAICompat) and be.host == "api.cerebras.ai"


def pass_route(be, items):
    recs = []
    for it in items:
        r = be.complete(ROUTER_SYSTEM, user_turn(it["text"]), max_tokens=64, temperature=0.0, oracle=json.dumps({"type": it["type"]}))
        recs.append({"id": it["id"], **r})
    return recs


def pass_pipe(be, items):
    """Design pipeline: classifier call (strict schema on Cerebras), generator call on every item."""
    recs = []
    for it in items:
        rf = {"type": "json_schema", "json_schema": {"name": "route", "strict": True, "schema": CLASSIFIER_SCHEMA}} \
            if is_cerebras(be) else None
        fast_gold = it["route"] == "fast"
        c = be.complete(CLASSIFIER_SYSTEM, user_turn(it["text"]), max_tokens=48, temperature=0.0, response_format=rf,
                        oracle=json.dumps({"route": "fast" if fast_gold else "escalate", "reason": "banter" if fast_gold else "fact"}))
        g = be.complete(GENERATOR_SYSTEM, user_turn(it["text"]), max_tokens=80, temperature=0.3,
                        oracle="Привет, рад тебя слышать." if fast_gold else "ESCALATE")
        recs.append({"id": it["id"], "cls": c, "gen": g})
    return recs


def pass_slots(be):
    recs = []
    for it in jload(DATA / "slots.jsonl"):
        r = be.complete(SLOT_SYSTEM, user_turn(it["text"]), max_tokens=96, temperature=0.0, oracle=json.dumps(it["gold"], ensure_ascii=False))
        recs.append({"id": it["id"], **r})
    return recs


def pass_talk(be):
    recs = []
    for it in jload(DATA / "smalltalk.jsonl"):
        r = be.complete(GENERATOR_SYSTEM, user_turn(it["text"]), max_tokens=80, temperature=0.3, oracle="Я рядом, расскажи подробнее.")
        recs.append({"id": it["id"], "text": it["text"], **r})
    return recs


def pass_para(be):
    recs = []
    for it in jload(DATA / "paraphrase.jsonl"):
        f = it["facts"]
        r = be.complete(PARAPHRASE_SYSTEM, "Данные инструмента (JSON): " + json.dumps(f, ensure_ascii=False), max_tokens=80, temperature=0.2,
                        oracle=f"{f['city']}, {f['day']}: {f['temp_c']} градусов, {f['condition']}.")
        recs.append({"id": it["id"], **r})
    return recs


def pass_know(be):
    recs = []
    for it in jload(DATA / "knowledge.jsonl"):
        r = be.complete(PLAIN_SYSTEM, user_turn(it["text"]), max_tokens=200, temperature=0.0, oracle=it["answers"][0])
        recs.append({"id": it["id"], **r})
    return recs


def pass_lat(be, runs=50, warmups=2):
    recs = []
    for i in range(runs + warmups):
        r = be.complete(GENERATOR_SYSTEM, user_turn(LAT_PROMPTS[i % len(LAT_PROMPTS)]), max_tokens=60, temperature=0.3, oracle="Привет!")
        recs.append({"warmup": i < warmups, **r})
    return recs


def pass_tps(be, runs=20):
    recs = []
    for _ in range(runs):
        recs.append(be.complete(None, TPS_PROMPT, max_tokens=TPS_MAX_TOKENS, temperature=0.7,
                                oracle="Жил-был ёжик. " * 30))
    return recs


CLASSIFY_QUESTIONS = {"type": {"type": "choice", "instructions": "Определи тип задачи голосовой фразы, распознанной колонкой.",
                               "criteria": dict(TYPES)}}


ROUTE_QUESTIONS = {"route": {"type": "choice", "instructions": "Выбери маршрут голосовой фразы, распознанной колонкой.", "criteria": {
    "deterministic": "ответ даёт локальный инструмент: погода, точное время, перевод единиц измерения",
    "fast": "только болтовня без фактов: приветствие, благодарность, настроение, шутка, вопрос о тебе самом, «да/нет/ладно»",
    "reprompt": "шум распознавания, обрывок фразы, одно слово без команды: нужно переспросить",
    "escalate": "всё остальное: факты о мире, новости, цены, советы, объяснения, тексты, задачи, команды, смесь болтовни с вопросом"}}}
ROUTE_TO_TYPE = {"deterministic": "deterministic_skill", "fast": "banter", "reprompt": "reprompt", "escalate": "open_fact"}


def pass_classify(be, items, mode="type"):
    questions = CLASSIFY_QUESTIONS if mode == "type" else ROUTE_QUESTIONS
    recs = []
    for it in items:
        oracle = it["type"] if mode == "type" else it["route"]
        r = be.classify(it["text"], questions, **({"oracle": oracle} if isinstance(be, MockBackend) else {}))
        recs.append({"id": it["id"], **r})
    return recs


LLM_PASSES = {"route": lambda be, items: pass_route(be, items), "pipe": lambda be, items: pass_pipe(be, items),
              "slots": lambda be, items: pass_slots(be), "talk": lambda be, items: pass_talk(be),
              "para": lambda be, items: pass_para(be), "know": lambda be, items: pass_know(be),
              "lat": lambda be, items: pass_lat(be), "tps": lambda be, items: pass_tps(be)}


def save_pass(out_path: Path, model: str, meta: dict, name: str, records: list) -> None:
    """Merge one finished pass into the model file under a lock: concurrent runs of different passes both survive."""
    with open(str(out_path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        res = json.loads(out_path.read_text()) if out_path.exists() else {"model": model, "passes": {}, "pass_meta": {}}
        res.setdefault("pass_meta", {})
        res["passes"][name] = records
        res["pass_meta"][name] = meta
        res["meta"] = meta
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False))
        os.replace(tmp, out_path)


def cmd_run(a):
    be = make_backend(a.model, a.claude_via, a.mock)
    items = eval_items()
    classifier = a.model in ("laya", "laya_en", "jev")
    passes = a.passes.split(",") if a.passes else (["classify", "classify_route"] if classifier else list(LLM_PASSES))
    out_path = Path(a.out_dir) / f"{a.model}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        earlier = json.loads(out_path.read_text()).get("pass_meta", {})
        if any(m.get("mock") != a.mock for m in earlier.values()):
            sys.exit(f"{out_path} already holds {'real' if a.mock else 'mock'} results; use another --out-dir for a {'mock' if a.mock else 'real'} run")
    be.warm()
    meta = {"date": time.strftime("%Y-%m-%d %H:%M:%S"), "host": socket.gethostname(), "mock": a.mock,
            "backend": type(be).__name__, "claude_via": a.claude_via if a.model in ("sonnet", "haiku") else None}
    if isinstance(be, OmpBackend):
        meta["prefix_tokens"] = be.prefix_tokens
    for p in passes:
        t0 = time.time()
        records = (pass_classify(be, items, "type" if p == "classify" else "route") if p.startswith("classify") else LLM_PASSES[p](be, items))
        errs = sum(1 for r in records if (r.get("error") or (r.get("cls") or {}).get("error") or (r.get("gen") or {}).get("error")))
        print(f"{a.model}/{p}: {len(records)} records, {errs} with errors, {time.time() - t0:.0f}s", flush=True)
        save_pass(out_path, a.model, meta, p, records)
    return 0


# ---------------------------------------------------------------------------------------------
# Judging: Russian quality of the small-talk replies by the strongest available model, blind.
# ---------------------------------------------------------------------------------------------
RUBRIC = """Оцени реплики голосового помощника на русском, не зная, какая модель их написала. Критерии, каждый 0-2:
grammar: правильность русского (2 безупречно, 1 мелкие ошибки или неловкие падежи, 0 бессвязно, чужие слова или символы)
natural: звучит как живая устная речь, не как калька с английского (2 естественно, 1 допустимо, 0 неестественно)
persona: соблюдены правила: 1-2 коротких предложения, без списков, цифр и разметки, без выдуманных фактов, без обещаний что-то включить или найти (2 всё соблюдено, 1 одно нарушение, 0 два и больше)
fit: ответ уместен и тёплый по смыслу фразы пользователя (2 да, 1 нейтрально или слегка мимо, 0 мимо или неуместно)
Верни строго JSON без пояснений: {"A":{"grammar":n,"natural":n,"persona":n,"fit":n},"B":{...}}"""
SXS = """Ты сравниваешь две реплики голосового помощника на одну фразу пользователя, не зная, кто их написал.
Лучше та, что звучит естественнее по-русски, грамотнее, короче в рамках правил (1-2 предложения, без списков, цифр, выдуманных фактов) и теплее по смыслу.
Верни строго JSON без пояснений: {"winner":"A"|"B"|"tie","reason":"<одна фраза>"}"""


def judge_call(system, user, mock, schema_ok):
    """One judge call; returns the parsed answer only when `schema_ok` accepts it, else None (failures are counted, not hidden)."""
    if mock:
        return {"A": {"grammar": 2, "natural": 2, "persona": 2, "fit": 2}, "B": {"grammar": 2, "natural": 2, "persona": 2, "fit": 2},
                "C": {"grammar": 2, "natural": 2, "persona": 2, "fit": 2}, "D": {"grammar": 2, "natural": 2, "persona": 2, "fit": 2},
                "winner": "A"}
    for _ in range(2):
        try:
            p = subprocess.run(["omp", "-p", "--mode", "json", "--model", JUDGE_MODEL, "--thinking", "low", "--no-tools", "--no-skills",
                                "--no-rules", "--no-context-files", "--no-extensions", "--no-lsp", "--no-session", "--no-title",
                                "--system-prompt", system, user], capture_output=True, text=True, encoding="utf-8", timeout=300)
        except subprocess.SubprocessError:
            continue
        text = ""
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "turn_end":
                text = "".join(c.get("text", "") for c in ev["message"].get("content", []) if c.get("type") == "text")
        obj = first_json(text)[0]
        if p.returncode == 0 and isinstance(obj, dict) and schema_ok(obj):
            return obj
    return None


def rubric_ok(letters):
    def check(obj):
        return all(isinstance(obj.get(l), dict) and all(isinstance(obj[l].get(k), int) and 0 <= obj[l][k] <= 2
                                                         for k in ("grammar", "natural", "persona", "fit")) for l in letters)
    return check


def sxs_ok(obj):
    return obj.get("winner") in ("A", "B", "tie")


def cmd_judge(a):
    d = Path(a.out_dir)
    models = [m for m in ("qwen", "gptoss", "sonnet", "haiku") if (d / f"{m}.json").exists()]
    talk = {m: {r["id"]: r for r in json.loads((d / f"{m}.json").read_text())["passes"].get("talk", [])} for m in models}
    prompts = jload(DATA / "smalltalk.jsonl")
    rng = random.Random(20261116)
    rubric, sxs, failed = [], [], 0
    for pr in prompts:
        avail = [m for m in models if (talk[m].get(pr["id"]) or {}).get("text") and not is_refusal(talk[m][pr["id"]]["text"])]
        order = avail[:]
        rng.shuffle(order)
        letters = "ABCD"[: len(order)]
        if not letters:
            continue
        body = f"Фраза пользователя: «{pr['text']}»\n" + "\n".join(f"{l}: {talk[m][pr['id']]['text'].strip()}" for l, m in zip(letters, order))
        j = judge_call(RUBRIC.replace('"A":{"grammar":n,"natural":n,"persona":n,"fit":n},"B":{...}', ", ".join(f'"{l}":{{...}}' for l in letters)),
                       body, a.mock, rubric_ok(letters))
        failed += j is None
        rubric.append({"id": pr["id"], "order": dict(zip(letters, order)), "scores": j})
    pair = ("qwen", "gptoss")
    if all(m in talk for m in pair):
        for pass_no, swap in ((1, False), (2, True)):
            for pr in prompts:
                ra, rb = (talk[m].get(pr["id"]) or {} for m in pair)
                if not (ra.get("text") and rb.get("text")) or is_refusal(ra["text"]) or is_refusal(rb["text"]):
                    sxs.append({"pass": pass_no, "id": pr["id"], "skipped": True})
                    continue
                first, second = (pair[1], pair[0]) if swap else pair
                body = (f"Фраза пользователя: «{pr['text']}»\nA: {talk[first][pr['id']]['text'].strip()}\nB: {talk[second][pr['id']]['text'].strip()}")
                j = judge_call(SXS, body, a.mock, sxs_ok)
                failed += j is None
                w = None if j is None else {"A": first, "B": second}.get(j["winner"], "tie")
                sxs.append({"pass": pass_no, "id": pr["id"], "A": first, "B": second, "winner": w, "reason": (j or {}).get("reason")})
    else:
        print("side-by-side skipped: it needs both qwen and gptoss results")
    (d / "judge.json").write_text(json.dumps({"judge_model": JUDGE_MODEL, "rubric": rubric, "sxs": sxs, "failed_calls": failed}, ensure_ascii=False))
    print(f"judge: {len(rubric)} rubric, {len(sxs)} side-by-side records, {failed} failed judge calls")
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------
# Report.
# ---------------------------------------------------------------------------------------------
def llm_text(r: dict) -> str:
    return (r.get("text") or "").strip()


def is_refusal(text: str) -> bool:
    """The generator's refusal is exactly the token ESCALATE; a reply that merely mentions it is an answer."""
    return norm(text).strip(" .,!?…\"'`«»*") == "escalate"


def valid_reply(text: str) -> bool:
    """Design section 6.2 post-validator (code, not model)."""
    t = text.strip()
    sentences = [s for s in re.split(r"[.!?…]+", t) if s.strip()]
    return (bool(t) and len(t) <= 220 and len(sentences) <= 2 and len(t.split()) <= 30 and not re.search(r"\d", t)
            and not re.search(r"https?://|www\.", t) and not LATIN.search(t) and "escalate" not in t.lower())


WORD_NUMBERS = {w: i for i, w in enumerate(
    "ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать "
    "шестнадцать семнадцать восемнадцать девятнадцать".split())}
WORD_NUMBERS.update({"нуль": 0, "одна": 1, "одно": 1, "две": 2, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
                     "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100, "двести": 200,
                     "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
                     "девятьсот": 900, "нуле": 0, "нулю": 0, "двух": 2, "трех": 3, "четырех": 4, "пяти": 5, "шести": 6,
                     "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10})


def number_spans(text: str) -> tuple[list[str], list[tuple[int, int, int]]]:
    """Tokens and the numbers in them as (signed value, first token, token after the last): digits and Russian number
    words, compounds such as «сто двадцать пять» summed, «минус»/«-» negating."""
    toks = re.findall(r"-?\d+|[а-яa-z]+|°", norm(text))
    spans: list[tuple[int, int, int]] = []
    cur = start = None
    sign, pending = 1, 1

    def flush(end):
        nonlocal cur, start, sign
        if cur is not None:
            spans.append((sign * cur, start, end))
            cur, sign = None, 1

    for i, tok in enumerate(toks):
        if re.fullmatch(r"-?\d+", tok):
            flush(i)
            spans.append((int(tok) if tok.startswith("-") else pending * int(tok), i, i + 1))
            pending = 1
        elif tok in ("минус", "плюс"):
            flush(i)
            pending = -1 if tok == "минус" else 1
        elif tok in WORD_NUMBERS:
            v = WORD_NUMBERS[tok]
            if cur is None:
                cur, start, sign, pending = v, i, pending, 1
            elif (cur % 100 == 0 and v < 100) or (cur % 10 == 0 and cur % 100 != 0 and v < 10):
                cur += v
            else:
                flush(i)
                cur, start = v, i
        else:
            flush(i)
            pending = 1
    flush(len(toks))
    return toks, spans


def matches_answer(kind: str, alts: list[str], reply: str) -> bool:
    """A number alternative must equal a number of the reply (exact value: «1391» is not «391», «сорок пять» is not «сорок»;
    a reply that lists several numbers can still pass). A text alternative must start a word of the reply."""
    t = norm(reply)
    values = [v for v, _, _ in number_spans(reply)[1]]
    for alt in alts:
        a = norm(alt)
        gold = [v for v, _, _ in number_spans(a)[1]]
        if gold and re.fullmatch(r"[\d ]+|[а-я ]+", a):
            if gold[0] in values:
                return True
        elif re.search(r"(?<![а-яa-z0-9])" + re.escape(a) + (r"(?![а-яa-z0-9])" if re.fullmatch(r"[a-z]{1,3}", a) else ""), t):
            return True
    return False


def para_check(reply: str, item: dict) -> tuple[bool, bool]:
    """(faithful, invented a number). Faithful: city and condition named, and the signed temperature stated as the
    temperature (next to «градус»/°, or after «температура»); a swapped temperature and wind or a lost sign fails."""
    facts = item["facts"]
    t = norm(reply)
    toks, spans = number_spans(reply)
    named = all(any(norm(alt) in t for alt in x.split("|")) for i, x in enumerate(item["must"]) if i != 1)
    temp = facts["temp_c"]
    temp_ok = any(v == temp and (any(w.startswith("градус") or w == "°" for w in toks[e:e + 3])
                                 or any(w.startswith("температур") for w in toks[max(0, s - 4):s])) for v, s, e in spans)
    extra = {abs(v) for v, _, _ in spans} - {abs(temp), facts["wind_ms"]}
    return named and temp_ok and not extra, bool(extra)


def lat_stats(values: list[float]) -> dict:
    v = [x for x in values if x is not None]
    return {"n": len(v), "p50": pct(v, 0.5), "p95": pct(v, 0.95), "mean": statistics.fmean(v) if v else None}


def metrics_llm(model: str, res: dict, items: list[dict]) -> dict:
    by_id = {it["id"]: it for it in items}
    P = res["passes"]
    m: dict = {}
    if "route" in P:
        rs = P["route"]
        pred = {}
        strict = valid = 0
        for r in rs:
            obj, is_strict = first_json(llm_text(r))
            t = obj.get("type") if isinstance(obj, dict) else None
            pred[r["id"]] = t if isinstance(t, str) and t in TYPES else None
            valid += pred[r["id"]] is not None
            strict += bool(is_strict and pred[r["id"]] is not None and llm_text(r).startswith("{"))
        n = len(rs)
        typ_ok = sum(pred[i] == by_id[i]["type"] for i in pred)
        route_ok = sum(pred[i] is not None and TYPE_TO_ROUTE[pred[i]] == by_id[i]["route"] for i in pred)
        esc = [i for i in pred if by_id[i]["route"] == "escalate"]
        rep = [i for i in pred if by_id[i]["route"] == "reprompt"]
        fast_to_esc = [i for i in pred if by_id[i]["route"] == "fast"]
        m["route"] = {
            "n": n, "type_acc": typ_ok / n, "route_acc": route_ok / n, "json_valid": valid / n, "json_strict": strict / n,
            "false_fast_on_escalate": sum(pred[i] is not None and TYPE_TO_ROUTE[pred[i]] == "fast" for i in esc),
            "n_escalate": len(esc),
            "false_fast_on_fact": sum(pred[i] is not None and TYPE_TO_ROUTE[pred[i]] == "fast" for i in esc if by_id[i]["type"] == "open_fact"),
            "n_fact": sum(by_id[i]["type"] == "open_fact" for i in esc),
            "false_deterministic_on_escalate": sum(pred[i] is not None and TYPE_TO_ROUTE[pred[i]] == "deterministic" for i in esc),
            "reprompt_to_fast": sum(pred[i] is not None and TYPE_TO_ROUTE[pred[i]] == "fast" for i in rep),
            "fast_recall": sum(pred[i] is not None and TYPE_TO_ROUTE[pred[i]] == "fast" for i in fast_to_esc) / max(1, len(fast_to_esc)),
            "per_type_acc": {t: (lambda ids: sum(pred[i] == t for i in ids) / max(1, len(ids)))([i for i in pred if by_id[i]["type"] == t]) for t in TYPES},
            "lat_total_ms": lat_stats([r["total_ms"] for r in rs]),
            "lat_by_type": {t: lat_stats([r["total_ms"] for r in rs if by_id[r["id"]]["type"] == t]) for t in TYPES},
            "errors": sum(1 for r in rs if r.get("error")),
            "in_tok": statistics.fmean([r["in_tok"] for r in rs if r.get("in_tok") is not None] or [0]),
            "out_tok": statistics.fmean([r["out_tok"] for r in rs if r.get("out_tok") is not None] or [0]),
        }
    if "pipe" in P:
        rs = P["pipe"]
        cls_ok = fast_cls = 0
        gen_content = {}
        wrong = collections.Counter()
        tot = collections.Counter()
        recall = reprompt_safe = reprompt_n = cls_json = cls_strict = 0
        for r in rs:
            it = by_id[r["id"]]
            obj, is_strict = first_json(llm_text(r["cls"]))
            cls_route = obj.get("route") if isinstance(obj, dict) and obj.get("route") in ("fast", "escalate") else None
            cls_json += cls_route is not None
            cls_strict += bool(cls_route and is_strict)
            g = llm_text(r["gen"])
            content = bool(g) and not is_refusal(g)
            ok_validator = content and valid_reply(g)
            fast_ok = cls_route == "fast" and ok_validator
            expect_fast = it["route"] == "fast"
            cls_ok += (cls_route == "fast") == expect_fast
            if it["route"] == "escalate":
                grp = "fact" if it["type"] == "open_fact" else ("trap" if it["type"] == "mixed_or_followup" else "complex")
                for scope in ("all", grp):
                    tot[scope] += 1
                    wrong[("alone", scope)] += content
                    wrong[("alone_validated", scope)] += ok_validator
                    wrong[("pipeline", scope)] += fast_ok
                    wrong[("classifier_fast", scope)] += cls_route == "fast"
            if it["route"] == "deterministic":
                tot["det"] += 1
                wrong[("alone", "det")] += content
                wrong[("pipeline", "det")] += fast_ok
            if it["route"] == "fast":
                recall += fast_ok
            if it["route"] == "reprompt":
                reprompt_n += 1
                reprompt_safe += (not content) or bool(REPEAT_REQ.search(g))
                wrong[("pipeline", "reprompt")] += fast_ok and not REPEAT_REQ.search(g)
        n_fast = sum(1 for it in items if it["route"] == "fast")
        m["pipe"] = {
            "n": len(rs), "classifier_fast_vs_escalate_acc": cls_ok / len(rs), "classifier_json_valid": cls_json / len(rs),
            "classifier_json_strict": cls_strict / len(rs),
            "totals": dict(tot), "wrong": {f"{k[0]}:{k[1]}": v for k, v in wrong.items()},
            "fast_recall": recall / n_fast, "n_fast": n_fast,
            "reprompt_safe": reprompt_safe, "reprompt_n": reprompt_n,
            "cls_lat": lat_stats([r["cls"]["total_ms"] for r in rs]), "gen_lat": lat_stats([r["gen"]["total_ms"] for r in rs]),
            "gen_ttft": lat_stats([r["gen"]["ttft_ms"] for r in rs]),
            "gen_lat_by_type": {t: lat_stats([r["gen"]["total_ms"] for r in rs if by_id[r["id"]]["type"] == t]) for t in TYPES},
            "cls_lat_by_type": {t: lat_stats([r["cls"]["total_ms"] for r in rs if by_id[r["id"]]["type"] == t]) for t in TYPES},
            "errors": sum(1 for r in rs if r["cls"].get("error") or r["gen"].get("error")),
            "cls_in": statistics.fmean([r["cls"]["in_tok"] for r in rs if r["cls"].get("in_tok") is not None] or [0]),
            "cls_out": statistics.fmean([r["cls"]["out_tok"] for r in rs if r["cls"].get("out_tok") is not None] or [0]),
            "gen_in": statistics.fmean([r["gen"]["in_tok"] for r in rs if r["gen"].get("in_tok") is not None] or [0]),
            "gen_out": statistics.fmean([r["gen"]["out_tok"] for r in rs if r["gen"].get("out_tok") is not None] or [0]),
        }
    if "slots" in P:
        gold = {s["id"]: s["gold"] for s in jload(DATA / "slots.jsonl")}
        fields = ["intent", "city", "day", "room", "device", "value"]
        ok = collections.Counter()
        cnt = collections.Counter()
        halluc = null_gold = full = parsed = 0
        for r in P["slots"]:
            obj, _ = first_json(llm_text(r))
            parsed += isinstance(obj, dict)
            obj = obj if isinstance(obj, dict) else {}
            item_ok = True
            for f in fields:
                g, p = gold[r["id"]][f], obj.get(f)
                good = norm(g) == norm(p) or (g is not None and p is not None and norm(str(g)) == norm(str(p)))
                ok[f] += good
                cnt[f] += 1
                item_ok &= good
                if f != "intent" and g is None:
                    null_gold += 1
                    halluc += p is not None
            full += item_ok
        n = len(P["slots"])
        m["slots"] = {"n": n, "parsed": parsed / n, "exact_item": full / n, "per_field": {f: ok[f] / cnt[f] for f in fields},
                      "five_slot_acc": sum(ok[f] for f in fields if f != "intent") / sum(cnt[f] for f in fields if f != "intent"),
                      "hallucinated_slot_rate": halluc / max(1, null_gold),
                      "lat": lat_stats([r["total_ms"] for r in P["slots"]])}
    if "talk" in P:
        rs = P["talk"]
        texts = [llm_text(r) for r in rs]
        m["talk"] = {"n": len(rs), "escalated": sum(is_refusal(t) for t in texts),
                     "validator_pass": sum(valid_reply(t) for t in texts if not is_refusal(t)),
                     "lat": lat_stats([r["total_ms"] for r in rs]), "mean_chars": statistics.fmean([len(t) for t in texts])}
    if "para" in P:
        facts = {p["id"]: p for p in jload(DATA / "paraphrase.jsonl")}
        faithful = extra_numbers = 0
        for r in P["para"]:
            ok, extra = para_check(llm_text(r), facts[r["id"]])
            faithful += ok
            extra_numbers += extra
        n = len(P["para"])
        m["para"] = {"n": n, "faithful": faithful / n, "extra_number_rate": extra_numbers / n,
                     "lat": lat_stats([r["total_ms"] for r in P["para"]])}
    if "know" in P:
        kn = {k["id"]: k for k in jload(DATA / "knowledge.jsonl")}
        acc = collections.Counter()
        cnt = collections.Counter()
        for r in P["know"]:
            k = kn[r["id"]]
            text = llm_text(r)
            groups = k.get("all") or [k["answers"]]
            kind = k["kind"] if not k.get("all") else "fact"
            acc[k["kind"]] += all(matches_answer(kind, g, text) for g in groups)
            cnt[k["kind"]] += 1
        m["know"] = {kind: {"acc": acc[kind] / cnt[kind], "n": cnt[kind]} for kind in cnt}
        m["know"]["lat"] = lat_stats([r["total_ms"] for r in P["know"]])
    if "lat" in P:
        rs = [r for r in P["lat"] if not r.get("warmup")]
        ok = [r for r in rs if not r.get("error") and r.get("ttft_ms") is not None]
        m["lat"] = {"runs": len(rs), "errors": len(rs) - len(ok), "ttft": lat_stats([r["ttft_ms"] for r in ok]),
                    "ttft_any": lat_stats([r["ttft_any_ms"] for r in ok]), "total": lat_stats([r["total_ms"] for r in ok]),
                    "retries": sum(r.get("retries", 0) for r in rs)}
    if "tps" in P:
        vals = []
        outs = []
        for r in P["tps"]:
            if r.get("error") or not r.get("out_tok") or r["out_tok"] < 8:
                continue
            outs.append(r["out_tok"])
            dec = r.get("decode_ms") if r.get("decode_ms") else (r["total_ms"] - r["ttft_any_ms"] if r.get("ttft_any_ms") is not None else None)
            if dec and dec > 0:
                vals.append((r["out_tok"] - 1) / (dec / 1000))
        m["tps"] = {"runs": len(P["tps"]), "ok": len(vals), "p50": pct(vals, 0.5), "p95_low": pct(vals, 0.05),
                    "mean_out_tok": statistics.fmean(outs) if outs else None}
    price = PRICES.get(model)
    if price and "pipe" in m:
        p = m["pipe"]
        per_call = lambda i, o: (i * price[0] + o * price[1]) / 1e6 * 1000
        m["cost_per_1000"] = {"classifier": per_call(p["cls_in"], p["cls_out"]), "generator": per_call(p["gen_in"], p["gen_out"])}
        m["cost_per_1000"]["pipeline_2_calls"] = m["cost_per_1000"]["classifier"] + m["cost_per_1000"]["generator"]
    return m


def metrics_classifier(res: dict, items: list[dict], pass_name: str = "classify") -> dict:
    by_id = {it["id"]: it for it in items}
    rs = res["passes"][pass_name]
    key = "type" if pass_name == "classify" else "route"
    ok = [r for r in rs if not r.get("error")]
    out: dict = {"n": len(rs), "errors": len(rs) - len(ok), "first_error": next((r["error"] for r in rs if r.get("error")), None)}
    if not ok:
        return out
    pred = {}
    for r in ok:
        a = r["answers"].get(key)
        a = a if isinstance(a, dict) else {}
        choice = a.get("choice") if isinstance(a.get("choice"), str) else None
        probs = [v for v in (a.get("probabilities") or {}).values() if isinstance(v, (int, float))] if isinstance(a.get("probabilities"), dict) else []
        pred[r["id"]] = (choice if key == "type" else ROUTE_TO_TYPE.get(choice), max(probs) if probs else None)
    esc = [i for i in pred if by_id[i]["route"] == "escalate"]

    def at(tau):
        c = fe = ff = rep = 0
        det_ok = det_n = 0
        for i, (lab, p) in pred.items():
            if lab not in TYPES or (p is not None and p < tau):
                lab = "mixed_or_followup"  # doubt -> escalate, per the design
            it = by_id[i]
            c += lab == it["type"]
            r = TYPE_TO_ROUTE[lab]
            if it["route"] == "escalate" and r == "fast":
                fe += 1
            if it["route"] == "reprompt" and r == "fast":
                rep += 1
            if it["route"] == "deterministic":
                det_n += 1
                det_ok += r == "deterministic"
        route_ok = sum(1 for i, (lab, p) in pred.items()
                       if TYPE_TO_ROUTE[lab if lab in TYPES and not (p is not None and p < tau) else "mixed_or_followup"] == by_id[i]["route"])
        return {"type_acc": c / len(pred), "route_acc": route_ok / len(pred), "false_fast_on_escalate": fe, "n_escalate": len(esc),
                "reprompt_to_fast": rep, "deterministic_recall": det_ok / max(1, det_n)}
    out["by_threshold"] = {str(t): at(t) for t in (0.0, 0.5, 0.7, 0.9)}
    out["per_type_acc"] = {t: (lambda ids: sum(pred[i][0] == t for i in ids) / max(1, len(ids)))([i for i in pred if by_id[i]["type"] == t]) for t in TYPES}
    out["lat"] = lat_stats([r["total_ms"] for r in ok])
    return out


def sum_percentiles(stages: list[list[float]]) -> dict:
    """Percentiles of a sum of independent stages, from every combination of the measured samples (not a sum of percentiles)."""
    sums = [0.0]
    for samples in stages:
        sums = [s + x for s in sums for x in samples]
    return {"n": len(sums), "p50": pct(sums, 0.5), "p95": pct(sums, 0.95)}


def stage_samples(d: Path) -> dict:
    """Measured per-call latencies (ms) of the stages that make up the pipelines of the report."""
    def load(m):
        return json.loads((d / f"{m}.json").read_text()) if (d / f"{m}.json").exists() else None
    out = {}
    q, s, j = load("qwen"), load("sonnet"), load("jev")
    if q and "pipe" in q["passes"]:
        out["qwen_classifier"] = [r["cls"]["total_ms"] for r in q["passes"]["pipe"] if r["cls"].get("total_ms") is not None and not r["cls"].get("error")]
    if q and "lat" in q["passes"]:
        out["qwen_generator"] = [r["total_ms"] for r in q["passes"]["lat"] if not r.get("warmup") and not r.get("error") and r.get("total_ms") is not None]
    if s and "lat" in s["passes"]:
        out["sonnet_short_answer"] = [r["total_ms"] for r in s["passes"]["lat"] if not r.get("warmup") and not r.get("error") and r.get("total_ms") is not None]
    if j and "classify_route" in j["passes"]:
        out["jev_route"] = [r["total_ms"] for r in j["passes"]["classify_route"] if not r.get("error")]
    return out


PIPELINES = {
    "rules + Qwen classifier + Qwen generator (design)": ["qwen_classifier", "qwen_generator"],
    "rules + Qwen generator only": ["qwen_generator"],
    "Jev + Qwen generator": ["jev_route", "qwen_generator"],
    "Jev + Sonnet short answer (escalate)": ["jev_route", "sonnet_short_answer"],
    "rules + Sonnet short answer (escalate)": ["sonnet_short_answer"],
}


def fmt_ms(x):
    return "-" if x is None else f"{x:,.0f}"


def run_is_mock(res: dict) -> bool:
    return any(m.get("mock") for m in (res.get("pass_meta") or {"all": res.get("meta") or {}}).values())


def cmd_report(a):
    d = Path(a.out_dir)
    items = eval_items()
    report: dict = {"models": {}, "classifiers": {}, "mock": False}
    for m in ("qwen", "gptoss", "sonnet", "haiku"):
        if (d / f"{m}.json").exists():
            res = json.loads((d / f"{m}.json").read_text())
            report["mock"] |= run_is_mock(res)
            report["models"][m] = {"meta": res.get("meta"), **metrics_llm(m, res, items)}
    report["classifiers_route"] = {}
    for m in ("laya", "laya_en", "jev"):
        if (d / f"{m}.json").exists():
            res = json.loads((d / f"{m}.json").read_text())
            report["mock"] |= run_is_mock(res)
            for name, dest in (("classify", "classifiers"), ("classify_route", "classifiers_route")):
                if name in res["passes"]:
                    report[dest][m] = {"meta": res.get("meta"), **metrics_classifier(res, items, name)}
    if (d / "judge.json").exists():
        j = json.loads((d / "judge.json").read_text())
        scores = collections.defaultdict(list)
        for r in j["rubric"]:
            for letter, model in r["order"].items():
                s = (r["scores"] or {}).get(letter)
                if isinstance(s, dict):
                    scores[model].append(sum(s.get(k, 0) for k in ("grammar", "natural", "persona", "fit")))
        wins = collections.Counter(r["winner"] for r in j["sxs"] if not r.get("skipped") and r.get("winner"))
        report["judge"] = {"judge_model": j["judge_model"], "rubric_mean_of_8": {m: statistics.fmean(v) for m, v in scores.items()},
                           "rubric_n": {m: len(v) for m, v in scores.items()}, "sxs_wins": dict(wins),
                           "sxs_n": sum(1 for r in j["sxs"] if not r.get("skipped") and r.get("winner")),
                           "failed_calls": j.get("failed_calls", 0)}
    samples = stage_samples(d)
    report["pipelines"] = {name: sum_percentiles([samples[s] for s in stages]) for name, stages in PIPELINES.items()
                           if all(samples.get(s) for s in stages)}
    out_json = Path(a.json_out) if a.json_out else d / "report.json"
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"wrote {out_json}")
    if a.md_out:
        Path(a.md_out).write_text(render_md(report), encoding="utf-8")
        print(f"wrote {a.md_out}")
    return 0


MODEL_LABEL = {"qwen": "Qwen 3.8 27B (Cerebras, no reasoning)", "gptoss": "gpt-oss-120b (Cerebras, low)",
               "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5", "laya": "Laya multilingual", "laya_en": "Laya english", "jev": "Jev"}


def render_md(rep: dict) -> str:
    """Markdown tables for docs/research/fast-llm-bench-2026-11.md, generated so no number is retyped."""
    ms, cl = rep["models"], rep["classifiers"]
    f = lambda x, d=0: "-" if x is None else f"{x:,.{d}f}"
    pc = lambda x: "-" if x is None else f"{100 * x:.0f}%"
    pr = lambda s: f"{f(s['p50'])} / {f(s['p95'])}" if s and s.get("p50") is not None else "-"
    out: list[str] = []

    def table(title, head, rows):
        out.append(f"\n**{title}**\n")
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "|".join("---" for _ in head) + "|")
        out.extend("| " + " | ".join(str(c) for c in r) + " |" for r in rows)

    lat = [(m, v) for m, v in ms.items() if "lat" in v]
    table("Speed (ms, short banter prompts, this PC)", ["model", "runs", "TTFT p50 / p95", "first token incl. reasoning p50", "total p50 / p95", "tok/s p50 (p5)", "errors"],
          [(MODEL_LABEL[m], v["lat"]["runs"], pr(v["lat"]["ttft"]), f(v["lat"]["ttft_any"]["p50"]), pr(v["lat"]["total"]),
            f"{f(v['tps']['p50'])} ({f(v['tps']['p95_low'])})" if "tps" in v and v["tps"]["p50"] else "-", v["lat"]["errors"]) for m, v in lat])
    rt = [(m, v) for m, v in ms.items() if "route" in v]
    table("Task-type classification by the model itself (149 labelled utterances, prompt-only JSON)",
          ["model", "type acc (7)", "route acc (4)", "wrongly fast on escalate", "...of which facts", "reprompt sent to fast", "fast recall", "strict JSON", "latency p50 / p95"],
          [(MODEL_LABEL[m], pc(v["route"]["type_acc"]), pc(v["route"]["route_acc"]),
            f"{v['route']['false_fast_on_escalate']}/{v['route']['n_escalate']}", f"{v['route']['false_fast_on_fact']}/{v['route']['n_fact']}",
            f"{v['route']['reprompt_to_fast']}/30", pc(v["route"]["fast_recall"]), pc(v["route"]["json_strict"]), pr(v["route"]["lat_total_ms"])) for m, v in rt])
    pp = [(m, v) for m, v in ms.items() if "pipe" in v]
    cell = lambda v, k, g: f"{v['wrong'].get(k + ':' + g, 0)}/{v['totals'].get(g, 0)}"
    table("Wrong fast answers: the model answers an escalate-labelled utterance (design generator prompt, no rules in front)",
          ["model", "any reply, all escalate", "facts", "traps+followup", "complex", "after post-validator", "full pipeline (classifier+generator+validator)", "tool-answerable answered by LLM (of 10)"],
          [(MODEL_LABEL[m], cell(v["pipe"], "alone", "all"), cell(v["pipe"], "alone", "fact"), cell(v["pipe"], "alone", "trap"),
            cell(v["pipe"], "alone", "complex"), cell(v["pipe"], "alone_validated", "all"), cell(v["pipe"], "pipeline", "all"),
            f"{v['pipe']['wrong'].get('alone:det', 0)}/10") for m, v in pp])
    table("Design pipeline (classifier fast/escalate + generator) on the other labels",
          ["model", "classifier fast-vs-escalate acc", "classifier strict JSON", "fast recall (38)", "reprompt safe (30)", "classifier p50 / p95 ms", "generator p50 / p95 ms"],
          [(MODEL_LABEL[m], pc(v["pipe"]["classifier_fast_vs_escalate_acc"]), pc(v["pipe"]["classifier_json_strict"]), pc(v["pipe"]["fast_recall"]),
            f"{v['pipe']['reprompt_safe']}/{v['pipe']['reprompt_n']}", pr(v["pipe"]["cls_lat"]), pr(v["pipe"]["gen_lat"])) for m, v in pp])
    sl = [(m, v) for m, v in ms.items() if "slots" in v]
    table("Slot extraction (47 phrases; city/day/room/device/value)",
          ["model", "five-slot acc", "city", "day", "room", "device", "value", "invented slot", "whole phrase exact", "valid JSON", "latency p50 / p95"],
          [(MODEL_LABEL[m], pc(v["slots"]["five_slot_acc"]), *[pc(v["slots"]["per_field"][k]) for k in ("city", "day", "room", "device", "value")],
            pc(v["slots"]["hallucinated_slot_rate"]), pc(v["slots"]["exact_item"]), pc(v["slots"]["parsed"]), pr(v["slots"]["lat"])) for m, v in sl])
    tk = [(m, v) for m, v in ms.items() if "talk" in v]
    j = rep.get("judge", {})
    table("Small talk (20 prompts, design generator prompt) and tool-result paraphrase / knowledge sanity",
          ["model", "answered (not ESCALATE)", "answered replies passing the validator", f"rubric /8 ({j.get('judge_model', 'judge n/a')})", "weather paraphrase faithful (12)", "invented number", "static facts (12)", "reasoning (8)"],
          [(MODEL_LABEL[m], f"{v['talk']['n'] - v['talk']['escalated']}/{v['talk']['n']}", f"{v['talk']['validator_pass']}/{v['talk']['n'] - v['talk']['escalated']}",
            f(j.get("rubric_mean_of_8", {}).get(m), 2), pc(v.get("para", {}).get("faithful")), pc(v.get("para", {}).get("extra_number_rate")),
            pc(v.get("know", {}).get("fact", {}).get("acc")), pc(v.get("know", {}).get("reason", {}).get("acc"))) for m, v in tk])
    if j:
        out.append(f"\nBlind side-by-side Qwen vs gpt-oss, {j['sxs_n']} judgements (two passes, positions swapped): " + ", ".join(f"{MODEL_LABEL.get(k, k)} {n}" for k, n in j["sxs_wins"].items()))
    cs = [(m, v) for m, v in ms.items() if "cost_per_1000" in v]
    table("Cost per 1000 requests (USD, tokens measured in the design pipeline)", ["model", "classifier call", "generator call", "two-call pipeline"],
          [(MODEL_LABEL[m], *[f"{v['cost_per_1000'][k]:.3f}" for k in ("classifier", "generator", "pipeline_2_calls")]) for m, v in cs])
    tr = ["canned_smalltalk", "banter", "reprompt", "deterministic_skill", "open_fact", "complex", "mixed_or_followup"]
    table("Task-type recall of each LLM acting as classifier (percent of that type labelled correctly)", ["model", *tr],
          [(MODEL_LABEL[m], *[pc(v["route"]["per_type_acc"][t]) for t in tr]) for m, v in rt])
    for title, src, with_type in (("Laya / Jev with the 7 task types (probability below the threshold means escalate)", cl, True),
                                  ("Laya / Jev with the 4 routes (deterministic / fast / reprompt / escalate)", rep.get("classifiers_route", {}), False)):
        cr = [(m, v) for m, v in src.items() if v.get("by_threshold")]
        table(title, ["classifier", "threshold", "type acc (7)", "route acc (4)", "wrongly fast on escalate (of 71)", "reprompt sent to fast (of 30)", "deterministic recall", "latency p50 / p95 ms"],
              [(MODEL_LABEL[m], t, pc(b["type_acc"]) if with_type else "-", pc(b["route_acc"]), b["false_fast_on_escalate"], b["reprompt_to_fast"],
                pc(b["deterministic_recall"]), pr(v["lat"])) for m, v in cr for t, b in v["by_threshold"].items()])
        for m, v in src.items():
            if v.get("errors"):
                out.append(f"\n{MODEL_LABEL[m]} ({title[:20]}...): {v['errors']}/{v['n']} calls failed; first error: `{v.get('first_error')}`")
    if rep.get("pipelines"):
        table("Full-pipeline latency, ms (independent stages: percentiles of all combinations of the measured calls)", ["path", "p50", "p95"],
              [(name, f(v["p50"]), f(v["p95"])) for name, v in rep["pipelines"].items()])
    if rep.get("mock"):
        out.insert(0, "\n**MOCK DATA: at least one pass was produced by the offline oracle, not a real model.**\n")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", required=True, choices=["qwen", "gptoss", "sonnet", "haiku", "laya", "laya_en", "jev"])
    r.add_argument("--claude-via", choices=["omp", "anthropic"], default="omp")
    r.add_argument("--passes", help="comma list of: " + ",".join(list(LLM_PASSES) + ["classify"]))
    r.add_argument("--out-dir", default="/tmp/fast-llm-bench")
    r.add_argument("--mock", action="store_true")
    r.set_defaults(fn=cmd_run)
    j = sub.add_parser("judge")
    j.add_argument("--out-dir", default="/tmp/fast-llm-bench")
    j.add_argument("--mock", action="store_true")
    j.set_defaults(fn=cmd_judge)
    p = sub.add_parser("report")
    p.add_argument("--out-dir", default="/tmp/fast-llm-bench")
    p.add_argument("--json-out")
    p.add_argument("--md-out")
    p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
