"""How an utterance trace (utterance_trace.py) reads to a human: which record belongs to a clip, and its lines in Russian.

Shared by the Telegram clip feed (compact lines) and the dashboard session page (the same lines plus the details).
"""
from __future__ import annotations

import math
import re
from typing import Any

import ha_action_log

LAYERS = {"grammar": "грамматика", "skill": "навык", "calc": "калькулятор", "jev": "Jev", "brain": "ИИ", "error": "ошибка"}
VIA = {"turn": "ход колонки", "hook": "хук квазара", "hook-shadow": "хук квазара (теневой)"}
ASKED = {"clarify": "уточнение", "reprompt": "переспрос", "brain": "вопрос ИИ"}
SPOTTER_RAN_IT = "команду выполнила сама колонка, серверу пришла только запись"   # a spotter clip («громче», «пауза»): no text turn, no adapter call
DECISIONS = {"ask": "уточнение", "ask-choice": "выбор из двух", "relisten": "переспрос", "none": "без вопроса"}
TRIGGERS = {"garbled": "фразу не разобрать", "contradiction": "подозрение на противоположную команду",
            "confusable-disagreement": "второе мнение слышит другую команду", "low-margin": "варианты почти равноценны"}
NO_QUESTION = {"agree": "оба распознавания согласны", "vetoed": "экстренная фраза, не вмешиваюсь", "long": "фраза слишком длинная",
               "no-evidence": "второго мнения нет", "below-floor": "альтернатива слишком слаба",
               "same-action": "варианты значат одно и то же", "no-action": "команды в варианте нет",
               "no-wording": "для этого сомнения нет формулировки вопроса"}
DIALOG_STATES = {"awaiting_confirm": "ждём «да» или «нет»", "awaiting_choice": "ждём выбор", "awaiting_repeat": "ждём повтор фразы",
                 "closed": "диалог закрыт"}
OUTCOMES = {"accepted": "принято", "denied": "отклонено", "kept": "оставлено как услышано", "cancelled": "отменено",
            "timeout": "ответа не было", "fallthrough": "ответ ушёл обычным путём", "gave_up": "сдались после повтора", "repeat": "вопрос повторён"}
CUES = {"noop": "команда ничего не меняет", "contradiction": "противоречит состоянию устройства", "state": "состояние устройства"}
ENGINES = {"gigaam-v3-ctc": "GigaAM", "faster-whisper": "Whisper", "yandex-spotter": "споттер колонки"}
EVIDENCE_SOURCES = {"spotter": "споттер колонки", "primary": "основное распознавание"}
WAKE_STATUS = "activation"
MATCH_AFTER_S = 20.0    # a clip's call starts within this long after the archived clip
MATCH_BEFORE_S = 3.0
EVIDENCE_LEAD_S = 1.0    # the quasar logs a clip's fast intent no earlier than this before the archived clip: tighter than MATCH_BEFORE_S so a repeat 2 s later is the next clip's
EVIDENCE_OPEN_S = 45.0   # the last clip of a dialog may be answered this long after it: the one deadline of the Telegram caption and the session page


def norm(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower().replace("ё", "е")).split())


def clip_window(clip: dict, until: float | None = None) -> tuple[float, float]:
    """The epoch span in which the processing of the clip happens: from just before it to `until` (the next clip of its dialog), capped."""
    end = clip["ts"] + MATCH_AFTER_S if until is None else min(until, clip["ts"] + 3 * MATCH_AFTER_S)
    return clip["ts"] - MATCH_BEFORE_S, end


def evidence_of_clip(clip: dict, items: list[dict] | None, key: str, until: float | None = None, first: bool = True) -> list[dict]:
    """The dialog's fast-intent `actions` (key "at") or brain turns (key "ts") that happened within the clip's window: a phrase said twice in
    one dialog has two answers, and the earlier clip must not read the later one. An item with no time is no evidence.
    Windows of neighbours never overlap: a clip that follows another (`first` False) starts at its own time, since an item just before it
    is as likely the previous clip's late answer; the last clip runs EVIDENCE_OPEN_S, not the call-start MATCH_AFTER_S."""
    lo = clip["ts"] - EVIDENCE_LEAD_S if first else clip["ts"]
    hi = min(until, clip["ts"] + 3 * MATCH_AFTER_S) if until is not None else clip["ts"] + EVIDENCE_OPEN_S
    return [i for i in items or [] if isinstance(i.get(key), (int, float)) and lo <= i[key] < hi]


def traces_of_clip(clip: dict, traces: list[dict], until: float | None = None) -> list[dict]:
    """The adapter's calls that processed this clip: only the ones whose phrase is the clip's text. A call that merely started near
    it is a neighbour's (consecutive unrelated commands), so an uncertain match stays empty; a clip the gateway rejected before
    routing (noise, a hallucination, an activation) has no call of its own."""
    lo, end = clip_window(clip, until)
    window = [t for t in traces if lo <= t["ts"] <= end]
    wanted = norm(clip.get("transcript"))
    if clip.get("status") not in (None, "sent") or not wanted:
        return []
    return [t for t in window if norm(t.get("text")) == wanted]


def ms_text(ms: Any) -> str:
    return f"{ms} мс" if isinstance(ms, (int, float)) else "—"


def slot_text(slots: dict | None) -> str:
    return "; ".join(f"{k}={v}" for k, v in (slots or {}).items())


def intent_bits(trace: dict) -> str:
    """The grammar's decision as one fragment: intent · band · priority · confidence."""
    bits = [trace["intent"]]
    if trace.get("tier"):
        bits.append(trace["tier"])
    if trace.get("priority") is not None:
        bits.append(f"p{trace['priority']}")
    if trace.get("conf") is not None:
        bits.append(f"{trace['conf']:.2f}" + (" fuzzy" if trace.get("fuzzy") else ""))
    return " · ".join(str(b) for b in bits)


# What the adapter's own reason for not running a grammar decision (intent_route._why_declined) says, in Russian.
DECLINED = [(re.compile(r"^reprompt cooldown"), "переспрос не чаще раза в минуту"),
            (re.compile(r"^compound:"), "составная команда: часть исполняет квазар"),
            (re.compile(r"^fuzzy guess"), "нечёткое совпадение: нужно подтверждение «да»"),
            (re.compile(r"^volume change is run by the quasar layer"), "громкость меняет сам квазар"),
            (re.compile(r"^volume arguments break the contract"), "параметры громкости не подходят адаптеру"),
            (re.compile(r"^executor (\S+) belongs to the quasar layer"), r"действие \1 исполняет квазар"),
            (re.compile(r"^timers are not configured"), "таймеры не настроены"),
            (re.compile(r"^Home Assistant actions are off"), "действия Home Assistant выключены"),
            (re.compile(r"^nothing to repeat"), "повторять нечего"),
            (re.compile(r"^executor (\S+) gave no answer"), r"действие \1 не дало ответа")]
HANDED_ON = "дальше решал квазар (быстрый интент или ИИ)"


def declined_text(reason: str) -> str:
    for pattern, text in DECLINED:
        found = pattern.match(reason)
        if found:
            return found.expand(text)
    return reason


def handoff_line(trace: dict, answered: str | None = None) -> str:
    """A call the adapter did not answer. For the quasar's grammar hook that is not a failure: the grammar did not know the phrase (or
    knew it and the adapter does not run it) and the quasar's own layers carried on. `answered` names who finally did, when known."""
    if trace.get("via") == "turn":
        return "адаптер не дал ответа"
    if trace.get("intent"):
        known = f"грамматика узнала фразу ({intent_bits(trace)})"
    elif trace.get("errors"):   # intent_route fails open: its crash is recorded and the call goes on as «not decided»
        known = "адаптер не смог разобрать фразу (ошибка ниже)"
    else:
        known = "грамматика не знает эту фразу"
    if trace.get("via") == "hook-shadow":
        return known + "; пробный прогон, ничего не выполнялось"
    if trace.get("intent"):
        known += ", но не выполнила её"
    return f"{known} → " + (f"ответ: {answered}" if answered else HANDED_ON)


def route_line(trace: dict, answered: str | None = None) -> str:
    """One line: who answered, the intent with its band, confidence and the arguments."""
    if trace.get("layer", "none") == "none":
        return handoff_line(trace, answered)
    parts = [LAYERS.get(trace["layer"], trace["layer"])]
    if trace.get("intent"):
        parts.append(intent_bits(trace))
    elif trace.get("executor"):
        parts.append(str(trace["executor"]))
    brain = trace.get("brain")
    if brain:
        parts.append(" ".join(str(x) for x in (brain.get("provider"), brain.get("model"), brain.get("slot")) if x))
    return " → ".join(p for p in parts if p)


_SPOKEN_MARKS = re.compile(r"\s*\((?:executed, )?silent\)|\s*\(listen=(?:true|false)\)", re.IGNORECASE)


def quasar_answer(wanted: str, actions: list[dict] | None) -> str | None:
    """What the quasar's own fast intent said to the (normalized) phrase, "" when it answered without words, None when it did not answer
    (no request line, or its result is null: the quasar's brain went on, as clipfeed.describe_result reads it too)."""
    for action in actions or []:
        if action.get("kind") == "запрос" and norm(action.get("text")) == wanted:
            result = str(action.get("res") or "").strip()
            if result in ("", "null"):
                continue
            return _SPOKEN_MARKS.sub("", result).strip()[:80]
    return None


def answered_by(text: str | None, traces: list[dict], actions: list[dict] | None, brain: list[dict] | None) -> str | None:
    """Who finally answered a phrase the grammar hook handed on: the adapter's own record of the turn if there is one, else the quasar's
    fast intent, else the brain's turn in the dialog; None when nothing in the data says."""
    wanted = norm(text)
    for trace in traces:
        if trace.get("via") == "turn" and norm(trace.get("text")) == wanted and trace.get("layer") in LAYERS:
            return f"{LAYERS[trace['layer']]} ({VIA['turn']})"
    spoken = quasar_answer(wanted, actions)
    if spoken is not None:
        return "быстрый интент квазара" + (f" → «{spoken}»" if spoken else "")
    if any(norm(turn.get("prompt")) == wanted for turn in brain or []):
        return "ИИ"
    return None


def untraced_reason(clip: dict, dialog: dict | None) -> str:
    """Why a clip has no adapter record, as far as the clip and its dialog tell. `dialog`: its fast-intent `actions` and `brain` turns;
    None when no dialog holds the clip."""
    base = "записи вызова адаптера нет"
    wanted = norm(clip.get("transcript"))
    if clip.get("status") not in (None, "sent"):
        return f"{base}: запись отброшена до разбора (не речь или галлюцинация), адаптер её не видел"
    if not wanted:
        return f"{base}: текста нет, разбирать нечего"
    if dialog is None:
        return f"{base} и диалога квазара нет: возможно, фраза не дошла до разбора"
    spoken = quasar_answer(wanted, dialog.get("actions"))
    if spoken is not None:
        return f"{base}: фразу обработал сам квазар (быстрый интент" + (f" → «{spoken}»" if spoken else "") + ")"
    if any(norm(turn.get("prompt")) == wanted for turn in dialog.get("brain") or []):
        return f"{base}, но в диалоге есть ответ ИИ"
    return f"{base}, и ответа квазара в диалоге нет: возможно, фраза не дошла до разбора (или квазар разбирал исправленный текст)"


def call_line(call: dict) -> str:
    kind = call.get("kind", "?")
    if kind == "ha":
        status = call.get("status") or call.get("error") or "—"
        extra = f" {call['data']}" if call.get("data") else ""
        queued = " (в очереди)" if call.get("queued") else ""
        head = f"HA {call.get('service', '?')} {call.get('entity') or ''}{extra}"
        if call.get("verify"):   # ha-cli read the device state after the call: that verdict, not Home Assistant's HTTP answer, is the result
            verdict = ha_action_log.verdict_line(call["verify"], ha=call.get("ha"), down=call.get("down"), queued=bool(call.get("queued")))
            return f"{head} → {verdict} · {ms_text(call.get('ms'))}".replace("  ", " ")
        return f"{head} → {status}{queued} · {ms_text(call.get('ms'))}".replace("  ", " ")
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


def ranked_lines(trace: dict, compact: bool = False, answered: str | None = None) -> list[tuple[int, str]]:
    """(rank, line) of one record in reading order; a lower rank survives a tight space (a Telegram caption) longer.
    `compact` drops what the session page keeps (the explicit argument list). `answered`: who finally answered a phrase the adapter
    handed on (answered_by)."""
    out: list[tuple[int, str]] = [(1, route_line(trace, answered))]
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
        out.append((1, f"↷ грамматика не выполнила: {declined_text(trace['declined'])}"))
    if trace.get("room"):
        out.append((3, f"🏠 комната: {trace['room']}"))
    if trace.get("outcome"):
        out.append((3, f"итог: {trace['outcome']}"))
    for call in trace.get("calls") or []:
        out.append((2, "🔧 " + call_line(call)))
    if trace.get("reply_why"):
        out.append((2, "💬 причина ответа: " + ha_action_log.WHY_RU.get(trace["reply_why"], trace["reply_why"])))
    if trace.get("jev"):
        out.append((3, "🧠 " + jev_line(trace["jev"])))
    if trace.get("brain"):
        out.append((3, "🤖 " + brain_line(trace["brain"])))
    stages = stages_line(trace.get("stages_ms"))
    if stages:
        out.append((4, stages))
    out.extend(correction_lines(trace.get("correction")))
    for error in trace.get("errors") or []:
        out.append((1, f"⚠ {error}"))
    return out


def lines(trace: dict, compact: bool = False, answered: str | None = None) -> list[str]:
    return [text for _, text in ranked_lines(trace, compact, answered)]


# ── the correction engine's record (#182) ────────────────────────────────────
# Everything below reads data that other components wrote: every field is optional and any shape may arrive, so nothing raises.
def mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def mappings(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def word(table: dict[str, str], key: Any) -> str:
    return table.get(key, str(key)) if isinstance(key, str) else ""


def percent(item: dict) -> int | None:
    for key, scale in (("percent", 1), ("posterior", 100)):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value * scale):
            return round(value * scale)
    return None


def _variant(item: dict) -> str:
    share = percent(item)
    return f"«{item.get('text', '')}»" + (f" {share}%" if share is not None else "")


def alternatives_lines(items: list[dict]) -> list[tuple[int, str]]:
    """The two leading alternatives (rank 1) and the rest (rank 2), with their percentages: the share among the LISTED readings.
    Percentages exist for diagnostics only; the speaker never says one."""
    items = [i for i in items if isinstance(i.get("text"), str) and i["text"]]
    out = []
    if items:
        out.append((1, "варианты: " + " · ".join(_variant(i) for i in items[:2])))
    if items[2:]:
        out.append((2, "ещё варианты: " + " · ".join(_variant(i) for i in items[2:])))
    return out


def outcome_text(outcome: str) -> str:
    return f"выбрано «{outcome[len('chose:'):]}»" if outcome.startswith("chose:") else word(OUTCOMES, outcome)


def dialog_is_open(outcome: Any) -> bool:
    """No outcome yet, or a denial that turned the dialog into a repeat request. Any other outcome ended it, and the state it keeps
    (the one it last had) says nothing."""
    return not (isinstance(outcome, str) and outcome) or outcome == "denied"


def dialog_line(dialog: dict, shadow: bool = False) -> str:
    """The question, the owner's answer and how it ended, or that the question is still open. A shadow question was only decided, never asked."""
    ask, answer, outcome, state = (dialog.get(k) for k in ("ask", "answer", "outcome", "state"))
    if not isinstance(ask, str) or not ask:
        return ""
    if shadow:
        return f"❓ вопрос не задан (теневой режим): «{ask}»"
    line = f"❓ «{ask}»"
    if isinstance(answer, str) and answer:
        line += f" → «{answer}»"
    if isinstance(outcome, str) and outcome:
        line += ": " + outcome_text(outcome)
    if isinstance(state, str) and state and dialog_is_open(outcome):
        line += " · " + word(DIALOG_STATES, state)
    return line


def _decision_line(c: dict) -> list[tuple[int, str]]:
    decision, trigger, reason = c.get("decision"), c.get("trigger"), c.get("reason")
    if decision == "none":
        return [(3, "🔎 без вопроса" + (": " + word(NO_QUESTION, reason) if reason else ""))]
    if isinstance(decision, str) and decision:
        return [(1, f"🔎 {word(DECISIONS, decision)}" + (f": {word(TRIGGERS, trigger)}" if trigger else ""))]
    return []


def _cue_lines(c: dict) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for cue in mappings(c.get("cue")):
        kind, supports = cue.get("kind"), cue.get("supports")
        if isinstance(kind, str) and kind:
            out.append((2, f"улика: {word(CUES, kind)}" + (f" (в пользу «{supports}»)" if isinstance(supports, str) and supports else "")))
    return out


def correction_lines(correction: Any) -> list[tuple[int, str]]:
    """Ranked lines of `trace.correction`: decision and trigger, alternatives, the question with the answer, cues, the second opinion's time."""
    c = mapping(correction)
    out = _decision_line(c)
    out.extend(alternatives_lines(mappings(c.get("nbest"))))
    line = dialog_line(mapping(c.get("dialog")), shadow=c.get("shadow") is True)
    if line:
        out.append((1, line))
    out.extend(_cue_lines(c))
    out.extend(ms_line(c.get("nbest_ms")))
    return out


def ms_line(ms: Any) -> list[tuple[int, str]]:
    return [(4, f"⏱ второе мнение: {ms} мс")] if isinstance(ms, (int, float)) and not isinstance(ms, bool) else []


def engine_label(engine: Any) -> str:
    return word(ENGINES, engine) or "?"


def _own_record(correction: dict) -> bool:
    """The correction was decided on this very utterance. A later turn of the dialog (the answer, or the expiry of the question) carries the
    question's `heard` and `nbest` again, with the answer or the timeout beside them: they describe another clip."""
    dialog = mapping(correction.get("dialog"))
    return not (isinstance(dialog.get("answer"), str) and dialog["answer"]) and dialog.get("outcome") != "timeout"


def _traces_list_alternatives(traces: list[dict], primary: Any) -> bool:
    """A trace's correction lists the alternatives of THIS clip when it was decided on the clip's own primary reading."""
    wanted = norm(primary) if isinstance(primary, str) else ""
    corrections = [mapping(t.get("correction")) for t in traces if isinstance(t, dict)]
    return bool(wanted) and any(
        isinstance(c.get("heard"), str) and norm(c["heard"]) == wanted and mappings(c.get("nbest")) and _own_record(c) for c in corrections)


def _primary_line(evidence: Any) -> list[tuple[int, str]]:
    primary = mapping(evidence).get("primary_text")
    if not (isinstance(primary, str) and primary):
        return []
    return [(2, f"основное распознавание ({word(EVIDENCE_SOURCES, mapping(evidence).get('source')) or '?'}): «{primary}»")]


def evidence_lines(evidence: Any, traces: list[dict]) -> list[tuple[int, str]]:
    """Lines for the second opinion stored for a clip (`voiceclips.hypotheses_of`), for a clip whose trace carries no alternatives itself
    (a spotter clip the engine never saw). When a trace's correction lists them, they are not repeated."""
    if _traces_list_alternatives(traces, mapping(evidence).get("primary_text")):
        return []
    nbest = mapping(mapping(evidence).get("nbest"))
    items = [i for i in mappings(nbest.get("hypotheses")) if isinstance(i.get("text"), str) and i["text"]]
    if not items:
        return []
    out = [(1, f"🔎 второе мнение ({engine_label(nbest.get('engine'))}): " + " · ".join(_variant(i) for i in items[:2]))]
    if items[2:]:
        out.append((2, "ещё варианты: " + " · ".join(_variant(i) for i in items[2:])))
    out.extend(_primary_line(evidence))
    out.extend(ms_line(mapping(evidence).get("ms")))
    return out
