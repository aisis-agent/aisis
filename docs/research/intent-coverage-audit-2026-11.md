# Intent coverage audit: what the speaker answers without an LLM, and the plan to reach 90 %

Ticket: alex-mextner/aisis#107. Audit date: 2026-11-16. Owner ask: how many intents exist and what is the hit rate; which language constructs the parsers support; how similar this is to the calendar bot; and finish so the speaker answers **without an LLM, instantly, in 90 % of cases**, with a nightly analysis that adds, consolidates and back-tests intents.

Labels used for every number: **[measured]** counted from the stores/logs/code during this audit, **[hand-labelled]** my own judgement of one utterance, **[replay]** the deployed deterministic code run offline over text, **[INFERENCE]** reasoning, not observed. The comparison of fast models (Cerebras Qwen 3.8 27B, gpt-oss-120b, Sonnet, Haiku 5.5) is ticket #106 and is not repeated here; this audit only fixes what share of traffic those models would ever see.

## 1. Headline

| Question | Answer |
|---|---|
| Intents that exist today | **43** hard-coded intent classes in the Station fast layer (52 if on/off targets are counted separately) + **15** built-in fuzzy catalog templates (3 of them overlap the hard-coded volume/brightness/setpoint) + **67** data-driven dashboard-setting intents (registry snapshot; live count is fetched at runtime) + **2** adapter-only skills (weather with 5 modes, «включи музыку»). About **112 voice-addressable intents**, of which **45 are the core command set**. ha-cli adds **0** (it is a proxy, not a verb set); the brain has no tools. |
| Hit rate, real voice traffic | 96 real spoken utterances 2026-11-07..16 [measured]. Answered without an LLM: **48 / 96 = 50 %** (40–60 % at 95 %); **34 / 79 = 43 %** (33–54 %) of the utterances actually addressed to the speaker. |
| Answered **correctly** without an LLM | **18 / 79 = 23 %** completed correctly, +10 useful clarifications (the slot-less «Громкость.» question), 2 wrong, 4 failed on a Home Assistant outage [hand-labelled]. The brain answered 45 / 79; 34 of those were fine, 11 were wrong or unhelpful. |
| Latency | Local deterministic answers **p50 24 ms**; any path that waits on the ASR corrector costs ~0.85–1.0 s; a Home Assistant action p50 1.3 s; an HA outage 3.0–3.9 s; the brain **p50 2.4 s, p90 3.5 s** to the decision plus ~1.9 s TTS first sentence. |
| What blocks 90 % | Not the lack of a smarter model. 87 % of the addressed traffic is servable deterministically (73 % by real intents, 14 % by a deterministic «Повтори?» for ASR garbage), and 15 of 96 utterances are Whisper hallucinations that must be dropped, not answered. |
| Plan headline | 8 steps, in-sample projection **70–72 / 79 = 89–91 %** (see §7; in-sample, so an optimistic ceiling): deploy what exists, canned wake/greeting/refusal lines, deterministic reprompt, clock, stop synonyms, repeat, music aliases, climate/volume/colour grammar; plus a shared declarative grammar with corpus back-tests and a <300 ms route that does not wait on Haiku or Home Assistant. |
| Cal bot similarity | Same architecture (deterministic first, LLM fallback, abstain over guess), different vocabulary. **Reusable**: the canonicaliser, date/time/duration parser, stemmer, cut-and-remainder slot extractor and the KPI-gate methodology. Not reusable: matcher and bindings (welded to its DB and workflow executor). No licence file; same owner. |

## 2. Sources, method, limits

| Source | Contents | Used for |
|---|---|---|
| Dialog store (`dialogs.sqlite3`: `raw_log`, `dialogs`, `dialog_turns`, `voice_clips`, `clip_feed`, `clip_truth`, `asr_corrections`) | quasar container logs 2026-11-06..16 (5.8k lines), adapter journal from 2026-11-09, 388 dialogs, 153 recorded clips from 2026-11-08 23:47, 157 clip-feed rows, 2 owner truth verdicts | utterances, layer attribution, latency, clip gating |
| Brain history (`router.sqlite3`) | 93 completed turns from 2026-11-01, all provider `claude` | cross-check only |
| Eval corpus (`corpus/intents-ru.jsonl`, 466 rows, hand-written, expectations pinned) | synthetic phrases per domain | design-time coverage, **[replay]** |
| Deployed quasar overlay (image `bg-tv-fireplace-contract-20261113`, `src/backend/*.ts`, 5k lines) and adapter on `main` | the parsers | inventory, grammar, offline replay |
| HyperCalendarBot (HEAD 2026-11-13, read-only) | parser, tests, corpora | comparison |

Not found: any dialog history before 2026-10-23, backups of `dialogs.sqlite3`, or ha-cli request logs (ha-cli only keeps a state cache). Staging runs of `router.sqlite3` hold test turns. So the **real sample is small: 96 spoken utterances in 11 days**, one household. Every rate therefore carries a Wilson interval of roughly ±10 points; read the numbers as a first measurement, not as a stable KPI.

**Utterance definition.** One `Heard:` line from the STT gateway (a final transcript that reached the quasar layer). Excluded: 9 voice turns from the filler-proof test harness, 61 typed `text` dialogs (smoke tests and API commands such as «громче», «стоп»; all answered by the fast layer in 2–10 ms, no information about spoken traffic), brain turns re-recorded from other sources (29 `router` + 9 `adapter` text dialogs, written by test agents).

**Layer attribution.** `Fast intent for '<heard>'` in the quasar log means layer 1 (Station fast intents, which also serve adapter skills such as weather through its helper hop); an `AliceResponse` with no preceding fast-intent line means the brain. Eight early turns lack the log line and were attributed through the dialog store's `brain` field.

**Correctness labelling.** The owner left clip truth for only **2** clips («громкость 5», «включи Lana del Rey», both `corrected`), so I hand-labelled **all 96** instead of sampling 100: for each transcript the intended class and whether the spoken reply (or action) was right, given the transcript text. I did not listen to audio, so a transcript that is a plausible-looking ASR error counts as heard. The labelled snapshot lives in the private corpus directory (`real-voice-2026-11-labelled.jsonl`, not committed: it contains household speech) and seeds the back-tests of §7.5.

**Addressed vs all.** 15 utterances were Whisper hallucinations («Продолжение следует…» ×13, subtitle credits ×2) and 2 were talk to the cat. They are reported separately: **79 addressed utterances** are the denominator for coverage; all 96 are used for latency and for the false-trigger count.

## 3. Inventory

### 3.1 Layers and route order (what can answer without an LLM)

```
Station mic ─► STT gateway (Whisper large-v3-turbo; VAD hangover 800 ms; hallucination/short/noise gates)
  ─► quasar overlay, first match wins (ha-intents.ts matchFastIntentCore):
       1 end-dialog (75 phrases)   2 repeat (11 regex)   3 pending yes/no/slot answer   4 «а на кухне?» follow-up
       5 math   6 TV app (Netflix/YouTube)   7 fireplace   8 media keys (stop/pause/next/previous/resume)
       9 decideFastIntent: settings → exact scorer → fuzzy catalog → entity query
      10 music verbs → adapter /music/play   11 ASR repair (Haiku via adapter, ≤1.2 s)   12 shadow classifier (log only)
  ─► adapter _process_turn (adapter.py:430-452): empty → «Повтори?», calc.evaluate, voice_skills (weather, nameless music)
  ─► brain: claude-sonnet-5-5, no tools (user_prompt.CAPABILITIES tells it the speaker already ran actions)
  ─► error: «Не удалось получить ответ ChatGPT…»
```

Observed routing artefacts [measured]: the weather answers on 2026-11-08 and 11-16 arrive as quasar `Fast intent` lines in 124–188 ms (adapter skill); `parseWeatherAsk` in the quasar overlay is **not imported anywhere** (parser + tests only).

### 3.2 Layer 1: Station fast intents (43 classes)

| Group | Intents | Slots | Examples (corpus) |
|---|---|---|---|
| End/dismiss | stop, dismiss, thanks, no (75 phrases) | none | «хватит», «спасибо», «ничего» |
| Repeat | repeat.answer, repeat.command, repeat.any (11 regex) | none | «повтори», «ещё раз» |
| Dialogue state | clarify.yes/no/home/music; room follow-up | room | «да», «а на кухне» |
| Math | `+ − × ÷`, digits or numeral words | numbers | «посчитай 12+12», «два плюс семь» |
| TV / video | netflix, youtube, fireplace | app | «включи камин» |
| Media keys | stop, pause, resume, next, previous (43 key words) | none | «пауза», «дальше» |
| Volume | step up/down, set (words or digits, ≤10 = level, >10 = %), max, min, query | number | «громче», «громкость на пять», «какая громкость» |
| State | state report, entity/sensor query (10 measures), settings query | room, measure | «что включено», «температура в гостиной» |
| On/off targets | light by room / all, climate by room, люстра, глобус, гирлянда, чайник, телевизор, проектор (9 rules) | room, on/off | «выключи свет на кухне», «включи кондей наверху» |
| Set values | light brightness 1–100, climate setpoint | number, room | «яркость пятьдесят», «кондиционер 23» |
| Music | play by artist/track/album/playlist/radio, bare band name | query, mediaType | «включи джаз», «играй Sex Pistols» |

Vocabulary [measured from code]: **6 rooms / 9 spoken keywords** (+7 whole-home words), 6 device concepts + generic light (6 words) + AC (5 words), 2 TV apps + 1 fixed video, 61 distinct command-verb forms in the union of the lists (on/off 19, set 11, music 21, volume step 12, settings 14; the lists overlap), 42 numeral words (nominative only), 37 politeness fillers, 21 question words that block commands. Mutating actions need score ≥ 0.9 (exact) or 0.8 (`auto` catalog templates: volume, brightness, setpoint, settings); 0.5–0.9 asks «Ты про X?»; confirmed phrases are remembered. A **settings** layer (67 voice-enabled dashboard settings: 25 bool, 18 number, 14 enum, 2 clock, 6 text-pointer, 2 action) is data-driven and lets the dashboard grow voice commands without code.

Eval corpus replay [replay, 466 rows]: **325 rows (69.7 %)** are handled before the brain, 138 (29.6 %) reach it, 3 get a reprompt. Brain-bound by domain: timer 24, general question 23, partial/negative 20, weather/time 11, comms 12, small talk 11, edge 11, music 7, unknown device 6, climate 5, media 4, no-enumeration 4. (The corpus labels 182 rows `llm`, but 14 are already served by earlier stages in production, 17 are music candidates, 12 are weather phrases whose parser is unwired.)

### 3.3 Layer 2: adapter skills (main, `services/yandex-station/code/`)

| Skill | Where | Trigger | Replies | Tests |
|---|---|---|---|---|
| Arithmetic | `calc.py:123` | optional question prefix, alternating numbers and operators, numerals up to «тысяча», Decimal-exact | the number; «На ноль делить нельзя.» | **none** (duplicate of quasar `evaluateMath`) |
| Weather (5 modes: now, tomorrow, week, umbrella, dress) | `voice_skills.py:78` | whole utterance ≤ 8 tokens made only of weather vocabulary (57 stems, 37 words, 16 lead-ins), 7 decision branches | weather-cli stdout verbatim; failure «Не получилось узнать погоду…» | 22 positive / 19 negative phrases |
| «включи музыку» (nameless) | `voice_skills.py:122` | ≤ 8 tokens, play verb + generic noun, nothing else | «Включаю.» / «Музыка уже играет.» / failure lines | 7 positive / 9 negative |
| Helpers for layer 1 | `adapter.py:732,756,711` | `/music/play`, `/music/control` (5 actions), `/asr/correct` | – | none |
| Reply tidy | `reply_guard.py` | post-filter on brain text; only canned line «Повтори?» | – | none |

Totals: **3 utterance skills, 13 trigger rules.** No unit conversion, no clock/date, no timer/alarm, no greeting/end-dialog in Python; `hacli.py` has 0 verbs (health, states, template, service-call proxy with a 2 s client timeout and a queue for HA outages). Branches: `weather-via-cli` (uncommitted) widens weather to 8 rules (wind, rain, bare temperature) and adds an install script and a quasar overlay; `router-three-routes` and `fast-llm-bench` have docs/data only. The `claude-primary-api` branch (old directory layout) carries the brain and an answer-length classifier, not skills.

### 3.4 Layer 3: brain and error replies

`claude-sonnet-5-5`, three keys with failover, ChatGPT browser bridge as last reserve; per-attempt 20 s, total ≤ 310 s; no tools, so it cannot act. Its persona prompt says that if a command-looking phrase reached it, it was misheard and must re-ask. Canned lines in the stack: «Повтори?», «Не расслышал. Повтори, пожалуйста.», «Не удалось выполнить команду умного дома.», «Умный дом перезагружается…», «Это я пока не умею.» (brain-authored).

### 3.5 Unit-level duplicates and orphans

- Math exists twice (`calc.py`, `evaluateMath`), only the quasar copy is tested. Weather parsing exists twice (`weather-parse.ts` unwired; `voice_skills.parse_weather_ask` live).
- `parseBareMusicName` (strict, ≤ 3 words) mis-reads subtitle credits as band names (§4, cluster 1).

## 4. Real-traffic hit rate

### 4.1 Which layer answered (96 spoken utterances, 2026-11-07..16)

| Layer | All 96 | Addressed 79 |
|---|---|---|
| Layer 1 (Station fast intent, incl. adapter weather/math) | 48 (50 %) | 34 (43 %) |
| Brain | 48 (50 %) | 45 (57 %) |
| Error text instead of an answer | 4 (inside layer 1) | 4 |

Besides these 96, **153 clips** were recorded from 11-08 23:47 on; **91 (59 %)** never reached the intent layer: 62 too short, 24 blacklisted as hallucination, 3 flat noise, 2 weak filler. So the intent layer is fed 41 % of what the microphone captures and the gates already work; the 15 hallucinations that did arrive are the leak.

### 4.2 Correctness of the 96 [hand-labelled]

| Layer | ok | clarify | wrong | failed |
|---|---|---|---|---|
| Layer 1 (48) | 18 | 10 | 16 | 4 |
| Brain (48) | 34 | 0 | 14 | 0 |
| **Addressed only (79)**: L1 34 | 18 | 10 | 2 | 4 |
| **Addressed only (79)**: brain 45 | 34 | 0 | 11 | 0 |

- Addressed accuracy 52 / 79 = **66 %** ok, **78 %** counting clarifications (CI 55–75 % / 68–86 %).
- Layer 1 **precision** is good where it fires on real commands (stop ×3, light ×5, volume, math, weather all right; one wrong device) but it **spoke on 14 of the 15 hallucinations**: «Продолжить воспроизведение?» 13× (older blue build; the 11-13 build no longer matches it, [replay]) and a track named after a subtitle credit. In the current deployed code the offline replay still maps 2 of the 15 to music intents (`music_bare`, `music_play`).
- Wrong brain answers (11 addressed): «включи музыку» ×2 and «включи Sex Pistols» ×3 refused with «нет доступа к музыке»; «который час» (brain has no clock); «повтори» answered with «повторить что?»; «урок из 5» (ASR for «громкость 5») asked back; cooling mode set in words refused; a colour request clarified only; ASR error on a confirmation answered as «что распустить?».
- The four layer-1 failures are the 2026-11-09 Home Assistant outage («Включи телик», «Который час», «Какая погода», «Дальше» all answered «Не удалось выполнить команду умного дома» after 3.0–3.9 s) and one AC command that failed and succeeded 40 s later. «Который час» and the weather question do not need Home Assistant at all.
- One **wrong device**: «выключи свет на спальне» turned off a living-room chandelier (room resolution; known, ticket #103).

### 4.3 Latency by layer (Heard → decision) [measured]

| Path | n | p50 | p90 | Note |
|---|---|---|---|---|
| Layer 1, local exact (stop, math, volume, yes/no) | 7 | **24 ms** | max 308 ms | no network |
| Layer 1, hallucination false triggers | 14 | 15 ms | 122 ms (one outlier 9.7 s) | wrong answers are fast |
| Layer 1, slot-less «Громкость.» clarification | 9 | 960 ms | 1.03 s | waits for the Haiku ASR corrector, which times out after ~850 ms (log: `ASR correction failed … 852 ms`) |
| Layer 1, weather via adapter skill | 2 | 156 ms | 188 ms | |
| Layer 1, light on/off executed through Home Assistant | 4 | 1.35 s | max 1.78 s | HA REST round trip dominates |
| Layer 1, state query / climate | 2 | 2.7 s / 2.4 s | – | |
| Layer 1, HA down | 5 | 3.0 s | max 3.85 s | spoken error |
| Layer 1, other (confirm question, music, pause, mis-resolved light) | 5 | 1.35 s | – | |
| **Layer 1 total** | 48 | **654 ms** | **3.0 s** | |
| **Brain** | 40 | **2.44 s** | **3.5 s** (max 5.1 s) | plus first TTS sentence p50 1.9 s (n = 26): **~4.3 s to first audio** |

A spoken reply from the deterministic layer also pays TTS first-sentence time (p50 1.9 s, n = 37), so «instant» requires earcon-only or prepared audio (§7.4). A filler («Одну секунду.») fired on 26 turns. The 800 ms VAD hangover and STT final time precede all of this and are not in the table.

### 4.4 Hit rate by intent class (79 addressed utterances)

| Intent class | n | Answered by | Right | Comment |
|---|---|---|---|---|
| Volume | 12 | L1 11, brain 1 | 2 ok + 9 clarify | 9× slot-less «Громкость.»; follow-up capture was empty each time (peak level 35–54, quiet speech) [INFERENCE: the number is lost to endpointing or distance, not the grammar] |
| ASR garbage / fragment | 11 | brain | 11 sensible re-asks | all cost 2.0–3.5 s; none needs an LLM |
| Media stop / pause / next | 10 | L1 6, brain 4 | stop 7 ok, pause 1 ok + 1 clarify, next failed (HA down) | brain answered four stop phrases before the end-dialog stage existed or on noisy ASR |
| Light | 7 | L1 6, brain 1 | 5 ok, 1 wrong device, colour unanswered | |
| Music | 6 | L1 1, brain 5 | 1 ok, 5 wrong | before the nameless-music skill; ASR aliases («6 pistols») |
| Wake-only / name mishear | 5 | brain | ok | canned candidates |
| Weather | 3 | L1 | 2 ok, 1 failed (HA down) | |
| Climate | 3 | L1 2, brain 1 | 1 ok, 1 failed, 1 wrong | mode + setpoint in words not parsed |
| Greeting / thanks / yes-no / «ничего» | 7 | brain 6, L1 1 | ok except one ASR-garbled confirmation | thanks now handled by end-dialog |
| Time | 2 | L1 1 (error), brain 1 | 0 / 2 | no clock skill |
| Math, state query, repeat, home cmd | 4 | L1 3, brain 1 | math ok, state ok, repeat wrong, TV failed | |
| Open conversation / knowledge / advice / policy | 9 | brain | 9 ok | the real LLM remainder |

### 4.5 Top clusters of brain-handled, unanswered, wrong or failed utterances

Real clusters first (R, n of 79 addressed; the hallucination and cat clusters use all 96), then clusters of the 138 brain-bound corpus rows (S, share of the 466-row corpus). «Gain» is the share of traffic a deterministic intent takes from the brain or turns from wrong/failed into correct; gains are not additive for clusters that overlap.

| # | Cluster (pattern) | n | Today | Deterministic handler | Gain |
|---|---|---|---|---|---|
| 1 | R Whisper hallucination: «Продолжение следует…», subtitle credits | 15 / 96 | L1 spoke 14×, brain 1× | extend gateway blacklist; require wake word or active dialog for L1 | removes 15.6 % false triggers |
| 2 | R «Громкость.» slot-less, follow-up lost | 9 | L1 clarify (0.96 s) | skip Haiku for incomplete-slot, accept «громкость» + level in one breath, re-open mic with longer window | 11.4 % complete |
| 3 | R ASR garbage («Что со стерлицей», «Голи со спортом», «Я лисен»…) | 9 | brain, 2.4 s | `reprompt` tier: «Не расслышал» without LLM | 11.4 % |
| 4 | R Wake-only / mis-heard name («Алиса.», «Тариса.», «Лисы.») | 5 | brain | canned «Слушаю.» | 6.3 % |
| 5 | R Fragments, cut-off, bad confirmation («Алиса, включи.», «Распущу.») | 3 | brain | reprompt with the pending question repeated | 3.8 % |
| 6 | R Greeting, how are you | 2 | brain | canned set | 2.5 % |
| 7 | R Thanks, «ничего», «не надо» | 3 | brain (L1 after fix) | end-dialog (exists) | 3.8 % (deploy) |
| 8 | R Named music + ASR alias («Sex Pistols» / «6 пистолей») | 3 | brain «нет доступа» | music play + Latin↔Cyrillic alias table from the library | 3.8 % |
| 9 | R «Включи музыку» | 2 | brain «нет доступа» | nameless-music skill (on main) | 2.5 % (deploy) |
| 10 | R Stop synonyms («остановись, не разговаривай», «стоп» in noise) | 4 | brain | stems остановись/замолчи/не разговаривай; substring tolerance | 5.1 % |
| 11 | R «Который час» | 2 | HA error / brain | clock/date skill, local | 2.5 % |
| 12 | R «Повтори» | 1 | brain «повторить что?» | repeat-last-answer in layer 1 (exists, needs history) | 1.3 % |
| 13 | R Climate mode + setpoint in words; AC failure | 2 | brain / error | mode words (обогрев, охлаждение) + setpoint numerals | 2.5 % |
| 14 | R Light colour / white («люстру белый цвет») | 1 | brain | colour/colour-temperature slot | 1.3 % |
| 15 | R Volume with ASR error («урок из 5») | 1 | brain | phonetic fuzzy on the noun, reprompt-with-guess | 1.3 % |
| 16 | R Wrong device for a room («на спальне») | 1 | L1 wrong | room resolution (#103) | correctness |
| 17 | R HA outage: weather, time, next-track answered with an error | 4 | L1 error 3–3.9 s | do not route no-HA intents through HA; optimistic ack; cached state | correctness + 3 s |
| 18 | R Talk not addressed to the speaker (cat) | 2 / 96 | brain answered | no answer without wake word outside a dialog | removes false replies |
| 19 | R Emergency / unsupported action («Полиция») | 1 | brain | canned «Не умею, звони 112» | 1.3 % |
| 20 | R Insult / opinion about a politician | 1 | brain | canned policy line | 1.3 % |
| 21 | R Open chit-chat («кто лучше…») | 3 | brain | lite LLM (#106) | – |
| 22 | R Knowledge («что такое фотосинтез») | 2 | brain | strong brain | – |
| 23 | R Follow-up to the last answer | 1 | brain | strong brain with context | – |
| 24 | R Relationship advice | 1 | brain | strong brain | – |
| 25 | S Timers, alarms, reminders («поставь таймер на пять минут») | 24 / 466 | brain | timer grammar, durations, existing clock parser | 5.2 % of corpus |
| 26 | S General questions | 23 | brain | strong brain | – |
| 27 | S Negation and narrative («не включай свет», «я включил свет») | 20 | brain (and a live risk: «не включай свет» likely executes ON [INFERENCE]) | negation guard → no-op | 4.3 % + safety |
| 28 | S Weather/time variants | 11 | brain / unwired parser | wire the existing weather parser; clock | 2.4 % |
| 29 | S Calls, messages, shopping, unknown device («позвони маме», «включи вытяжку») | 18 | brain | canned «Это я пока не умею.» / «Такого устройства нет.» | 3.9 % |
| 30 | S Small talk, edge cases, music, climate, media, no-enumeration | 37 | brain | canned set, music, mode words | 7.9 % |

Fewer than 30 distinct real patterns exist in 11 days of one household; the table fills the rest from the corpus. This is why the plan relies on a nightly loop rather than on this snapshot.

## 5. Language constructs

Verdicts are for the quasar layer unless noted [read from code; the negation and compound items are [INFERENCE] from code, not executed].

| Construct | Verdict | Evidence |
|---|---|---|
| verb + object + room | **supported** | order-free («свет в спальне выключи»); multi-room lists with «и»; default room Гостиная |
| numerals as words | **partial** | 42 nominative words and compounds («двадцать три», «двести пятьдесят четыре»), «полтора», «N с половиной»; no oblique cases («на двух», «пяти»), no ordinals, no «одна/одно»; ASR-damaged numerals repaired fuzzily; adapter calc handles up to «тысяча» |
| digits, `%`, units | supported | «50%» rewritten, unit words (процент, градус, деления) |
| relative values («на 10 процентов», «на две единицы») | **missing / wrong** | only ±1 step (±2 with «намного»); «прибавь громкость на 10» is read as the absolute level 10 → maximum [INFERENCE from `matchVolume`] |
| «тише / громче / потише» | supported | 6 up and 6 down stems |
| times, durations, timers, alarms | **missing** | the scorer returns null for «напомни / поставь таймер / будильник» on purpose; a clock parser exists but serves only quiet-hours settings; «семь тридцать утра» documented as unparsed |
| named entities | partial | 6 rooms, 6 devices hard-coded (not from HA names); artists forwarded to Music Assistant (adapter validates); entity query reads arbitrary HA names (Latin→Cyrillic, stems, phonetic) |
| negation | **missing** | «не» only as a yes/no answer; «не включай свет» likely matches «включай» |
| «и» conjunction, several commands | **missing** | split on `. ! ? …` only, first non-LLM sentence wins; «включи свет и выключи чайник» sets on and off, on wins |
| polite particles | supported | 37 fillers, «-ка», «пожалуйста» |
| wake words / name | partial | «алиса», «яндекс» are filler tokens; no other assistant name |
| cases / declension | partial | stem-prefix matching (≥ 3 chars), settings use 49 endings; no lemmatiser; numerals undeclined |
| ASR error tolerance | **supported (strong)** | edit distance with transposition, phonetic key (ы/и, о/а, …), trigram tie-break, token gluing («гром кость»), fuzzy numerals, cutoff 0.6; Haiku repair for short undecided phrases, 0.9 act / 0.6 confirm |
| follow-up / anaphora | partial | «а на кухне?» only after a device command; no «выключи его», «ещё ярче» |
| questions / state | partial | volume, state list, sensors; no setpoint read, no time/date |
| confirmation policy | supported | execute ≥ 0.9; confirm; learned ambiguity file |
| scenes, floors/ordinals, exclusions, unknown devices, curtains/fans | missing | all go to the brain |

Ten sketched grammar gaps (for the plan): relative `(прибавь|убавь) <noun> на <N> [unit]`; negation guard `не <verb>`; compound clauses `<cmd> (и|,|потом) <cmd>`; timers `(поставь|заведи) таймер на <duration>` / `будильник на <clock>` / `через <duration> <cmd>`; declined numerals and ordinals; temperature words `теплее|холоднее [на N градус*]`; scenes `режим <name>`; anaphora `выключи его`; quantified rooms and exclusions `везде кроме <room>`; time/date and sensor questions.

## 6. Comparison with HyperCalendarBot

Read from the repo (HEAD 2026-11-13); counts are static greps.

| Aspect | Calendar bot | Speaker (quasar + adapter) |
|---|---|---|
| Approach | hybrid, deterministic first: exact phrase → whole-message regex → typo-repaired pass → typed slot bindings; ambiguity throws and the LLM agent takes over; no confidence score | tiered: exact scorer → fuzzy catalog → entity query → Haiku repair; scores and thresholds, confirm before ambiguous writes |
| Contract | **abstain over guess**; whole-message match; after a rule wrote, the message is never re-sent to the LLM | execute only at score ≥ 0.9; otherwise ask or fall through to the brain |
| Core size | ~1.4k lines of parser (event-phrase 425, date-time-words 277, canonical-input 269, matcher 303, bindings 614); 13.2k in the intent directory incl. seeds | ~4.8k lines TS in 12 modules (ha-intents 1.4k, catalog 0.65k, entity-index 0.9k, settings 0.7k, fuzzy 0.27k, …) + ~0.75k Python (calc, voice_skills, asr_correct, reply_guard) |
| Language | RU+EN, text-first, digits | RU, speech, numerals as words, ASR noise |
| Date/time | rich: today/tomorrow, accusative weekdays, «через N дней», absolute dates, 24h/am-pm/day-part, ranges, durations, per-user tz with DST skip/overlap refusal; **no** word numerals, **no** «в половине шестого», **no** recurrence («каждый вторник» goes to the LLM) | almost none (quiet-hours clock only) |
| Entities | title = remainder after cutting slots; one @invitee; no place NER; event reference by stem | rooms/devices from small tables; artists via the music backend |
| ASR/typo | ≤ 1-edit repair of a fixed vocabulary, filler strip, span map | strong phonetic fuzzy + Haiku repair |
| Tests | 564 test files, 8,188 declarations; intent dir 42 files / 803; table-driven with frozen clock and refusal tables; 755 synthetic turns; **552 real scrubbed messages with two-annotator labels, dev/heldout split**; benchmark harness (18k matrix cells) and a committed KPI gate (real coverage 85.7 %, precision 100 %) | 145 Python test functions (none for calc, asr_correct, reply_guard, hacli proxy); a 466-row hand-written corpus (575 + 319 noisy rows in a branch); no real-traffic corpus before this audit; no gate |
| Voice | transcripts bypass the rules and go straight to the agent (grep) | voice is the main path |

**Similarity.** Same shape, different vocabulary: roughly the same layering (deterministic before LLM, whole-utterance discipline, typed slots) with almost no lexical overlap. The cal bot is better engineered where the speaker is weakest (time grammar, refusal discipline, labelled real corpus, KPI gate); the speaker is better where the bot is weakest (spoken numerals, ASR noise, confidence tiers, follow-ups).

**Reusable parts** (TS, mostly pure; one dependency, `@date-fns/tz`, ~700 lines): `canonical-input` (ё-fold, filler strip, span map, safe typo repair), `date-time-words` (date, time, duration with tz-safe arithmetic: exactly what timers and alarms need), the suffix stemmer from `event-reference`, the cut-one-slot/remainder pattern with «two matches → refuse», `day-references` as an independent validator, and the corpus schema, matrix and gate methodology. **Not reusable as-is**: the matcher and bindings (tied to SQLite rule rows, zod and the workflow executor), the real corpus (Telegram text, pseudonymised, not ASR output), duplicated month/weekday tables in four or five files. **Blockers**: no `LICENSE` (private repo, same owner, so a decision, not an obstacle) and Bun-style TS versus the quasar container's Node/tsx CommonJS [pure modules should run in both; not tried]. **Both lack** declined spoken numerals, negation and compound handling; a shared `numerals-ru` module would be new work for both.

## 7. Plan to 90 % without an LLM, instantly

### 7.1 Target and definitions

- **No-LLM answered** = the layer-1 route produced the spoken reply or the action without a model call. **Task completed** = the user's intent happened. Reprompts and clarifications count for the first but not the second; both are reported.
- Target: ≥ 90 % no-LLM answered on addressed utterances, ≥ 80 % task completed, 0 false actions on hallucinated/unaddressed speech, deterministic p95 < 300 ms.
- The brain stays for open knowledge, advice and follow-ups; the lite model of #106 may take banter, never actions.

### 7.2 Ordered steps and expected gain (79 addressed utterances; in-sample)

| # | Step | New or exists | utterances | cumulative no-LLM |
|---|---|---|---|---|
| 0 | baseline: layer 1 in production | – | 34 | 43 % |
| 1 | **deploy and verify what exists**: end-dialog (stop, thanks, «ничего»), music play and bare names, media keys [replay: 42 / 79 would already be answered by the 11-13 build] | exists | +8 | 53 % |
| 2 | **hygiene first, no coverage**: extend hallucination blacklist; no layer-1 or brain answer to non-addressed speech without wake word or active dialog; never wait on Haiku for incomplete-slot or exact-match phrases; no-HA intents (weather, time, math) bypass HA | new | (−15 false triggers) | – |
| 3 | canned wake / greeting / thanks lines (prepared audio) | new | +7 | 62 % |
| 4 | deterministic `reprompt` for ASR garbage and fragments, repeating the pending question | new | +12 | 77 % |
| 5 | clock/date skill (1); stop-synonym stems (2); repeat-last-answer history (1); deploy the nameless-music skill already on main (2) | new + exists | +6 | 85 % |
| 6 | grammar steps: climate mode + setpoint words, light colour, volume ASR-noun fuzzy, declined numerals | new | +3 | 89 % |
| 7 | canned «не умею» (calls, messages, emergency, insult) | new | +2 | 91 % |
| 8 | timers/alarms, negation guard, compound commands, relative values: **0 real utterances in the sample, 49 corpus rows**; build for the traffic the sample does not show | new | (protects the 90 % out of sample) | – |

Remaining 7–9 / 79 (≈ 9–11 %) go to a model: 3 chit-chat (lite), 4 knowledge/advice/follow-up (strong). **In-sample caveat:** the steps were derived from the same 79 utterances, so 89–91 % is a ceiling with a 95 % interval of roughly 80–94 %. The honest check is held-out traffic: the 466-row corpus now, and each night's new utterances (§7.5). Step 4 inflates the headline (12 reprompts are not completed tasks), which is why §7.1 requires a separate completion rate.

### 7.3 Shared declarative intent grammar

**Why.** Intents today are hard-coded TS arrays and Python regex lists in two runtimes, the same numerals/stems/fillers are re-implemented three times, and there is no pinned example per intent. The settings layer already proves that data-driven intents work (67 intents added without code).

**Runtime decision.** One engine in the **quasar TS layer** (lowest latency: the adapter is a second WebSocket hop and a process boundary; the cal bot is TS too, enabling shared modules). Python skills become *executors* behind HTTP, as `/music/play` already is; `voice_skills.py` and `calc.py` migrate to grammar files plus executor stubs, and the adapter keeps only the brain. A small conformance corpus is run against both engines until the Python path is retired, so there is never a silent drift.

**Format** (YAML; compiled at build time to a JSON bundle with precompiled automata, no regex at runtime):

```yaml
version: 1
locale: ru
lexicon:                      # shared, generated where possible
  rooms:   {$from: ha.areas}  # stems + synonyms from Home Assistant, not hard-coded
  numbers: {$module: numerals-ru}      # digits, words, all cases, «полтора», «с половиной»
  fillers: [пожалуйста, ну, -ка, алиса, ...]
  wake:    [алиса, яндекс, брачо]
intents:
  - id: volume.set
    tier: execute              # execute | confirm | clarify | query | canned | reprompt | brain
    patterns:                  # token grammar: ?optional  |alt  {slot:type}  stem*  ~fuzzy
      - "{set_verb}? громкост* {value:number<=10|percent}"
      - "{value:number<=10} громкость"
    slots:  {value: {type: number, range: [0, 100], required: true, ask: "Какую громкость поставить?"}}
    guards: [not_negated, not_question]
    action: {executor: speaker.volume, args: {level: "$value"}}
    reply:  {kind: earcon}             # earcon | canned:<id> | template | none
    examples:                          # become positive back-tests
      - "громкость на пять"
      - {say: "сделай громкость семь", slots: {value: 7}}
    negatives: ["громкость музыки в клипе", "не делай громче"]
  - id: timer.set
    patterns: ["(поставь|заведи|засеки) таймер на {duration}", "через {duration} {then:intent}"]
    slots: {duration: {type: duration, min: 5s, max: 24h}}
    action: {executor: timer.start}
  - id: smalltalk.wake
    tier: canned
    patterns: ["{wake}"]
    reply: {kind: canned, id: wake_listening}   # prepared audio, no synthesis on the hot path
```

Slot types: `number` (digits, nominative and oblique words, fractions), `delta` (`на N`, `на N процентов`, `чуть`), `percent`, `duration` and `clock` (ported from the cal bot's `date-time-words`), `room`/`device`/`app` from the registry, `artist` from the music library with a Latin↔Cyrillic alias table, `enum`, `intent` (for «через пять минут выключи свет»). Cross-cutting passes, not per-intent rules: wake-word strip, ё-fold, filler strip with span map, negation detection («не», «нельзя»), clause split on «и / , / потом» when each side has a verb and an object, polite particles. Matching is whole-utterance after strip (the cal-bot contract: two readings → abstain), exact first, then fuzzy with the existing thresholds. Every intent declares its **tier**, so the confirm/clarify policy lives in data, not in 1.4k lines of TS.

### 7.4 Latency budget for the deterministic route (< 300 ms p95, transcript → first audio or earcon)

| Stage | Budget | Today |
|---|---|---|
| Normalise, strip, tokenise | ≤ 2 ms | ~1 ms |
| Match (precompiled automaton + fuzzy only on failure) | ≤ 10 ms exact, ≤ 25 ms fuzzy | 3–24 ms |
| Slot resolution from a cached registry | ≤ 5 ms | registry refresh 60 s |
| ASR repair via Haiku | **removed from the path**; run only in the background for logging and learning | 0.85–1.2 s |
| Execution | acknowledge immediately (earcon), dispatch HA or MA asynchronously, verify state afterwards; failure spoken as a follow-up | 1.3 s synchronous |
| Reply audio | earcon or prepared canned audio (the Bracho announcement cache is the precedent); never first-sentence TTS on this path | 1.9 s TTS |
| Total, no-HA intent | **≤ 60 ms** to decision, ≤ 120 ms to audio start | 24 ms decision, +1.9 s TTS |
| Total, HA intent | **≤ 300 ms** to earcon | 1.3–3 s |

Two measurable consequences: weather, time and math never depend on Home Assistant, and a spoken weather reply is the only case that legitimately needs live TTS or a prepared template (weather answers will be slower than 300 ms and are reported separately).

### 7.5 Automatic back-tests and the nightly loop

**Corpus.** `corpus/real-voice-YYYY-MM.jsonl`: `{text, truth_intent, slots, source: real|synthetic|asr_variant, labeller: owner|annotator|llm, ts, layer_at_time, verdict}`. Sources: (a) layer-attributed utterances extracted from the dialog store as in §2 (the extraction logic of this audit becomes a script); (b) owner verdicts from the clip feed (`clip_truth`: «=», correction text, «шум»); (c) **ASR variants for free**: `asr_bench` holds several engines' transcripts per clip and `asr_corrections` holds raw→corrected pairs, so every real command gets noisy siblings; (d) the hand-written 466 rows (+575 / 319 in the branch) as `synthetic`. Heldout split frozen by clip id.

**Back-test runner** (the existing `run_corpus.mts`, extended): per row, match with the production engine and a fixture registry; report coverage, **precision** (intent and slot exact), abstain correctness on negatives, p50/p95 in-process latency, per-intent and per-source breakdowns, and a confusion list. A **KPI gate** file in the cal bot's style: precision 100 % on intents that move hardware, no regression of coverage on the heldout set, no new false match on hallucination/negation rows, p95 < 25 ms in-process; it runs in CI on every grammar change.

**Nightly job** (design; nothing is implemented here):
1. 03:30 local: extract the day's utterances and layer attribution; refresh the corpus; compute the §4 report (coverage, correctness, latency, top clusters).
2. Cluster brain-handled, reprompted and failed utterances (ASR-normalised; an LLM may adjudicate and propose, offline, so it costs the household nothing at runtime).
3. **Proposals, not auto-merge:** new example phrases for existing intents, new intents (YAML diff), consolidations (two intents with the same executor and overlapping patterns), rejected phrases for the negatives list. Each proposal is back-tested against the full corpus; only those that raise heldout coverage with no precision loss are kept.
4. Ambiguous or new-class utterances go to the existing Telegram clip feed as one-tap questions; answers become `labeller: owner` rows.
5. Output: aggregate numbers and the YAML diff as a pull request (no raw household utterances in git), plus a message with the day's hit rate.

**Schedule.** A systemd user timer is the established precedent (`aisis-whisper-prompt.timer`, every 6 h). I found no scheduling facility in the omp docs I searched (only reviewer cadences), so the request «schedule in omp» is unconfirmed: Main should state which omp mechanism is meant; a systemd timer plus an agent step in the job covers it either way.

### 7.6 Rollout and risk

1. Ship the corpus extractor and the back-test runner first (read-only; this audit is its first run).
2. Hygiene (§7.2 step 2) behind a `shadow` flag that logs what it would drop; compare against owner replies for a week.
3. Move intents to the grammar one domain at a time, starting with volume and media keys (all real traffic, pure local), asserting identical decisions on the 466 rows and the real corpus before switching.
4. Risks: a small sample hides rare commands (hence the nightly loop); canned lines and reprompts can mask an ASR problem (track the reprompt rate separately); the 9× lost volume follow-up and the quiet-speech clips point at mic gain or endpointing, which no grammar fixes; actions on hallucinated speech are the one safety-relevant defect found.

## 8. Not verified

- All labels are mine; 2 owner truth rows exist. I did not listen to audio.
- Latency covers Heard → decision from container logs. STT time, endpointing and TTS audio start are not in the table; first-audio figures are decision plus the logged first-sentence synthesis, not measured at the speaker.
- The replay runs the deployed text parsers statelessly (no pending question, no repeat history, no adapter match for music): it shows what the code *can* decide, not what a live turn does. The 13 hallucination triggers occurred on the older build and are absent from the current replay.
- «не включай свет» and compound commands are read from code, not executed. The prod container was not touched; the replay used a throwaway container without network.
- Settings count (67) is a registry snapshot; the live registry may differ. The cal bot's «каждый вторник» handling outside the LLM and wizard was not found by grep, not exhaustively read.
- Fast-model quality and latency are out of scope here (ticket #106).
