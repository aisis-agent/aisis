#!/usr/bin/env python3
"""Generates voice-stack-layers.svg: layered map of the speaker voice stack. Hand layout; the Mermaid twin is voice-stack-layers.mmd."""
import html

W = 2200
F = "DejaVu Sans, Noto Sans, Arial, sans-serif"
CH = {"reg": 0.545, "bold": 0.64}     # em per character, measured against DejaVu Sans in Chrome (Cyrillic and digits included, with margin)
TITLE_PX, LINE_PX, TAG_PX, LH = 17, 14, 13, 18
STYLE = {
    "own": ("#c8e6c9", "#2e7d32"), "asis": ("#e0e0e0", "#616161"), "cfg": ("#bbdefb", "#1565c0"),
    "fork": ("#ffe0b2", "#e65100"), "ownrepo": ("#b2dfdb", "#00695c"), "ext": ("#e1bee7", "#6a1b9a"),
    "vend": ("#f8bbd0", "#ad1457"), "unk": ("#fff9c4", "#f9a825"),
}
panels, edges, labels, nodes = [], [], [], {}


def esc(s):
    return html.escape(s, quote=False)


def tw(s, px, bold=False):
    return len(s) * px * CH["bold" if bold else "reg"]


def wrap(par, maxpx):
    words, lines, cur = par.split(" "), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if tw(t, LINE_PX) <= maxpx:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


class Node:
    def __init__(self, nid, x, y, w, kind, title, paras, tag, h=None):
        self.id, self.x, self.y, self.w, self.kind, self.title, self.tag = nid, x, y, w, kind, title, tag
        inner = w - 36
        self.lines = [ln for p in paras for ln in wrap(p, inner)]
        if tw(title, TITLE_PX, True) > inner + 10:
            print(f"WARN title overflow {nid}: {title}", file=__import__("sys").stderr)
        if tag and tw(tag, TAG_PX) > inner + 10:
            print(f"WARN tag overflow {nid}: {tag}", file=__import__("sys").stderr)
        self.h = h or (14 + 24 + LH * len(self.lines) + (LH + 2 if tag else 0) + 10)

    cx = property(lambda s: s.x + s.w / 2)
    cy = property(lambda s: s.y + s.h / 2)
    r = property(lambda s: s.x + s.w)
    b = property(lambda s: s.y + s.h)

    def svg(self):
        fill, stroke = STYLE[self.kind]
        x, y, w, h, k = self.x, self.y, self.w, self.h, self.kind
        o = []
        if k == "asis":
            o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        elif k == "cfg":
            o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{min(h/2, 32)}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        elif k == "fork":
            o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
            o.append(f'<rect x="{x+5}" y="{y+5}" width="{w-10}" height="{h-10}" fill="none" stroke="{stroke}" stroke-width="2"/>')
        elif k == "ownrepo":
            o.append(f'<polygon points="{x+16},{y} {x+w},{y} {x+w-16},{y+h} {x},{y+h}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        elif k == "ext":
            o.append(f'<polygon points="{x+18},{y} {x+w-18},{y} {x+w},{y+h/2} {x+w-18},{y+h} {x+18},{y+h} {x},{y+h/2}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        elif k == "vend":
            o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="3" stroke-dasharray="3 3"/>')
        elif k == "unk":
            o.append(f'<polygon points="{x},{y} {x+w-16},{y} {x+w},{y+h/2} {x+w-16},{y+h} {x},{y+h}" fill="{fill}" stroke="{stroke}" stroke-width="3" stroke-dasharray="8 5"/>')
        else:
            o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        ty = y + 14 + TITLE_PX
        if self.title:
            o.append(f'<text x="{self.cx}" y="{ty}" text-anchor="middle" font-size="{TITLE_PX}" font-weight="bold" fill="#1b1b1b">{esc(self.title)}</text>')
        for i, ln in enumerate(self.lines):
            o.append(f'<text x="{self.cx}" y="{ty + 6 + LH*(i+1)}" text-anchor="middle" font-size="{LINE_PX}" fill="#222">{esc(ln)}</text>')
        if self.tag:
            o.append(f'<text x="{self.cx}" y="{ty + 6 + LH*(len(self.lines)+1) + 4}" text-anchor="middle" font-size="{TAG_PX}" font-style="italic" fill="#3b3b3b">{esc(self.tag)}</text>')
        return "\n".join(o)


def N(nid, x, y, w, kind, title, paras, tag="", h=None):
    nodes[nid] = Node(nid, x, y, w, kind, title, paras, tag, h)
    return nodes[nid]


def panel(x, y, w, h, title, sub=""):
    panels.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="#f6f8f9" stroke="#90a4ae" stroke-width="1.5"/>'
                  f'<text x="{x+12}" y="{y+23}" font-size="16" font-weight="bold" fill="#263238">{esc(title)}</text>'
                  + (f'<text x="{x+w-12}" y="{y+23}" text-anchor="end" font-size="14" font-style="italic" fill="#455a64">{esc(sub)}</text>' if sub else ""))


def edge(pts, label=None, num=None, dashed=False, at=None, both=False, color="#37474f"):
    edges.append((pts, dashed, both, color))
    if label or num:
        if at is None:      # middle of the longest segment
            i = max(range(len(pts) - 1), key=lambda j: abs(pts[j+1][0]-pts[j][0]) + abs(pts[j+1][1]-pts[j][1]))
            at = ((pts[i][0] + pts[i+1][0]) / 2, (pts[i][1] + pts[i+1][1]) / 2)
        labels.append((at, label, num))


def draw_label(at, label, num):
    lx, ly = at
    w = (tw(label, 13) + 14) if label else 0
    nb = (26 if len(str(num)) == 1 else 40) if num else 0
    total = w + nb
    x0 = lx - total / 2
    o = []
    if label:
        o.append(f'<rect x="{x0}" y="{ly-12}" width="{total}" height="24" rx="7" fill="#fff" stroke="#9fa8da" stroke-width="1"/>')
        o.append(f'<text x="{x0 + nb + 7}" y="{ly+5}" font-size="13" fill="#1a237e">{esc(label)}</text>')
    if num:
        cx = x0 + nb / 2 if label else lx
        if len(str(num)) == 1:
            o.append(f'<circle cx="{cx}" cy="{ly}" r="12" fill="#0d47a1"/>')
        else:
            o.append(f'<rect x="{cx-18}" y="{ly-12}" width="36" height="24" rx="12" fill="#0d47a1"/>')
        o.append(f'<text x="{cx}" y="{ly+5}" text-anchor="middle" font-size="13" font-weight="bold" fill="#fff">{num}</text>')
    return "\n".join(o)


# ------------------------------------------------------------------ rows
TOP = 96
# ROW A -------------------------------------------------------------
dev_x, dev_w = 20, 400
gw_x, gw_w = dev_x + dev_w + 60, 400
q_x = gw_x + gw_w + 60
q_w = W - 20 - q_x
station = N("station", dev_x + 14, TOP + 36, dev_w - 28, "asis", "Yandex Station Mini (rooted)",
            ["vendor firmware; the wake-word spotter and the local-command spotter run ON the speaker; mic, speaker, LED; root + ADB through lite-kit"],
            "upstream as-is: Yandex firmware")
kit = N("kit", dev_x + 14, station.b + 62, dev_w - 28, "fork", "aisis-station-kit :18080/18443",
        ["eQuasar server: answers the speaker's get_sync_info, switches ADB on, merges our settings overlay (Python)"],
        "fork: krevetkqqq/lite-kit (MIT) + local patch")
dns = N("dns", gw_x + 14, TOP + 36, gw_w - 28, "own", "dns-relay (container, Python)",
        ["dns_forwarder.py: answers the Yandex voice and API names with this PC, only for the speaker (IP + ARP MAC); all other DNS is forwarded"], "own")
tls = N("tls", gw_x + 14, dns.b + 30, gw_w - 28, "cfg", "tls-voice nginx :80 / :443",
        ["terminates TLS for quasar.yandex.* with the eMudhra-CA workaround certificates"], "upstream v3rt3p/https-proxy + our config")
sw = N("sw", gw_x + 14, tls.b + 30, gw_w - 28, "own", "switch-prod nginx :31115/6/4",
       ["blue/green selector in front of the active quasar colour"], "own config on the https-proxy image")
qn_w = (q_w - 4 * 18) // 3
core = N("core", q_x + 18, TOP + 36, qn_w, "fork", "quasar-server core (TS)",
         ["@v3rt3p/quasar-server: uniproxy WS, API :31116, directives, STT / processor / TTS plug-in points. Blue :31215, green :31315"],
         "fork: upstream fefcd58, 12 files patched")
fast = N("fast", core.r + 18, TOP + 36, qn_w, "own", "fast-intent layer (TS)",
         ["ha-intents, entity-index, intent-catalog, music-intents: lights, climate, TV, volume, media keys, «play <artist>»; room of the speaker that heard"],
         "own, NOT in git (ticket #108)")
hook = N("hook", fast.r + 18, TOP + 36, qn_w, "own", "grammar hook + overlays",
         ["grammar-hook.ts asks the adapter first (250 ms, fails open); overlays speaker-room, weather-via-cli, volume-zero, hash-pinned patches"],
         "own: services/yandex-station/overlays")
pg = N("pg", core.r - 270, max(core.b, fast.b, hook.b) + 26, 270, "cfg", "postgres 17.6 :31119", ["stations, dialogs, settings"], "upstream + our config")
rowA_h = max(station.b, kit.b, sw.b, pg.b) + 18 - TOP
panel(dev_x, TOP, dev_w, rowA_h, "① DEVICE", "firmware, protocol edge")
panel(gw_x, TOP, gw_w, rowA_h, "② GATEWAY", "network edge, transport")
panel(q_x, TOP, q_w, rowA_h, "③ QUASAR", "API, uniproxy, fast intents: own layer inside a forked server")

# ROW B -------------------------------------------------------------
GAP_B = 150
RB = TOP + rowA_h + GAP_B
CW = 315
HA_GAP = 60
HACLI_CX = 20 + 18 + CW + HA_GAP + CW / 2
asr_w, nlu_w, tts_w = int(HACLI_CX - 30 - 20), 900, 0
asr_x = 20
nlu_x = asr_x + asr_w + 60
tts_x = nlu_x + nlu_w + 60
tts_w = W - 20 - tts_x
stt = N("stt", asr_x + 14, RB + 36, asr_w - 28, "own", "aisis-station-stt :31121 (Python)",
        ["streaming gateway: speaks the GigaAM WebSocket protocol to quasar; VAD, wake-word window cut, level front-end, clip archive, POST /spotter-clip"], "own")
wh = N("wh", asr_x + 14, stt.b + 30, asr_w - 28, "cfg", "wyoming-whisper :10300 (CUDA)",
       ["wyoming-faster-whisper 3.1.0 (MIT), model large-v3-turbo; our flags and systemd drop-ins (prompt, tmpdir)"], "upstream + our flags and drop-ins")
wpt = N("wpt", asr_x + 14, wh.b + 30, asr_w - 28, "own", "aisis-whisper-prompt.timer",
        ["every 6 h: home vocabulary prompt for the recognizer"], "own")
gr_w = 430
gr = N("gr", nlu_x + 14, RB + 36, gr_w, "own", "intent grammar + intent_route",
       ["declarative ru.yaml, no model: /intent/decide (pre phase) and the turn route; canned lines, clock, timers, AC setpoint, emergency"], "own")
mem = N("mem", gr.r + 18, RB + 36, nlu_w - 28 - gr_w - 18, "own", "memory + utterance trace",
        ["dialogs.sqlite3, utterance_traces, e5-small ONNX embed sidecar"], "own code; upstream ONNX model")
jev = N("jev", gr.x, max(gr.b, mem.b) + 26, gr_w, "ext", "Jev advisory classifier (OFF)",
        ["TypeSafe model via Vercel AI Gateway; only logs by default"], "external service")
sk = N("sk", mem.x, max(gr.b, mem.b) + 26, mem.w, "own", "skills + executors",
       ["voice_skills (weather, music), timers, ha_actions (AC, colour), music_airplay; arithmetic in calc.py"], "own")
tts = N("tts", tts_x + 14, RB + 36, tts_w - 28, "own", "adapter POST /v1/audio/speech",
        ["OpenAI-style TTS endpoint quasar calls; ffmpeg to Ogg Opus 48 kHz"], "own")
fish = N("fish", tts_x + 14, tts.b + 30, tts_w - 28, "ext", "Fish Audio / Yandex TTS",
         ["voice «Брачо» (Fish) by default; Alice voice through the uniproxy WS"], "external services")
rowB_h = max(wpt.b, mem.b, jev.b, fish.b) + 18 - RB
panel(asr_x, RB, asr_w, rowB_h, "④ ASR", "speech to text")
panel(nlu_x, RB, nlu_w, rowB_h, "⑤ NLU · DIALOG · SKILLS", "aisis-station-adapter :31117 (Python)")
panel(tts_x, RB, tts_w, rowB_h, "⑥ TTS", "text to audio out")

# ROW C -------------------------------------------------------------
GAP_C = 120
RC = RB + rowB_h + GAP_C
ex_x = 20
cw = CW
ha = N("ha", ex_x + 18, RC + 36, cw, "cfg", "Home Assistant 2026.9.3",
       ["image home-assistant:stable on the odroid, REST :8123"], "upstream + our config")
hacli = N("hacli", ha.r + HA_GAP, RC + 36, cw, "own", "aisis-ha-cli :8795",
          ["Python proxy for Home Assistant: state cache, offline queue of service calls"], "own")
ma = N("ma", hacli.r + 18, RC + 36, cw, "cfg", "Music Assistant 2.10.4",
       ["ghcr.io/music-assistant/server on the odroid; AirPlay to the speaker"], "upstream + our config")
wcli = N("wcli", ma.r + 18, RC + 36, cw, "ownrepo", "weather-cli 0.2.0",
         ["Python, `weather say --now|--tomorrow|...`: HA + Open-Meteo, Russian phrases"], "own repo, MIT")
row1b = max(ha.b, hacli.b, ma.b, wcli.b)
asp = N("asp", ha.x, row1b + 28, cw, "vend", "aisis_speaker component",
        ["HA media_player, notify, sensor of the speaker; adopted from the installed copy"], "vendored, author UNKNOWN")
rem = N("rem", hacli.x, row1b + 28, cw * 2 + 18, "ownrepo", "remote_announcements, ha-host-actions",
        ["HA component and the Kodi/host action bridge (HA config repos)"], "own repos ha-configs-*")
ex_w = wcli.r + 18 - ex_x
rowC_h = max(ha.b, asp.b, rem.b) + 18 - RC
panel(ex_x, RC, ex_w, rowC_h, "⑦ EXECUTION: home, media, weather")
br_x = ex_x + ex_w + 60
br_w = W - 20 - br_x
cp = N("cp", br_x + 18, RC + 36, br_w - 36, "own", "claude_primary.py router",
       ["Claude API first (credential slots), guarded ChatGPT web as the last reserve; no tools"], "own; pinned release dir, branch not merged")
pm = N("pm", br_x + 18, cp.b + 28, (br_w - 36 - 18) // 2, "own", "aisis-station-pm :31118",
       ["ChatGPT tab driven through CDP :9226 (chatgpt-guardian)"], "own + own repo agent-tools")
llm = N("llm", pm.r + 18, cp.b + 28, (br_w - 36 - 18) // 2, "ext", "Claude API / ChatGPT",
        ["Anthropic, OpenAI"], "external services", h=pm.h)
rowC_h = max(rowC_h, llm.b + 18 - RC)
panel(br_x, RC, br_w, rowC_h, "⑧ BRAIN", "language model, no tools")

# ROW D: ops ----------------------------------------------------------
RD = RC + rowC_h + 36
ox = 34
ow = [330, 330, 270, 170, 170, 330, 330, 0]
dash_ = N("dash", ox, RD + 34, 330, "own", "aisis-dashboard :8781", ["Python HTTP + session pages"], "own")
spa = N("spa", dash_.r + 14, RD + 34, 330, "unk", "dashboard SPA source", ["React/Vite, only in the state dir"], "UNKNOWN: no repo", h=dash_.h)
clip = N("clip", spa.r + 24, RD + 34, 270, "own", "aisis-clipfeed", ["every utterance to Telegram"], "own", h=dash_.h)
tgc = N("tgc", clip.r + 12, RD + 34, 150, "ownrepo", "tg-cli", ["own repo"], "", h=dash_.h)
tgs = N("tgs", tgc.r + 12, RD + 34, 150, "ext", "Telegram", ["external"], "", h=dash_.h)
fo = N("fo", tgs.r + 24, RD + 34, 300, "own", "aisis-failover", ["+ service-failover (.files module)"], "own", h=dash_.h)
bg = N("bg", fo.r + 14, RD + 34, 310, "own", "bluegreen/ + corpus-runner", ["deploy, rollback, nightly loop"], "own", h=dash_.h)
kc = N("kc", bg.r + 14, RD + 34, W - 34 - bg.r - 14, "ownrepo", "keychain-cli", ["over gnome-keyring"], "own repo", h=dash_.h)
rowD_h = dash_.b + 16 - RD
panel(20, RD, W - 40, rowD_h, "⑨ OPS", "monitoring, deploy, feedback: not on the request path")

LG = RD + rowD_h + 24
H = LG + 150

# ------------------------------------------------------------------ edges
laneY0 = TOP + rowA_h + 22          # first lane under row A; lanes every 22 px
L = [laneY0 + 22 * i for i in range(6)]
# 1 device -> dns
edge([(station.r, dns.cy), (dns.x, dns.cy)], "audio up, WSS", 1, at=((station.r + dns.x) / 2, dns.cy - 22))
edge([(dns.cx, dns.b), (dns.cx, tls.y)])
edge([(tls.cx, tls.b), (tls.cx, sw.y)])
edge([(sw.r, sw.cy), (q_x - 30, sw.cy), (q_x - 30, core.cy), (core.x, core.cy)], "active colour", 2, at=(q_x - 30, sw.cy - 70))
ytop = TOP - 34
edge([(core.cx, core.y), (core.cx, ytop), (station.cx, ytop), (station.cx, station.y)], "Ogg Opus + directives down, WSS", 9, at=(gw_x + gw_w / 2 + 110, ytop))
edge([(kit.cx, kit.y), (kit.cx, station.b)], "sync + ADB", None, dashed=True)
# lanes under row A: leftmost source = shallowest lane (no crossings)
edge([(core.x + 50, core.b), (core.x + 50, L[0]), (stt.x + 300, L[0]), (stt.x + 300, stt.y)], "PCM up, text down, WS", "3/5", both=True, at=((core.x + 50 + stt.x + 300) / 2 + 60, L[0]))
edge([(stt.cx, stt.b), (stt.cx, wh.y)], "Wyoming TCP", 4, both=True)
edge([(wpt.cx, wpt.y), (wpt.cx, wh.b)], None, dashed=True)
edge([(fast.x + 60, fast.b), (fast.x + 60, L[1]), (hacli.cx, L[1]), (hacli.cx, hacli.y)], "HA REST :8795", 7, at=((fast.x + 60 + hacli.cx) / 2 + 130, L[1]))
edge([(fast.cx, fast.b), (fast.cx, L[2]), (gr.x + 230, L[2]), (gr.x + 230, gr.y)], "unanswered turn: WS /processor", "A", dashed=True, at=(gr.x + 230 + 330, L[2]))
edge([(hook.cx, hook.b), (hook.cx, L[3]), (gr.x + 340, L[3]), (gr.x + 340, gr.y)], "POST /intent/decide, 250 ms", 6, at=(gr.x + 340 + 330, L[3]))
edge([(hook.r - 70, hook.b), (hook.r - 70, L[0]), (tts.cx, L[0]), (tts.cx, tts.y)], "POST /v1/audio/speech", 8, both=True, at=(tts.cx + 20, L[0]))
edge([(tts.cx, tts.b), (tts.cx, fish.y)], "HTTPS", None)
edge([(hacli.cx, hacli.b), (hacli.cx, hacli.b)], None) if False else None
edge([(hacli.x, hacli.cy), (ha.r, ha.cy)], "REST :8123", None, at=((hacli.x + ha.r) / 2, hacli.cy - 34))
nb = RB + rowB_h
LC = [nb + 24, nb + 56]
edge([(sk.cx, sk.b), (sk.cx, nb + 0), (sk.cx, LC[0]), (hacli.cx + 70, LC[0]), (hacli.cx + 70, hacli.y)], "lights, climate, timers", None, at=(hacli.cx + 70, (LC[0] + hacli.y) / 2))
edge([(sk.cx, LC[0]), (ma.cx, LC[0]), (ma.cx, ma.y)], "music :31117/music/play", None, at=(ma.cx + 70, LC[0] + 0)) if False else None
edge([(sk.cx, LC[0]), (ma.cx, LC[0]), (ma.cx, ma.y)], "music", None, at=(ma.cx, (LC[0] + ma.y) / 2))
edge([(sk.cx, LC[0]), (wcli.cx, LC[0]), (wcli.cx, wcli.y)], "weather", None, at=(wcli.cx, (LC[0] + wcli.y) / 2))
edge([(jev.cx, jev.b), (jev.cx, LC[1]), (cp.cx, LC[1]), (cp.cx, cp.y)], "B: no skill answered, ask the brain", "B", at=(cp.cx - 330, LC[1]))
edge([(cp.cx - 110, cp.b), (cp.cx - 110, pm.y)], "ChatGPT reserve", None, dashed=True)
edge([(cp.cx + 110, cp.b), (cp.cx + 110, llm.y)], "HTTPS", None)

# ------------------------------------------------------------------ emit
o = []
o.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{F}">')
o.append('<defs><marker id="arr" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#37474f"/></marker></defs>')
o.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
o.append(f'<text x="20" y="32" font-size="25" font-weight="bold" fill="#102027">AISIS voice stack: layers, what runs where, and where each part comes from</text>')
o.extend(panels)
for pts, dashed, both, color in edges:
    path = "M " + " L ".join(f"{a},{b}" for a, b in pts)
    dash = ' stroke-dasharray="9 6"' if dashed else ""
    ms = ' marker-start="url(#arr)"' if both else ""
    o.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.2"{dash}{ms} marker-end="url(#arr)" stroke-linejoin="round"/>')
for n in nodes.values():
    o.append(n.svg())
for at, label, num in labels:
    o.append(draw_label(at, label, num))
# legend
o.append(f'<rect x="20" y="{LG}" width="{W-40}" height="132" rx="10" fill="#fff" stroke="#90a4ae" stroke-width="1.5"/>')
o.append(f'<text x="32" y="{LG+23}" font-size="16" font-weight="bold" fill="#263238">LEGEND: provenance = color + shape + border. Numbered circles = the request path of one spoken command; dashed = fallback or side path</text>')
items = [("own", "own: written by us (this repo)"), ("cfg", "upstream + our config or flags"), ("fork", "fork / patched upstream"), ("ownrepo", "own repo elsewhere"),
         ("ext", "external cloud service"), ("vend", "vendored, author UNKNOWN"), ("asis", "upstream as-is"), ("unk", "UNKNOWN: no evidence")]
for i, (k, t) in enumerate(items):
    cx, cy = 34 + (i % 4) * 300, LG + 42 + (i // 4) * 44
    sw_ = Node("lg", cx, cy, 56, k, "", [], "", h=30)
    sw_.lines = []
    o.append(sw_.svg())
    o.append(f'<text x="{cx+68}" y="{cy+20}" font-size="14" fill="#222">{esc(t)}</text>')
steps = ["1 audio up   2 to the active colour   3 PCM to STT   4 Whisper   5 final text back",
         "6 grammar hook (route none = quasar tiers decide)   7 Home Assistant   8 TTS only when the reply has text",
         "9 audio and directives down to the speaker   A (dashed) = turn the quasar layer did not answer: adapter /processor"]
for i, s in enumerate(steps):
    o.append(f'<text x="1250" y="{LG+50+i*24}" font-size="14" fill="#222">{esc(s)}</text>')
o.append('</svg>')
print("\n".join(o))
