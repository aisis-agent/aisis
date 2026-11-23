#!/usr/bin/env python3
"""AISIS JSON command: three Claude API credentials, guarded ChatGPT web last.

No tools/actions are supplied to Claude. No browser controls are changed.
Credential values and provider response bodies never appear in diagnostics.
Default storage contains hashes/status only. Bounded private context is opt-in.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import sys
import time
from typing import Any

API_URL = 'https://api.anthropic.com/v1/messages'
MODEL = 'claude-sonnet-5-5'
KEY_VARIABLES = frozenset({'CLAUDE_API_TOKEN', 'CLAUDE_API_TOKEN2', 'CLAUDE_API_KEY', 'ANTHROPIC_API_KEY'})
# sk-ant-api...: API keys; sk-ant-usr-...: Console user keys observed in the bot
# deployments (95 URL-safe characters). OAuth sk-ant-oat tokens never qualify.
KEY_RE = re.compile(r'^sk-ant-(?:api|usr-)[0-9A-Za-z_-]{20,256}$')
PM_URL_RE = re.compile(r'^https://chatgpt\.com/c/[A-Za-z0-9_-]+$')
SYSTEM = ('Ты голосовой помощник АИСИС. Отвечай на языке пользователя, кратко и '
          'понятно для прослушивания. Не используй Markdown без необходимости. '
          'Не утверждай, что выполнил действие, для которого у тебя нет инструмента.')
MAX_RESPONSE_BYTES = 1024 * 1024


class RouterError(Exception):
    """Only fixed, non-secret error codes cross the CLI boundary."""


class Failover(RouterError):
    def __init__(self, code: str, cooldown_seconds: float):
        super().__init__(code)
        self.cooldown_seconds = cooldown_seconds


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Credential:
    label: str
    secret: str = field(repr=False)

    @property
    def fingerprint(self) -> str:
        return digest(self.secret)


def unique_credentials(credentials: list[Credential]) -> list[Credential]:
    selected, seen = [], set()
    for credential in credentials:
        if credential.fingerprint not in seen:
            selected.append(credential)
            seen.add(credential.fingerprint)
    return selected[:3]


def load_credentials(path: Path) -> list[Credential]:
    """Read only explicit dotenv variables. Never source shell or interpolate."""
    try:
        sources = json.loads(path.read_text())
    except (OSError, ValueError):
        raise RouterError('credential_sources_unreadable') from None
    if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
        raise RouterError('invalid_credential_sources')
    credentials = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'label', 'path', 'variables'}:
            raise RouterError('invalid_credential_source')
        label, filename, variables = source['label'], source['path'], source['variables']
        if (not isinstance(label, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}', label)
                or not isinstance(filename, str) or not Path(filename).is_absolute()
                or not isinstance(variables, list) or not variables
                or any(v not in KEY_VARIABLES for v in variables)):
            raise RouterError('invalid_credential_source')
        try:
            with Path(filename).open() as source_file:
                info = os.fstat(source_file.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077):
                    raise RouterError('credential_source_not_private')
                content = source_file.read(1024 * 1024 + 1)
        except OSError:
            continue  # A removed source must not disable the remaining reserves.
        if len(content) > 1024 * 1024:
            raise RouterError('credential_source_too_large')
        values = {}
        for line in content.splitlines():
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$', line)
            if not match or match[1] not in variables:
                continue
            # Dotenv uses the final assignment, including an empty/revoked value.
            values.pop(match[1], None)
            try:
                parts = shlex.split(match[2], comments=True, posix=True)
            except ValueError:
                continue
            if len(parts) == 1 and KEY_RE.fullmatch(parts[0]):
                values[match[1]] = parts[0]
        for variable in variables:
            if variable in values:
                credentials.append(Credential(label, values[variable]))
                break
    return unique_credentials(credentials)


@dataclass(frozen=True)
class Settings:
    allowed: frozenset[str]
    model: str = MODEL
    attempt_timeout: float = 20.0
    fallback_timeout: float = 240.0
    total_timeout: float = 310.0
    max_tokens: int = 768
    history_pairs: int = 0
    history_ttl: float = 86400.0

    def validate(self):
        if not self.allowed or self.model != MODEL:
            raise RouterError('allowed_stations_or_model_profile_invalid')
        numbers = (self.attempt_timeout, self.fallback_timeout, self.total_timeout, self.history_ttl)
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x > 0 for x in numbers):
            raise RouterError('invalid_timeouts')
        if self.attempt_timeout > 60 or self.fallback_timeout > 300:
            raise RouterError('invalid_timeouts')
        if self.total_timeout < 3 * self.attempt_timeout + self.fallback_timeout + 5:
            raise RouterError('insufficient_total_timeout')
        if type(self.max_tokens) is not int or not 32 <= self.max_tokens <= 4096:
            raise RouterError('invalid_max_tokens')
        if type(self.history_pairs) is not int or not 0 <= self.history_pairs <= 16:
            raise RouterError('invalid_history_limit')

    @classmethod
    def from_env(cls, env):
        try:
            settings = cls(
                allowed=frozenset(s.strip() for s in env.get('AISIS_ALLOWED_STATIONS', '').split(',') if s.strip()),
                model=env.get('AISIS_CLAUDE_MODEL', MODEL),
                attempt_timeout=float(env.get('AISIS_CLAUDE_TIMEOUT', '20')),
                fallback_timeout=float(env.get('AISIS_PM_HTTP_TIMEOUT', '240')),
                total_timeout=float(env.get('AISIS_ROUTER_TIMEOUT', '310')),
                max_tokens=int(env.get('AISIS_CLAUDE_MAX_TOKENS', '768')),
                history_pairs=int(env.get('AISIS_CLAUDE_HISTORY_PAIRS', '0')),
                history_ttl=float(env.get('AISIS_CLAUDE_HISTORY_TTL', '86400')))
            settings.validate()
            outer = float(env.get('AISIS_CHATGPT_TIMEOUT', '320'))
            if not math.isfinite(outer) or outer <= settings.total_timeout:
                raise RouterError('adapter_timeout_must_exceed_router_timeout')
            return settings
        except (ValueError, TypeError):
            raise RouterError('invalid_router_settings') from None


def retry_after(headers: dict, default: float = 60.0) -> float:
    raw = next((v for k, v in headers.items() if k.lower() == 'retry-after'), None)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        try:
            value = parsedate_to_datetime(str(raw)).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return default
    # Provider (pinned, TLS-verified origin) says how long to wait: never retry early.
    return max(default, value) if math.isfinite(value) else default


def parse_response(status: int, headers: dict, body: Any) -> str:
    error = body.get('error', {}) if isinstance(body, dict) else {}
    message = error.get('message', '') if isinstance(error, dict) else ''
    billing = isinstance(message, str) and any(part in message.lower() for part in (
        'credit balance is too low', 'you have reached your specified api usage limits',
        'you have reached your specified workspace api usage limits'))
    if status == 429:
        raise Failover('rate_limit', retry_after(headers, 3600 if billing else 60))
    if status in (401, 403):
        raise Failover('credential_unavailable', 300)
    if status == 402 or status == 400 and billing:
        raise Failover('billing_unavailable', 3600)
    if status == 408 or 500 <= status <= 599:
        raise Failover('server_unavailable', retry_after(headers, 15))
    if status != 200:
        raise RouterError('claude_request_rejected')
    if not isinstance(body, dict) or not isinstance(body.get('content'), list):
        raise RouterError('invalid_answer')
    stop = body.get('stop_reason')
    if stop not in ('end_turn', 'stop_sequence', 'refusal'):
        raise RouterError('incomplete_answer')
    blocks = body['content']
    if any(not isinstance(b, dict) or b.get('type') != 'text' for b in blocks):
        raise RouterError('unexpected_content_block')
    if any(not isinstance(b.get('text'), str) for b in blocks):
        raise RouterError('invalid_answer')
    text = '\n'.join(b['text'] for b in blocks).strip()
    if stop == 'refusal' and not text:
        return 'Не могу помочь с этим запросом.'
    if not text or len(text) > 16000:
        raise RouterError('invalid_answer')
    return text


async def anthropic_send(credential: Credential, body: dict, timeout: float) -> str:
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False) as session:
            async with session.post(API_URL, headers={'x-api-key': credential.secret,
                    'anthropic-version': '2023-06-01', 'content-type': 'application/json'},
                    json=body, allow_redirects=False) as response:
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise RouterError('provider_response_too_large')
                try:
                    result = json.loads(data)
                except (ValueError, UnicodeDecodeError):
                    result = None
                return parse_response(response.status, dict(response.headers), result)
    except (asyncio.TimeoutError, aiohttp.ClientError):
        # Text-only inference can be duplicated at the provider after a timeout;
        # no tool actions are enabled and only one final answer is emitted.
        raise Failover('transport_unavailable', 30) from None


class State:
    """Separate state: never opens or rewrites the PM/Guardian/adapter databases."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=3)
        self.db.executescript('''
          PRAGMA synchronous=FULL;
          PRAGMA secure_delete=ON;
          CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY, station TEXT NOT NULL, digest TEXT NOT NULL,
            status TEXT NOT NULL, provider TEXT, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS cooldowns (keyhash TEXT PRIMARY KEY, until_time REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS context (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, station TEXT NOT NULL,
            prompt TEXT NOT NULL, answer TEXT NOT NULL, created REAL NOT NULL);
        ''')
    def close(self):
        self.db.close()
    def count_turns(self):
        return self.db.execute('SELECT count(*) FROM turns').fetchone()[0]
    def turn_status(self, turn):
        row = self.db.execute('SELECT status FROM turns WHERE id=?', (turn,)).fetchone()
        return row[0] if row else None
    def reserve(self, payload, now):
        try:
            with self.db:
                self.db.execute('INSERT INTO turns VALUES(?,?,?,?,?,?)',
                    (payload['turn_id'], payload['station_id'], digest(payload['text']), 'reserved', None, now))
        except sqlite3.IntegrityError:
            raise RouterError('duplicate_turn') from None
    def mark(self, turn, status, provider=None):
        with self.db:
            self.db.execute('UPDATE turns SET status=?,provider=? WHERE id=?', (status, provider, turn))
    def cooldown(self, keyhash, until):
        with self.db:
            self.db.execute('INSERT INTO cooldowns VALUES(?,?) ON CONFLICT(keyhash) DO UPDATE SET until_time=MAX(until_time,excluded.until_time)', (keyhash, until))
    def cooling(self, keyhash, now):
        row = self.db.execute('SELECT until_time FROM cooldowns WHERE keyhash=?', (keyhash,)).fetchone()
        return bool(row and row[0] > now)
    def prune(self, station, pairs, cutoff):
        with self.db:
            self.db.execute('DELETE FROM context WHERE created<?', (cutoff,))
            if not pairs:
                self.db.execute('DELETE FROM context WHERE station=?', (station,))
    def context(self, station, pairs, now):
        if not pairs:
            return []
        rows = self.db.execute('SELECT prompt,answer FROM context WHERE station=? ORDER BY seq DESC LIMIT ?', (station,pairs)).fetchall()
        messages = []
        # Bound bytes as well as turns: at most 24,000 characters of prior text.
        size = 0
        for prompt, answer in rows:
            size += len(prompt) + len(answer)
            if size > 24000:
                break
            messages.insert(0, {'role':'assistant', 'content':answer})
            messages.insert(0, {'role':'user', 'content':prompt})
        return messages
    def complete(self, payload, text, provider, pairs, now):
        with self.db:
            self.db.execute('UPDATE turns SET status=?,provider=? WHERE id=?', ('complete',provider,payload['turn_id']))
            if pairs:
                station=payload['station_id']
                self.db.execute('INSERT INTO context(station,prompt,answer,created) VALUES(?,?,?,?)', (station,payload['text'],text,now))
                self.db.execute('DELETE FROM context WHERE station=? AND seq NOT IN (SELECT seq FROM context WHERE station=? ORDER BY seq DESC LIMIT ?)', (station,station,pairs))


async def pm_fallback(payload: dict) -> dict:
    # Existing client validates its loopback origin and uses the unchanged PM
    # service, which enforces Guardian controls and conversation ownership.
    from chatgpt_pm import run
    return await run(payload)


class Router:
    def __init__(self, settings: Settings, credentials, state: State, *,
                 send=anthropic_send, fallback=pm_fallback, clock=time.time):
        settings.validate()
        self.settings, self.credentials, self.state = settings, unique_credentials(credentials), state
        self.send, self.fallback, self.clock = send, fallback, clock
        self.locks = defaultdict(asyncio.Lock)

    def validate(self, payload):
        if not isinstance(payload, dict) or set(payload)-{'station_id','text','turn_id','conversation_id'}:
            raise RouterError('invalid_payload')
        for name, maximum in [('station_id',256),('turn_id',128),('text',16000)]:
            value=payload.get(name)
            if not isinstance(value,str) or not value.strip() or len(value)>maximum:
                raise RouterError('invalid_payload')
        if payload['station_id'] not in self.settings.allowed:
            raise RouterError('station_not_allowed')
        conversation=payload.get('conversation_id')
        if conversation is not None and (not isinstance(conversation,str) or not PM_URL_RE.fullmatch(conversation)):
            raise RouterError('invalid_conversation')

    async def submit(self, payload):
        self.validate(payload)
        lock=self.locks[payload['station_id']]
        if lock.locked():
            raise RouterError('station_busy')
        async with lock:
            self.state.reserve(payload,self.clock())
            try:
                async with asyncio.timeout(self.settings.total_timeout):
                    return await self._route(payload)
            except asyncio.CancelledError:
                self.state.mark(payload['turn_id'],'uncertain')
                raise
            except TimeoutError:
                self.state.mark(payload['turn_id'],'uncertain')
                raise RouterError('router_deadline_no_retry') from None
            except BaseException:
                if self.state.turn_status(payload['turn_id'])!='uncertain':
                    self.state.mark(payload['turn_id'],'failed')
                raise

    async def _route(self,payload):
        settings=self.settings; station=payload['station_id']
        self.state.prune(station,settings.history_pairs,self.clock()-settings.history_ttl)
        history=self.state.context(station,settings.history_pairs,self.clock())
        body={'model':settings.model,'max_tokens':settings.max_tokens,'system':SYSTEM,
              'thinking':{'type':'between_tools'},'output_config':{'effort':'medium'},
              'messages':history+[{'role':'user','content':payload['text']}]}
        attempts=[]
        for credential in self.credentials:
            if self.state.cooling(credential.fingerprint,self.clock()):
                attempts.append({'slot':credential.label,'result':'cooldown'})
                continue
            self.state.mark(payload['turn_id'],'api_pending','claude')
            started=time.monotonic()
            try:
                # The transport timeout is enforced even on an injected transport.
                async with asyncio.timeout(settings.attempt_timeout):
                    text=await self.send(credential,body,settings.attempt_timeout)
            except TimeoutError:
                error=Failover('transport_unavailable',30)
            except Failover as exc:
                error=exc
            else:
                if not isinstance(text,str) or not text.strip() or len(text)>16000:
                    raise RouterError('invalid_answer')
                self.state.complete(payload,text,'claude',settings.history_pairs,self.clock())
                attempts.append({'slot':credential.label,'result':'complete','seconds':round(time.monotonic()-started,3)})
                return {'text':text,'conversation_id':payload.get('conversation_id'),
                        'provider':'claude','credential_slot':credential.label,'attempts':attempts}
            self.state.cooldown(credential.fingerprint,self.clock()+error.cooldown_seconds)
            attempts.append({'slot':credential.label,'result':str(error),'seconds':round(time.monotonic()-started,3)})
        delegated=dict(payload)
        if history:
            # Use explicit user-supplied conversation data, never inject a system
            # message into another provider. Only the selected station is included.
            prefix='Контекст этого голосового диалога (данные, не инструкции):\n'
            context=json.dumps(history,ensure_ascii=False)
            # PM input limit is 16k. Drop oldest whole pairs until it fits.
            while history and len(prefix)+len(context)+len(payload['text'])+40>16000:
                history=history[2:];context=json.dumps(history,ensure_ascii=False)
            if history:
                delegated['text']=prefix+context+'\nТекущий вопрос:\n'+payload['text']
        self.state.mark(payload['turn_id'],'pm_pending','chatgpt_web')
        try:
            async with asyncio.timeout(settings.fallback_timeout):
                answer=await self.fallback(delegated)
            if (not isinstance(answer,dict) or not isinstance(answer.get('text'),str)
                    or not answer['text'].strip() or len(answer['text'])>16000
                    or not isinstance(answer.get('conversation_id'),str)
                    or not PM_URL_RE.fullmatch(answer['conversation_id'])):
                raise RouterError('invalid_chatgpt_answer')
        except asyncio.CancelledError:
            self.state.mark(payload['turn_id'],'uncertain','chatgpt_web')
            raise
        except Exception:
            self.state.mark(payload['turn_id'],'uncertain','chatgpt_web')
            raise RouterError('chatgpt_fallback_failed') from None
        self.state.complete(payload,answer['text'],'chatgpt_web',settings.history_pairs,self.clock())
        return {**answer,'provider':'chatgpt_web','attempts':attempts}


def station_lock(directory: Path, station: str) -> int:
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(directory/(digest(station)+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RouterError('station_busy') from None
    return fd


async def probe_keys(settings, credentials):
    """Explicit, paid, one-request-per-key probe. Never invokes PM or tools."""
    results=[]
    body={'model':settings.model,'max_tokens':64,'thinking':{'type':'between_tools'},
          'output_config':{'effort':'medium'},'messages':[{'role':'user','content':'Ответь только: ОК.'}]}
    for credential in credentials:
        start=time.monotonic()
        try:
            answer=await anthropic_send(credential,body,settings.attempt_timeout)
            results.append({'slot':credential.label,'ok':True,'answer':answer,'seconds':round(time.monotonic()-start,3)})
        except RouterError as error:
            results.append({'slot':credential.label,'ok':False,'error':str(error),'seconds':round(time.monotonic()-start,3)})
    return {'model':settings.model,'distinct_keys':len(credentials),'probes':results,
            'chatgpt_called':False}


def main(argv=None):
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--check-config',action='store_true')
    group.add_argument('--probe-keys',action='store_true')
    args=parser.parse_args(argv)
    state=None; lock=None
    try:
        settings=Settings.from_env(os.environ)
        path=Path(os.environ.get('AISIS_CLAUDE_KEY_SOURCES',str(Path.home()/'.config/aisis-yandex/claude-sources.json')))
        credentials=load_credentials(path)
        if args.check_config:
            result={'primary':'claude','model':settings.model,'distinct_keys':len(credentials),
                    'slots':[k.label for k in credentials],'fallback':'chatgpt_web',
                    'history_pairs':settings.history_pairs,'ready':len(credentials)==3,
                    'live_calls':0}
        elif args.probe_keys:
            result=asyncio.run(probe_keys(settings,credentials))
        else:
            data=sys.stdin.buffer.read(131073)
            if len(data)>131072:
                raise RouterError('input_too_large')
            payload=json.loads(data)
            # Validate before creating state or a lock for an untrusted station.
            dummy=Router(settings,credentials,None)
            dummy.validate(payload)
            root=Path(os.environ.get('AISIS_CLAUDE_STATE_DIR',str(Path.home()/'.local/state/aisis-yandex/claude')))
            lock=station_lock(root/'locks',payload['station_id'])
            state=State(root/'router.sqlite3')
            result=asyncio.run(Router(settings,credentials,state).submit(payload))
        print(json.dumps(result,ensure_ascii=False))
        if args.check_config:
            return 0 if result['ready'] else 2
        if args.probe_keys:
            return 0 if result['distinct_keys']==3 and all(p['ok'] for p in result['probes']) else 2
        return 0
    except Exception as exc:
        code=str(exc) if isinstance(exc,RouterError) and re.fullmatch(r'[a-z_]{1,64}',str(exc)) else 'router_failed'
        print(json.dumps({'error':code,'automatic_replay':False}),file=sys.stderr)
        return 1
    finally:
        if state is not None:state.close()
        if lock is not None:os.close(lock)


if __name__=='__main__':
    raise SystemExit(main())
