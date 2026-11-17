"""How an utterance trace (utterance_trace.py) reads to a human: which record belongs to a clip, and its lines in Russian.

Shared by the Telegram clip feed (compact lines) and the dashboard session page (the same lines plus the details).
"""
from __future__ import annotations

import re
from typing import Any

LAYERS = {"grammar": "грамматика", "skill": "навык", "calc": "калькулятор", "jev": "Jev", "brain": "ИИ",
          "none": "не обработано адаптером", "error": "ошибка"}
VIA = {"turn": "ход колонки", "hook": "хук квазара", "hook-shadow": "хук квазара (теневой)"}
ASKED = {"clarify": "уточнение", "reprompt": "переспрос", "brain": "вопрос ИИ"}
WAKE_STATUS = "activation"
MATCH_AFTER_S = 20.0    # a clip's call starts within this long after the archived clip
MATCH_BEFORE_S = 3.0


def norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е")).split())


def traces_of_clip(clip: dict, traces: list[dict], until: float | None = None) -> list[dict]:
    """The adapter's calls that processed this clip: only the ones whose phrase is the clip's text. A call that merely started near
    it is a neighbour's (consecutive unrelated commands), so an uncertain match stays empty; a clip the gateway rejected before
    routing (noise, a hallucination, an activation) has no call of its own."""
    end = clip["ts"] + MATCH_AFTER_S if until is None else min(until, clip["ts"] + 3 * MATCH_AFTER_S)
    window = [t for t in traces if clip["ts"] - MATCH_BEFORE_S <= t["ts"] <= end]
    wanted = norm(clip.get("transcript"))
    if clip.get("status") not in (None, "sent") or not wanted:
        return []
    return [t for t in window if norm(t.get("text")) == wanted]


def ms_text(ms: Any) -> str:
    return f"{ms} мс" if isinstance(ms, (int, float)) else "—"


def slot_text(slots: dict | None) -> str:
    return "; ".join(f"{k}={v}" for k, v in (slots or {}).items())


def route_line(trace: dict) -> str:
    """One line: who answered, the intent with its band, confidence and the arguments."""
    layer = LAYERS.get(trace.get("layer", "none"), trace.get("layer", "?"))
    parts = [layer]
    if trace.get("intent"):
        bits = [trace["intent"]]
        if trace.get("tier"):
            bits.append(trace["tier"])
        if trace.get("priority") is not None:
            bits.append(f"p{trace['priority']}")
        if trace.get("conf") is not None:
            bits.append(f"{trace['conf']:.2f}" + (" fuzzy" if trace.get("fuzzy") else ""))
        parts.append(" · ".join(str(b) for b in bits))
    elif trace.get("executor"):
        parts.append(str(trace["executor"]))
    brain = trace.get("brain")
    if brain:
        parts.append(" ".join(str(x) for x in (brain.get("provider"), brain.get("model"), brain.get("slot")) if x))
    return " → ".join(p for p in parts if p)


def call_line(call: dict) -> str:
    kind = call.get("kind", "?")
    if kind == "ha":
        status = call.get("status") or call.get("error") or "—"
        extra = f" {call['data']}" if call.get("data") else ""
        queued = " (в очереди)" if call.get("queued") else ""
        return f"HA {call.get('service', '?')} {call.get('entity') or ''}{extra} → {status}{queued} · {ms_text(call.get('ms'))}".replace("  ", " ")
    if kind == "weather-cli":
        return f"weather-cli {call.get('argv', '')} → {'ок' if call.get('ok') else 'сбой'} · {ms_text(call.get('ms'))}"
    if kind == "music":
        what = call.get("name") or call.get("action") or call.get("error") or ""
        return f"музыка {what} → {'ок' if call.get('ok') else 'сбой'} · {ms_text(call.get('ms'))}"
    if kind == "timer":
        args = ", ".join(f"{k}={v}" for k, v in (call.get("args") or {}).items())
        return f"{call.get('executor', 'timer')} {args} → {'ок' if call.get('ok') else 'сбой'} · {ms_text(call.get('ms'))}"
    return f"{kind} · {ms_text(call.get('ms'))}"


def jev_line(jev: dict) -> str:
    bits = [jev.get("utterance"), jev.get("route") and f"маршрут {jev['route']}", jev.get("decision"), jev.get("outcome")]
    acted = "действовал" if jev.get("acted") else ("только совет" if jev.get("mode") == "on" else "тень")
    return f"Jev {jev.get('mode', '?')}: " + " · ".join(str(b) for b in bits if b) + f" · {acted} · {ms_text(jev.get('ms'))}"


def brain_line(brain: dict) -> str:
    attempts = "; ".join(f"{a.get('slot')} {a.get('result')} {a.get('s')} с" for a in brain.get("attempts") or [])
    head = " ".join(str(x) for x in (brain.get("provider"), brain.get("model"), brain.get("slot") and f"слот {brain['slot']}") if x)
    extras = [x for x in (brain.get("persona") and "персона", brain.get("memories") and f"память {brain['memories']}",
                          brain.get("prompt_chars") and f"промпт {brain['prompt_chars']} симв.") if x]
    return "ИИ " + " · ".join(x for x in (head, attempts, ", ".join(extras)) if x)


def stages_line(stages: dict | None) -> str:
    names = {"grammar": "грамматика", "route": "маршрут", "skills": "навыки", "jev": "Jev", "context": "контекст", "brain": "ИИ"}
    parts = [f"{names.get(k, k)} {v}" for k, v in (stages or {}).items() if k != "total"]
    if stages and "total" in stages:
        parts.append(f"всего {stages['total']}")
    return "⏱ " + " · ".join(parts) + " мс" if parts else ""


def ranked_lines(trace: dict, compact: bool = False) -> list[tuple[int, str]]:
    """(rank, line) of one record in reading order; a lower rank survives a tight space (a Telegram caption) longer.
    `compact` drops what the session page keeps (the explicit argument list)."""
    out: list[tuple[int, str]] = [(1, route_line(trace))]
    if trace.get("asked"):
        out.append((1, f"❓ {ASKED.get(trace['asked'], trace['asked'])}: «{trace.get('reply', '')}»"
                       + (" (микрофон открыт)" if trace.get("listen") else "")))
    if trace.get("joined"):
        out.append((1, f"↪ склеено с уточнением: «{trace['joined']}»"))
    if trace.get("slots"):
        out.append((2, "слоты: " + slot_text(trace["slots"])))
    if trace.get("args") and not compact:
        out.append((2, "аргументы: " + slot_text(trace["args"])))
    if trace.get("declined"):
        out.append((1, f"↷ грамматика не выполнила: {trace['declined']}"))
    if trace.get("room"):
        out.append((3, f"🏠 комната: {trace['room']}"))
    if trace.get("outcome"):
        out.append((3, f"итог: {trace['outcome']}"))
    for call in trace.get("calls") or []:
        out.append((2, "🔧 " + call_line(call)))
    if trace.get("jev"):
        out.append((3, "🧠 " + jev_line(trace["jev"])))
    if trace.get("brain"):
        out.append((3, "🤖 " + brain_line(trace["brain"])))
    stages = stages_line(trace.get("stages_ms"))
    if stages:
        out.append((4, stages))
    for error in trace.get("errors") or []:
        out.append((1, f"⚠ {error}"))
    return out


def lines(trace: dict, compact: bool = False) -> list[str]:
    return [text for _, text in ranked_lines(trace, compact)]
