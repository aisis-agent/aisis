"""Russian wording of what happened to a Home Assistant call: the verdict line of a traced call and the reason of a reply.

Pure text, no I/O. `trace_view` and `utterance_trace` show these in the utterance trace (clip post, session page); the codes come from
`ha_call` (verify {result, reason, ms, after, wrong, unchecked}, `ha` = HTTP status or transport error kind) and `ha_actions` (`reply_why`).
"""
from __future__ import annotations

from typing import Any

ERROR_RU = {"timeout": "таймаут", "refused": "соединение отклонено", "reset": "соединение сброшено", "dns": "имя не найдено",
            "unreachable": "сеть недоступна", "ssl": "ошибка TLS", "starting": "HA отвечает «запускается»", "other": "сбой связи"}
STATE_RU = {"on": "вкл", "off": "выкл", "open": "открыто", "closed": "закрыто", "playing": "играет", "paused": "пауза",
            "unavailable": "недоступно", "unknown": "неизвестно", "locked": "закрыт", "unlocked": "открыт"}


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


def verdict_line(verify: dict[str, Any], *, ha: str | None = None, ha_ms: Any = None, down: str | None = None) -> str:
    """What was found on the device after a call, in Russian: «проверено: light.x выкл за 0.8 с», «не подтверждено: …».
    `ha`: Home Assistant's own outcome (HTTP status or error kind), `down`: the transport error that shows an outage."""
    result, reason = verify.get("result"), verify.get("reason")
    note = _ha_note(ha, ha_ms)
    if result == "ok":
        tail = f"проверено: {_entities_ru(verify.get('after') or {}) or 'все цели'} за {_seconds(verify.get('ms'))}"
        if verify.get("unchecked"):
            tail = "проверено частично: " + tail.removeprefix("проверено: ") + f"; не проверено: {', '.join(verify['unchecked'])}"
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
        tail = f"Home Assistant был недоступен, команда не отправлена ({ERROR_RU.get(down or ha or '', down or ha or 'нет связи')})"
        note = ""
    elif result == "unknown" and reason in ("ha_down_in_flight", "ha_down_after_answer"):
        tail = "не проверено: HA пропал, пока команда выполнялась"
    elif result == "unknown" and reason == "not_yet_confirmed":
        tail = f"пока не подтверждено: {_entities_ru(verify.get('after') or {}) or 'состояние'}, устройство ещё меняется"
    elif result == "unknown":
        tail = f"не проверено: HA не ответил за {_seconds(verify.get('ms'))}"
    elif reason == "rejected":
        tail = f"HA отклонил команду (HTTP {ha})"
        note = ""
    else:
        tail = "проверка невозможна (по состоянию устройства не видно)"
        note = note if ha and not ha.isdigit() else ""
    return tail + (f" ({note})" if note else "")


# Why the reply was what it was (the reason codes of ha_actions.py `reply_why`).
WHY_RU = {
    "ha_verified": "состояние устройства проверено и совпало",
    "ha_partially_verified": "состояние проверено только частично: часть запроса по состоянию не видна",
    "verified_state": "состояние устройства проверено и совпало",
    "ha_acknowledged": "HA принял команду, состояние не проверялось",
    "passed_through": "ответ HA как есть: по состоянию проверить нечего",
    "ha_not_confirmed": "HA принял, но устройство не выполнило команду",
    "state_not_as_expected": "HA принял, но устройство не выполнило команду",
    "ha_device_unavailable": "HA видит устройство недоступным",
    "ha_unverified": "ответ HA получить не удалось, состояние не прочитано",
    "unverified_no_answer": "ответ HA получить не удалось, состояние не прочитано",
    "ha_unreachable": "до Home Assistant не достучались",
    "ha_down_observed": "Home Assistant был недоступен, команда не отправлена",
    "ha_rejected": "HA отклонил команду",
    "ha_error": "HA ответил ошибкой",
    "ha_read_failed": "не удалось прочитать данные HA до команды",
    "ha_exception": "сбой при обращении к HA",
    "not_executed": "команда не отправлялась",
}
