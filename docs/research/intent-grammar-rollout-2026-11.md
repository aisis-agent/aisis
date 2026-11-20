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
| dev | 61 | 55.7 % / 54.1 % | 85.2 % (74.3–92.0) | 82.0 % (70.5–89.6) | 57.4 % (44.9–69.0) |
| **held-out** | **18** | 27.8 % / 27.8 % | **83.3 % (60.8–94.2)** | 77.8 % (54.8–91.0) | 55.6 % (33.7–75.4) |
| all | 79 | 49.4 % / 48.1 % | **84.8 % (75.3–91.1)** | 81.0 % (71.0–88.1) | 57.0 % (46.0–67.3) |
| synthetic (not tuned on) | 466 | 67.8 % / 67.8 % | 87.8 % (84.5–90.4) | 79.2 % (75.3–82.6) | 70.0 % (65.7–73.9) |

95 % Wilson intervals in brackets. **The ≥ 90 % target is NOT met**: 84.8 % coverage and 81.0 % correct on the real set, 83.3 % / 77.8 % on the held-out split (the held-out interval is about ±17 points: 18 rows cannot confirm or refute a rate).
**Measurement correction (review of 2026-11-16).** The first version of this table (real all 86.1 % coverage / 82.3 % correct / 73.4 % completed, synthetic 88.4 % / 79.2 % / 79.2 %) over-counted: a grammar
decision counted as an answer even where the runtime declines it (a volume change in the adapter turn, which only the overlay hook can make; a guess that waits for the owner's «да»; an executor that is
not configured), and a phrase the quasar container answered was scored as a completed action with the row's own label copied in, so a replayed clarification («громкость», `fast-clarify`) or confirmation
(«освещение 30 процентов», `fast-confirm`) counted as executed. The route now counts a decision only when the runtime acts on it (`IntentRouter.executable`, shared with the runtime) and keeps the tier the quasar
container answered with; its label and arguments are not in the replay, so those rows are judged on the tier. The grammar edits of the same review (relative volume only with levels 1–10 or a percent,
the refusals leaving media requests alone, question guards on the hardware rules) are inside the new numbers. The «before» column did not move. Nothing was tuned to restore the old numbers.
About 10 % of real traffic (chit-chat, knowledge, advice, follow-ups: 8 of 79) is the brain's by design, and the remaining misses are Whisper garbage made of valid words («Голи со спортом», «Что со стерлицей?») that no rule can tell from speech;
3 of the 8 catch-all reprompt answers were themselves false triggers or wrong actions (ticket «the catch-all reprompt misfires on short real speech»).
Of the 67 answered real rows, 8 are catch-all reprompts, 11 clarifications (the grammar's and the quasar container's) and 2 confirmations; those count as «answered» but not as «completed». **False triggers** (speech not addressed to the
speaker that got an answer): before 2 (hallucinations resumed playback), after 2 (both in dev; one is a person talking to a cat that now gets «Не расслышал»); 1 wrong action on real dev, 0 on held-out; 1 wrong action on the synthetic set.

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
- Timers need the owner's TTS target; without it they refuse honestly instead of promising. The back-test assumes the target is configured.
- **[not verified live]** A Home Assistant command without a room (colour, brightness, climate) is about the room of the speaker that heard it: the HA area of the station's `media_player` (its id ends in the station id; the AISIS player wins over the Yandex one), the same source as the overlay's speaker-room layer. A station Home Assistant does not place in an area is asked «В какой комнате?», never given the living room; a room without the device says so («В комнате «Спальня» нет кондиционера»). Printer LEDs (`k1se`, `snapmaker`, `cavity`) are never part of a room's lights. While Home Assistant is down the whole command goes to hacli's recovery queue, not only its first call.
- A voice reminder is stored apart from timers («отмени таймер» keeps it and, when only a reminder is left, says so and names «отмени напоминание», which cancels it), «сколько осталось» names the nearest of both, and an alarm for a wall-clock time rings at its nearest real occurrence: inside the repeated hour of a DST fall-back the nearer pass, and a time the spring-forward skips falls on the next day that has it (up to two days ahead). Without a timers target a timer phrase, alone or in a compound made only of timer clauses, answers «не умею» (a compound with any other clause goes to the old path whole). One utterance delivered twice within 5 s (overlay hook, then turn) runs once (a compound unless it has a Home Assistant or «повтори» clause, which depend on the speaking station; such a compound runs again, timer clause included); any other timer utterance in between empties the one remembered slot.
- The back-test report schema is 3 (it also versions the scoring rule): a baseline saved under the older scoring is `incompatible` and needs the owner's `--init-baseline "why"`.
- The corpus is one household for 11 days; every rate above has an interval of ±10 points. The nightly loop (phase 4) is what makes the numbers stable.

## 5. Held-out gate design and the emergency safety set

**Gate decision (a `review brainstorm`, Opus + two Sol seats, finished after two rounds with STOP; the transcript is in the review-cli log of job 20261116T192926-d90d2774).** The held-out split has 18 addressed rows, so the old absolute `real/holdout >= 0.85` floor (set while the retracted 95.2 % figure was believed) is not a meaningful gate: 15/18 = 83.3 %, the 95 % interval is 61-94 %. **The brainstorm did not agree on this point:** its converged recommendation was to keep that floor enforced (red) and not to delay it with a minimum-n switch. The owner decision this change follows is the opposite: the floor stops being a gate, is not silently lowered, and stays printed in every report as `legacy_floor`, NOT met, next to the unmet 90 % `goal`. The dissent is recorded here so a reviewer can reverse it with one config line (`gate.holdout.floor_min_addressed`). What gates instead, with the brainstorm's agreed parts adopted: (a) a per-row ratchet against the last promoted report with no tolerance (the same phrases meet a deterministic engine, so any worsening is real; a one-row allowance would let a 5.6-point loss through), where wrong -> abstained counts as better and missed -> wrong as worse; (b) losing or relabelling a baseline row, or a baseline of another route/split, fails; no usable baseline fails unless `--init-baseline REASON`; (c) absolute floors that switch on with n (interval test from 20 addressed rows, point floor from 60); (d) the 90 % target is reported as `goal` with its interval, never gated. Not taken from the brainstorm yet (tracked in #131): origin tags for owner-feedback rows (hash-stable selection is not representative), one baseline file per route, opt-in `--promote` only when every check is green, a label-aware fingerprint of corpus files and an independent inventory so rows added in a red run cannot vanish later.

**Rebaseline.** A corrected label, a renamed corpus file or a removed row turns the ratchet red for every later run. The owner's way out is `backtest --route combined --gate --init-baseline "why"`: accepted against a compatible baseline only when rows were lost or relabelled, it records the reason and the counts in the report and keeps every other check (regressions included). The nightly loop never passes it.

**Safety.** `refuse.emergency` (priority 1000, pre phase) catches ambulance/police/fire/«мне плохо»/heart/breathing/drowning/poisoning/unconscious/«помогите» phrases, including with leading words and «позвони в полицию», answers that the speaker cannot call anyone and gives 112 first (Serbia: 194 ambulance, 192 police, 193 fire), and never reaches the brain. `gate.safety` pins 26 phrases: one miss fails the gate and the nightly run regardless of coverage.

## 6. Repair dialog rollout (#182)

Contexts and the standard replies are grammar rows (`contexts:` in `ru.yaml`, intents `repair.*`), so the grammar/back-test rules above hold for them: **gate verdict of this change** `backtest --route combined --gate`: ok, every split's n and numbers identical to the run before the change (the intents added are context-only and never see a plain `decide`), plus a new `gate.dialog` rule: the committed `grammar/dialog-ru.jsonl` rows (kind `synthetic`: the matrix of every standard reply in every context; kind `authored`: written from the owner's wording; there is no recorded real reply yet) must all decide as labelled, and every `gate.safety` phrase must stay `refuse.emergency` inside every context. These rows are reported apart from the coverage numbers.

Flag `AISIS_CORRECTION=off|shadow|on` in the adapter environment, default `off`:
1. Merge with it off (the old path byte for byte; `/healthz` shows `correction.mode: off`).
2. `shadow`: the engine decides and writes `trace.correction` (`shadow: true`) for every utterance that has a stored second opinion or a no-op cue; it never speaks, opens a context or touches the answer. Read the question rate on real turns before trusting it.
3. `on`: questions are spoken; replies are read by the contexts. Needs the gateway's second opinion (`/asr/hypotheses`) for acoustic triggers; the no-op trigger needs the quasar deploy (hook `noop`/`deviceId`/`rerun`), until then the dialog runs from second opinions and answers a «да» with «Скажите ещё раз: …».
4. Rollback: `AISIS_CORRECTION=off` and restart the adapter (the grammar bundle's `repair.*` intents are inert without an open context).
