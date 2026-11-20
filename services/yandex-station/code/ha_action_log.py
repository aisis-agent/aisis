"""The ha-cli action journal: what every Home Assistant service call did, was it verified, and how Home Assistant was doing.

One JSON line per record, written to the service journal (stderr, prefix `ha_action `) and to a size-bounded file
(`~/.local/share/aisis/ha-actions.jsonl` + `.1`, `.2`) that the clip feed and the session pages read by time. Stdlib only: ha-cli
runs on the system Python.

Records (all carry `v`, `kind`, `ts`):
  action     id, service (`light.turn_off`), entities, data (scalar arguments), expect, before/after (`state_text` per entity),
             call {status|error, ms, late}, verify {result ok|failed|unknown|skipped, reason, attempts, ms, after, wrong},
             reply {status, why}: what ha-cli answered its caller and the reason code of that answer, ms (total)
  ha_status  from, to (up|slow|down), error, down_s: Home Assistant's availability as ha-cli observed it
  replay     id (of the queued action), status/error, ms: a call that waited for Home Assistant's return and was sent again
  ha_response id, call {status|error, ms}: how a call that outlived its caller's answer finished
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import Any, TextIO

VERSION = 1
MAX_LINE = 1800              # a journal line stays well inside journald's line limit
MAX_BYTES = 1_000_000
BACKUPS = 2
_SECRET = re.compile(r"token|secret|password|passwd|authorization|api[_-]?key|bearer", re.IGNORECASE)
_VALUE_CHARS = 40

ERROR_RU = {"timeout": "таймаут", "refused": "соединение отклонено", "reset": "соединение сброшено", "dns": "имя не найдено",
            "unreachable": "сеть недоступна", "ssl": "ошибка TLS", "starting": "HA отвечает «запускается»", "other": "сбой связи"}
STATE_RU = {"on": "вкл", "off": "выкл", "open": "открыто", "closed": "закрыто", "playing": "играет", "paused": "пауза",
            "unavailable": "недоступно", "unknown": "неизвестно", "locked": "закрыт", "unlocked": "открыт"}


def clean_data(data: Any) -> dict[str, Any]:
    """Scalar arguments of a service call for the journal: no secrets, no long texts (a tts message is speech, not telemetry)."""
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "entity_id" or _SECRET.search(str(key)):
            continue
        if isinstance(value, str):
            out[str(key)] = value if len(value) <= _VALUE_CHARS else f"({len(value)} chars)"
        elif isinstance(value, (int, float, bool)):
            out[str(key)] = value
    return out


def _bounded(record: dict[str, Any]) -> str:
    """The record as one line of at most MAX_LINE characters: the bulky fields go first, `cut: true` says it happened."""
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(line) <= MAX_LINE:
        return line
    record = dict(record, cut=True)
    for field in ("data", "before"):
        record.pop(field, None)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(line) <= MAX_LINE:
            return line
    verify = dict(record.get("verify") or {})
    for field in ("after", "wrong"):
        verify.pop(field, None)
    record["verify"] = verify
    record["entities"] = list(record.get("entities") or [])[:5]
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return line if len(line) <= MAX_LINE else line[:MAX_LINE - 1] + "…"


class Journal:
    def __init__(self, path: str | os.PathLike, max_bytes: int = MAX_BYTES, backups: int = BACKUPS, stream: TextIO | None = None) -> None:
        self.path = str(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        """Never raises: a journal that cannot be written must not fail a voice command."""
        record = {**record, "data": clean_data(record["data"])} if "data" in record else record
        line = _bounded({"v": VERSION, **record})
        with self._lock:
            try:
                print("ha_action " + line, file=self.stream, flush=True)
            except (OSError, ValueError):
                pass
            try:
                self._append(line + "\n")
            except OSError:
                pass

    def _append(self, text: str) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        if size and size + len(text.encode("utf-8")) > self.max_bytes:
            self._rotate()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)

    def _rotate(self) -> None:
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else f"{self.path}.{index - 1}"
            if os.path.exists(source):
                os.replace(source, f"{self.path}.{index}")


def read(path: str | os.PathLike, since: float, until: float, backups: int = BACKUPS) -> list[dict[str, Any]]:
    """The records with `since <= ts <= until`, oldest first; a missing file or a half-written last line is skipped."""
    found: list[dict[str, Any]] = []
    for name in [f"{path}.{index}" for index in range(backups, 0, -1)] + [str(path)]:
        try:
            with open(name, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    ts = record.get("ts") if isinstance(record, dict) else None
                    if isinstance(ts, (int, float)) and since <= ts <= until:
                        found.append(record)
        except OSError:
            continue
    return sorted(found, key=lambda record: record["ts"])


def _seconds(ms: Any) -> str:
    return f"{ms / 1000:.1f} с" if isinstance(ms, (int, float)) else "—"


def _state_ru(text: str) -> str:
    head, _, rest = str(text).partition(" ")
    return (STATE_RU.get(head, head) + (" " + rest if rest else "")).strip()


def _entities_ru(states: dict[str, str]) -> str:
    return ", ".join(f"{entity} {_state_ru(text)}" for entity, text in list(states.items())[:3])


def _ha_note(ha: str | None, ha_ms: Any = None) -> str:
    """How Home Assistant's own answer to the call went, only when it was not a plain 2xx. `ha`: an HTTP status or an error kind."""
    if not ha or ha == "pending":
        return ""
    if ha.isdigit():
        return "" if 200 <= int(ha) < 300 else f"ответ HA: HTTP {ha}"
    return f"ответ HA: {ERROR_RU.get(ha, ha)}" + (f" {_seconds(ha_ms)}" if ha_ms is not None else "")


def verdict_line(verify: dict[str, Any], *, ha: str | None = None, ha_ms: Any = None, down: str | None = None, queued: bool = False) -> str:
    """What ha-cli found on the device after a call, in Russian: «проверено: light.x выкл за 0.8 с», «не подтверждено: …».
    `ha`: Home Assistant's own outcome (HTTP status or error kind), `down`: the evidence of an outage on a queued call."""
    result, reason = verify.get("result"), verify.get("reason")
    note = _ha_note(ha, ha_ms)
    if result == "ok":
        tail = f"проверено: {_entities_ru(verify.get('after') or {}) or 'все цели'} за {_seconds(verify.get('ms'))}"
        if reason == "already_in_state":
            note = "уже было в этом состоянии"
    elif result == "failed":
        if reason == "device_unavailable":
            tail = "не подтверждено: " + ", ".join(f"{entity} недоступно в HA" for entity in list(verify.get("wrong") or {})[:3])
        else:
            tail = "не подтверждено: " + (_entities_ru(verify.get("wrong") or {}) or "состояние не совпало")
        if not note and ha and ha.isdigit():
            note = f"HA ответил {ha}"
    elif result == "unknown" and reason == "ha_down_observed":
        tail = f"HA недоступен ({ERROR_RU.get(down or ha or '', down or ha or 'нет связи')})"
        note = "команда в очереди до возвращения HA" if queued else ""
    elif result == "unknown":
        tail = f"не проверено: HA не ответил за {_seconds(verify.get('ms'))}"
    elif reason == "rejected":
        tail = f"HA отклонил команду (HTTP {ha})"
        note = ""
    else:
        tail = "проверка невозможна (по состоянию устройства не видно)"
        note = note if ha and not ha.isdigit() else ""
    return tail + (f" ({note})" if note else "")


def action_line(record: dict[str, Any]) -> str:
    """One Russian line of a journal record for the clip post and the session page."""
    kind = record.get("kind")
    if kind == "ha_status":
        error = record.get("error")
        return f"HA: {record.get('from')} → {record.get('to')}" + (f" ({ERROR_RU.get(error, error)})" if error else "")
    if kind == "replay":
        return f"HA повтор из очереди {record.get('service', '')} → {record.get('call', {}).get('status') or ERROR_RU.get(record.get('call', {}).get('error'), record.get('call', {}).get('error'))}"
    if kind != "action":
        return ""
    entities = list(record.get("entities") or [])
    shown = " ".join(entities[:3]) + (f" +{len(entities) - 3}" if len(entities) > 3 else "")
    data = " ".join(f"{k}={v}" for k, v in (record.get("data") or {}).items())
    head = " ".join(part for part in (f"HA {record.get('service', '?')}", shown, data) if part)
    call = record.get("call") or {}
    ha = call.get("error") or (str(call["status"]) if "status" in call else None)
    tail = verdict_line(record.get("verify") or {}, ha=ha, ha_ms=call.get("ms"), down=call.get("error"),
                        queued=(record.get("reply") or {}).get("status") == 202)
    return f"{head} → {tail}"


# Why the reply was what it was (the reason codes of ha_actions.py `reply_why` and of the ha-cli journal `reply.why`).
WHY_RU = {
    "ha_verified": "состояние устройства проверено и совпало",
    "verified_state": "состояние устройства проверено и совпало",
    "ha_acknowledged": "HA принял команду, состояние не проверялось",
    "passed_through": "ответ HA как есть: по состоянию проверить нечего",
    "ha_not_confirmed": "HA принял, но устройство не выполнило команду",
    "state_not_as_expected": "HA принял, но устройство не выполнило команду",
    "ha_device_unavailable": "HA видит устройство недоступным",
    "ha_unverified": "ответ HA получить не удалось, состояние не прочитано",
    "unverified_no_answer": "ответ HA получить не удалось, состояние не прочитано",
    "ha_unreachable": "до ha-cli не достучались",
    "ha_down_observed": "HA недоступен: наблюдалось, команда в очереди",
    "queued_ha_down": "HA недоступен: наблюдалось, команда в очереди",
    "ha_queued_unverified": "ha-cli поставил команду в очередь без подтверждения причины",
    "ha_rejected": "HA отклонил команду",
    "ha_error": "HA ответил ошибкой",
    "ha_read_failed": "не удалось прочитать данные HA до команды",
    "ha_exception": "сбой при обращении к HA",
    "not_executed": "команда не отправлялась",
}

JOURNAL_PATH = os.environ.get("AISIS_HACLI_JOURNAL", os.path.join(os.path.expanduser("~"), ".local", "share", "aisis", "ha-actions.jsonl"))


def record_lines(records: list[dict[str, Any]], known: set[str] | None = None) -> list[tuple[int, str]]:
    """Ranked caption lines for journal records: the actions of the quasar layer (which the adapter's trace does not hold), Home
    Assistant's availability changes, replays. `known`: journal ids the adapter's trace already shows."""
    out: list[tuple[int, str]] = []
    for record in records:
        kind = record.get("kind")
        if kind == "action" and record.get("id") not in (known or ()):
            out.append((2, "🔧 " + action_line(record)))
            why = (record.get("reply") or {}).get("why")
            if why and why not in ("verified_state", "passed_through"):
                out.append((3, "💬 причина ответа ha-cli: " + WHY_RU.get(why, why)))
        elif kind == "ha_status":
            out.append((1, "⚠ " + action_line(record)))
        elif kind == "replay":
            out.append((3, "🔧 " + action_line(record)))
    return out


def window_lines(since: float, until: float, known: set[str] | None = None, path: str | os.PathLike | None = None) -> list[tuple[int, str]]:
    """`record_lines` of what ha-cli journalled in the time window."""
    return record_lines(read(path or JOURNAL_PATH, since, until), known)
