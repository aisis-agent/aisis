# Jev as the speaker's route classifier: live benchmark and where it is used

Ticket: alex-mextner/aisis#107 (audit §7, owner ask «Jev everywhere it can speed things up»). Date: 2026-11-16. Code: `services/yandex-station/code/jev.py`
(client, `RouteDecisionProvider`, breaker, nightly helper), `bench_jev.py` (this benchmark), the wiring in `adapter.py`. Contract: `docs/architecture/contracts.md`
`RouteDecisionProvider`; spec 040 («Jev is optional; loss of Jev never blocks the static/deterministic fallback»).

Labels: **[measured]** run live against the Vercel AI Gateway from the speaker host on 2026-11-16 (§3: the three-question call), **[measured, #125]** the four-question 149-row run of §5, from the development workstation, **[INFERENCE]** my reasoning.

## 1. What Jev is here

TypeSafe AI's decision model `typesafe-ai/jev` through the Vercel AI Gateway (`POST https://ai-gateway.vercel.sh/v1/decisions`, bearer `VERCEL_JEV_KEY`): typed
questions about a text (choice / predicate / score) answered together in one round trip, with confidences. Provider rule honoured: Vercel only for Jev; Claude stays
on the direct API; fast models stay on Cerebras. The gateway answered 200 with the key in the repository `.env` (the card is on the account now), so the former «may return 403»
caveat did not apply today; the code treats any non-200 as an abstention anyway.

## 2. Where it speeds the speaker up (and where it does not)

A Jev call is ~0.25 s warm. The deterministic route answers in ~1 ms, so Jev never sits in front of it. It is asked **only for turns that nothing answered** (the grammar, the quasar
layer, arithmetic, weather all abstained), which would otherwise wait ~2.4 s for the brain. One call asks four typed questions at about the same cost:

1. **Utterance kind** (route classification): `command` (something nobody implements: calls, shopping lists, …) → instant «Этого я пока не умею.» (`AISIS_JEV_REFUSE=on`); `garbled` → instant
   «Не расслышал. Повтори, пожалуйста.» (`AISIS_JEV_REPROMPT=on`; once a minute per station, microphone left open); `talk` → the brain; `not_addressed` → logged only. Never for a phrase that mentions an emergency.
2. **Tier**, 3. **effort** and 4. **route** (deterministic / fast / reprompt / escalate, the FastModelBench wording) for the brain call: advisory today (the brain command has no such knob and the lite tier of the fast-model work is not merged); logged so the tier table can be
   checked against real traffic before anything is wired to it.
5. **Nightly helper** (`jev.label_with_jev`, used by `intent_grammar nightly`): the nearest existing intent of each unanswered cluster, to propose grammar changes. Offline, no latency budget.

Off by default: `AISIS_JEV=off|shadow|on` and `VERCEL_JEV_KEY` in the adapter environment. `shadow` decides concurrently with the brain call and only logs `jev_shadow utterance=… tier=… effort=… conf=… … turn=<hash> ms=… cost=…` (never the text), so it
adds no latency. `on` acts only for the classes switched on (`AISIS_JEV_REFUSE`, `AISIS_JEV_REPROMPT`, default off each) at confidence ≥ `AISIS_JEV_THRESHOLD` (default 0.95). Hard budget 0.4 s per call (`AISIS_JEV_TIMEOUT` can only lower it), a breaker opens after 4 consecutive failures for 30 s, every failure is an abstention,
no exception reaches the turn, the key and the transcript never appear in a log line or an error (unit-tested). `/healthz` shows `jev.mode`, `acting`, the switches, the threshold and `jev.stats` (calls, ok, failed, skipped_open, low_confidence).

## 3. Numbers [measured]

`python3 bench_jev.py` sent every phrase of both corpora once (562 calls: 96 real, 466 synthetic; one warm-up call first; 4 in parallel; benchmark budget 2 s so the tail is visible).

- Reliability: **562 / 562 answered, 0 failures.**
- Latency from the speaker host: **p50 248 ms, p95 309 ms, max 533 ms; 4 of 562 (0.7 %) above the 0.4 s production budget.** A cold first call took ~0.8 s (TLS), which is why the adapter keeps one warm connection
  (a request every 25 s, like the Haiku corrector). The gateway's own record for the provider hop was ~0.17 s.
- Accuracy on the 65 phrases the deterministic route abstains on (these are the ones Jev would see), against the corpus labels mapped to the four kinds:

| corpus kind → Jev answered | talk | command | garbled | not_addressed |
|---|---|---|---|---|
| talk (33) | 30 | 3 | 0 | 0 |
| command (28) | 18 | 8 | 1 | 1 |
| garbled (4) | 2 | 0 | 2 | 0 |

  At the act threshold **0.95: 34 of 65 answers are that confident and 29 of them match the label; «command» acted on 6, all 6 right; «garbled» never reached 0.95** (so with this threshold it acts on «command» only).
  Across all 562 rows only 2 chit-chat phrases were ever answered «garbled/command» at ≥ 0.9, so the risk of refusing a real question is small but not zero. Honest reading: Jev is a weak detector of
  recognition garbage (2 of 4 right, below the threshold) and a decent one of «talk»; the «command» branch saves ~2.4 s on about a fifth of the brain-bound commands.
- Tier on the 54 chit-chat/knowledge phrases: **fast 35, balanced 18, deep 1**; effort none 42, low 9, medium 2, high 1. [INFERENCE] About two thirds of the brain's traffic would be served by a fast tier once one exists:
  that is the real latency win (the fast-model bench of the router work owns the tier table); this module only records the evidence and carries the decision shape (`RouteDecision.model_alias`, `effort`).

The corpus is one household for 11 days plus hand-written phrases; every rate above has a wide interval (the 65 rows decide ±12 points).

## 4. Owner steps (exact)

1. Add `VERCEL_JEV_KEY=…` (the key already in the repository `.env`) and `AISIS_JEV=shadow` to `~/.config/aisis/runtime.env`, then restart the adapter. Read `journalctl --user -u aisis-station-adapter | grep jev_shadow` for a week
   next to what the brain answered; `/healthz` must show `jev.stats.failed` near 0 and `skipped_open` 0.
2. If the lines look right, switch classes on one at a time: `AISIS_JEV=on` with `AISIS_JEV_REFUSE=on` (and `AISIS_JEV_REPROMPT=on` only after its own review), optionally `AISIS_JEV_THRESHOLD`. Roll back with `AISIS_JEV=off` (the old path, byte for byte).
3. Spend: ~$0.00003 to $0.00004 per call [measured from the gateway's cost field: $0.0000257 with three questions, $0.0000405 with four], only for brain-bound turns.
4. Privacy: the final transcript of those turns goes to Vercel/TypeSafe (spec 140 allows final accepted transcripts to model providers; no audio, no speaker identity). Leave `AISIS_JEV` off to keep them local.

## 5. The route question next to the tier question, 149 rows [measured 2026-11-16, #125]

`jev.ROUTE` is the FastModelBench route question (deterministic / fast / reprompt / escalate; instructions and option texts copied word for word from `scripts/bench_fast_llm.py` `ROUTE_QUESTIONS`, which the benchmark sent to `/v1/evaluate`). The adapter asks it in the same `/v1/decisions` call as `UTTERANCE`, `TIER`, `EFFORT` and only logs the answer. §3 above is the earlier three-question call; this run is the four-question call the adapter now makes.

Run: `VERCEL_JEV_KEY=… python3 bench_jev.py --eval docs/research/fast-llm-eval.jsonl --out report.json` from `services/yandex-station/code` (the 149 labelled utterances of the fast-model benchmark: fast 38, reprompt 30, escalate 71, deterministic 10; they are the benchmark's own synthetic rows, not household speech; one warm-up call, 4 in parallel, 2 s budget). One run, from the development workstation (not the speaker host), the key read from the untracked repository `.env` into the child process only. Every rate has the wide interval of its n.

| label \ route answer | deterministic | fast | reprompt | escalate |
|---|---|---|---|---|
| deterministic (10) | 10 | 0 | 0 | 0 |
| fast (38) | 0 | 36 | 2 | 0 |
| reprompt (30) | 0 | 0 | 26 | 4 |
| escalate (71) | 6 | 0 | 0 | 65 |

- **Route accuracy 137/149 = 91.9 %; 0 of 71 escalate rows and 0 of 30 reprompt rows routed to fast, with no threshold at all.** FastModelBench measured 92 % / 0 of 71 / 0 of 30 on `/v1/evaluate` (its §5): the `/v1/decisions` call reproduces it. The 6 escalate rows read as deterministic are the date/weather/translation rows its document already names.
- The tier question, same call and rows (`wrong fast` = answered fast for an escalate / reprompt row):

| answer confidence at or above | route question: escalate→fast, reprompt→fast | tier question: escalate→fast, reprompt→fast |
|---|---|---|
| 0 | 0, 0 | 16, 25 |
| 0.5 | 0, 0 | 11, 19 |
| 0.7 | 0, 0 | 1, 11 |
| 0.9 | 0, 0 | 0, 3 |
| 0.95 | 0, 0 | 0, 1 |

  The route question is safe at every threshold and the tier question only from 0.9: the wording, not the model, carries the result (the 16 of 71 of the usage document's §3.2 is reproduced).
- Latency p50 258 ms, p95 362 ms, max 466 ms; **5 of 149 (3.4 %) above the 0.4 s production budget** (the three-question runs saw 0.7 % and 2 %). Gateway cost mean **$0.0000405 per call** over 149 calls (the three-question call was $0.0000257): the fourth question adds tokens and cost, and perhaps tail. [INFERENCE] 149 calls cannot separate that from noise; the shadow week's `ms` and `cost` fields measure it on the speaker host.
- What this does not show: Jev on the household's real brain-bound phrases (they are mostly not in this set), the route answer's agreement with the owner's labels, and whether the fourth question moves the other three answers (the utterance confusion of §3 was not re-run on the corpus with the four-question call).

## 6. On-mode controls and what is deliberately not built

- `AISIS_JEV_REFUSE`, `AISIS_JEV_REPROMPT` (`on`|`off`, default `off` each) switch the two acting classes independently; `AISIS_JEV=on` with neither is advisory and runs like `shadow`. `AISIS_JEV_THRESHOLD` sets the confidence needed to act (default 0.95, 0.5 to 1.0; an invalid value falls back to 0.95 and is logged).
- `jev.is_emergency` (a deterministic stem guard: скор*, полици*, пожар*, помощ*, «плохо», сердц*, дыш*, тон*, отрав*, сознан*, врач*, 112 …) keeps a phrase that mentions an emergency away from Jev in `on` mode: it never gets Jev's refusal or reprompt (test: «Мне плохо, вызови скорую», «Позвони в полицию»). Over-inclusive by design.
- Shadow lines carry a 10-hex `turn` hash (never the text), latency `ms`, gateway `cost`, `outcome` (`ok` / `below_threshold` / the failure reason), the `decision` the answers read as even below the threshold, and all four answers with confidences (format: `jev-usage-2026-11.md` §4.3).
- **Open, documented only** (`jev-usage-2026-11.md` §6 items 4, 5): whether `on` should run Jev concurrently with the brain call (it is serial today, +~0.25 s on every turn it does not answer) and an independent tier gate that makes doubt go up. Neither is built.
