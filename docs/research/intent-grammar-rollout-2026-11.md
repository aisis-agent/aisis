# Intent grammar rollout: what answers without a model now, measured

Ticket: alex-mextner/aisis#107 (audit: `intent-coverage-audit-2026-11.md`, plan §7). Date: 2026-11-16. Code: `services/yandex-station/code/intent_grammar/`
(engine, grammar, back-test), `intent_route.py` (the adapter route), `timers.py`, `ha_actions.py`, `services/yandex-station/overlays/intent-grammar/` (quasar hook).

Labels: **[measured]** run by the back-test on the labelled corpora, **[INFERENCE]** my reasoning, not observed, **[not verified live]** written and unit-tested, never run against the real speaker, Home Assistant or the audio path.

## 1. What changed in the route

```
Station mic ─► STT gateway (hallucination / noise gates, unchanged)
  ─► quasar overlay, NEW hook (pre-phase): POST adapter /intent/decide, 250 ms cap, fails open
        drop the caption · canned wake/greeting/thanks/farewell/refusals · clock/date · timers/alarms · negation no-op · relative volume
  ─► quasar's own handlers (unchanged: pending answers, end-dialog, repeat, math, TV, media keys, volume, lights, devices, music)
        NEW: an incomplete-slot «Громкость.» no longer waits for the Haiku corrector (~0.85 s)
  ─► adapter _process_turn, NEW grammar route:
        real intents (climate, colour, …) → arithmetic → weather → catch-alls: «Не расслышал. Повтори, пожалуйста.» for short unmatched
        non-questions (once a minute per station), «Такого устройства я не знаю.», dangling «включи» → «Что именно?»
  ─► brain (unchanged): knowledge, advice, chit-chat, follow-ups
```

Switches (adapter env): `AISIS_INTENT_GRAMMAR=on|shadow|off` (default `on`; `shadow` only classifies and logs `intent_shadow intent=… tier=…`: no reply, timer, Home Assistant call or reprompt cooldown), `AISIS_INTENT_CATCHALL=off`
keeps the reprompt/refusal catch-alls away, `AISIS_HA_ACTIONS=off`, timers only with `AISIS_TIMER_TTS_ENTITY` + `AISIS_TIMER_PLAYER` (else «Таймеры и будильники я пока не умею.»).
Quasar: `GRAMMAR_HOOK=on|shadow|off`. `/healthz` shows `intent_grammar.routes` (counts by source).

## 2. Numbers [measured]

Corpora: 96 real spoken utterances (79 addressed) of 2026-11-07..16 from one household, hand-labelled in the audit; 466 hand-written synthetic phrases (`intents-ru.jsonl`).
«Before» = what the deployed quasar container decides without a model, replayed offline over every phrase (`tools/replay_quasar.mts`: stateless text parsers, no music library, so a phrase
the music parser accepts counts as handled). «After» = the production order: grammar pre-phase → quasar → grammar rest. Back-test `--route quasar` / `--route combined`.
Coverage = answered without a model / addressed. Correct = coverage with the right intent. Completed = correct and not a reprompt/clarification.
Splits: 20 % of the distinct phrases (19 real rows, 18 addressed) held out by a hash of a grammar-independent canonical form (lower case, ё → е, punctuation removed; `SPLIT_VERSION` 1), so editing the grammar can never move a row
between dev and holdout; I tuned on dev only and never printed held-out texts. The audit classes were derived from all 96 rows, so held-out is **not** fully independent of the design; the unbiased test is the rows that arrive after this change.

**Correction (retraction).** An earlier version of this document, and a message to the owner, quoted a held-out coverage of 95.2 % on 21 rows. That split hashed a text form that the grammar's own vocabulary changed, so a
grammar edit moved rows between dev and holdout and the held-out number was contaminated by tuning. It is withdrawn. The numbers below use the independent split; row by row the outcomes are identical to the old tree, only the split moved.

| Real, addressed | n | before: coverage / correct | after: coverage | after: correct | after: completed |
|---|---|---|---|---|---|
| dev | 61 | 55.7 % / 54.1 % | 86.9 % (76.2–93.2) | 83.6 % (72.4–90.8) | 77.0 % (65.1–85.8) |
| **held-out** | **18** | 27.8 % / 27.8 % | **83.3 % (60.8–94.2)** | 77.8 % (54.8–91.0) | 61.1 % (38.6–79.7) |
| all | 79 | 49.4 % / 48.1 % | **86.1 % (76.8–92.0)** | 82.3 % (72.4–89.1) | 73.4 % (62.8–81.9) |
| synthetic (not tuned on) | 466 | 67.8 % / 67.8 % | 88.4 % | 79.2 % | 79.2 % |

95 % Wilson intervals in brackets. **The ≥ 90 % target is NOT met**: 86.1 % coverage and 82.3 % correct on the real set, 83.3 % / 77.8 % on the held-out split (the held-out interval is about ±17 points: 18 rows cannot confirm or refute a rate).
About 10 % of real traffic (chit-chat, knowledge, advice, follow-ups: 8 of 79) is the brain's by design, and the remaining misses are Whisper garbage made of valid words («Голи со спортом», «Что со стерлицей?») that no rule can tell from speech;
3 of the 8 catch-all reprompt answers were themselves false triggers or wrong actions (ticket «the catch-all reprompt misfires on short real speech»).
Of the 68 answered real rows, 8 are catch-all reprompts and 1 is a clarification; those count as «answered» but not as «completed». **False triggers** (speech not addressed to the
speaker that got an answer): before 2 (hallucinations resumed playback), after 2 (both in dev; one is a person talking to a cat that now gets «Не расслышал»); 1 wrong action on real dev, 0 on held-out; 4 wrong actions on the synthetic set.

Latency [measured, in process]: decision p95 **1.4 ms** (bare engine over the corpus), adapter turn p95 **0.8 ms** and the overlay's `/intent/decide` p95 **0.65 ms** over every pinned example (755 calls; unit test caps both at 300 ms).
These exclude STT, endpointing, TTS first sentence (~1.9 s, unchanged: a spoken canned line still pays synthesis; prepared audio is a follow-up) and Home Assistant round trips (climate/colour ≈ 1.3 s).
Removed waits: the incomplete-slot Haiku wait (~0.85 s, overlay) and the brain call (p50 2.4 s) for every phrase the grammar answers.

## 3. Owner steps (exact)

1. Merge, then restart the adapter (`systemctl --user restart aisis-station-adapter`): the grammar loads from the committed JSON bundle, no new Python dependency. `curl -s 127.0.0.1:31117/healthz` shows `intent_grammar.loaded: true`.
2. Quasar: build the overlay with the README of `overlays/intent-grammar/` (captures the active image's two files, hash-checked) and deploy it next to the weather overlay; until then the adapter route works on the turns that reach it
   (clock, timers, climate/colour, canned lines, reprompts) but «не включай свет» and hallucinations are still seen by the quasar layer first.
3. Timers: set `AISIS_TIMER_TTS_ENTITY=tts.bracho_bracho` and `AISIS_TIMER_PLAYER=<the station's media_player entity>` in `~/.config/aisis/runtime.env` **[not verified live: speaking a timer through `tts.speak` on this station was not tried]**.
4. Roll back by `AISIS_INTENT_GRAMMAR=off` (adapter) and `GRAMMAR_HOOK=off` (quasar): both restore the previous path byte for byte.

## 4. Risks and what is not verified

- **[not verified live]** HA climate/colour executors (`ha_actions.py`: unit tests with a mock Home Assistant and a live smoke that reads the real entities but only records the service calls), timer announcements, the overlay in the real running container (it passed the in-image check with a stub adapter, 34/34).
- The catch-all reprompt answers a short unmatched non-question with «Не расслышал», once a minute per station; a real short remark to the speaker («Мне нравится») gets that line once. Switch: `AISIS_INTENT_CATCHALL=off`. Track the reprompt rate (`/healthz routes`) before trusting it.
- Unknown-device refusal says «Такого устройства я не знаю.» for any unmatched `выключи|открой|закрой|…` phrase that reached the adapter; the quasar layer has already tried every HA name and the music library.
- Timers need the owner's TTS target; without it they refuse honestly instead of promising.
- The corpus is one household for 11 days; every rate above has an interval of ±10 points. The nightly loop (phase 4) is what makes the numbers stable.

## 5. Held-out gate design and the emergency safety set

**Gate decision (a `review brainstorm`, Opus + two Sol seats, finished after two rounds with STOP; the transcript is in the review-cli log of job 20261116T192926-d90d2774).** The held-out split has 18 addressed rows, so the old absolute `real/holdout >= 0.85` floor (set while the retracted 95.2 % figure was believed) is not a meaningful gate: 15/18 = 83.3 %, the 95 % interval is 61-94 %. **The brainstorm did not agree on this point:** its converged recommendation was to keep that floor enforced (red) and not to delay it with a minimum-n switch. The owner decision this change follows is the opposite: the floor stops being a gate, is not silently lowered, and stays printed in every report as `legacy_floor`, NOT met, next to the unmet 90 % `goal`. The dissent is recorded here so a reviewer can reverse it with one config line (`gate.holdout.floor_min_addressed`). What gates instead, with the brainstorm's agreed parts adopted: (a) a per-row ratchet against the last promoted report with no tolerance (the same phrases meet a deterministic engine, so any worsening is real; a one-row allowance would let a 5.6-point loss through), where wrong -> abstained counts as better and missed -> wrong as worse; (b) losing or relabelling a baseline row, or a baseline of another route/split, fails; no usable baseline fails unless `--init-baseline REASON`; (c) absolute floors that switch on with n (interval test from 20 addressed rows, point floor from 60); (d) the 90 % target is reported as `goal` with its interval, never gated. Not taken from the brainstorm yet (tracked in #131): origin tags for owner-feedback rows (hash-stable selection is not representative), one baseline file per route, opt-in `--promote` only when every check is green, a label-aware fingerprint of corpus files and an independent inventory so rows added in a red run cannot vanish later.

**Safety.** `refuse.emergency` (priority 1000, pre phase) catches ambulance/police/fire/«мне плохо»/heart/breathing/drowning/poisoning/unconscious/«помогите» phrases, including with leading words and «позвони в полицию», answers that the speaker cannot call anyone and gives 112 first (Serbia: 194 ambulance, 192 police, 193 fire), and never reaches the brain. `gate.safety` pins 26 phrases: one miss fails the gate and the nightly run regardless of coverage.
