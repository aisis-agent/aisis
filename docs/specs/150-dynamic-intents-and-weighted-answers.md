# Dynamic intents: capabilities, home status and weighted answers

Ticket [#275](https://github.com/alex-mextner/aisis/issues/275). Builds on the grammar route ([`services/yandex-station/code/intent_grammar/README.md`](../../services/yandex-station/code/intent_grammar/README.md)), the repair dialog ([spec 140](140-local-voice-speaker.md), #182) and the layers in [`docs/architecture/voice-stack-layers.md`](../architecture/voice-stack-layers.md). Wording rules of this spec are the owner's: the speaker is microphones, a loudspeaker and a wake-word spotter, nothing more; the stack decides, in layers (grammar route, adapter, quasar layer, brain); answers are neutral and impersonal, they never name an actor («колонка включит», «я включу»), and no Alice skills exist.

## 1. What the stored clips show

Two owner clips of 2026-11-22 are the evidence; both went to the language model and both answers were wrong in kind.

| Heard (after recognition) | Layer that answered | Answer | What was wrong |
|---|---|---|---|
| «Тоже неплохо. А что ты умеешь?» (a reaction to the model's own «Бодро… А у тебя как?», then the question) | grammar abstained, quasar did not handle it, brain (`claude`, no tools, ~1.9 s) | «Болтаю, отвечаю на вопросы и язвлю по делу. Свет, климат, музыку и погоду колонка включает сама, только скажи.» | A fixed persona sentence (`user_prompt.CAPABILITIES`), not data; names the speaker as the actor; promises without knowing what is configured or reachable. |
| «Какой кондиционер включил?» (the owner meant «включён», the recogniser wrote the past tense) | the same | «Я кондиционер не включал, этим занимается сама колонка. Скажи команду ещё раз, например «включи кондиционер».» | The question was a state question; the answer denies an action nobody asked about, names an actor, and asks for a repetition. At that moment both air conditioners were on in heat mode (read from the cached Home Assistant states: `last_changed` 10:47 and 10:48 local, i.e. more than an hour). |

An earlier clip of 2026-11-20 («Кондиционер на втором этаже.») was answered by the quasar layer's fixed question «Включить или выключить кондиционер наверху?» (overlay `ac-defer-to-adapter` describes it). The same shape: a short, ambiguous phrase about one device is met with a question although the device's state is known.

The stored data around the first clip: one dialog, user turn 11:48:49 (`conf 0.00, unclear` for the correction view, the ASR correction rejected), assistant turn 11:48:52, trace `via: turn`, `layer: brain`, hook trace `layer: none` (the grammar knew nothing). The gateway stored no second opinion (`clip_hypotheses`) for either clip: the correction engine runs in shadow and the hypothesis store lives 15 s, so no alternatives with weights exist for them. The policy in §6 is designed on the engine's contract, not on observed weights; the first live weights arrive with the shadow trace.

Phrases of the same class that exist in the real corpus: «Какой свет сейчас включен?» (answered by the quasar layer: «Сейчас ничего не включено.», a fixed template over a state read), «Какой статус у коляна?» and «Алиса, какой статус у коляна?» («кондея» heard as «коляна»), «Алиса, который час?» (clock intent, grammar). The private synthetic corpus holds one row for the first clip's class (`что ты умеешь`, expected `llm`).

## 2. Scope

In this change (one pull request, ticket #275):

1. Intent `assistant.capabilities` («что ты умеешь» and its typical phrasings, a leading reaction included) with the dynamic answer of §4.1, topic variant `assistant.capabilities_topic` («что ты умеешь со светом»), and the follow-up «а ещё?».
2. Intent `home.climate_status` (which air conditioner is on, mode, set-point, since when) with the dynamic answer of §4.2, a read-only executor `ha.climate_status`, and the bare-noun phrase («кондиционер на втором этаже») in shadow.
3. The weighted-answer policy of §6 as a specification and an interface (§6.5); its implementation is separate tickets (§9).

Out of scope here: lights, TV, volume and timer status intents on the adapter side (§9), the model's persona text (#273, wording), the Jev path (off by default).

## 3. The existing mechanism and what is reused

| Need | Existing piece | Used as |
|---|---|---|
| Typical phrasings, wake-word forms, fillers | `ru.yaml` lexicon (`wake`, `lead_in`, `fillers`, `rooms`, `ac_words`), whole-utterance stem matching, `examples:` and `negatives:` pinned by `compile --check` | The two new intents are ordinary `tier: query` intents. |
| A dynamic answer | `IntentRouter._adapter_executor` returns text from `clock.say`, `timer.remaining`, `skill.weather` | New executors `skill.capabilities` and `ha.climate_status`, in `ADAPTER_EXECUTORS`. |
| Live state, read-only | `HaActions._all_states` (GET `/api/states` through the shared Home Assistant client, a live read, no cache, entities carry `last_changed`) and `_area_registry` (entity to area) | New read-only methods; nothing is ever sent to a service. |
| Availability of abilities | `IntentRouter.timers`, `.ha`, `.skills` (`Skills.weather_command`), `AISIS_HA_ACTIONS` | Probed per answer: an unconfigured ability is not offered. |
| Routing before the quasar layer | `phase: pre` intents are decided by the hook `/intent/decide` before the container's own handlers | `home.climate_status` is `pre`: the quasar layer cannot answer it with a question or a denial. `assistant.capabilities` is post-phase (the container has no reading for it). |
| Alternatives with weights | adapter `HypothesisStore`, the correction engine (`Correction.stored`, `core.decide`), trace field `correction.nbest`, `trace_view.alternatives_lines` | §6; the debug presentation extends `trace_view`, it adds no second renderer. |

No quasar change is needed: both intents are answered in the adapter (hash-pinned, receipt-bound quasar deploys are not touched).

## 4. The intents and their answers

### 4.1 `assistant.capabilities`

Slots: `topic` (optional: `light`, `climate`, `timers`, `weather`, `music`, `volume`, `tv`, `home`). Executor `skill.capabilities {topic}`. Tier `query`, not acting, so a negation or a question mark changes nothing.

**Typical phrasings** (the grammar's patterns cover each group; the fixture `intent_grammar/grammar/info-ru.jsonl` pins every phrase below and more):

| Group | Phrasings |
|---|---|
| Plain | «что ты умеешь», «что умеешь», «что ты можешь», «что ты можешь делать», «что ты умеешь делать», «что ты вообще умеешь», «а что ты умеешь», «что ты ещё умеешь» (see the follow-up below) |
| Wake word | «Алиса, что ты умеешь?», «Алиса, а что ты можешь?», «Яндекс, что ты умеешь», mishearings of the name from the lexicon (`Малис`, `Лисы`, `Олиса`) |
| Leading reaction (the real clip) | «Тоже неплохо. А что ты умеешь?», «Ладно, а что ты умеешь?», «Понятно. Что ты умеешь?», «Хорошо, а что ты можешь?», «Круто, что ты умеешь делать» |
| Fillers and lead-ins | «слушай, а что ты умеешь», «ну и что ты умеешь», «а что ты умеешь, пожалуйста», «так что ты умеешь» |
| Imperative | «расскажи, что ты умеешь», «скажи, что ты умеешь», «покажи, что ты умеешь», «перечисли, что ты умеешь», «расскажи о своих возможностях», «назови свои команды» |
| Possibilities and commands | «какие у тебя возможности», «какие у тебя есть команды», «какие команды ты знаешь», «какие есть команды», «какие у тебя функции» |
| Asking what to ask | «что у тебя можно попросить», «что можно попросить», «что можно сказать», «что тебя можно спросить», «чем ты можешь помочь», «чем можешь помочь» |
| Help word | «помощь», «справка», «подсказка» |
| Ability question | «ты что-нибудь умеешь», «ты вообще что-нибудь умеешь» |
| Topic | «что ты умеешь со светом», «что ты умеешь делать с кондиционером», «что можно сделать со светом», «что ты умеешь по части музыки», «что ты умеешь с таймерами», «что ты умеешь с погодой», «умеешь ли ты ставить таймеры», «ты умеешь включать музыку» |
| Follow-up (within two minutes of an answer) | «а ещё», «что ещё», «а что ещё», «и что ещё», «ещё что-нибудь»; «что ещё ты умеешь» is the plain question, which continues with the next page while an answer is fresh |

«можешь» is a request, not an ability question: «можешь включить свет» is NOT read here (it stays with the acting intents or the brain); only `умеешь`, `умеете` and the open «что … можешь» form are. «на помощь …» and «помогите» stay with `refuse.emergency` (priority 1000).

**The answer is built from live facts**, never from a fixed list:

* which abilities are configured: timers and reminders (`TimerBook` bound), weather (`weather-cli` found), music, Home Assistant actions (`AISIS_HA_ACTIONS` not off);
* the rooms that really have lights and air conditioners, from the Home Assistant registry and states (rooms named the way the grammar names them: «в гостиной», «на кухне», «на втором этаже»);
* the time of day orders the abilities (evening: light and climate first; morning: weather and the timer first), and a per-station cursor rotates the groups, so the same question twice gives the next group, which is also what «а ещё?» asks;
* one example phrase built from a real room closes the answer.

Answer templates (impersonal and nominal; `{rooms}` is a list in the locative, «в гостиной, на кухне и в спальне», at most four and then «и в других комнатах»; `{ex}` is an example the grammar is pinned to read; the first variant of each is shown, the others are picked at random):

* A page holds three groups, in the order of the hour, each group a nominal clause: light «управление светом {rooms}» / «свет {rooms}: включение, цвет и яркость»; climate «кондиционеры {rooms}: режим и температура» / «управление климатом {rooms}»; timers «таймеры, будильники и напоминания» (plus «(сейчас идёт таймер: осталось 4 минуты)» while one runs); weather «погода на сегодня, завтра и неделю»; music «музыка: любая или по имени исполнителя»; clock «время, дата и счёт»; status «справка о том, что включено и в каком режиме кондиционер».
* First page: «Доступно: {clause}; {clause}; {clause}. Например: «{ex}».» / «Сейчас доступны: …» / «Из команд сейчас работают: …». Example: «Доступно: управление светом в гостиной и на втором этаже; кондиционеры в гостиной и на втором этаже: режим и температура; таймеры, будильники и напоминания. Например: «включи свет в гостиной».»
* Next page (the same question again, or «а ещё»): «Ещё доступно: …» / «Ещё: …»; after the last page: «Это все основные команды. Остальное идёт в обычный разговор.» and the cursor is gone.
* Topic, with real rooms and an example: «Со светом доступно: включить и выключить в гостиной и на втором этаже, поменять цвет и яркость. Например: «включи свет в гостиной».»; «С кондиционерами в гостиной и на втором этаже доступно: включить, выбрать режим и температуру, узнать, какой включён. …»; timers, weather, music, volume, TV and the whole home («Умный дом: …») likewise. An ability question («умеешь ли ты ставить таймеры») gets the topic answer: what is available, with an example.
* Not available: «Таймеры и будильники сейчас недоступны.», «Погода сейчас недоступна.», «Музыка сейчас недоступна.», «Света в умном доме не нашлось.», «Кондиционеров в умном доме не нашлось.» (never «не умею» for a gap in the configuration).

**Graceful degradation.** Home Assistant not answering within 1.5 s (or its registry unreadable): the home abilities are left out of the list and the first page says so once: «Умный дом сейчас не отвечает, поэтому свет и климат недоступны. Доступно: таймеры, будильники и напоминания; …»; a topic about the home answers only that sentence. Home Assistant actions switched off (`AISIS_HA_ACTIONS=off`): the home is not mentioned at all. «Время, дата и счёт» are always available, so the list is never empty. Weather is offered only when weather-cli is configured or found (`Skills.weather_available`, nothing is run), music when the voice skills are bound (the Music Assistant itself is not probed). An executor that fails returns `None`: the old path (brain) answers as before, so the change can only add an answer.

### 4.2 `home.climate_status`

Slots: `room` (optional, the grammar's rooms), `aspect` (`power` default, `mode`, `temp`, `since`). Executor `ha.climate_status {room, aspect}`, `phase: pre`, tier `query`. It reads `climate.*` states and the area registry through the same shared Home Assistant client as the acting executors and sends nothing. No room means every air conditioner of the home (a question about the state, not an action, so the speaker's own room is not assumed).

**Typical phrasings.**

| Group | Phrasings |
|---|---|
| Which is on | «какой кондиционер включён», «какой кондиционер включен», «какие кондиционеры включены», «какой кондиционер сейчас работает», «какие кондиционеры работают» |
| Recognition variants of the same request | «какой кондиционер включил» (the real clip), «какой кондиционер включила», «какой кондей включён», «какой статус у кондея» |
| Is it on | «включён ли кондиционер», «включен ли кондиционер на втором этаже», «работает ли кондиционер», «выключен ли кондиционер», «кондиционер включён», «кондиционер работает», «кондиционеры включены» |
| With a wake word and fillers | «Алиса, какой кондиционер включён?», «скажи, включён ли кондиционер», «слушай, а кондиционер сейчас работает», «подскажи, какой кондиционер включён» |
| What is going on | «что с кондиционером», «что там с кондиционером», «как там кондиционер», «что с кондеем», «какой статус у кондиционера», «состояние кондиционера» |
| Mode | «в каком режиме кондиционер», «какой режим на кондиционере», «в каком режиме кондиционер на втором этаже» |
| Set-point and temperature | «на сколько градусов стоит кондиционер», «на сколько выставлен кондиционер», «какая температура на кондиционере», «сколько градусов на кондиционере» |
| Since when | «как давно работает кондиционер», «сколько работает кондиционер», «давно включён кондиционер» |
| Room named | «кондиционер на кухне включён», «включён ли кондиционер в гостиной», «что с кондиционером наверху» |
| Bare noun (shadow first, §4.2.3) | «кондиционер на втором этаже», «кондиционер наверху», «кондиционер» |

Not read here (the acting intents keep them): «включи кондиционер», «включи кондиционер на втором этаже на обогрев», «выключи кондиционер», «поставь кондиционер на 24», «кондиционер 23», «сделай теплее», «не включай кондиционер», and the outdoor temperature («какая температура на улице»).

**What the answer says** (facts from the entity: `state`, `attributes.temperature` = set-point, `current_temperature`, `last_changed`; the area from the registry):

1. Which units are on or off, per area («на втором этаже», «в гостиной»), by their state (`off` vs a mode).
2. For a unit that is on: mode (`heat` обогрев, `cool` охлаждение, `dry` осушение, `fan_only` вентиляция, `auto` автоматический), set-point, and since when. Since is the time the entity has been in its current state (`last_changed`; a change of mode also resets it, so it reads «в этом режиме», and the spoken form is rounded so the difference is not heard).
3. For «температура»: both numbers, so that the answer is right under either reading: «Кондиционер в гостиной выставлен на 30 градусов, сейчас в комнате 19,8.»

**Rounding of the duration** (`since = now - last_changed`, local time; the spoken value is always an approximation and says so):

| Elapsed | Spoken |
|---|---|
| under 1 min | «меньше минуты» |
| 1 min | «около минуты» |
| 2 min | «пару минут» |
| 3 to 9 min | «около N минут» (N as it is) |
| 10 to 54 min | «около N минут», N the nearest multiple of 5 («около 20 минут»); 53 and 54 are «около часа» |
| 55 to 69 min | «около часа» |
| 70 to 119 min | «больше часа» |
| 2 h and more, the same day, switched before 10:00 and asked from 12:00 on | «с утра» |
| 2 to 5 h otherwise | «около N часов» (N rounded to the nearest hour) |
| 6 h and more, the same day, otherwise | «несколько часов» |
| yesterday, under 24 h: switched from 17:00 to 05:00 | «со вчерашнего вечера» |
| yesterday, under 24 h, otherwise | «со вчерашнего дня» |
| 24 to 48 h | «больше суток» |
| more | «несколько дней» |

Every form fits «уже …» («уже меньше минуты», «уже с утра»). On: «уже около 20 минут», «уже больше часа», «уже с утра». Off: the same figure with «выключен». No `last_changed` (the attribute missing, unparsable or without a zone): the duration is left out, the rest of the sentence stands. The states are read FRESH from Home Assistant for every status question (there is no cache to skip), so a unit switched a moment ago is not reported as it was.

**Answer templates** (impersonal; several variants per case, chosen at random so that a repeated question does not sound the same; `{where}` is the locative of the area, `{what}` is «обогрев, 30 градусов»):

* One unit on: «Сейчас включён кондиционер {where}: {what}, уже {since}.» / «Кондиционер {where} включён уже {since}: {what}.» / «Работает кондиционер {where}: {what}, уже {since}.»
* Several on, the same state: «Включены оба кондиционера: {where_a} и {where_b}, {what}, уже {since}.»
* Several on, different: one sentence of the «one unit on» kind for each.
* On and off: the sentence for the unit that is on, then «Кондиционер {where} выключен.»
* None on: «Сейчас все кондиционеры выключены.» / «Все кондиционеры выключены.»; one unit: «Кондиционер {where} выключен уже {since}.» / «Сейчас кондиционер {where} выключен уже {since}.»
* A room without a unit: «В комнате «{area}» нет кондиционера.» (the string the acting executor already uses)
* A unit that does not respond (`unavailable`): «Кондиционер {where} сейчас не отвечает.»
* Aspect `mode`: «Кондиционер {where} работает в режиме «{mode}».» / off: «Кондиционер {where} сейчас выключен.»
* Aspect `temp`: «Кондиционер {where} выставлен на {set}, сейчас в комнате {current}.» (the missing one is left out)
* Aspect `since`: «Кондиционер {where} включён {since}.»
* Home Assistant unreachable: «Состояние кондиционеров сейчас узнать не удаётся.»; no `climate` entity in the home (or none in the named room): «Кондиционеров в доме не найдено.» / «В комнате «{area}» нет кондиционера.»
* Every state is a live read through the shared client (no cache), so an answer can never be stale; an unreachable Home Assistant gives the data-unavailable sentence instead.

None of these names an actor, asks for a repetition or says what the speaker «не включал». The set-point is spoken as «N градусов» (`home_status.degrees`).

#### 4.2.3 The bare-noun phrase

«Кондиционер на втором этаже.» alone has three readings (status, on, off). Today the quasar layer asks «Включить или выключить кондиционер наверху?». The status reading is the only one that is both safe and informative, and it is the answer the owner asked for in §6. The pattern lives in its own intent `home.climate_bare` (same executor, `bare: true`) so it can be switched independently: `AISIS_STATUS_BARE=shadow|on`, default `shadow` (the decision is traced with the reason `declined: … AISIS_STATUS_BARE`, nothing is said or read, the quasar layer's question stands). `on` is the owner's call after one look at the shadow traces. Corpus consequence of `on`: the private synthetic row `кондей` expects the quasar layer's clarification («Включить или выключить кондиционер?»); with `on` the pre-phase answers it first, so that row has to be re-expected as a query (the private corpus is pinned by hash in the offload manifest: a deliberate owner step, not part of this change).

## 5. Where the code goes

All in the adapter (`services/yandex-station/code/`):

| File | Change |
|---|---|
| `intent_grammar/grammar/ru.yaml` (+ compiled `ru.json`) | lexicon sets `reaction`, `tell_verb`, `capability_topics`, `ac_forms`, `status_head`, `status_on`, `status_off`, `status_aspect`; intents `assistant.capabilities`, `assistant.capabilities_topic`, `assistant.capabilities_more`, `home.climate_status`, `home.climate_bare` with `examples:` and `negatives:` |
| `intent_grammar/grammar/info-ru.jsonl` | the authored phrase fixture of §4 (text, expected intent and slots), read by `test_info_intents.py`; not part of the private back-test corpus |
| `capabilities.py` | facts gathering and the answer text (`Facts`, `answer`, per-station cursor); pure given facts |
| `home_status.py` | the duration rounding and the climate sentences; pure given units and `now` |
| `ha_actions.py` | read-only `ha.climate_status`, `rooms_with(domain)`; injectable wall clock |
| `intent_route.py` | the two executors in `ADAPTER_EXECUTORS`, the dispatch, the `AISIS_STATUS_BARE` gate, the cursor |
| `adapter.py` | passes `AISIS_STATUS_BARE` to the router |
| tests | `test_capabilities.py`, `test_home_status.py`, `test_ha_status.py`, `test_info_intents.py` |

`ru.json` is the compiled bundle (CI runs `compile --check`). Every grammar change moves the eight files pinned by the offloaded corpus gate (`corpus-runner/offloaded_corpus.py`: `ADAPTER_FILES` hashes and `ADAPTER_REVISION`); see §8.

## 6. The weighted-answer policy («answer, do not ask»)

Owner rules (2026-11-22): when the agent would ask a question, or something is unclear or looks strange, it consults the weights of the possible transcriptions and answers as informatively as it can; it answers the reading with the higher weight even when that reading is wrong, but the answer must not leave the user puzzled; the actor is dropped from the wording; when the request looks like an ACTION («включи …») and the weight is LOW, the agent asks to confirm and neither acts nor guesses; all of this holds for any action and question (timers, reminders, music, weather, home), not for the air conditioner alone; and the debug messages show the variants with their weights whenever the weights were used or the top weight is not very high.

### 6.1 Input

* the primary text, with the grammar's decision for it (or none);
* the alternatives of the second opinion: `NBest.hypotheses` of `Correction.stored(...)` (text, `posterior` = the share among the LISTED readings, `fit`), live 15 s in the adapter's `HypothesisStore`; evidence is absent when the engine is off or the gateway published nothing;
* the cues of the quasar layer (`noop`);
* live data through the same executors as normal turns (live Home Assistant states, timers, weather-cli, the dialog store).

### 6.2 Reading the alternatives

Each alternative, and the primary, is read by the same grammar (`IntentRouter.classify`) into a **reading**: the intent, the tier and the executor, keyed with `correction.action_key`. Readings with the same key are pooled (`weight` is the summed posterior; two wordings of one intent are one reading), as the core policy pools them. A reading whose own `fit` is below `garbled_fit` is dropped from the pool. Every reading is classified by the kind of its executor:

* **inform** (read-only, no state changes): `ha.climate_status`, `clock.say`, `clock.date`, `timer.remaining`, `skill.weather`, `skill.math`, `skill.capabilities`, the state reports;
* **act** (a state changes, a timer starts, music plays): everything else the adapter or the quasar layer executes; the **risky** acts are those outside the correction engine's `SAFE_EXECUTORS` (climate, timers, TV, sockets, kettle);
* **other** (no action: chat, knowledge): the brain's.

### 6.3 Thresholds (from the merged engine, not invented)

The core policy (`stt_cli.hypotheses.repair.Policy`, defaults) is the one source: `ask_floor = 0.30`, `primary_weak = 0.60`, `margin_max = 0.35`, `garbled_fit = 0.35`; for a risky action the engine replaces them with `ask_floor = cue_floor = 0.05`, `primary_weak = 1.0`, `margin_max = 1.0` (any disagreement asks). The policy adds exactly one derived number:

> **`HIGH = max(primary_weak, 1 - ask_floor) = 0.70`**, a top reading with this weight leaves less than `ask_floor` of the mass to every other reading together, so no alternative can reach the floor that would open a question: the engine could never have asked. "Very high" in the debug rule below means this.

### 6.4 The three regimes (top reading with weight `w`)

| Top reading | `w >= HIGH` (0.70) | `ask_floor <= w < HIGH` | `w < ask_floor` |
|---|---|---|---|
| **inform** | answer it | answer it, when the answer is coherent (below); otherwise ask the short clarification | the core's relisten, said as what was heard and what can be asked for («Прозвучало «…», но что сделать, не ясно: свет, кондиционер или телевизор?»; `correction.describe`, never a request to repeat) |
| **act** | run it through the normal route (a top that is NOT the primary still goes through the engine's confirmation: nothing runs from an alternative without a spoken yes) | **ask**: the engine's `ask` / `ask-choice` question; when no rival action exists (the rest of the mass is noise) a new single-reading **confirmation**: «Включить кондиционер?», answered by the repair dialog's `да`/`нет`; never run, never guessed | relisten |
| **other** | the brain, as now | the brain | the brain |

Risky acts use the risky thresholds above, so every doubt about them is a question.

**Coherence rule for an informative answer in the middle band.** Let `P` be the readings with weight `>= ask_floor` (the plausible readings). The informative answer `A` for the top inform reading is spoken only when it cannot mislead under any `p` in `P`:

1. `p` has the same key as the top reading: `A` is its answer; or
2. `p` is an act or inform reading about the **same subject** as the top (the same device class or domain: all `ha.climate*` readings share `climate`; `timer.*` and `alarm.*` share `time`), because a state answer about that subject is useful under it: the user who meant «включи кондиционер» hears that it is on, since when and in what mode, and a repeated command is the natural next step; or
3. `p` is an **other** reading (no action): the brain's reading is outweighed, `A` is harmless.

Any `p` with a different subject (the weather versus the air conditioner), or an act reading that the answer would contradict (the user said «выключи» and `A` says it is on: that is information, not a contradiction; an act reading of a different device is a different subject), makes `A` incoherent: the short clarification is asked instead, naming at most two readings («Какой кондиционер включён или какая погода?» is never said; the question names the readings in the engine's wording: «Кондиционер или погода?»).

The informative answer **never actuates**, never states what was done, and says nothing about the recognition («мне послышалось» is for questions only).

Worked examples:

* «Какой кондиционер включил?» with alternatives `какой кондиционер включён 31%`, `какой кондиционер включил 9%`, rest noise: the primary has no reading (`включил` is not in the grammar), the top inform reading `home.climate_status` has `w = 0.31 >= ask_floor`, the other plausible readings are the same key: answer «Сейчас включён кондиционер на втором этаже: обогрев, 30 градусов, уже около 20 минут.» This is the owner's example; it is also right when the user meant «включи кондиционер» (reading 2).
* «Кондиционер на втором этаже.», alternatives `включи кондиционер …`, `выключи кондиционер …`, `кондиционер на втором этаже`: all are the subject `climate`; the bare reading is `home.climate_bare`, inform. The answer is the status of that unit, in place of «Включить или выключить?».
* «Поставь таймер на пять минут» with alternatives `поставь таймер на пятнадцать минут 45%` and `на пять минут 40%`: both act readings of one key family but with different arguments: the keys differ, the margin is below `margin_max`: the engine's `ask-choice` («пять минут или пятнадцать?»). Not an inform case.
* «Какая погода завтра» vs `какая погода в среду`: both inform, same executor, different `mode`: two keys; `w` of the top 0.5 and the rival 0.4 both `>= ask_floor`: incoherent (different answers), so a clarification, or, since both are weather, an answer that carries both when the executor supports a list (future).

### 6.5 Interface for the correction FSM

A pure module (`weighing.py`, ticket §9), no I/O, deterministic, injected classifier:

```python
@dataclass(frozen=True)
class Reading:
    text: str            # the wording (the best-weighted one of the pooled alternatives)
    key: str             # correction.action_key(decision)
    kind: str            # "inform" | "act" | "other"
    subject: str | None  # "climate", "time", "weather", "light", ...
    weight: float        # pooled posterior, 0..1; 1.0 for an utterance with no evidence
    risky: bool
    decision: Decision | None

@dataclass(frozen=True)
class Verdict:
    regime: str          # "proceed" | "answer" | "ask" | "confirm" | "relisten" | "none"
    top: Reading | None
    readings: tuple[Reading, ...]   # all, by weight descending: the debug line
    high: float          # the HIGH of §6.3 for this utterance (risky: 1.0)
    reason: str          # why this regime (trace)

def weigh(primary: str, nbest: NBest | None, classify: Callable[[str], Decision | None], policy: Policy) -> Verdict: ...
```

The FSM (`IntentRouter._correct`) calls it once per utterance after the dialog's own reply handling and before `consult`:

* `proceed` or `none`: the normal route.
* `answer`: the router runs the top reading's executor (read-only, from `ADAPTER_EXECUTORS`) and replies; no dialog opens; an open one for the station is closed.
* `ask`, `confirm`, `relisten`: `Correction.consult` opens the matching dialog (the existing `ask`/`ask-choice`/`relisten`; `confirm` is `awaiting_confirm` with the heard reading as the alternative, a new case of the same state).
* Every verdict, including `proceed`, is written to the trace (§6.6).

Mode: `AISIS_INFORM=off|shadow|on`, default `off`; `shadow` computes and traces, speaks and opens nothing. It runs only while `AISIS_CORRECTION` is `shadow` or `on` (the evidence is the engine's). Shadow first for everything that changes an existing question: the `answer` regime replaces a spoken question, the `confirm` regime adds one.

### 6.6 Debug presentation

Rule: **the variants with their weights are shown whenever the weights were consulted (any verdict was computed from an `NBest`), or the top weight is below `HIGH`.** Compact, neutral, in the style of the key line:

```
🎚 варианты: какой кондиционер включён 31% · включи кондиционер 54% · какой кондиционер включил 9%
🎚 решение: ответ по состоянию · высокий вес от 70%
```

The first line is `trace_view.alternatives_lines` (already used for `correction.nbest`: the two leading variants, the rest as «ещё варианты»); the verdict goes into `trace.weights` as `{regime, reason, high, variants: [{text, percent, kind}]}` and `trace_view` gets a `weights_lines(trace)` that feeds the same renderer, so the clip post, the session page and the compact trace share one code path. No percentage is ever spoken. This is the display part of the policy; it lands after the neutral-wording change that edits the same lines (§9).

### 6.7 Interaction with existing pieces

* **Correction FSM (#182).** The engine stays the author of every question; the new layer decides before it whether a question is needed at all. `consult` is untouched in the `ask`/`ask-choice` paths; the `confirm` regime is one more constructor of `Dialog(state=CONFIRM)`.
* **#212 / #216 (air conditioner phrases).** The acting phrases still belong to `climate.*` through the quasar overlay `ac-defer-to-adapter`; the status intents add no acting pattern. A phrase read as an act with `w < HIGH` is the engine's `ask`/`confirm` (risky thresholds), never executed.
* **#190 (verification).** The status answer is a state read; it quotes only what Home Assistant reports (`last_changed` of the entity); states are live reads, so there is no stale cache to hedge. Actuation verification rules are untouched.
* **Jev, brain.** A verdict of `answer` removes the 2 s brain call; `other` keeps it.
* **#269 / #268 (extra dictionary).** The dictionary feeds the recogniser; this layer consumes the alternatives it produces. They do not share state.

## 7. Error and fallback behaviour

| Failure | Behaviour |
|---|---|
| grammar bundle unavailable | the adapter runs without it (existing): the brain answers |
| executor raises, `ha` unbound, Home Assistant unreachable | the intent answers its data-unavailable sentence (status) or leaves out the unreachable abilities (capabilities); an exception returns `None`: the old path, never a stack trace or silence |
| states unparsable, `last_changed` missing | the field is dropped from the sentence |
| no `climate` entity | «Кондиционеров в доме не найдено.» (named room: «В комнате «{area}» нет кондиционера.») |
| bare phrase in shadow | traced, declined: the quasar layer's question stands |

## 8. Tests

* **Grammar**: `examples` / `negatives` pinned in `ru.yaml` (`python3 -m intent_grammar check`); `compile --check`; `backtest --route combined --gate` against the private corpus (numbers in the corpus contract below).
* **`test_info_intents.py`**: every phrase of `info-ru.jsonl` is classified to the expected intent and slots by `IntentRouter.classify`; the negatives stay with their own intents (`climate.set`, `light.on`, `state.report`, `smalltalk.*`); the acting phrases never match a status intent.
* **`test_home_status.py`**: the rounding table (both sides of every boundary, `now` injected, the «с утра» and «со вчерашнего вечера» cases, another time zone), every template case with fake units, the aspects, and a check that no sentence contains «колонк», «я», «не включал», «не умею» or a first-person verb.
* **`test_capabilities.py`**: fake facts (timers on/off, weather on/off, Home Assistant up/down, rooms), time-of-day ordering with an injected clock, cursor rotation and wrap, topic answers, and **every example phrase offered in an answer is read by the grammar into the executor it demonstrates** (the answer cannot offer a phrase the stack does not understand).
* **`test_ha_status.py`**: `ha.climate_status` and `rooms_with` against the fake Home Assistant client (`ha_fakes`-shaped states with `last_changed`), the registry areas, a fresh read per question, an unreachable or unreadable Home Assistant, a garbled or missing `last_changed`, a dead unit, and an assertion that **no service is ever called**.
* **`test_info_intents.py`** (also): routing of the executors through `IntentRouter.route` with a fake Home Assistant client: the real clip answered without the brain, the misheard state question answered from states, the room narrowing, the follow-up that continues only an answer just given, the topic question, the bare phrase in shadow (traced, declined, nothing read) and `on`, no Home Assistant actions, and that no service is ever called.
* **Policy tests** (with the module in §9): fake `NBest`s over the thresholds of §6.3, one case per cell of the regime table, the coherence rule (same key, same subject, different subject, other), pooling, and that no regime ever executes an act.

**Corpus contract.** The private corpus and its hashes (`intents-ru.jsonl`, 469 rows, pinned in the offload manifest) are NOT edited by this change: new phrases live in git as the fixture above and as grammar `examples`. The quasar corpus runner reaches no adapter, so the row `что ты умеешь` (`llm`) stays `llm` there. Measured on the host that holds the private corpus (`backtest --route combined --gate --no-latest`, a copy of the last promoted report as the baseline, nothing promoted): gate `ok`, 0 rows worse, 2 better (the row `что ты умеешь`, and the held-out row `какая температура на кондиционере`, `llm`/`climate`, which `home.climate_status` now answers: the intent carries the corpus label `climate`, a state question about the climate is a climate-domain answer). Any change to `ru.yaml` / `ru.json` changes two of the eight pinned adapter files: `ADAPTER_FILES` is updated in the same pull request, and `ADAPTER_REVISION` must name a commit that holds those bytes: the pull request pins its own grammar commit (so its CI is green), and after the squash-merge a one-line re-pin pull request names the squash commit (the precedent: #236, #254). The merge must not be deployed to a quasar colour with the old pin.

## 9. Rollout and follow-up tickets

1. **This change (#275):** §4 intents, the spec, the bare phrase in shadow. Restart: the adapter (it loads the compiled bundle at start); no quasar deploy, no corpus change.
2. **Policy module and shadow wiring** ([#277](https://github.com/alex-mextner/aisis/issues/277): `weighing.py`, `AISIS_INFORM`, trace `weights`, tests of §8).
3. **Single-reading confirmation** ([#276](https://github.com/alex-mextner/aisis/issues/276)) («Включить кондиционер?» for an act with a low weight and no rival): a `confirm` constructor in `correction.Dialog`, shadow first, with the grammar contexts reused.
4. **Debug lines** ([#278](https://github.com/alex-mextner/aisis/issues/278)): `trace_view.weights_lines`, clip post, session page; after the neutral-wording pull request (#270/#273) that edits `trace_view.py`, `session_pages.py` and `clipfeed.py`.
5. **More status intents** ([#279](https://github.com/alex-mextner/aisis/issues/279)): lights (the quasar layer's `state.report` answers «Сейчас ничего не включено.» from a template; an adapter-side reading with `last_changed` needs the pre-phase hand-over), TV and projector, volume, timers and reminders («что поставлено»), weather follow-ups.
6. **Persona text** (#273): `user_prompt.CAPABILITIES` still tells the model the speaker «выполняет сама». With §4 the model rarely sees capability questions, but its fallback text is the same actor wording and is changed by the wording ticket, not here.
7. **Typical-phrase growth** through the nightly intent loop: the phrases of §4 are authored from the grammar's conventions and one real clip each; real variants arrive in the corpus and extend `info-ru.jsonl`.
8. **RETIRED: stale-cache hedge for the status answer** ([#280](https://github.com/alex-mextner/aisis/issues/280)): obsolete. It hedged answers served from the old local proxy's cache (`X-Aisis-Cache: stale`); the proxy and every state cache are gone, states are live reads through the shared client, so nothing can be stale.
