# Jev in the speaker stack: where it is used, how to run it, what it measured

Ticket: alex-mextner/aisis#124 (owner ask, 2026-11-16: «On Jev there is no breakdown: where we decided to use it and how to use it, etc.»). Date: 2026-11-16. Related: #107 (intent coverage, the code), #106 (fast-model benchmark), #88 (three-route design).
Specs that name Jev: `docs/specs/040-model-router-and-providers.md` (routing chain, «Jev is optional»), `120-aggregator-assistant.md` (tool-group choice), `docs/research/existing-systems.md` (what Jev is suited for), `docs/architecture/contracts.md` (`RouteDecisionProvider`).

Labels: **[jev-bench]** `bench_jev.py` live run over the speaker corpora (562 calls), **[measured now]** a run made for this document on 2026-11-16 (§3.2, §4.7, scripts in §8), **[FastModelBench]** numbers copied from [fast-llm-bench-2026-11.md](fast-llm-bench-2026-11.md) (§5; that document is authoritative if it differs), **[replay]** the grammar code run offline over text, **[INFERENCE]** my reasoning, not observed, **[not verified]** nobody checked.

Where the code is (2026-11-16): `jev.py`, `bench_jev.py`, the `adapter.py` wiring and `docs/research/jev-route-bench-2026-11.md` are in the `intent-jev` branch of the intent work (not yet merged to `main`); the nightly `--jev` step is in the `intent-nightly` branch; the benchmark harness is in `docs/fast-llm-bench`. Paths below name the files as they will be on `main`; I read them in those worktrees. **Production today: Jev is off** (`~/.config/aisis/runtime.env` has no `AISIS_JEV*` or `VERCEL_JEV_KEY` entry).

## 1. Answer first

| Question | Answer |
|---|---|
| What is Jev here | An optional, remote **typed-choice classifier** (TypeSafe AI `typesafe-ai/jev` via the Vercel AI Gateway): it picks one value among options **that the request defines**, with a confidence, in ~0.25 s. It does not generate text, call tools or see more than the one transcript string it is given. |
| Where it is used | **Use now (opt-in, offline): nightly labelling** of unanswered utterance clusters. **Shadow (log only, no effect on a reply): the utterance kind (command / talk / garbled / not addressed), the brain tier and the effort, and the task-type route.** Later: tier choice once a lite model exists, aggregator routing. **Not used, by design: anything the grammar/tools can answer, slots and devices, safety actions, reprompt state, conversation context.** Full table: §3. |
| Is it worth turning on to act | **Not yet, and its direct latency gain is small.** `AISIS_JEV=on` as written is **slightly slower on average** (+0.03 to +0.19 s per brain-bound turn [INFERENCE from measured inputs, §3.1]): it adds ~0.25 s to every turn it only lets pass to the brain and saves ~2.2 s on the 2–9 % of turns it refuses or re-prompts. Its refusal branch also nearly refuses emergency phrases (§3.2). Its real value is the classifier for a **lite model tier that does not exist yet** (route accuracy 92 %, 0 of 71 escalate utterances sent to the fast tier, [FastModelBench]). |
| Privacy | The final transcript of a turn that **already goes to the brain (Anthropic)** also goes to Vercel and TypeSafe. No audio, no speaker/station identity, no history. Transcripts are never logged locally by Jev code. Vercel/TypeSafe retention: **[not verified]**. `AISIS_JEV=off` keeps everything local (§2.2). |
| Safe to try | Yes in `shadow`: it never changes or delays a reply, a hard 0.4 s budget, a breaker, no exception reaches the turn; kill switch is `AISIS_JEV=off` plus an adapter restart (§4.6). |
| Cost | **~$0.000026 per call** [measured now: 5 calls, the gateway's `cost` field]. At the real brain-bound traffic (a few turns a day) that is cents a year. |
| What blocks acting | Missing code (not in this document's scope; filed as a follow-up, §6): per-class switches, an env for the 0.95 threshold, a log line that carries a turn id, latency and below-threshold decisions, a route-type question in the wording FastModelBench measured; and **evidence volume**: a week of shadow produces tens of rows, so accuracy must be calibrated on the corpus, not on the shadow week (§4.5). |

## 2. What Jev is here

### 2.1 The contract and what it can and cannot do

`POST https://ai-gateway.vercel.sh/v1/decisions`, bearer `VERCEL_JEV_KEY`, body `{"model": "typesafe-ai/jev", "input": <text>, "questions": [...]}`. Each question is `{"type": "choice", "name", "instructions", "choices": [{"value", "description"}]}`; the answer per question is `{"choice", "confidence", "probabilities": [...]}` (`jev.py` `JevClient.choose`, `_parse_choices`). The gateway also has `POST /v1/evaluate` with `state` and a `questions` **record** (name to spec; the array shape returns HTTP 400, probed 2026-11-16): that is what the benchmark harness of #106 uses. Both reach the same model.

- **Can**: choose one of a closed list of options defined in the request (a kind, a tier, an effort level, an intent id, a room name), several questions per call at the same cost, with a confidence. Because the options are in the request, the same model serves every question: no training, no deployment per use.
- **Cannot**: generate or rewrite text, extract free-form slots, call tools, remember anything between calls, see audio or earlier turns. `RouteTurn` has one field, `text` (`jev.py`; spec 140: no audio, no speaker identity).
- **Confidence is not the probability.** `answers[].confidence` is a separate signal from `probabilities`: on a probe the answer was `choice=command, confidence=0.4` with `probabilities command 0.7 / talk 0.3` [measured now]. `jev.py` reads `confidence` and falls back to the top probability only when `confidence` is absent (`_parse_choices`, covered by a unit test); the benchmark harness of #106 thresholds the top probability. **A threshold in one document does not transfer to the other** (the `0.95` of `jev.py` is not the `0.9` row of [FastModelBench]); recalibrate on the endpoint that production calls (§4.5).
- **Wording is part of the model.** Asked «how capable a model does this need» (`jev.py` TIER) Jev sent 16 of 71 hard utterances to `fast` raw, before any confidence gate (§3.2); asked the route question of the design («deterministic / fast / reprompt / escalate», [FastModelBench]) it sent 0 of 71. Treat the question text as code under test.
- Latency: p50 248 ms, p95 309 ms, max 533 ms, 0.7 % over the 0.4 s budget over 562 calls [jev-bench]; p50 255 ms, p95 324 ms, max 438 ms over 149 calls [measured now]; a cold first call ~0.8 s (TLS), hence the adapter's keep-warm request every 25 s. Both runs were made on `ubuntu-pc`; whether that is the speaker host is stated differently by the two documents, so the real latency is a shadow-week measurement (§4.5).

### 2.2 What leaves the house, and what never does

| Sent to Vercel / TypeSafe (per call) | Never sent |
|---|---|
| The **final transcript string of the turn** (`input`), unchanged and without a length cap in `jev.py` (an utterance is a sentence). The fixed question texts and option lists (no personal data). `Authorization: Bearer <key>`. | Audio, voiceprint or speaker score, the station or room, the principal, conversation history, the brain's answer, Home Assistant state, any other turn (`RouteTurn` is `text` only). |
| Nightly step (`--jev`): **one representative utterance per cluster, at most 25 a night**, plus the grammar's own example phrases as the option list (`intent_grammar/nightly.py`). | The rest of the day's utterances; the nightly working directory stays `0600` on the host. |

- Which turns: only those that **nothing local answered** (grammar, calculator, weather/music skills abstained) and that are about to be sent to the brain anyway. The same text therefore already goes to Anthropic; Jev adds a **second recipient for the same text**, not a new class of data. Spec 140 allows final accepted transcripts to model providers and forbids audio and identity; Jev code sends none of the latter.
- Locally nothing is logged except `utterance`, `tier`, `effort`, `conf` (`adapter.py`, `jev_shadow` / `jev_route`); `jev_failed reason=...` carries `timeout`, `http <status>` or the exception class, never the key or the text (unit tests `test_the_key_and_the_transcript_are_never_logged`, `test_shadow_logs_and_never_changes_or_delays_the_reply`).
- The nightly README says the working files «never leave the machine»; with `--jev` the representative utterances do leave it (to Vercel). That is the one exception; keep `--jev` off if it is not wanted.
- Provider rule of the project: Vercel carries **Jev only**; Claude stays on the direct API, fast models on Cerebras. The card on the Vercel account is the owner's; the key is in the untracked repository `.env` (`VERCEL_JEV_KEY`) and, for the adapter, belongs in `~/.config/aisis/runtime.env`.
- **[not verified]** Vercel's and TypeSafe's retention, logging and training terms for gateway requests. The owner should read the gateway's data-retention setting before `on`; `shadow` has the same exposure as `on` for the turns it sees.

## 3. Decision table: every candidate place

Decisions: **use now** (enabled by the owner, safe by construction), **shadow** (decide and log, never act), **later** (needs something that does not exist), **don't** (a model must not make this decision). «Gain» is measured unless marked [INFERENCE]. Fail-open for every row: any Jev failure, timeout, open breaker or confidence below threshold is an abstention and the turn takes the path it takes without Jev (`jev.py`: `no exception escapes decide`, `CircuitBreaker`).

| # | Candidate place | Decision | Why, and measured or estimated gain | Risk | Flag |
|---|---|---|---|---|---|
| 1 | **Jev in front of the grammar, calculator, weather, time and tools** | **don't** | The deterministic route answers in ~1–24 ms (audit §1: p50 24 ms for local deterministic answers); a Jev call is +246 ms on a path that needs none ([FastModelBench] §4.2: «rules-first answers in about a millisecond plus the tool, Jev-first adds 246 ms»). Gain: negative. The adapter asks Jev only after `IntentRouter`, `calc_evaluate` and `skills.answer` abstained (`adapter.py` `_process_turn`). | none if kept | n/a |
| 2 | **Task-type classification before choosing a model** (canned / banter / reprompt / tool-answerable / fact / complex / mixed; or the four routes) | **shadow**, act **later** (needs the lite tier of #88; `AISIS_FAST_TIER` is a proposal, not in code) | Best non-Claude classifier measured: route accuracy **92 %**, 0/71 escalate utterances sent to fast, 0/30 reprompt sent to fast, 10/10 tool-answerable recognised, **246 / 292 ms** p50/p95; 7-type accuracy 84 %. Qwen as its own classifier: 93 %, 0/71, 2/30, 226 / 828 ms [FastModelBench]. As the classifier of the design's classify-then-generate pair Jev would cut the classifier call from 276 / 602 ms to 246 / 292 ms (p95 −310 ms) and its cost from $0.28 to ~$0.014–0.026 per 1000 [FastModelBench, measured now]. **Not in the code yet**: `jev.py` asks `UTTERANCE`, `TIER`, `EFFORT`, not this question. Use Jev as a **veto only**: the rules and the generator's `ESCALATE` rule grant fast eligibility; Jev may only deny it. | Different wording gives different safety (§2.1); a second vendor on text that already goes to Cerebras for the generator; one run, 149 items, all 95 % intervals wide | `AISIS_JEV=shadow` (+ a new route question, §6) |
| 3 | **Skill vs brain for utterances the grammar misses**: kind = `command` (nobody implements it), `talk`, `garbled`, `not_addressed` | **shadow** (implemented). **Do not set `on` yet.** | On the 65 speaker-corpus phrases the grammar abstains on [jev-bench]: at conf ≥ 0.95, 34 answers, 29 right; `command` acted on 6, all right; `garbled` never reached 0.95. On the 149-row eval corpus, replaying the grammar first, Jev sees 87 rows (7 of them weather/time/conversion that the skills layer answers in production) and would act on **2** (conf ≥ 0.95), both `command`: «Составь список покупок на неделю…» (a text the brain writes: **wrong refusal**) and «Напомни завтра в девять позвонить маме» (right) [measured now, replay]. Net latency effect of `on` today: **+0.03 to +0.19 s slower** on average (§3.1). | A wrong «Этого я пока не умею.» to a real request; near-miss on emergencies: «Мне плохо, вызови скорую» was `command` at 0.92, «Позвони в полицию» at 0.93 [measured now]: **one calibration step from refusing an emergency**. The grammar already answers «Позвони в полицию» (`refuse.calls`) before Jev; «вызови скорую» is not matched | `AISIS_JEV=shadow`; promotion needs the gates of §4.5, a deterministic emergency stem guard in front of any refusal, and a per-class switch (§6) |
| 4 | **Brain tier and reasoning effort** (`TIER`, `EFFORT` in `jev.py`) | **shadow**, advisory; act **later** | The brain has no tier or effort knob today (`claude_primary`, fixed model); the answer is logged. Evidence on 149 rows [measured now]: raw `fast` is chosen for 38/38 fast items **and for 16/71 escalate items** (8 of 32 facts), always with tier confidence ≤ 0.73; at tier confidence ≥ 0.9 **0/71** escalate go to fast and 29/38 fast items are recognised. `route_from` also requires utterance confidence ≥ 0.95, which leaves **6/38** fast items with `model_alias=fast` (0 wrong) and, on the Jev-visible subset after the grammar, 0. Effort has confidence ≥ 0.95 on only 21/149 rows (it degrades to `medium`). The speaker corpora give fast 35, balanced 18, deep 1 on 54 chit-chat/knowledge phrases without ground truth [jev-bench]. Potential gain **[INFERENCE]**: a fast tier answering in 238 ms behind a 246 ms Jev call (484 ms) instead of Sonnet at ~1.5–2.3 s, for about **4 % of addressed turns** (after the grammar plan the audit expects 3 chit-chat of 79 for a lite tier, §7.2). | An unsafe `fast` on a hard question (the raw tier is not safe, only the gated one) | `AISIS_JEV=shadow` |
| 5 | **Slot disambiguation**: which room, which device | **don't** (acting); later, as an offline suggestion only | Resolving a room/device is a registry lookup (design budget ≤ 5 ms, audit §7.4); the audit's one wrong device, «выключи свет на спальне» turning off the living-room chandelier, is a deterministic room-resolution bug tracked as #103. A model's guess must not drive a Home Assistant write: smart-home actions confirm only from tool receipts, voice never authorises sensitive writes (fast-llm-routing §6.1). If a model is ever needed to fill a slot the measured candidate is Qwen 3.8 27B (five-slot accuracy 100 %, 241 / 1,228 ms), not Jev; Jev was **not measured on slots** [FastModelBench]. Gain: none (1 of 79 addressed utterances). | Wrong room, wrong device | none |
| 6 | **ASR garbage vs real speech** (`garbled`) | **shadow**; act only after a re-measure with the route wording | Jev's `garbled` is precise but rarely confident: on the 30 reprompt items it said `garbled` for 18 at any confidence, 11 ≥ 0.5, 4 ≥ 0.8, 2 ≥ 0.9, **1 ≥ 0.95**, with 0 false `garbled` at those thresholds and 1 false at <0.5 [measured now]; 2/4 right on the speaker corpora, never ≥ 0.95 [jev-bench]. The same Jev asked the route question recognised 26/30 reprompt items with 0 sent to fast [FastModelBench]. The grammar already answers ASR garbage deterministically (`asr.*` catch-alls, one reprompt a minute per station) and 14 % of the addressed traffic is servable that way (audit §1); Whisper hallucinations are a blacklist problem, not a model problem (audit §4.5 row 1). Gain: ≈0 today, ≤ 2 % of turns after a re-word, saving ~2.2 s each [INFERENCE]. | Re-prompting a real question | `AISIS_JEV=shadow` |
| 7 | **Not addressed to the speaker** (`not_addressed`: TV, a person, the cat) | **shadow**, log only (already) | Jev said `not_addressed` for 6/149 rows, none ≥ 0.8, 5 of them fast-labelled real speech (false) [measured now]. The remedy for hallucinated or ambient input is the gateway blacklist plus «wake word or active dialog» gating (audit §7.2 step 2). No action is wired and none should be. | Silencing a real request | `AISIS_JEV=shadow` |
| 8 | **Reprompt decisions** (whether to re-ask, what to say) | **don't** (beyond row 6's trigger) | State is deterministic: pending question, one reprompt a minute per station (`REPROMPT_COOLDOWN_S = 60`), canned wording. Jev's `garbled` can only be one more trigger sharing the same cooldown (`allow_reprompt`). | A reprompt loop | none |
| 9 | **Nightly clustering and labelling of unanswered utterances** | **use now** (opt-in) | Offline, no latency budget: for each of the ≤25 biggest clusters Jev names the nearest existing intent or `_none` (a new intent is needed) to brief the grammar-proposing session (`jev.label_with_jev`, `nightly.py` step 3). The proposals are still gated by the back-test (no regression, held-out coverage, pinned examples). Cost ≤ 25 calls a night, well under a cent [INFERENCE: the option list makes the input longer than the 600 tokens measured for row 3]. Accuracy of the label **was not measured**, so it is advisory. | The representative utterance leaves the host (§2.2); a wrong label misleads the proposer, which the gate catches | `python3 -m intent_grammar nightly run --jev` with `VERCEL_JEV_KEY` in `~/.config/aisis/intent-nightly.env` |
| 10 | **Weather, time, conversion, music-by-name (tool routing)** | **don't** | Tool-answerable questions never reach an LLM (fast-llm-routing §6: «if a local tool can produce the answer, use the tool»). Jev's `UTTERANCE` has no «tool-answerable» option: it called 7 of 10 such items `talk` at ≥ 0.95, i.e. it would send them to the brain [measured now]. Gain: negative. | Weather answered by a model | none |
| 11 | **Safety and irreversible actions**: emergency calls, locks, payments, deletes, confirmation of an action | **don't** | Voice never authorises sensitive writes; confirmations come from tool receipts (fast-llm-routing §6.1.5). Row 3 shows why: an emergency phrase scored `command` 0.92. Emergencies need a deterministic stem rule that wins before any model and before any refusal text. | Harm | none |
| 12 | **Follow-up and conversation context** | **don't** | `RouteTurn` is one string, so Jev cannot see the previous turn. The context gate is deterministic: after an escalated turn or an unresolved question, follow-ups go to the strong path for 120 s (fast-llm-routing §6.1.1). `mixed_or_followup` is the weakest class for every classifier (Jev 78 %, Qwen 78 %, Sonnet 67 %, Haiku 56 %, gpt-oss 33 %) [FastModelBench]. | Wrong lite answer to a follow-up | none |
| 13 | **Deciding whether to run the Haiku ASR corrector** | **don't** | The corrector costs ~0.85–1.0 s only on phrases that survive the grammar (the intent-jev branch already short-circuits `/asr/correct` for grammar-known phrases, `adapter.py`); a 0.25 s Jev call before it would add latency to every remaining phrase for a decision the corrector's own `confidence` already provides. Gain: none. | Latency | none |
| 14 | **Aggregator routing** (OpenClaw `openclaw-routing`): model alias, reasoning effort, harness and **tool-group choice** for Telegram/Alice/web turns (specs 040, 120) | **later** | The package is planned, not built (`docs/architecture/repo-layout.md`). Choosing one tool group or harness from a closed list is exactly Jev's contract and the text surfaces have a looser latency budget than a voice turn. No measurement exists for those classes. For Russian the measured local alternative is worse: Laya multilingual was 29–49 % route accuracy (§5), so Jev is the only measured **optional model** in the spec 040 chain; Laya stays a privacy option only after tuning. Telegram and personal-connector text must be kept off Jev unless the owner opts in per surface (§2.2). | Private text to a third party | `RouteDecisionProvider` chain of spec 040 |

### 3.1 Why `on` is not a latency feature today

`AISIS_JEV=on` awaits the Jev call (p50 ~0.25 s, budget 0.4 s) **before** every brain call it does not refuse; only a confident `garbled` or `command` saves the ~2.4 s brain round trip (audit §1, p50 2.4 s). With the measured inputs [INFERENCE: arithmetic]:

| Share of brain-bound turns that Jev refuses or re-prompts | Source | Average change per brain-bound turn |
|---|---|---|
| 6 of 65 = 9.2 % | [jev-bench] phrases the grammar abstains on, 0.95 | 0.092 × (2.4 − 0.25) − 0.908 × 0.25 = **−0.03 s** (i.e. +0.03 s slower) |
| 2 of ~80 = 2.5 % | [measured now, replay], Jev-visible rows | 0.025 × (2.4 − 0.255) − 0.975 × 0.255 = **−0.19 s** (+0.19 s slower) |

After the grammar plan reaches ~90 % (audit §7) only ~10 % of addressed turns (≈0.8 a day at the audited 9 spoken utterances a day) reach this branch at all. The refusals themselves are a **correctness and wait-time** feature (an instant honest answer instead of 2.4 s of a model saying «не умею»), not a speed-up. Running Jev **concurrently** with the brain call and cancelling the brain on a confident refusal would remove the +0.25 s, but cancelling a dispatched turn collides with «no retry of uncertain dispatch» (fast-llm-routing §6.3); that is a design decision for the owner of the adapter, not made here.

### 3.2 The 149-row measurement behind rows 3, 4, 6, 7, 10

[measured now] 2026-11-16: the three questions of `jev.py` (`UTTERANCE`, `TIER`, `EFFORT`) sent once for each of the 149 utterances of [fast-llm-eval.jsonl](fast-llm-eval.jsonl) (labels fast 38, reprompt 30, escalate 71, deterministic 10) through `JevClient` as the adapter builds it, 4 in parallel, after one warm-up call: **149/149 answered, 0 failures**, p50 255 ms, p95 324 ms, max 438 ms, 3 calls over 0.4 s. One run, so every rate below carries the wide interval of its n. The grammar replay is the `intent-jev` `IntentRouter.classify` plus `calc.evaluate` over the same text, stateless and without the weather/music skills.

Tier chosen (raw; and the number at confidence ≥ 0.95 in brackets):

| Label (n) | fast | balanced | deep |
|---|---|---|---|
| fast (38) | 38 (25) | 0 | 0 |
| reprompt (30) | 25 (1) | 5 | 0 |
| deterministic (10) | 3 (0) | 7 | 0 |
| **escalate (71)** | **16 (0)** | 42 (4) | 13 (2) |

`fast` at confidence ≥ t over all 149 rows: t = 0.5: 64 rows (9 escalate); t = 0.7: 49 (2 escalate); t = 0.9: 32 (0 escalate, 29 fast, 3 reprompt); t = 0.95: 26 (0 escalate, 25 fast, 1 reprompt). The 16 wrong raw `fast` answers include 8 of 32 facts, 5 of 30 complex, 3 of 8 traps, all with tier confidence ≤ 0.73.

Utterance kind: `command` at ≥ 0.9: 6 rows (4 escalate, 2 reprompt), at ≥ 0.95: 4 (2 escalate, 2 reprompt; the 2 reprompt rows are the bare «Включи» / «Выключи», which the grammar answers first as `asr.dangling_verb`). `garbled` at ≥ 0.95: 1 («Мм», correct, and the grammar answers it first as `unaddressed.filler`). `not_addressed`: 6 rows, none ≥ 0.8.

## 4. How to use it

### 4.1 Flags and environment

The adapter reads its environment from `~/.config/aisis/runtime.env` (systemd `EnvironmentFile`; service `aisis-station-adapter`, health on `127.0.0.1:$AISIS_ADAPTER_PORT`, default 31117).

| Name | Where | Default | Meaning |
|---|---|---|---|
| `AISIS_JEV` | adapter env | `off` | `off`: no Jev, the old path byte for byte. `shadow`: decide **concurrently** with the brain, log, never change or delay a reply. `on`: wait for Jev (≤ 0.4 s) before the brain; act on a confident `garbled` (reprompt, once a minute per station) or `command` (refusal); `talk` goes to the brain with tier and effort only logged. |
| `VERCEL_JEV_KEY` | adapter env | unset | Gateway key. Without it Jev is not used whatever `AISIS_JEV` says (`jev.from_env` returns `None`; `/healthz` shows `"active": false`). Never printed or logged. |
| `AISIS_JEV_URL`, `AISIS_JEV_MODEL` | adapter env | `https://ai-gateway.vercel.sh/v1/decisions`, `typesafe-ai/jev` | Endpoint and model id. |
| `AISIS_JEV_TIMEOUT` | adapter env, seconds | 0.4 | Per-call budget; it can only be **lowered** (capped at 0.4, floor 0.05; a non-number falls back to 0.4). |
| `AISIS_INTENT_GRAMMAR` | adapter env | `on` | `on|shadow|off`. In `shadow` the grammar does not answer, so Jev's shadow lines then also include turns the grammar would have answered: **run the Jev shadow week with the grammar `on`** or drop the rows whose `intent_shadow` line matched. |
| `--jev` of `python3 -m intent_grammar nightly prepare|run` | CLI | off | Label clusters with Jev; needs `VERCEL_JEV_KEY` in `~/.config/aisis/intent-nightly.env`. A failure is a missing label. |
| code constants, **not** env | `jev.py` | `CONFIDENT = 0.95`, `BREAKER_FAILURES = 4`, `BREAKER_OPEN_S = 30`, `HARD_TIMEOUT_S = 0.4`; the adapter's keep-warm request every 25 s and `ClientTimeout(total=1.0)` | Changing them is a code change (§6 asks for the threshold as an env). |
| `AISIS_FAST_TIER` | proposed | n/a | In the three-route design (#88); **not in code**. |

### 4.2 The `RouteDecisionProvider` wiring

- Contract (`docs/architecture/contracts.md`): `RouteDecisionProvider.decide(turn) -> RouteDecision{kind, model_alias, harness, effort, tool_groups, context_budget_tokens, time_budget_ms, reason_code}`. Spec 040's chain: deterministic rules, optional local Laya, optional Jev, conservative static fallback; «loss of Jev never blocks the static/deterministic fallback».
- `jev.py` implements it with two deliberate deviations: `decide` returns `RouteDecision | None` (**`None` is an abstention** so the chain can fall through) and the turn is `RouteTurn(text)`, not a `ProductTurn` with identity (spec 140). `RouteDecision` adds `utterance` (`command|talk|garbled|not_addressed`) and `confidence`.
- `from_env` builds `ChainedRouteProvider([JevRouteProvider(client), StaticRouteProvider()])`. The static provider never abstains (`kind=model, balanced, medium, reason_code=static_fallback, confidence=0.0`), so **in `on` and `shadow` every call yields a decision**: a line with `conf=0.00` is an abstention (the call failed, the utterance confidence was below 0.95, or the breaker was open), not a Jev answer. The deterministic layers are not providers in this chain: they run earlier in `adapter._process_turn` (`IntentRouter`, `calc_evaluate`, `skills.answer`).
- `JevRouteProvider.decide`: breaker check, one call asking `UTTERANCE`, `TIER`, `EFFORT`, abstain if the utterance confidence < 0.95, then `route_from`: `garbled` → `deterministic/jev_garbled`, `not_addressed` → `deterministic/jev_not_addressed`, `command` → `deterministic/jev_command_unsupported`, `talk` → `model/jev_talk` with the tier (`balanced` if the tier confidence < 0.95) and the effort (`medium` if its confidence < 0.95). Degradation never goes upward.
- Laya has a **different** place today: the adapter proxies the local classify socket at `POST /laya` for the quasar container's semantic-intents shadow layer; it is not a `RouteDecisionProvider`. A Laya provider would sit before Jev in the chain; §5 says why not for routing.
- Breaker: 4 consecutive failures open it for 30 s, then exactly one probe is let through; a success closes it. `/healthz` returns `jev.mode`, `jev.active` and `jev.stats` `{calls, ok, failed, skipped_open, low_confidence}`.

### 4.3 Reading the logs

Lines (logger `aisis.yandex`, no transcript, all in the journal):

```
INFO:aisis.yandex:jev_shadow utterance=talk tier=fast effort=none conf=0.97     # shadow
INFO:aisis.yandex:jev_route utterance=command tier=None effort=none conf=0.99   # on
WARNING:aisis.jev:jev_failed reason=timeout breaker_open=False                  # also: http 401, transport ClientConnectorError, malformed answer
```

`conf` is the utterance confidence for `command`/`garbled`/`not_addressed`, and `min(utterance, tier, effort)` for `talk`; `conf=0.00` is the static fallback (an abstention). **What the logs cannot show today**: a decision below 0.95 (the provider abstains before logging; `stats.low_confidence` counts it), the reason of an abstention other than `jev_failed`, the latency of a call, the cost, and which turn a line belongs to. Counts and bands:

```
journalctl --user -u aisis-station-adapter --since '7 days ago' -o cat --no-pager | python3 summarise.py
curl -s 127.0.0.1:${AISIS_ADAPTER_PORT:-31117}/healthz | python3 -m json.tool      # jev.stats: failed, skipped_open, low_confidence
```

(`summarise.py` is in §8.) Healthy shadow: `failed / calls` ≤ 1 %, `skipped_open` = 0, no `http 4xx` (a 401/403 is the key or the card), `jev_failed reason=timeout` ≤ 1 % (0.7 % is the benchmark's over-budget share).

### 4.4 Agreement against the deterministic route and against labelled truth

Three comparators, from cheapest to strongest. **Shadow lines carry no text and no turn id**, so the first two do not need them.

1. **Jev vs the deterministic route on text** (no humans, no shadow needed): replay the corpus through the production grammar (`python3 -m intent_grammar backtest --route adapter`, the same code that answers a turn) and through Jev with `bench_jev.py --corpus <dir>`, which prints the confusion of Jev's answer against the corpus label mapped to the four kinds, accuracy by confidence threshold and latency percentiles, and never prints a transcript. Disagreements where the deterministic label is a real intent are Jev errors by construction.
2. **Jev vs the owner's labels on the corpus:** the labelled real corpus (`corpus/real-voice-YYYY-MM.jsonl`, grown nightly) is the unbiased test; the held-out split of the back-test (`holdout_percent`) keeps tuning honest.
3. **Jev vs labelled truth on live turns:** join each `jev_shadow` line to the transcript that caused it with `join.py` (§8: the latest `user/speech` turn of `dialogs.sqlite3` that precedes the line by ≤ 5 s; two stations speaking within 5 s can mismatch, so drop ambiguous rows), label the `truth` column by hand (`talk | command | garbled | not_addressed`, plus the tier if it is judged; the audit labelled by hand the same way), then count per class and confidence band: precision, coverage, wrongly-fast, wrongly-refused. The output file holds real utterances (mode 0600): keep it on the host.

### 4.5 Rollout

**Volume caveat.** The audited household spoke ~9 utterances a day and the brain answered ~4 of them; after the grammar work ~1 turn a day reaches a model [INFERENCE from audit §1, §7]. A week of shadow is therefore **a few to a few tens of rows**: enough to prove availability, latency and stability, **not** enough to calibrate precision. Accuracy gates are measured on the corpus (comparators 1 and 2); the shadow week is the operational gate.

| Stage | Setting | Exit gate |
|---|---|---|
| 0 now | `AISIS_JEV=off` (production today) | The `intent-jev` branch merged; the key in `runtime.env`; the owner has read the gateway's retention terms (§2.2). |
| 1 shadow, 7 days | `AISIS_JEV=shadow`, grammar `on` | Operational: `failed/calls` ≤ 1 %, `skipped_open` = 0, no `http 4xx`, no journal growth; shadow lines' `talk`/tier/effort read sensibly next to what the brain answered. Latency: a real p95 ≤ 0.4 s from the host (a one-off `bench_jev.py --limit 100` from the speaker host while shadow runs; the log has no latency, §6). |
| 2 calibrate offline | corpus run, per class | See the thresholds below. Re-run when the corpus grows. |
| 3 `on` for refusals only | `AISIS_JEV=on` | Only when row 3's gates hold **and** per-class switches and the emergency stem guard exist. `on` today enables `garbled` and `command` together and nothing else. |
| 4 tier advice acts | lite tier + the route question | Only with the lite tier of #88 in shadow first (its own gate: ≥ 300 shadow turns, #88 §7), then the design's «any doubt means escalate». |

Thresholds (all to be **re-measured on the `/v1/decisions` `confidence`**, §2.1; the starting values are what the evidence supports):

| Class | Act at | Evidence now | Promotion gate |
|---|---|---|---|
| refuse `command` | ≥ 0.95, and never when an emergency/safety stem matches | 6/6 right [jev-bench]; 1 of 2 right on the Jev-visible 149-row subset; emergencies at 0.92–0.93 | ≥ 40 labelled `command ≥ 0.95` rows with 0 wrong refusals (Wilson 95 % lower bound 0.91) |
| reprompt `garbled` | start ≥ 0.9, set from data | fires on ≤ 1 row in 149 at 0.95; 4/30 at ≥ 0.8, all correct | Re-word with the route question, then ≥ 40 rows with 0 wrong reprompts |
| fast tier (via the route question) | ≥ 0.9 and veto-only | tier `fast` ≥ 0.9: 0/71 wrong with `jev.py`'s TIER; route question: 0/71 and 0/30 [FastModelBench] | 0 wrong-fast in ≥ 300 shadow turns plus the 71 escalate items; the Wilson 95 % upper bound on 71 is 5.1 % |
| effort | none | confident on 21/149 | do not act |
| nightly label | n/a | not measured | none: proposals pass the back-test |

### 4.6 Kill switch

- Per process: `AISIS_JEV=off` in `~/.config/aisis/runtime.env`, then `systemctl --user restart aisis-station-adapter`; check `curl 127.0.0.1:31117/healthz` shows `jev.mode: off, active: false`. Deleting `VERCEL_JEV_KEY` has the same effect. The environment is read at start: there is no hot toggle.
- Automatic: a hard 0.4 s timeout per call, a breaker after 4 consecutive failures (30 s), every failure an abstention. A slow Jev costs at most its budget, a dead one costs the breaker's open time (unit test `test_a_slow_jev_costs_the_budget_not_the_turn`).
- At the provider: revoking the key or a lapsed card (HTTP 401/403, the same `customer_verification_required` that blocked the first benchmark) makes every call a failure, the breaker opens, and the speaker behaves as with Jev off.
- Nightly: omit `--jev`.

### 4.7 Cost tracking

Price: $0.042 per 1M input tokens, no output charge (gateway catalogue, [FastModelBench]). **Measured now**: the three-question call is 604–623 input tokens (127 output, not billed) and the gateway's `provider_metadata.gateway.cost` was **$0.0000254 to $0.0000262 (mean $0.0000257)** on 5 calls; a one-question call (339 tokens) $0.0000142. Per 1000 calls ~$0.026. The adapter does not log the cost (§6); until it does, estimate `jev.stats.calls × $0.000026` from `/healthz` and compare with the gateway dashboard's usage for the key. At the audited volume (≈4 brain-bound turns a day) a year is ~$0.04; a runaway cannot happen faster than people speak (the call is per turn, there is no loop and no retry).

## 5. Results: Jev vs Laya vs LLMs as task-type classifier

Source: [FastModelBench] (`docs/research/fast-llm-bench-2026-11.md`, harness `scripts/bench_fast_llm.py`, 149 labelled utterances, run 2026-11-16 on `ubuntu-pc`; **single run, n = 149, differences of a few points are noise**; the Claude latencies include omp's process start and a ~22k-token prefix, so they are upper bounds). The document lands with #106; if its merged numbers differ, they win. «Wrongly fast» = an escalate-labelled utterance routed to the fast tier (of 71).

| Classifier | Route acc (4 routes) | 7-type acc | Wrongly fast (of 71) | Reprompt sent to fast (of 30) | p50 / p95 ms | Cost per 1000 calls |
|---|---|---|---|---|---|---|
| **Jev**, route question | **92 %** | 84 % | **0** | **0** | **246 / 292** | ~$0.014 [INFERENCE from price × 350 tokens; $0.0000142 measured for a 339-token call, §4.7] |
| Qwen 3.8 27B (Cerebras, no reasoning) | 93 % | 89 % | 0 | 2 | 226 / 828 | $0.282 |
| gpt-oss-120b (Cerebras, low) | 93 % | 83 % | 0 | 0 | 303 / 464 | $0.150 |
| Sonnet 5.5 | 95 % | 91 % | 0 | 0 | 1,958 / 2,520 | $0.470 |
| Haiku 5.5 | 95 % | 85 % | 0 | 0 | 1,051 / 1,237 | $0.024 |
| Laya multilingual (local), best threshold | 49 % (at 0.7) | 15 % (at 0.0) | 1 at 0.7; 43 at 0.0 | 1 at 0.7; 15 at 0.0 | 204 / 214 (352 / 378 with 7 types) | $0 |
| Laya english (local) | 22–48 % | 11 % at best | 0 only where deterministic recall is 0 % | 0 only where deterministic recall is 0 % | 22 / 22 | $0 |

Reading:

- **Jev is a good classifier**, equal to the LLMs within noise on route accuracy, with the tightest tail of the remote options (p95 292 ms against Qwen's 828 ms and gpt-oss's 464 ms) and a price 6–20× below the fast LLM classifiers. A confidence threshold (probability of the top choice, below it = escalate) costs accuracy and never added a wrong-fast answer: route accuracy 92 % / 90 % / 87 % / 77 % at 0 / 0.5 / 0.7 / 0.9, 0 wrongly fast at each. Its misses go the safe way: 6 escalate items sent to deterministic (3 of them date/weather questions the design itself calls tool-answerable, labelled `escalate` in the corpus: relabelled it would be 94 %), 4 noisy items to escalate, 2 praise/cancel items to reprompt. By type (7-type run): canned 100 %, deterministic 100 %, complex 87 %, open fact 81 %, mixed/follow-up 78 %, banter 73 %, reprompt 73 %.
- **Laya multilingual is not usable as a router on this set**: 29 % route accuracy with 43 of 71 escalate utterances sent to fast at no threshold; at 0.7 only 1 is wrong but deterministic recall is 0 % and accuracy 49 %: it escalates nearly everything. Its probabilities are not calibrated for this checkpoint (its model card says so), and it was run with untuned criteria text. Laya english is 22 ms but 22–28 % on Russian.
- **Qwen as its own classifier** matches Jev (93 %) at the same p50 with a wider tail and 11–20× the cost; it needs no extra vendor. Haiku is cheapest of the LLMs ($0.075 per 1000 classify-then-generate pairs) but 1.1 s, so it is a nightly or fallback tool, not a voice-turn classifier.
- **The wording matters more than the model** (§2.1): with `jev.py`'s TIER question Jev was **not** equally safe (16 of 71 hard utterances on `fast` raw, §3.2). The route result is a property of the route question, which `jev.py` does not ask yet.
- Not measured by anyone: Jev on slots, Jev's availability over days, rate limits, real speech noise, Jev's nightly labelling accuracy, Laya with tuned criteria.

## 6. Gaps and what they need (outside this document; tracked as #125)

1. A **route-type question** in the wording of the benchmark (deterministic / fast / reprompt / escalate) next to `UTTERANCE`, `TIER`, `EFFORT`; re-run `bench_jev.py` and the 149 rows with it (rows 2, 4, 6).
2. **Per-class switches** for `on` (refusal vs reprompt) and an **env for the 0.95 threshold** (`CONFIDENT` is a constant), plus a **deterministic emergency/safety stem guard** in front of any refusal text.
3. **Shadow logging that supports §4.4**: a turn id (or a hash of the text) on the `jev_shadow` line, the call latency, the gateway `cost`, the abstention reason, and the below-threshold decision (kind, confidence) so the confidence bands can be calibrated from shadow data.
4. Decide whether `on` should run Jev **concurrently** with the brain (§3.1).
5. Let `tier` gate independently of the utterance confidence if a lite tier is added (today the utterance confidence also gates the tier hint: 6 of 38 fast items get `alias=fast`).
6. `label_with_jev` accuracy on a labelled sample of clusters.

## 7. Not verified

- Vercel/TypeSafe retention, logging and training terms for gateway requests; whether a zero-retention setting exists for this key.
- Jev's behaviour over days: availability, rate limits, price changes (the 0.4 s budget and the breaker are the protection, not a guarantee).
- Latency from the actual speaker host; both benchmarks ran on `ubuntu-pc`.
- The grammar replay of §3.2 is stateless and omits the weather/music skills; the counts of rows Jev would see are approximate.
- The 149-row eval corpus was written for the router benchmark, not for Jev's `command` option (its labels do not say «refuse» or «answer»); the wrong-refusal judgements of §3.2 are mine.

## 8. Scripts used (not committed as code; they hold no secrets)

`summarise.py` (counts and bands from the journal lines, §4.3):

```python
import collections, re, sys
LINE = re.compile(r"jev_(shadow|route) utterance=(\w+) tier=(\w+) effort=(\w+) conf=([\d.]+)")
kinds, bands, total, abstained = collections.Counter(), collections.Counter(), 0, 0
for line in sys.stdin:
    m = LINE.search(line)
    if not m:
        continue
    total += 1
    conf = float(m[5])
    if conf == 0.0:                   # the static fallback answered: a failure, utterance conf < 0.95, or the breaker open
        abstained += 1
        continue
    kinds[m[2]] += 1
    bands[(m[2], "tier=" + m[3], "conf>=0.99" if conf >= 0.99 else "0.95..0.99" if conf >= 0.95 else "<0.95 (talk: weak tier/effort)")] += 1
print(f"jev lines {total}, abstained {abstained}, decided {total - abstained}")
for kind, n in kinds.most_common():
    print(f"  {kind}: {n}")
for key, n in sorted(bands.items()):
    print("  ", *key, n)
```

`join.py` (shadow lines to transcripts for hand labelling, §4.4; run on the host, output `0600`):

```python
import bisect, os, re, sqlite3, subprocess, sys
DAYS = sys.argv[1] if len(sys.argv) > 1 else "7"
LINE = re.compile(r"^(\d+\.\d+) \S+ \S+: .*?jev_(?:shadow|route) utterance=(\w+) tier=(\w+) effort=(\w+) conf=([\d.]+)")
journal = subprocess.run(["journalctl", "--user", "-u", "aisis-station-adapter", "--since", f"{DAYS} days ago", "-o", "short-unix", "--no-pager"],
                         capture_output=True, text=True, check=True).stdout
db = sqlite3.connect("file:" + os.path.expanduser("~/.local/share/aisis/dialogs.sqlite3") + "?mode=ro", uri=True)
heard = db.execute("SELECT ts, text FROM dialog_turns WHERE role='user' AND kind='speech' AND text<>'' ORDER BY ts").fetchall()
stamps = [ts for ts, _ in heard]
fd = os.open("jev-shadow-to-label.tsv", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as out:
    out.write("ts\tutterance\ttier\teffort\tconf\ttext\ttruth\n")
    for line in journal.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        ts = float(m[1])
        i = bisect.bisect_right(stamps, ts) - 1          # the latest utterance heard before the log line
        text = heard[i][1] if i >= 0 and ts - stamps[i] <= 5 else ""
        out.write("\t".join([m[1], m[2], m[3], m[4], m[5], text.replace("\t", " "), ""]) + "\n")
```

The 149-row run of §3.2: from the repository root, with the key loaded into the subshell only, using the adapter's interpreter (`~/.local/share/aisis/venv/bin/python`, which has `aiohttp`) and the `jev.py` of the `intent-jev` branch on `sys.path`: for each row of `docs/research/fast-llm-eval.jsonl` call `jev.JevClient(jev.aiohttp_post(session), key, timeout=3.0).choose(text, [jev.UTTERANCE, jev.TIER, jev.EFFORT])` four in parallel after a warm-up call, and count the answers by label and confidence. The grammar replay: `intent_route.IntentRouter(intent_route.load_grammar(None), None, None, None, catchall=True).classify(text)` and `calc.evaluate(text)`. The FastModelBench tables are rebuilt with the commands of §8 of its document.
