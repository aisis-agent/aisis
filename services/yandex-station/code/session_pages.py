"""Server-rendered dashboard pages the Telegram feed links to: one speaker session, and the intent definitions.

`/session/<dialog id>` and `/session/clip/<clip id>` show a session the way it was processed: the activation, every request (audio,
recognized text, ASR engine, VAD/gain facts), then how the adapter handled it (utterance_trace.py: layer, grammar intent with
its parameters, calls, latencies, errors) and the quasar log events. A grammar intent links to `/grammar#intent-<id>`, which
shows its definition from the grammar bundle, the exact YAML block and a GitHub link to its line.

The pages are plain HTML on purpose: they open on a phone from a Telegram link without the SPA, and they live in the repo
(the SPA's sources do not).
"""
from __future__ import annotations

import html
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import session_links
import trace_view

STYLE = """
:root{color-scheme:dark light;--bg:#0f1115;--card:#181b22;--fg:#e6e8ee;--dim:#8d93a3;--line:#2a2f3a;--accent:#6aa9ff;--ok:#4cc38a;--warn:#f0b429;--bad:#ef6461}
@media (prefers-color-scheme:light){:root{--bg:#f5f6f8;--card:#fff;--fg:#1c1f26;--dim:#6b7280;--line:#dde0e6;--accent:#1d5fd6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:12px}h1{font-size:18px;margin:8px 0}h2{font-size:15px;margin:0 0 6px}
a{color:var(--accent)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0}
.dim{color:var(--dim)}.chip{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 8px;margin:0 4px 2px 0;font-size:12px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);
border:1px solid var(--line);border-radius:6px;padding:8px;font-size:12.5px;margin:6px 0}audio{width:100%;margin:6px 0}
ul{margin:4px 0;padding-left:20px}li{margin:2px 0}details>summary{cursor:pointer}.said{font-size:17px}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:3px 6px;text-align:left;vertical-align:top;font-size:13px}
"""
SCRIPT = ("<script>function o(){var h=location.hash.slice(1);if(!h)return;var e=document.getElementById(h);"
          "if(e&&e.tagName==='DETAILS'){e.open=true;e.scrollIntoView()}}addEventListener('hashchange',o);o()</script>")
CLIP_AUDIO = "/api/voice/clip/"
GRAMMAR_DIR = Path(__file__).parent / "intent_grammar" / "grammar"


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{STYLE}</style></head><body><main>"
            f"<div class=dim><a href='/'>дашборд</a> · <a href='/grammar'>интенты</a></div>{body}</main>{SCRIPT}</body></html>")


def hms(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "—"


def day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


# ── intents page ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=2)
def _definitions(path: str, mtime: float) -> dict[str, dict[str, Any]]:
    """intent id -> {line, end, source} from the YAML text (the line numbers the GitHub link needs; the bundle has none)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    starts = [(i, m.group(1)) for i, text in enumerate(lines) if (m := re.match(r"^  - id:\s*(\S+)\s*(?:#.*)?$", text))]
    found: dict[str, dict[str, Any]] = {}
    for n, (i, intent_id) in enumerate(starts):
        stop = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        while stop > i + 1 and not lines[stop - 1].strip():
            stop -= 1
        found[intent_id] = {"line": i + 1, "end": stop, "source": "\n".join(lines[i:stop])}
    return found


def definitions() -> dict[str, dict[str, Any]]:
    path = GRAMMAR_DIR / "ru.yaml"
    try:
        return _definitions(str(path), path.stat().st_mtime)
    except OSError:
        return {}


@lru_cache(maxsize=2)
def _grammar(path: str, mtime: float):
    import intent_route
    return intent_route.load_grammar(path)


def grammar():
    path = GRAMMAR_DIR / "ru.json"
    try:
        return _grammar(str(path), path.stat().st_mtime)
    except OSError:
        return None


def intent_link(intent_id: str | None, label: str | None = None) -> str:
    """An anchor to the intent's definition, or plain text when the id is not a grammar intent (skill.math, jev.*, quasar)."""
    if not intent_id:
        return ""
    if intent_id in definitions():
        return f"<a href='{e(session_links.intent_path(intent_id))}'>{e(label or intent_id)}</a>"
    return e(label or intent_id)


def render_intents() -> str:
    gram, defs = grammar(), definitions()
    if gram is None:
        return page("Интенты", "<h1>Интенты</h1><div class=card>Грамматика не загружена.</div>")
    intents = sorted(gram.intents, key=lambda i: (-i.priority, i.id))
    cards = []
    for intent in intents:
        d = defs.get(intent.id)
        action = intent.action or {}
        head = (f"<span class=chip>{e(intent.tier)}</span><span class=chip>p{e(intent.priority)}</span>"
                + (f"<span class=chip>{e(intent.phase)}</span>" if intent.phase else "")
                + (f"<span class=chip>{e(action.get('executor'))}</span>" if action.get("executor") else ""))
        rows = []
        if intent.labels:
            rows.append(f"<li>метки: {e(', '.join(intent.labels))}</li>")
        if action.get("args"):
            rows.append(f"<li>аргументы: {e(action['args'])}</li>")
        if intent.reply:
            rows.append(f"<li>ответ: {e(intent.reply)}</li>")
        if intent.pattern_text:
            rows.append("<li>шаблоны:<ul>" + "".join(f"<li><code>{e(p)}</code></li>" for p in intent.pattern_text[:12]) + "</ul></li>")
        examples = [x.say for x in intent.examples][:8]
        if examples:
            rows.append("<li>примеры: " + e(" · ".join(examples)) + "</li>")
        link = ""
        if d:
            link = (f"<div class=dim>ru.yaml, строки {d['line']}–{d['end']} · "
                    f"<a href='{e(session_links.github_line_url(d['line']))}'>на GitHub</a></div><pre>{e(d['source'])}</pre>")
        cards.append(f"<details class=card id='intent-{e(intent.id)}'><summary><b>{e(intent.id)}</b> {head}</summary>"
                     f"<ul>{''.join(rows)}</ul>{link}</details>")
    return page("Интенты", f"<h1>Интенты грамматики <span class=dim>({len(intents)})</span></h1>"
                f"<div class=dim>ru.yaml, версия {e(gram.version)}. Сессии ссылаются сюда по идентификатору интента.</div>" + "".join(cards))


# ── session page ─────────────────────────────────────────────────────────────
def _clip_block(clip: dict, traces: list[dict], focus: str | None) -> str:
    status = clip.get("status")
    text = clip.get("transcript") or ""
    chips = [f"<span class=chip>{e(clip.get('engine') or 'ASR ?')}</span>"]
    if clip.get("asr_ms") is not None:
        chips.append(f"<span class=chip>ASR {e(clip['asr_ms'])} мс</span>")
    if clip.get("final_ms") is not None:
        chips.append(f"<span class=chip>до ответа станции {e(clip['final_ms'])} мс</span>")
    facts = [("длина", f"{clip['dur']:.2f} с" if clip.get("dur") is not None else "—"), ("речь", f"{clip.get('voiced_ms', '—')} мс"),
             ("хвост тишины", f"{clip.get('silence_ms', '—')} мс"), ("пик / rms", f"{clip.get('peak', '—')} / {clip.get('rms', '—')}"),
             ("пол шума", clip.get("floor_rms", "—")), ("контраст", clip.get("contrast", "—")),
             ("закрыто", clip.get("trigger") or "?"), ("станция", clip.get("station") or "—")]
    if clip.get("gain"):
        facts.append(("усиление в ленте", f"×{clip['gain']}"))
    verdict = ""
    if status in ("gate", "blacklist"):
        verdict = f"<div class=warn>отброшено ({e(status)}): {e(clip.get('reason') or '')}</div>"
    elif clip.get("reason"):
        verdict = f"<div class=dim>{e(clip['reason'])}</div>"
    if clip.get("truth"):
        verdict += f"<div>ваш вердикт: <b>{e(clip['truth'])}</b></div>"
    mark = " style='border-color:var(--accent)'" if focus and focus == clip.get("id") else ""
    audio = f"<audio controls preload=none src='{CLIP_AUDIO}{e(clip['id'])}'></audio>" if clip.get("id") else ""
    traces_html = "".join(_trace_block(t, len(traces) > 1) for t in traces) or \
        "<div class=dim>Запись адаптера об этой фразе не найдена: её обработал квазар сам (быстрый интент) или фраза не дошла до адаптера.</div>"
    return (f"<div class=card{mark}><h2>🎙 {hms(clip.get('ts'))} · {e(text) if text else '— текста нет —'}</h2>{audio}{''.join(chips)}"
            f"<table>{''.join(f'<tr><td class=dim>{e(k)}</td><td>{e(v)}</td></tr>' for k, v in facts)}</table>{verdict}{traces_html}</div>")


def _trace_block(trace: dict, several: bool) -> str:
    via = f"<span class=chip>{e(trace_view.VIA.get(trace.get('via'), trace.get('via')))}</span>" if several or trace.get("via") != "turn" else ""
    items = []
    head = trace_view.route_line(trace)
    for line in trace_view.lines(trace)[1:]:
        items.append(f"<li>{e(line)}</li>")
    link = intent_link(trace.get("intent"))
    intent_html = f" · определение интента: {link}" if link.startswith("<a") else ""
    reply = f"<div class=said>💬 {e(trace['reply'])}</div>" if trace.get("reply") else ""
    start = f"<span class=dim>{hms(trace.get('ts'))} · «{e(trace.get('text'))}»</span>"
    return (f"<div style='border-top:1px solid var(--line);margin-top:8px;padding-top:6px'>{via}<b>{e(head)}</b>{intent_html} {start}"
            f"{reply}<ul>{''.join(items)}</ul></div>")


def _turn_lines(dialog: dict) -> str:
    out = []
    for turn in dialog.get("turns") or []:
        if turn["role"] == "event" and turn["kind"] in ("intent", "action", "asr", "error"):
            label = {"intent": "квазар: быстрый интент", "action": "квазар: действие", "asr": "коррекция ASR", "error": "ошибка"}[turn["kind"]]
            out.append(f"<li><span class=dim>{e(turn.get('t'))}</span> {e(label)}: {e(turn['text'])}"
                       + (f" → {e(turn['res'])}" if turn.get("res") else "") + "</li>")
        elif turn["role"] == "assistant":
            out.append(f"<li><span class=dim>{e(turn.get('t'))}</span> 🔊 {e(turn['text'])}</li>")
    return "<div class=card><h2>Журнал квазара</h2><ul>" + "".join(out) + "</ul></div>" if out else ""


def render_session(dialog: dict | None, clips: list[dict], traces: list[dict], focus_clip: str | None = None, key: str = "") -> str:
    """`dialog`: dialogstore.get_dialog (None for a clip that has no dialog); `clips`: voice_clips rows (dicts) of the session, oldest
    first; `traces`: the adapter's records that fall into it. Orphan traces (matching no clip) are listed at the end."""
    if dialog is None and not clips:
        return page("Сессия не найдена", f"<h1>Сессия не найдена</h1><div class=card>Нет диалога или записи «{e(key)}».</div>")
    first = (dialog or {}).get("ts") or clips[0]["ts"]
    station = (dialog or {}).get("station") or next((c.get("station") for c in clips if c.get("station")), None)
    status = (dialog or {}).get("status") or "диалога нет (запись без диалога)"
    chips = f"<span class=chip>станция {e(station or '—')}</span><span class=chip>{e(status)}</span>"
    if dialog and dialog.get("dur") not in ("", None):
        chips += f"<span class=chip>{e(dialog['dur'])} с</span>"
    wake = [c for c in clips if c.get("status") == trace_view.WAKE_STATUS]
    speech = [c for c in clips if c.get("status") != trace_view.WAKE_STATUS]
    body = [f"<h1>Сессия {hms(first)} <span class=dim>{day(first)}</span></h1><div>{chips}</div>"]
    if dialog and dialog.get("err"):
        body.append(f"<div class='card bad'>⚠ {e(dialog['err'])}</div>")
    for clip in wake:
        body.append(f"<div class=card><h2>🔔 Активация {hms(clip['ts'])}</h2>"
                    f"<div class=dim>слово активации (распознано как «{e(clip.get('transcript') or '')}»); сама по себе фразой не считается, "
                    f"сессия ждёт запрос. {e(clip.get('reason') or '')}</div>"
                    f"<audio controls preload=none src='{CLIP_AUDIO}{e(clip['id'])}'></audio></div>")
    used: set[int] = set()
    ordered = sorted(speech, key=lambda c: c["ts"])
    for n, clip in enumerate(ordered):
        until = ordered[n + 1]["ts"] if n + 1 < len(ordered) else None
        mine = [t for t in trace_view.traces_of_clip(clip, traces, until) if t["id"] not in used]
        used.update(t["id"] for t in mine)
        body.append(_clip_block(clip, mine, focus_clip))
    if dialog and not ordered:
        heard = "".join(f"<li>{e(h)}</li>" for h in dialog.get("heard") or [])
        if heard:
            body.append(f"<div class=card><h2>Распознано (записи в архиве нет)</h2><ul>{heard}</ul></div>")
    rest = [t for t in traces if t["id"] not in used]
    if rest:
        body.append("<div class=card><h2>Вызовы адаптера без записи</h2>" + "".join(_trace_block(t, True) for t in rest) + "</div>")
    if dialog:
        body.append(_turn_lines(dialog))
        raw = dialog.get("raw") or []
        if raw:
            body.append("<details class=card><summary>Сырой лог диалога</summary><pre>" + e("\n".join(raw)) + "</pre></details>")
    ident = (dialog or {}).get("id") or key
    body.append(f"<div class=dim>id {e(ident)}</div>")
    return page(f"Сессия {hms(first)}", "".join(body))
