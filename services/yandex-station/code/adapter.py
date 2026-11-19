"""Local Quasar processor and OpenAI-compatible Брачо speech gateway.

The ChatGPT command consumes one JSON line on stdin and returns one JSON object
{text, conversation_id}. It owns the browser; this service never retries a turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
from pathlib import Path
import signal
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from typing import Any
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, WSMsgType, web

import announcement_cache
import asr_correct
import dialog_context
import dialogstore
import ha_actions
import intent_route
import jev
import led_animation
import reply_guard
import user_prompt
import timers
import tts_settings
import uniproxy_tts
import utterance_trace
import voice_skills
import voice_vocab

# Local instant-arithmetic tool (fin-bot calculator port): pure-math turns
# are answered without the browser roundtrip.
from calc import evaluate as calc_evaluate
# Cross-dialog memory: hybrid retrieval injected into brain prompts, durable
# facts written back by a gated LLM extractor (tg#27845).
from memory import MEMORY_HINT_RE, Memory, parse_facts, sync_dialogs

LOG = logging.getLogger("aisis.yandex")
BRACHO_REFERENCE = "5cd1ffa5dbb4419cb5766435139c6d92"
ERROR_REPLY = "Не удалось получить ответ ChatGPT. Проверь авторизацию и подключение. Я не буду повторять запрос автоматически."


NO_SPEECH_REPLY = "Повтори?"

EXTRACT_PROMPT = ('Из диалога голосового ассистента выпиши устойчивые факты о владельце и его доме: '
    'имена, предпочтения, привычки, устройства, расписание, даты. Игнорируй сиюминутные просьбы и '
    'состояния, которые скоро изменятся. Ответь строго одним JSON без пояснений: '
    '{"facts":[{"text":"...","kind":"fact или preference"}]}. Если устойчивых фактов нет, '
    'ответь {"facts":[]}.')


class TurnError(Exception):
    pass


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (station TEXT PRIMARY KEY, conversation TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, station TEXT, status TEXT, started REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS persona (station TEXT PRIMARY KEY, delivered REAL, digest TEXT)")
        if "digest" not in {col[1] for col in self.db.execute("PRAGMA table_info(persona)")}:
            self.db.execute("ALTER TABLE persona ADD COLUMN digest TEXT")
        self.db.execute("UPDATE turns SET status='interrupted' WHERE status='running'")
        self.db.commit()
        os.chmod(path, 0o600)

    def conversation(self, station: str) -> str | None:
        row = self.db.execute("SELECT conversation FROM sessions WHERE station=?", (station,)).fetchone()
        return row[0] if row else None

    def begin(self, station: str) -> str:
        turn = uuid.uuid4().hex
        self.db.execute("INSERT INTO turns VALUES (?,?,?,?)", (turn, station, "running", time.time()))
        self.db.commit()
        return turn

    def persona_due(self, station: str, idle: float, turns: int, digest: str) -> bool:
        """True when the persona block is outside the brain's live context window or its text changed."""
        row = self.db.execute("SELECT delivered, digest FROM persona WHERE station=?", (station,)).fetchone()
        if row is None or row[1] != digest or time.time() - row[0] > idle:
            return True
        done = self.db.execute("SELECT count(*) FROM turns WHERE station=? AND status='complete' AND started>?",
            (station, row[0])).fetchone()[0]
        return done >= turns

    def persona_sent(self, station: str, digest: str):
        self.db.execute("INSERT INTO persona (station, delivered, digest) VALUES (?,?,?) "
            "ON CONFLICT(station) DO UPDATE SET delivered=excluded.delivered, digest=excluded.digest",
            (station, time.time(), digest))
        self.db.commit()

    def finish(self, turn: str, status: str, station: str, conversation: str | None = None):
        self.db.execute("UPDATE turns SET status=? WHERE id=?", (status, turn))
        if conversation:
            self.db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(station) DO UPDATE SET conversation=excluded.conversation", (station, conversation))
        self.db.commit()

async def run_command(command: list[str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """No shell, no automatic retry; kill children after disconnect or timeout."""
    proc = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        limit=1024 * 1024)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate((json.dumps(payload, ensure_ascii=False) + "\n").encode()), timeout)
        if proc.returncode or len(stdout) > 1024 * 1024:
            raise TurnError("chatgpt_command_failed")
        result = json.loads(stdout)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
            raise TurnError("chatgpt_invalid_response")
        if result.get("conversation_id") is not None and not isinstance(result["conversation_id"], str):
            raise TurnError("chatgpt_invalid_conversation")
        return result
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        raise


class Adapter:
    def __init__(self, env: dict[str, str] | None = None):
        self.env = env if env is not None else dict(os.environ)
        self.allowed = {station.strip() for station in self.env.get("AISIS_ALLOWED_STATIONS", "").split(",") if station.strip()}
        if not self.allowed:
            raise ValueError("AISIS_ALLOWED_STATIONS must contain at least one speaker ID")
        self.skills = voice_skills.Skills(self.env)
        # Grammar route before the brain (intent_route.py): canned lines, reprompts, clock, timers, HA climate/colour.
        # AISIS_INTENT_GRAMMAR=on|shadow|off (shadow logs the decision, never acts); AISIS_INTENT_CATCHALL=off keeps the
        # catch-all «не расслышал» reprompts away. Any failure leaves calc -> skills -> brain as it was.
        self.intent_mode = self.env.get("AISIS_INTENT_GRAMMAR", "on").lower()
        self.intents: intent_route.IntentRouter | None = None
        self.timers: timers.TimerBook | None = None
        self.hacli_session: ClientSession | None = None
        self.intent_ha_url = self.env.get("AISIS_HACLI_URL", "http://127.0.0.1:8795").rstrip("/")
        if self.intent_mode != "off":
            self.intents = self._build_intents()
        # Jev (decision model on the Vercel AI Gateway) for the turns the grammar and the quasar layer could not answer:
        # AISIS_JEV=off|shadow|on (default off) plus VERCEL_JEV_KEY. `on` acts per class (AISIS_JEV_REFUSE, AISIS_JEV_REPROMPT,
        # both off by default) above AISIS_JEV_THRESHOLD (default 0.95). Hard 0.4 s budget, breaker, fail-open (jev.py).
        self.jev_mode = self.env.get("AISIS_JEV", "off").lower()
        self.jev_settings = jev.settings_from_env(self.env)
        self.jev: jev.JevRouteProvider | None = None
        self.jev_session: ClientSession | None = None
        self.jev_task: asyncio.Task | None = None
        self.jev_pending: set[asyncio.Task] = set()
        self.trace_pending: set[asyncio.Task] = set()
        # AISIS_UTTERANCE_TRACE=off: no per-utterance records (utterance_trace.py) in dialogs.sqlite3; the diagnostics of the clip feed and the session page go empty.
        self.trace_enabled = self.env.get("AISIS_UTTERANCE_TRACE", "on").lower() != "off"
        self.chatgpt_timeout: float | None = self._chatgpt_timeout()
        self.store = Store(self.env.get("AISIS_STATE_DB", str(Path.home()/".local/state/aisis/adapter.sqlite3")))
        self.command = json.loads(self.env.get("AISIS_CHATGPT_COMMAND", "[]"))
        if not isinstance(self.command, list) or not all(isinstance(x, str) for x in self.command):
            raise ValueError("AISIS_CHATGPT_COMMAND must be a JSON argv array")
        # The brain exposes no system/instructions field (its payload whitelist
        # is station_id/text/turn_id/conversation_id), so the persona travels inside
        # the first turn text of each dialog session.
        self.persona, self.persona_idle, self.persona_turns = self._persona_settings()
        # Cross-dialog memory (tg#27845): optional, degrades to nothing on any
        # embedder/store failure so voice turns are never blocked.
        self.memory: Memory | None = None
        if self.env.get("AISIS_MEMORY", "1") != "0":
            try:
                # embed= wires the vector channel; Memory degrades to lexical-only on any embedder failure.
                self.memory = Memory(self.env.get("AISIS_MEMORY_DB",
                    str(Path.home()/".local/state/aisis/memory.sqlite3")),
                    embed=self._embed,
                    topk=int(self.env.get("AISIS_MEMORY_TOPK", "5")),
                    max_chars=int(self.env.get("AISIS_MEMORY_MAX_CHARS", "1200")))
            except ValueError:
                self.memory = None
        self.embed_url = self.env.get("AISIS_EMBED_URL", "http://127.0.0.1:31123/embed")
        self.remember_task: asyncio.Task | None = None
        self.upkeep_task: asyncio.Task | None = None
        self.dialogs_db = self.env.get("AISIS_DIALOGS_DB", dialogstore.DB_PATH)
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.tts_limit = asyncio.Semaphore(2)
        self.alice_last: dict[str, Any] = {"ok": None, "status": None, "at": None}
        self.http: ClientSession | None = None
        self.corrector: asr_correct.Corrector | None = None
        self.corrector_http: ClientSession | None = None
        self.warm_task: asyncio.Task | None = None
        self.catalog_cache: tuple[float, voice_vocab.Catalog] | None = None

    def _build_intents(self) -> intent_route.IntentRouter | None:
        grammar = intent_route.load_grammar(self.env.get("AISIS_INTENT_BUNDLE") or None)
        if grammar is None:
            return None
        tts_entity, player = self.env.get("AISIS_TIMER_TTS_ENTITY"), self.env.get("AISIS_TIMER_PLAYER")
        if tts_entity and player:
            self.timers = timers.TimerBook(self.env.get("AISIS_TIMERS_DB", str(Path.home()/".local/state/aisis/timers.json")),
                timers.HaAnnouncer(self._ha_post_status, tts_entity, player))
        ha = ha_actions.HaActions(self._ha_get_json, self._ha_post_json) if self.env.get("AISIS_HA_ACTIONS", "on") != "off" else None
        return intent_route.IntentRouter(grammar, self.skills, self.timers, ha,
                                         catchall=self.env.get("AISIS_INTENT_CATCHALL", "on") != "off")

    def _hacli(self) -> ClientSession:
        """One short-timeout session for ha-cli calls: a stuck Home Assistant must not stall a voice turn for a minute."""
        if self.hacli_session is None:
            self.hacli_session = ClientSession(timeout=ClientTimeout(total=3), trust_env=False)
        return self.hacli_session

    async def _ha_get_json(self, path: str):
        async with self._hacli().get(self.intent_ha_url + path) as result:
            result.raise_for_status()
            return await result.json(content_type=None)

    async def _ha_post_json(self, path: str, body: dict):
        async with self._hacli().post(self.intent_ha_url + path, json=body) as result:
            text = await result.text()
            try:
                return result.status, json.loads(text) if text else None
            except ValueError:
                return result.status, text

    async def _ha_post_status(self, path: str, body: dict) -> int:
        return (await self._ha_post_json(path, body))[0]

    def _chatgpt_timeout(self):
        if not self.env.get("AISIS_PM_URL"):
            return None
        keys = ("AISIS_PM_REPLY_TIMEOUT", "AISIS_PM_REQUEST_TIMEOUT",
            "AISIS_PM_HTTP_TIMEOUT", "AISIS_CHATGPT_TIMEOUT")
        try:
            budgets = [float(self.env.get(key, default)) for key, default in
                zip(keys, ("170", "230", "240", "250"))]
        except ValueError:
            raise ValueError("PM timeout settings must be finite positive numbers") from None
        if not all(math.isfinite(value) and value > 0 for value in budgets):
            raise ValueError("PM timeout settings must be finite positive numbers")
        if not all(left < right for left, right in zip(budgets, budgets[1:])):
            raise ValueError("PM timeout order must be AISIS_PM_REPLY_TIMEOUT < "
                "AISIS_PM_REQUEST_TIMEOUT < AISIS_PM_HTTP_TIMEOUT < AISIS_CHATGPT_TIMEOUT")
        return budgets[-1]

    def _persona_settings(self) -> tuple[str, float, int]:
        try:
            idle = float(self.env.get("AISIS_PERSONA_IDLE", "1800"))
            turns = int(self.env.get("AISIS_PERSONA_TURNS", "6"))
        except ValueError:
            raise ValueError("AISIS_PERSONA_IDLE and AISIS_PERSONA_TURNS must be numbers") from None
        if not math.isfinite(idle) or idle <= 0 or turns < 0:
            raise ValueError("AISIS_PERSONA_IDLE must be positive and AISIS_PERSONA_TURNS non-negative")
        return self.env.get("AISIS_PERSONA", "").strip(), idle, turns

    def _persona_block(self) -> str:
        """Base persona plus the owner's tone preset and global prompt (user_prompt.py); AISIS_USER_PROMPT=0 turns the owner part off."""
        if self.env.get("AISIS_USER_PROMPT", "1") == "0":
            return "\n\n".join(part for part in (self.persona, user_prompt.CAPABILITIES) if part)
        return user_prompt.assemble(self.persona)

    async def start(self, _app):
        self.http = ClientSession(timeout=ClientTimeout(total=60))
        self._asr_corrector()  # opens the keep-alive connection before the first phrase needs it
        if self.timers is not None:
            await self.timers.resume()
        if self.env.get("AISIS_LED_ANIMATION") == "on":  # adb on the speaker: opt-in per process (not in tests/stage)
            led_animation.start_watcher()  # sound-start animation on the clock display, fire-and-forget
        if self.jev_mode in ("on", "shadow") and self.env.get("VERCEL_JEV_KEY"):
            self.jev_session = ClientSession(timeout=ClientTimeout(total=1.0), trust_env=False,
                                             connector=TCPConnector(keepalive_timeout=120))
            self.jev = jev.from_env(self.env, self.jev_session, self.jev_settings)
            self.jev_task = asyncio.create_task(self._keep_jev_warm())

    async def _keep_jev_warm(self):
        """A warm TLS connection makes a Jev call ~0.25 s; a cold one would not fit the 0.4 s budget."""
        base = self.env.get("AISIS_JEV_URL", jev.DEFAULT_URL).split("/v1/")[0]
        while True:
            try:
                async with self.jev_session.get(base, allow_redirects=False):
                    pass
            except (ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(25)

    async def _close_background(self):
        """Stop the background tasks before closing their shared resources."""
        if self.jev_task is not None:
            self.jev_task.cancel()
            await asyncio.gather(self.jev_task, *self.jev_pending, return_exceptions=True)
        if self.jev_session is not None:
            await self.jev_session.close()
        if self.warm_task is not None:
            self.warm_task.cancel()
            await asyncio.gather(self.warm_task, return_exceptions=True)
        if self.upkeep_task and not self.upkeep_task.done():
            self.upkeep_task.cancel()
            await asyncio.gather(self.upkeep_task, return_exceptions=True)
        if self.remember_task and not self.remember_task.done():
            self.remember_task.cancel()
            await asyncio.gather(self.remember_task, return_exceptions=True)

    async def close(self, _app):
        await self.flush_traces()   # the last turn's record is written before the process goes
        await self._close_background()
        if self.timers is not None:
            await self.timers.close()
        if self.hacli_session is not None:
            await self.hacli_session.close()
        if self.http is not None:
            await self.http.close()
        if self.corrector_http is not None:
            await self.corrector_http.close()
        if self.memory is not None:
            self.memory.close()
        flush_usage = getattr(self._claude_module(), "flush_usage", None)
        if callable(flush_usage):
            await flush_usage()
        self.store.db.close()

    def authorize(self, request):
        token = self.env.get("AISIS_ADAPTER_TOKEN")
        if token and not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            raise web.HTTPUnauthorized()

    def fish_key(self):
        key = self.env.get("FISH_API_KEY", "")
        if self.env.get("FISH_API_KEY_FILE"):
            key = Path(self.env["FISH_API_KEY_FILE"]).read_text().strip()
        return key

    async def health(self, request):
        return web.json_response({"service": "aisis", "processor_configured": bool(self.command),
            "tts_configured": bool(self.fish_key()), "automatic_retries": False,
            "tts_voice": tts_settings.load()["tts_voice"], "alice_tts": self.alice_last,
            "intent_grammar": {"mode": self.intent_mode, "loaded": self.intents is not None,
                               "routes": dict(self.intents.stats) if self.intents else {}},
            "jev": {"mode": self.jev_mode, "active": self.jev is not None,
                    "acting": self.jev is not None and self.jev_mode == "on" and self.jev_settings.acts,
                    "threshold": self.jev_settings.threshold, "refuse": self.jev_settings.refuse,
                    "reprompt": self.jev_settings.reprompt, "stats": dict(self.jev.stats) if self.jev else {}}})

    async def _turn_payload(self, station: str, text: str, turn: str):
        """Build one request while keeping stable persona apart from changing context."""
        # The native router receives the real stable persona on every request, before
        # changing dialog/memory context. Older command backends keep their existing
        # periodic text injection until the reviewed router release is pinned.
        block = self._persona_block()
        digest = hashlib.sha256(block.encode()).hexdigest()[:16]
        native = (len(self.command) > 1 and Path(self.command[1]).name == "claude_primary.py"
                  and getattr(self._claude_module(), "SUPPORTS_CACHE_TELEMETRY", False) is True
                  and len(block) <= 16000)
        inject = bool(block) and (native or self.store.persona_due(
            station, self.persona_idle, self.persona_turns, digest))
        with utterance_trace.stage("context"):   # memory retrieval and the recent-dialogs blocks
            memories = await self.memory.retrieve(station, text) if self.memory is not None else []
            # Dialog context (tg#28364): last dialogs of this station, plus matching old ones when the
            # question asks for history. claude_primary stores this whole outgoing text in its
            # per-station history, so both blocks are clipped to a few hundred characters each.
            dialog_blocks = await self._dialog_blocks(station, text)
        prefix = ("[Системная установка: " + block + "]\n" if inject and not native else "") + dialog_blocks \
            + (self.memory.format_block(memories) if memories else "")
        outgoing = prefix + "Пользователь: " + text if prefix else text
        if len(outgoing) > 16000:
            outgoing, inject, memories = text, bool(block) and native, []
        payload = {"station_id": station, "text": outgoing,
                   "conversation_id": self.store.conversation(station), "turn_id": turn}
        if native and block:
            payload["persona"] = block
        return payload, inject, native, digest, len(memories)

    async def turn(self, station: str, text: str):
        if not self.command:
            raise TurnError("chatgpt_not_configured")
        async with self.locks[station]:
            turn = self.store.begin(station)
            payload, inject, native, digest, memory_count = await self._turn_payload(station, text, turn)
            try:
                with utterance_trace.stage("brain"):
                    result = await run_command(self.command, payload,
                        self.chatgpt_timeout if self.chatgpt_timeout is not None else
                        float(self.env.get("AISIS_CHATGPT_TIMEOUT", "250")))
                try:   # diagnostics never cost the answer (the router's metadata is whatever it printed)
                    utterance_trace.note(brain=self._brain_trace(
                        result, inject, memory_count, len(payload["text"]) + len(payload.get("persona", ""))))
                except Exception as exc:
                    utterance_trace.error("brain metadata unreadable: " + type(exc).__name__)
                self.store.finish(turn, "complete", station, result.get("conversation_id"))
                if inject and not native:
                    self.store.persona_sent(station, digest)
                reply = reply_guard.tidy(result["text"].strip())
                if self.memory is not None:
                    self.memory.record_episode(station, text, reply)
                    self._schedule_remember(station, text, reply)
                    self._schedule_upkeep(station)
                return reply
            except asyncio.CancelledError:
                self.store.finish(turn, "interrupted", station)
                raise
            except Exception:
                self.store.finish(turn, "failed", station)
                raise

    @staticmethod
    def _usage_trace(raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None
        fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
                  "cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens")
        return {name: value if type(value := raw.get(name)) is int and value >= 0 else None
                for name in fields}

    def _brain_trace(self, result: dict, persona: bool, memories: int, prompt_chars: int) -> dict:
        """Which brain answered and what rode in the prompt: provider/credential slot/model from the router's own answer, the attempts
        (a failed key and the fallback show here), persona and memory blocks. Never the text or a credential."""
        attempts = [{"slot": a.get("slot"), "result": str(a.get("result"))[:40], "s": a.get("seconds")}
                    for a in (result.get("attempts") if isinstance(result.get("attempts"), list) else []) if isinstance(a, dict)][:4]
        usage = self._usage_trace(result.get("usage"))
        request_id = result.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            request_id = None
        return {k: v for k, v in {"provider": result.get("provider"), "slot": result.get("credential_slot"),
                                  "model": result.get("model") or self.env.get("AISIS_CLAUDE_MODEL"), "attempts": attempts or None,
                                  "persona": persona or None, "memories": memories or None, "prompt_chars": prompt_chars,
                                  "usage": usage, "request_id": request_id}.items()
                if v is not None}

    def _recent_sync(self, station: str, text: str) -> tuple[dict, str]:
        """Settings plus the recent-dialogs block; blocking file/DB reads, run in a thread."""
        settings = dialog_context.load_settings()
        if settings["recent"] <= 0:
            return settings, ""
        return settings, dialog_context.recent_block(self.dialogs_db, station, settings["recent"], exclude_text=text)

    async def _dialog_blocks(self, station: str, text: str) -> str:
        """Recent-dialogs block + history-search block; each is fail-soft and never delays a voice turn long."""
        settings = dict(dialog_context.DEFAULTS)
        recent = ""
        try:
            settings, recent = await asyncio.wait_for(asyncio.to_thread(self._recent_sync, station, text), 1.5)
        except Exception as exc:
            LOG.warning("dialog_context_recent_failed class=%s", type(exc).__name__)
        history = ""
        if settings["history_search"]:
            try:
                history = await asyncio.wait_for(self._history_block(station, text), 3.0)
            except Exception as exc:
                LOG.warning("dialog_context_history_failed class=%s", type(exc).__name__)
        return recent + history

    async def _history_block(self, station: str, text: str) -> str:
        """Old dialogs matching a history-seeking question («что я спрашивал вчера»), '' for ordinary speech."""
        intent = dialog_context.history_intent(text)
        if intent is None:
            return ""
        hits: list[dict] = []
        if self.memory is not None:
            sync_dialogs(self.memory, self.dialogs_db, station, limit=300)
            hits = await self.memory.retrieve_history(station, intent["words"], intent["since"], intent["until"], limit=8)
        if not hits:
            hits = await asyncio.to_thread(self._history_fallback, station, intent)
        return dialog_context.history_block(hits)

    def _history_fallback(self, station: str, intent: dict) -> list[dict]:
        """Plain substring search in dialogs.sqlite3 for when the memory index is off or found nothing."""
        word = max(intent["words"], key=len, default=None)
        conn = dialog_context.open_readonly(self.dialogs_db)
        try:
            found = dialogstore.search(conn, word[:max(4, len(word) - 2)] if word else None,
                                       intent["since"], intent["until"], 8, station)
        finally:
            conn.close()
        hits = []
        for dialog in found:
            indexed = dialog_context.index_text(dialog["heard"], dialog["said"], dialog["reqs"], dialog["err"])
            if indexed is not None:
                hits.append({"ts": dialog["ts"], "text": indexed})
        return hits

    def _schedule_upkeep(self, station: str):
        """Off the turn path: index new dialogs and embed rows lacking vectors; one run at a time."""
        if self.memory is None or (self.upkeep_task and not self.upkeep_task.done()):
            return
        self.upkeep_task = asyncio.create_task(self._upkeep(station))

    async def _upkeep(self, station: str):
        try:
            sync_dialogs(self.memory, self.dialogs_db, station)
            for dialogs in (False, True):
                for _ in range(8):
                    if not await self.memory.backfill_vectors(dialogs=dialogs):
                        break
        except Exception as exc:
            LOG.warning("memory_upkeep_failed class=%s", type(exc).__name__)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Vectors from the local sidecar; caller treats any failure as 'no vectors'."""
        async with ClientSession(timeout=ClientTimeout(total=2)) as session:
            async with session.post(self.embed_url, json={"texts": texts}) as result:
                if result.status != 200:
                    raise RuntimeError("embed_status_" + str(result.status))
                return (await result.json())["vectors"]

    def _schedule_remember(self, station: str, prompt: str, reply: str):
        """One background write-back at a time; a busy slot drops, never queues."""
        if self.remember_task and not self.remember_task.done():
            return
        self.remember_task = asyncio.create_task(self._remember(station, prompt, reply))

    async def _remember(self, station: str, prompt: str, reply: str):
        try:
            if not (MEMORY_HINT_RE.search(prompt) or "запомн" in reply.lower()):
                return
            async with asyncio.timeout(60):
                facts = await self._extract_facts(prompt, reply)
            for fact in facts:
                vector = None
                try:
                    vector = (await self._embed([fact["text"]]))[0]
                except Exception:
                    vector = None
                self.memory.add_fact(station, fact["text"], fact["kind"], vector)
                LOG.info("memory_fact_saved kind=%s", fact["kind"])
        except Exception as exc:
            LOG.warning("memory_writeback_failed class=%s", type(exc).__name__)

    async def _extract_facts(self, prompt: str, reply: str) -> list[dict]:
        """Background extraction through the same Claude keys the brain uses.

        Deliberately not the claude_primary router: its station lock would
        reject live follow-up turns with station_busy while we extract.
        """
        claude_primary = self._claude_module()
        if claude_primary is None:
            return []
        settings = claude_primary.Settings.from_env(dict(os.environ))
        sources = Path(os.environ.get("AISIS_CLAUDE_KEY_SOURCES",
            str(Path.home()/".config/aisis/claude-sources.json")))
        credentials = claude_primary.load_credentials(sources)
        native = getattr(claude_primary, "SUPPORTS_CACHE_TELEMETRY", False) is True
        content = "Владелец: " + prompt + "\nАссистент: " + reply
        body = {"model": settings.model, "max_tokens": 512, "messages": [{"role": "user",
            "content": content if native else EXTRACT_PROMPT + "\n" + content}]}
        if native:
            body["system"] = claude_primary.stable_system(EXTRACT_PROMPT)
        for credential in credentials:
            try:
                raw = await claude_primary.anthropic_send(credential, body, 20.0,
                    **({"purpose": "facts"} if native else {}))
            except Exception:
                continue
            facts = parse_facts(raw)
            if facts or raw.lstrip().startswith("{"):
                return facts
        return []

    def _turn_input(self, data: dict) -> tuple[str, str | None]:
        """Validated (station, text); text is None when the turn carries no speech."""
        station = data.get("metadata", {}).get("stationId")
        text = data.get("text")
        if not isinstance(station, str) or not station or station in {"unknown", "undefined"}:
            raise TurnError("missing_station_id")
        if station not in self.allowed:
            raise TurnError("station_not_allowed")
        if data.get("isExternalEvent"):
            raise TurnError("external_events_disabled")
        if isinstance(text, str) and not text.strip():
            return station, None
        if not isinstance(text, str) or len(text) > 16000:
            raise TurnError("invalid_text")
        return station, text

    def _store_trace(self, trace: utterance_trace.Trace, layer: str, reply: str | None = None, listen: bool = False) -> None:
        """Close the call's trace and store it (own thread: SQLite never blocks the loop; a task, so a cancelled turn still stores).
        Fail-soft: the record is diagnostics, never a reason to fail or delay a turn."""
        if trace.closed:
            return
        trace.note(layer=layer, reply=reply, listen=True if listen else None)
        record = utterance_trace.end(trace)
        LOG.info("utterance_trace via=%s layer=%s intent=%s ms=%s", record["via"], layer, record.get("intent"), record["ms"])
        if not self.trace_enabled:
            return

        async def store():
            try:
                await asyncio.to_thread(dialogstore.add_trace, self.dialogs_db, record)
            except Exception as exc:
                LOG.warning("utterance_trace_store_failed class=%s", type(exc).__name__)

        task = asyncio.create_task(store())
        self.trace_pending.add(task)
        task.add_done_callback(self.trace_pending.discard)

    async def flush_traces(self) -> None:
        """Wait for the trace records still being written (shutdown, tests)."""
        if self.trace_pending:
            await asyncio.wait(self.trace_pending, timeout=3)

    async def _process_turn(self, ws, data):
        listen = False      # the grammar's question («На сколько поставить таймер?») keeps the microphone open
        from_brain = False  # the reply is the brain's own (the router treats a question of its as one a short answer belongs to)
        station = None     # set once _turn_input has accepted the turn: a rejected one still gets its error reply
        trace = None       # opened once there is a phrase to route (utterance_trace.py)
        layer = "none"
        try:
            station, text = self._turn_input(data)
            if text is None:
                if not ws.closed:
                    await ws.send_json({"type": "partialResponse", "data": {"text": NO_SPEECH_REPLY,
                        "directives": [], "finished": True, "shouldListen": False}})
                return
            trace = utterance_trace.start("turn", text, station)
            # The grammar route answers what needs no model: canned lines, reprompts, the clock, timers, climate and
            # colour, then arithmetic and weather (intent_route.py). Instant arithmetic never wakes the slow browser
            # brain; neither do weather and «включи музыку», which the brain cannot do and would refuse.
            reply = None
            if self.intents is not None and self.intent_mode == "shadow":
                decision = self.intents.classify(text)       # decide only: no timer, Home Assistant call or reprompt cooldown
                LOG.info("intent_shadow intent=%s tier=%s", decision.intent if decision else None, decision.tier if decision else None)
                trace.note(shadow=True, **(self.intents.trace_fields(decision) if decision else {}))
            elif self.intents is not None:
                route = await self.intents.route(station, text)
                if route.kind in ("reply", "silent"):
                    reply = route.text or ""
                    listen = route.listen
                    layer = "skill" if route.source == "skill" else "grammar"
            if reply is None:
                reply = calc_evaluate(text)
                if reply is not None:
                    layer = "calc"
            if reply is None:
                with utterance_trace.stage("skills"):
                    reply = await self.skills.answer(text)
                if reply is not None:
                    layer = "skill"
                    trace.note(executor="skills.answer")
            if reply is None:
                jev_route = await self._jev_reply(station, text)
                if jev_route is not None:
                    reply, listen = jev_route.text, jev_route.listen
                    layer = "jev"
                    trace.note(intent=jev_route.intent, asked="reprompt" if listen else None)
            if reply is None:
                reply = await self.turn(station, text)
                from_brain = True
                layer = "brain"
                if listen is False and reply.rstrip().endswith("?"):
                    trace.note(asked="brain")
        except asyncio.CancelledError:
            if trace is not None:
                trace.error("turn cancelled (the speaker's socket closed before the reply)")
                self._store_trace(trace, "error")
            raise
        except Exception as exc:
            LOG.warning("processor_failed class=%s", type(exc).__name__)
            reply = ERROR_REPLY
            layer = "error"
            if trace is not None:
                trace.error(str(exc) if isinstance(exc, TurnError) else type(exc).__name__)
        try:
            if self.intents is not None and station is not None:
                self.intents.note_reply(station, reply, brain=from_brain)
            if not ws.closed:
                await ws.send_json({"type": "partialResponse", "data": {"text": reply,
                    "directives": [], "finished": True, "shouldListen": listen}})
        except BaseException as exc:   # a socket that broke (or a cancel) mid-send: the work was done, the record must say so
            if trace is not None:
                trace.error("reply not delivered: " + type(exc).__name__)
            raise
        finally:
            if trace is not None:
                self._store_trace(trace, layer, reply, listen)

    async def _jev_reply(self, station: str, text: str) -> intent_route.Route | None:
        """What Jev lets the speaker say instead of waiting ~2.4 s for the brain; None leaves the turn to the brain.

        Reached only by turns the grammar, the quasar layer, arithmetic and the skills left unanswered. `shadow` (and `on`
        while neither AISIS_JEV_REFUSE nor AISIS_JEV_REPROMPT is on) decides concurrently with the brain call and only logs
        (turn hash, never the text). `on` waits for Jev (<= 0.4 s) and acts per class: a confident «garbled» gets one reprompt
        a minute per station (the microphone stays open), a confident «command» nobody implements gets the plain refusal; «talk»
        goes to the brain with the tier and effort logged as advice (the brain has no such knob yet). A phrase that mentions an
        emergency (jev.is_emergency) never reaches Jev in `on` and never gets a Jev text.
        """
        if self.jev is None:
            return None
        if self.jev_mode != "on" or not self.jev_settings.acts:
            task = asyncio.create_task(self._jev_shadow(text))
            self.jev_pending.add(task)
            task.add_done_callback(self.jev_pending.discard)
            return None
        if self.intents is not None and self.intents.asked_a_question(station):
            return None   # a short answer to the brain's own question is the brain's to continue, as for the grammar's catch-alls
        turn = jev.turn_hash(text)
        if jev.is_emergency(text):
            LOG.info("jev_route outcome=skipped guard=emergency turn=%s", turn)
            return None
        try:
            with utterance_trace.stage("jev"):
                verdict = await self.jev.assess(jev.RouteTurn(text))
            LOG.info("jev_route %s", jev.describe(verdict, turn))
            route = self._jev_action(station, verdict.decision)
            utterance_trace.note(jev=self._jev_trace(verdict, "on", route is not None))
            return route
        except Exception as err:   # fail open: a bug in the optional layer sends the turn to the brain
            LOG.warning("jev_route_failed class=%s", type(err).__name__)
            utterance_trace.error("jev: " + type(err).__name__)
            return None

    @staticmethod
    def _jev_trace(verdict: jev.Verdict, mode: str, acted: bool) -> dict:
        """Jev's verdict for the trace: what it read the phrase as, the route it advised, why that stood or not, and whether it acted."""
        def answer(name: str) -> str | None:
            got = verdict.answers.get(name)
            return f"{got.choice} {got.confidence:.2f}" if got else None

        candidate = verdict.candidate
        return {k: v for k, v in {"mode": mode, "utterance": answer("utterance"), "tier": answer("tier"), "effort": answer("effort"),
                                  "route": answer("route"), "decision": candidate.reason_code if candidate else None,
                                  "outcome": verdict.reason, "ms": verdict.latency_ms, "acted": acted}.items() if v is not None}

    def _jev_action(self, station: str, decision: jev.RouteDecision | None) -> intent_route.Route | None:
        if decision is None:
            return None
        settings = self.jev_settings
        if decision.reason_code == "jev_garbled" and settings.reprompt and self.intents is not None \
                and self.intents.allow_reprompt(station):
            return intent_route.Route("reply", intent_route.REPROMPT_TEXT, listen=True, intent="jev.garbled", source="jev")
        if decision.reason_code == "jev_command_unsupported" and settings.refuse:
            return intent_route.Route("reply", intent_route.UNSUPPORTED_TEXT, intent="jev.command", source="jev")
        return None

    async def _jev_shadow(self, text: str) -> None:
        try:
            verdict = await self.jev.assess(jev.RouteTurn(text))
            LOG.info("jev_shadow %s", jev.describe(verdict, jev.turn_hash(text), "emergency" if jev.is_emergency(text) else "none"))
            utterance_trace.note(jev=self._jev_trace(verdict, "shadow", False))   # the task copied the turn's context; lands only if the record is still open
        except Exception as err:
            LOG.warning("jev_shadow_failed class=%s", type(err).__name__)

    async def processor(self, request):
        self.authorize(request)
        # The quasar container ships full device/environment state in each
        # process envelope (measured 113KB); 64KB silently killed the socket.
        ws = web.WebSocketResponse(max_msg_size=16*1024*1024, heartbeat=30)
        await ws.prepare(request)
        pending: asyncio.Task | None = None

        async def process(data):
            await self._process_turn(ws, data)

        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    LOG.debug("processor_msg %s", str(message.data)[:120])
                    pending, keep_going = await self._dispatch(ws, pending, message, process)
                    if not keep_going:
                        break
        except Exception as exc:
            LOG.warning("processor_ws_error class=%s", type(exc).__name__)
        finally:
            LOG.info("processor_ws_exit code=%s pending=%s", ws.close_code, bool(pending and not pending.done()))
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        return ws

    async def _dispatch(self, ws, pending, message, process):
        """Handle one socket message; returns (pending, keep_going)."""
        try:
            payload = json.loads(message.data)
            if payload.get("type") == "prepare":
                return pending, True
            if payload.get("type") != "process" or not isinstance(payload.get("data"), dict):
                await ws.close(code=1008, message=b"invalid process envelope")
                return pending, False
            if pending and not pending.done():
                await ws.close(code=1008, message=b"turn already running")
                return pending, False
            return asyncio.create_task(process(payload["data"])), True
        except (ValueError, AttributeError):
            await ws.close(code=1008, message=b"invalid JSON")
            return pending, False
        except Exception as exc:
            LOG.warning("processor_ws_error class=%s", type(exc).__name__)
        finally:
            LOG.info("processor_ws_exit code=%s pending=%s", ws.close_code, bool(pending and not pending.done()))
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        return ws

    async def _read_limited(self, result) -> bytes:
        chunks = []
        size = 0
        async for chunk in result.content.iter_chunked(65536):
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                raise web.HTTPBadGateway(text="speech response too large")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _fish_audio(self, text: str) -> bytes:
        key = self.fish_key()
        if not key:
            raise web.HTTPServiceUnavailable(text="Fish voice credentials not configured")
        # Fish HTTP JSON supports fixed voice reference and wav.
        async with self.http.post(self.env.get("FISH_TTS_URL", "https://api.fish.audio/v1/tts"),
            headers={"Authorization": "Bearer " + key, "model": self.env.get("FISH_MODEL", "s2.1-pro-free")},
            json={"text": text, "reference_id": BRACHO_REFERENCE, "format": "wav",
                "latency": "balanced"}, allow_redirects=False) as result:
            if result.status != 200:
                LOG.warning("fish_tts_failed status=%s", result.status)
                raise web.HTTPBadGateway(text="Fish synthesis failed")
            return await self._read_limited(result)

    async def _alice_audio(self, text: str, settings: dict[str, str]) -> tuple[bytes, str]:
        """The Alice voice: keyless uniproxy by default, SpeechKit when chosen and keyed."""
        engine = tts_settings.alice_engine(settings)
        if engine == "speechkit":
            return await self._yandex_audio(text, settings), engine
        try:
            return await uniproxy_tts.synthesize(self.http, text, settings["alice_voice"],
                settings["yandex_emotion"], url=self.env.get("AISIS_UNIPROXY_URL", uniproxy_tts.UNIPROXY_URL)), "keyless"
        except uniproxy_tts.UniproxyError as error:
            LOG.warning("alice_keyless_failed reason=%s", error)
            raise web.HTTPBadGateway(text="Alice keyless synthesis failed") from None

    async def _yandex_audio(self, text: str, settings: dict[str, str]) -> bytes:
        """Yandex SpeechKit v1 (cloud: the text leaves the host). Opt-in via tts_voice=alice."""
        key = tts_settings.read_key()
        if not key:
            raise web.HTTPServiceUnavailable(text="Yandex SpeechKit key not configured")
        if len(text) > 5000:
            raise web.HTTPBadRequest(text="text exceeds the SpeechKit v1 limit")
        form = {"text": text, "lang": "ru-RU", "voice": settings["yandex_voice"],
            "emotion": settings["yandex_emotion"], "format": "oggopus", "speed": "1.0"}
        if settings["yandex_folder_id"]:
            form["folderId"] = settings["yandex_folder_id"]
        try:
            async with self.http.post(self.env.get("YANDEX_TTS_URL",
                    "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"),
                headers={"Authorization": "Api-Key " + key}, data=form, allow_redirects=False,
                timeout=ClientTimeout(total=20)) as result:
                if result.status != 200:
                    LOG.warning("yandex_tts_failed status=%s", result.status)
                    raise web.HTTPBadGateway(text="Yandex synthesis failed")
                return await self._read_limited(result)
        except (ClientError, asyncio.TimeoutError):
            LOG.warning("yandex_tts_failed status=transport")
            raise web.HTTPBadGateway(text="Yandex synthesis unreachable") from None

    async def announcement_status(self, request):
        self.authorize(request)
        result = await asyncio.to_thread(announcement_cache.status)
        return web.json_response(result)

    async def speech(self, request):
        self.authorize(request)
        body = await request.json()
        text = body.get("input")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise web.HTTPBadRequest(text="invalid speech input")
        if body.get("voice") not in (None, "Брачо", "bracho", BRACHO_REFERENCE):
            raise web.HTTPBadRequest(text="only Брачо is configured")
        if body.get("response_format", "opus") != "opus":
            raise web.HTTPBadRequest(text="only opus is supported")
        if announcement_cache.is_marker(text):
            try:
                ogg = await asyncio.to_thread(announcement_cache.read_clip, text)
            except announcement_cache.CacheNotReady:
                raise web.HTTPServiceUnavailable(text="Remote announcement cache unavailable") from None
            return web.Response(body=ogg, content_type="audio/ogg")

        # Quasar always names Брачо; the real voice is the dashboard setting, read per request.
        # X-AISIS-Voice is an explicit pin (filler generation): no silent fallback for a pin.
        pinned = request.headers.get("X-AISIS-Voice")
        if pinned not in (None, *tts_settings.VOICES):
            raise web.HTTPBadRequest(text="unknown X-AISIS-Voice")
        settings = tts_settings.load()
        wanted = pinned or settings["tts_voice"]
        if wanted == "bracho" and not self.fish_key():
            raise web.HTTPServiceUnavailable(text="Fish voice credentials not configured")
        async with self.tts_limit:
            raw = await self._synthesize_speech(text, settings, wanted, pinned)
            ogg = await self._encode_speech(raw)
            return web.Response(body=ogg, content_type="audio/ogg")

    async def _synthesize_speech(self, text, settings, wanted, pinned):
        raw = None
        if wanted == "alice":
            try:
                raw, engine = await self._alice_audio(text, settings)
                self.alice_last = {"ok": True, "status": 200, "engine": engine, "at": time.time()}
            except web.HTTPException as error:
                self.alice_last = {"ok": False, "status": error.status,
                    "engine": tts_settings.alice_engine(settings), "at": time.time()}
                if pinned:
                    raise
                LOG.warning("alice_voice_fallback_to_bracho status=%s", error.status)
        if raw is None:
            raw = await self._fish_audio(text)
        return raw

    async def _encode_speech(self, raw):
        # Always transcode to Ogg Opus: quasar's downstream decoder expects an Ogg container.
        proc = await asyncio.create_subprocess_exec(self.env.get("FFMPEG", "ffmpeg"),
            "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            ogg, _ = await asyncio.wait_for(proc.communicate(raw), 30)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode or not ogg.startswith(b"OggS") or b"OpusHead" not in ogg[:256]:
            raise web.HTTPBadGateway(text="Opus encoding failed")
        return ogg

    async def laya(self, request):
        """Proxy one JSON request line to the local laya classify-server unix socket.

        Read-only inference endpoint for the semantic-intents shadow layer in the
        quasar container; the private socket itself never crosses the boundary.
        """
        self.authorize(request)
        body = await request.json()
        message = body.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise web.HTTPBadRequest(text="invalid laya message")
        sock_path = Path(self.env.get("LAYA_SOCKET",
            os.path.expanduser("~/.local/state/task-cli/classify/laya-0.23.1.sock")))
        if not sock_path.exists():
            raise web.HTTPServiceUnavailable(text="laya socket unavailable")
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            req = json.dumps({"op": "predict", "message": message,
                "questions": body.get("questions", {})}, ensure_ascii=False)
            writer.write(req.encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 30)
        finally:
            writer.close()
            await writer.wait_closed()
        if not line:
            raise web.HTTPBadGateway(text="laya empty response")
        return web.Response(text=line.decode(), content_type="application/json")

    def _claude_module(self):
        """claude_primary, imported from the brain's directory; None when it is not installed beside the adapter."""
        if len(self.command) < 2:
            return None
        module_dir = Path(self.command[1]).parent
        if not (module_dir / "claude_primary.py").exists():
            return None
        if str(module_dir) not in sys.path:
            sys.path.insert(0, str(module_dir))
        import claude_primary
        return claude_primary

    def _catalog(self) -> voice_vocab.Catalog:
        """Entity names, rooms and library artists; refreshed at most every 5 minutes, stale copy on a failed refresh."""
        now = time.monotonic()
        if self.catalog_cache is None or now - self.catalog_cache[0] > 300:
            fresh = voice_vocab.fetch_catalog(self.env.get("AISIS_HACLI_URL", "http://127.0.0.1:8795"), voice_vocab.library_artists)
            if not fresh.empty() or self.catalog_cache is None:
                self.catalog_cache = (now, fresh)
            else:
                self.catalog_cache = (now, self.catalog_cache[1])
        return self.catalog_cache[1]

    def _asr_corrector(self) -> asr_correct.Corrector | None:
        if self.corrector is None:
            claude = self._claude_module()
            if claude is None:
                return None
            sources = Path(os.environ.get("AISIS_CLAUDE_KEY_SOURCES",
                str(Path.home()/".config/aisis/claude-sources.json")))
            self.corrector_http = ClientSession(trust_env=False, connector=TCPConnector(keepalive_timeout=120))
            self.corrector = asr_correct.Corrector(lambda: claude.load_credentials(sources),
                asr_correct.make_post(self.corrector_http, claude), self._catalog,
                log_db=self.env.get("AISIS_DIALOGS_DB", str(Path.home()/".local/share/aisis/dialogs.sqlite3")))
            self.warm_task = asyncio.create_task(self._keep_corrector_warm(claude.API_URL))
        return self.corrector

    async def _keep_corrector_warm(self, url: str):
        """An idle TLS connection to the API is closed by the far end; a fresh handshake would cost ~200 ms of the budget."""
        while True:
            await asyncio.sleep(25)
            try:
                async with self.corrector_http.get(url, timeout=ClientTimeout(total=3), allow_redirects=False):
                    pass
            except (ClientError, asyncio.TimeoutError):
                pass

    async def asr_correct(self, request):
        """Post-ASR correction of one short phrase for the fast-intent layer (see asr_correct.py).

        Always answers 200 with the raw text and `mode: "ignore"` when it cannot help (disabled, no key, timeout,
        unclear): the caller's behaviour without this endpoint is the fallback, never an error.
        """
        self.authorize(request)
        body = await request.json()
        text, alternatives, recent = body.get("text"), body.get("alternatives", []), body.get("recent", [])
        if not isinstance(text, str) or not text.strip() or len(text) > 400:
            raise web.HTTPBadRequest(text="invalid text")
        if not (isinstance(alternatives, list) and len(alternatives) <= 8 and all(isinstance(a, str) and len(a) <= 400 for a in alternatives)):
            raise web.HTTPBadRequest(text="invalid alternatives")
        if not (isinstance(recent, list) and len(recent) <= 8 and all(isinstance(t, dict) for t in recent)):
            raise web.HTTPBadRequest(text="invalid recent")
        if self.intent_mode == "on" and self.intents is not None and self.intents.classify(text, "pre") is not None:
            # The grammar already knows this phrase (canned line, clock, timer, …): the ~0.85 s Haiku repair would only delay it.
            return web.json_response({"text": text.strip(), "confidence": 0.0, "intent": "grammar", "changed": False,
                                      "mode": "ignore", "status": "grammar", "ms": 0})
        corrector = self._asr_corrector()
        if corrector is None:
            return web.json_response({"text": text.strip(), "confidence": 0.0, "intent": "unclear", "changed": False,
                                      "mode": "ignore", "status": "unavailable", "ms": 0})
        return web.json_response(await corrector.correct(text, alternatives, recent, str(body.get("source", ""))[:40]))

    async def intent_decide(self, request):
        """The quasar overlay's first question for a phrase: does the grammar answer it before the container's own handlers?

        Always 200. `route: "none"` (grammar off, abstain, nothing the adapter may do before the quasar layer, any error)
        tells the caller to carry on with its own code; the overlay (overlays/intent-grammar) gives this call 250 ms.
        """
        self.authorize(request)
        body = await request.json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 400:
            raise web.HTTPBadRequest(text="invalid text")
        if self.intents is None or self.intent_mode == "off":
            return web.json_response(intent_route.NONE.as_json())
        shadow = body.get("shadow") is True
        if not shadow and self.intent_mode != "on":
            return web.json_response(intent_route.NONE.as_json())
        trace = utterance_trace.start("hook-shadow" if shadow else "hook", text)   # the quasar hook knows no station
        layer, reply, listen = "none", None, False
        try:
            if shadow:
                route = self.intents.shadow(text, "pre")   # classify only: executes nothing
                verdict = self.intents.classify(text, "pre")
                if verdict is not None:
                    trace.note(**self.intents.trace_fields(verdict))
            else:
                route = await self.intents.route("quasar", text, phase="pre", via_quasar=True)
                if route.kind != "none":
                    layer = "skill" if route.source == "skill" else "grammar"
                    reply, listen = route.text, route.listen
                    trace.note(route=route.kind, volume=route.volume)
            return web.json_response(route.as_json())
        except Exception as exc:
            trace.error(type(exc).__name__)
            layer = "error"
            raise
        finally:
            self._store_trace(trace, layer, reply, listen)

    async def music_play(self, request):
        """Voice «включи <артист/трек/альбом>»: search Music Assistant and play on the speaker.

        Called by the quasar container's fast-intent layer; the MA token stays on the
        host (music_airplay.py reads ~/.config/aisis/ma.env). `err: "no_match"`
        tells the caller to hand the utterance to the brain instead.
        """
        self.authorize(request)
        body = await request.json()
        query = body.get("query")
        media_type = body.get("media_type")
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise web.HTTPBadRequest(text="invalid music query")
        if media_type is not None and not isinstance(media_type, str):
            raise web.HTTPBadRequest(text="invalid media_type")
        import music_airplay
        # `strict`: the caller guessed this was a bare band name («6 pistols»): artists only, close match only.
        strict = body.get("strict") is True
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: music_airplay.music_play(
                query.strip(), "artist" if strict else media_type, early_ack=True,
                min_score=music_airplay.BARE_NAME_MIN_SCORE if strict else music_airplay.MATCH_THRESHOLD))
        return web.json_response(result)

    async def music_control(self, request):
        """Voice media keys «стоп/пауза/продолжи/дальше/назад» for the speaker's AirPlay playback.

        `ok: false, err: "idle"` tells the caller nothing of ours is playing, so it may try Home
        Assistant's players instead; the speaker never answers a media key with a spoken phrase.
        """
        self.authorize(request)
        body = await request.json()
        action = body.get("action")
        import music_airplay
        if action not in music_airplay.MEDIA_ACTIONS:
            raise web.HTTPBadRequest(text="invalid media action")
        result = await asyncio.get_running_loop().run_in_executor(
            None, lambda: music_airplay.media_control(action))
        return web.json_response(result)

    def app(self):
        app = web.Application(client_max_size=65536)
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/processor", self.processor)
        app.router.add_post("/v1/audio/speech", self.speech)
        app.router.add_get("/v1/remote-announcements/status", self.announcement_status)
        app.router.add_post("/laya", self.laya)
        app.router.add_post("/asr/correct", self.asr_correct)
        app.router.add_post("/intent/decide", self.intent_decide)
        app.router.add_post("/music/play", self.music_play)
        app.router.add_post("/music/control", self.music_control)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.close)
        return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(Adapter().app(), host="127.0.0.1", port=int(os.environ.get("AISIS_ADAPTER_PORT", "31117")), access_log=None)
