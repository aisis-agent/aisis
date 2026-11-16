# Fast LLM benchmark and per-task-type routing, November 2026

Ticket: alex-mextner/aisis#106 (relates to #88, the three-route design in [fast-llm-routing.md](fast-llm-routing.md)). Measured 2026-11-16 from `ubuntu-pc`, the owner's PC (not the speaker host). Harness: `scripts/bench_fast_llm.py`. Results in `fast-llm-bench-data/results/`: `report.json` (every metric behind the tables), `smalltalk-replies-and-judgements.json` (the 80 small-talk replies, blind rubric scores and side-by-side verdicts, readable), `raw-per-call.tar.gz` (every call: text, timings, tokens; unpack into an output directory and run `report` on it).

Labels: **[here]** measured by this harness, **[vendor]** a provider's own figure, **[INFERENCE]** reasoning, not observed. All results are one run on one day: n is small (47 to 149 items per metric), so differences of a few points are noise (§7).

## 1. Answer first

1. **The owner's assumption "fast models are dumb and suit only simple tasks" is half right.** On the tasks the voice speaker actually gives a fast model, Qwen 3.8 27B on Cerebras (reasoning off) matches Sonnet 5.5: slot extraction 100% vs 99% (five slots), task-type classification 89% vs 91% (about 3 items of 149, within noise), small-talk rubric 7.12 vs 7.17 out of 8, zero wrong fast answers (0/71) for all four models. It is clearly weaker only on multi-step reasoning (6/8 vs 8/8 on a tiny set) and it refuses a fifth of small talk (4/20 answered `ESCALATE`). gpt-oss-120b is the weaker of the two fast models for this job: five-slot accuracy 86%, small-talk rubric 6.61, one stale fact (wrong capital of Kazakhstan).
2. **What fast models buy is speed, not savings.** Total latency p50 for a short reply: Qwen 238 ms, gpt-oss 322 ms, Haiku 1,126 ms, Sonnet 2,320 ms (the Claude figures go through omp and are upper bounds, §2). But the tail is not free: Qwen exceeded 1 s on 31 of 514 calls (6%, worst 7.9 s); gpt-oss on 5 (1%, worst 2.2 s).
3. **The critical safety metric holds on this corpus, with a stated limit.** Wrong fast answers on open-world facts: 0/32 facts and 0/71 escalate-labelled utterances for every model, with the design's own generator prompt and no rules in front. The 95% Wilson upper bounds are 10.7% and 5.1%, so this is "no failure observed", not proof. A separate classifier call added about 250 to 300 ms and no measurable safety gain here (§4.1).
4. **Cheapest by far is Haiku 5.5, not Cerebras:** the design's classify-then-generate pair costs $0.075 per 1000 requests on Haiku, $0.31 on gpt-oss, $0.61 on Qwen, $1.52 on Sonnet (list prices x measured tokens). Haiku's problem is 1.1 s latency, so it suits non-voice work (nightly analysis, fallback).
5. **Jev is a good classifier; Laya is not usable as a router on this set.** With the four routes Jev reached 92% route accuracy, 0 wrongly-fast of 71, 100% recall on tool-answerable items, 246 ms p50. Laya multilingual reached 29% with 43 of 71 escalate utterances sent to fast, and becomes safe only at a probability threshold where it escalates nearly everything (§5). Qwen as its own classifier is as accurate as Jev (93%) at the same p50 but with a worse tail.
6. **Routing table (§6):** rules/canned for greetings and noise; Qwen for banter and slot filling; Sonnet for facts, advice and anything mixed; gpt-oss and Haiku as fallbacks; Jev optional.

## 2. Method

### 2.1 What was called, per model

| Model | Provider and path | Notes |
|---|---|---|
| Qwen 3.8 27B | Cerebras direct, `qwen-3.8-27b`, `reasoning_effort: none`, streaming over one keep-alive connection | key `CEREBRAS_KEY` from the owner's untracked `.env`, loaded into the process only |
| gpt-oss-120b | Cerebras direct, `gpt-oss-120b`, `reasoning_effort: low`, completion budget 1024 (the design's thinking budget) | cannot switch reasoning off; reasoning tokens are billed and counted |
| Claude Sonnet 5.5 / Haiku 5.5 | the `omp` CLI, which calls the **Anthropic Messages API directly** (events show `api: anthropic-messages`, `provider: anthropic`) with omp's own configured Anthropic account; thinking off, tools off, minimal system prompt | no raw Anthropic key exists in `.env` or the keychain. Not through Vercel. The harness also has `--claude-via anthropic` (needs `ANTHROPIC_API_KEY`), tested only against a local fake server, never against the real API |
| Jev | Vercel AI Gateway `POST /v1/evaluate`, model `typesafe-ai/jev` (the same call `task classify` makes), key `VERCEL_JEV_KEY` | the gateway is used for Jev only. A first attempt returned 403 `customer_verification_required` until the owner added a card; all Jev numbers below come from the run after that |
| Laya | resident local Laya classify server on this PC (unix socket), `multilingual` checkpoint (and `english` for contrast) | shared GPU/CPU with the speaker's other services |
| Judge | `anthropic/claude-mythos-5-2` via omp, the most expensive and newest model available; not a contestant | same vendor as two contestants, so rubric scores may favour Claude style; the Qwen vs gpt-oss side-by-side is not affected by that |

Prices (USD per 1M tokens, in/out): Qwen 0.99/1.49 and gpt-oss 0.35/0.75 from the design doc; Sonnet 2/10 and Haiku 0.1/0.5 from the omp model registry; Jev 0.042 per 1M input tokens, 0 output (gateway catalog).

**Caveat on Claude latency.** omp adds a ~22k-token cached prefix (measured: 21,991 tokens; the content is not visible, probably MCP tool definitions [INFERENCE]) and a process start per call. Claude latencies are therefore upper bounds. The design doc quotes published figures of 1.5 s (Sonnet) and 1.1 s (Haiku) for the real API [vendor/AA]. The same prefix made Haiku answer the user instead of classifying on 54% of route calls in a first run; every prompt is now wrapped as `Распознанная фраза пользователя: «…»` for **all** models, which fixed it. The first, unwrapped run is not used except as a second latency sample (§7).

### 2.2 Data

- [fast-llm-eval.jsonl](fast-llm-eval.jsonl): **149** labelled utterances in total (the 10 deterministic ones are part of the 149, not extra): fast 38, reprompt 30, escalate 71 (32 facts, 30 complex, 8 traps, 1 follow-up), deterministic 10.
- Task types (the unit of the routing table), assigned from the corpus `tag`: `canned_smalltalk` (greeting, farewell, thanks, praise, ack: 23), `banter` (persona, joke, mood: 15), `reprompt` (noise, incomplete, wake word: 30), `deterministic_skill` (conversion, weather, time: 10), `open_fact` (32), `complex` (30), `mixed_or_followup` (trap + follow-up: 9). Route = fast for the first two, reprompt, deterministic, escalate for the rest.
- [fast-llm-bench-data/](fast-llm-bench-data/): `slots.jsonl` (47 phrases with gold city/day/room/device/value), `smalltalk.jsonl` (20 small-talk prompts), `paraphrase.jsonl` (12 tool results), `knowledge.jsonl` (12 static facts, 8 small reasoning tasks), all written for this benchmark.

### 2.3 Prompts

The **generator** system prompt and the **classifier** instruction are the design's §6.2 prompts verbatim (classifier on Cerebras with the strict JSON schema; on Claude prompt-only JSON). The 7-type router prompt extends the classifier with the task types above. Slot, paraphrase and plain-QA prompts are in the script. Temperature 0 for classification/slots, 0.3 for the generator, as in the design; omp cannot set temperature, so Claude ran at its default.

### 2.4 Metrics

- **Latency**: TTFT = request sent to first answer token; total = to last token; 50 timed runs after 2 warm-ups, six short banter prompts, connection kept alive; sequential, one model at a time. tok/s = (output tokens - 1) / decode time, 20 runs of a story prompt capped at 150 tokens for Cerebras (omp cannot cap, Claude wrote about 600 to 1,000 tokens). First token "incl. reasoning" counts gpt-oss reasoning deltas.
- **Route accuracy**: type accuracy (7 labels) and route accuracy (4 routes). **Wrong fast answer**: the model returns anything other than `ESCALATE` to an escalate-labelled utterance (generator alone), after the design's code validator, and through the full classifier + generator + validator pipeline.
- **Slots**: exact match after case/ё folding; an invented slot is a non-null value where gold is null.
- **Paraphrase**: the reply names the city and the condition, states the signed temperature right next to «градус»/° or after «температура» (digits or Russian number words; a lost minus or a temperature swapped with the wind fails), and contains no number beyond the temperature and the wind. Declined number words beyond a short list are not parsed.
- **Knowledge sanity**: a number answer must equal a number in the reply exactly (digits or Russian number words, «сорок пять» is not «сорок»); text answers must start a word of the reply; the quadratic needs both roots. A reply listing several numbers can still pass; ordinal forms such as «тысяча девятьсот девяносто первый» are not parsed.
- **Refusal**: the generator reply is a refusal only when it is exactly `ESCALATE` (punctuation aside); a reply that mentions the token is scored as an answer.
- **Russian quality**: blind rubric by the judge, 0-2 each for grammar, naturalness, persona rules (1-2 short sentences, no lists/digits/facts/promises), fit/warmth, total 0-8, over answered replies only (counts differ per model); plus two blind side-by-side passes Qwen vs gpt-oss (positions swapped between passes) over the 16 prompts both answered = 32 judgements.
- **JSON adherence**: output is exactly one JSON object with a valid label, no prose or fences.
- **Cost**: measured average tokens (reasoning included) x list price.

## 3. Per-model quality and speed

**Speed (ms, short banter prompts, this PC)**

| model | runs | TTFT p50 / p95 | first token incl. reasoning p50 | total p50 / p95 | tok/s p50 (p5) | errors |
|---|---|---|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 50 | 226 / 595 | 226 | 238 / 607 | 1,027 (743) | 0 |
| gpt-oss-120b (Cerebras, low) | 50 | 304 / 417 | 292 | 322 / 474 | 1,295 (1,126) | 0 |
| Sonnet 5.5 | 50 | 1,991 / 2,393 | 1,991 | 2,320 / 2,909 | 98 (92) | 0 |
| Haiku 5.5 | 50 | 903 / 1,063 | 903 | 1,126 / 1,353 | 158 (149) | 0 |

**Task-type classification by the model itself (149 labelled utterances, prompt-only JSON)**

| model | type acc (7) | route acc (4) | wrongly fast on escalate | ...of which facts | reprompt sent to fast | fast recall | strict JSON | latency p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 89% | 93% | 0/71 | 0/32 | 2/30 | 95% | 100% | 226 / 828 |
| gpt-oss-120b (Cerebras, low) | 83% | 93% | 0/71 | 0/32 | 0/30 | 95% | 100% | 303 / 464 |
| Sonnet 5.5 | 91% | 95% | 0/71 | 0/32 | 0/30 | 100% | 99% | 1,958 / 2,520 |
| Haiku 5.5 | 85% | 95% | 0/71 | 0/32 | 0/30 | 100% | 100% | 1,051 / 1,237 |

**Wrong fast answers: the model answers an escalate-labelled utterance (design generator prompt, no rules in front)**

| model | any reply, all escalate | facts | traps+followup | complex | after post-validator | full pipeline (classifier+generator+validator) | tool-answerable answered by LLM (of 10) |
|---|---|---|---|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 0/71 | 0/32 | 0/9 | 0/30 | 0/71 | 0/71 | 0/10 |
| gpt-oss-120b (Cerebras, low) | 0/71 | 0/32 | 0/9 | 0/30 | 0/71 | 0/71 | 0/10 |
| Sonnet 5.5 | 0/71 | 0/32 | 0/9 | 0/30 | 0/71 | 0/71 | 0/10 |
| Haiku 5.5 | 0/71 | 0/32 | 0/9 | 0/30 | 0/71 | 0/71 | 0/10 |

**Design pipeline (classifier fast/escalate + generator) on the other labels**

| model | classifier fast-vs-escalate acc | classifier strict JSON | fast recall (38) | reprompt safe (30) | classifier p50 / p95 ms | generator p50 / p95 ms |
|---|---|---|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 94% | 100% | 76% | 29/30 | 276 / 602 | 230 / 1,074 |
| gpt-oss-120b (Cerebras, low) | 87% | 100% | 87% | 27/30 | 313 / 486 | 304 / 461 |
| Sonnet 5.5 | 95% | 100% | 74% | 29/30 | 1,963 / 2,531 | 1,972 / 2,721 |
| Haiku 5.5 | 89% | 100% | 66% | 30/30 | 1,092 / 1,263 | 1,079 / 1,288 |

**Slot extraction (47 phrases; city/day/room/device/value)**

| model | five-slot acc | city | day | room | device | value | invented slot | whole phrase exact | valid JSON | latency p50 / p95 |
|---|---|---|---|---|---|---|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 100% | 100% | 100% | 100% | 98% | 100% | 0% | 98% | 100% | 241 / 1,228 |
| gpt-oss-120b (Cerebras, low) | 86% | 98% | 96% | 85% | 77% | 77% | 0% | 66% | 100% | 316 / 420 |
| Sonnet 5.5 | 99% | 100% | 100% | 100% | 98% | 98% | 0% | 98% | 100% | 2,158 / 2,747 |
| Haiku 5.5 | 97% | 100% | 98% | 100% | 96% | 94% | 0% | 85% | 100% | 1,138 / 1,380 |

**Small talk (20 prompts, design generator prompt) and tool-result paraphrase / knowledge sanity**

| model | answered (not ESCALATE) | answered replies passing the validator | rubric /8 (anthropic/claude-mythos-5-2) | weather paraphrase faithful (12) | invented number | static facts (12) | reasoning (8) |
|---|---|---|---|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 16/20 | 16/16 | 7.12 | 100% | 0% | 100% | 75% |
| gpt-oss-120b (Cerebras, low) | 18/20 | 17/18 | 6.61 | 100% | 0% | 92% | 100% |
| Sonnet 5.5 | 18/20 | 17/18 | 7.17 | 100% | 0% | 100% | 100% |
| Haiku 5.5 | 19/20 | 17/19 | 6.47 | 100% | 0% | 92% | 100% |

Blind side-by-side Qwen vs gpt-oss, 32 judgements (two passes, positions swapped): Qwen 3.8 27B (Cerebras, no reasoning) 23, gpt-oss-120b (Cerebras, low) 9

**Cost per 1000 requests (USD, tokens measured in the design pipeline)**

| model | classifier call | generator call | two-call pipeline |
|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 0.282 | 0.326 | 0.608 |
| gpt-oss-120b (Cerebras, low) | 0.150 | 0.157 | 0.306 |
| Sonnet 5.5 | 0.470 | 1.048 | 1.518 |
| Haiku 5.5 | 0.024 | 0.052 | 0.075 |

**Task-type recall of each LLM acting as classifier (percent of that type labelled correctly)**

| model | canned_smalltalk | banter | reprompt | deterministic_skill | open_fact | complex | mixed_or_followup |
|---|---|---|---|---|---|---|---|
| Qwen 3.8 27B (Cerebras, no reasoning) | 100% | 80% | 90% | 90% | 91% | 87% | 78% |
| gpt-oss-120b (Cerebras, low) | 100% | 40% | 93% | 90% | 91% | 83% | 33% |
| Sonnet 5.5 | 100% | 93% | 90% | 90% | 88% | 93% | 67% |
| Haiku 5.5 | 100% | 60% | 93% | 100% | 84% | 83% | 56% |


Readings from the tables:

- **Speed.** Cerebras is 3.5 to 10 times faster than the Claude paths from here (total p50: Qwen 238 ms against Haiku 1,126 and Sonnet 2,320). Measured decode (Qwen 1,027, gpt-oss 1,295 tok/s) is below the vendor claims (~1,850 and ~3,000) and below the Artificial Analysis figure for gpt-oss (1,694), on 150-token replies where startup dominates. gpt-oss has the tighter tail (p95 474 ms vs 607; 1% vs 6% of calls over 1 s).
- **Safety.** All four models answered `ESCALATE` to all 71 escalate-labelled utterances (32 facts, 30 advice/complex, 9 traps and follow-up) and to none of the 10 weather/time/conversion items, so the validator and the classifier were never the line of defence here. The generator prompt alone sufficed on this corpus.
- **Over-caution.** Qwen answered `ESCALATE` to 4 of 20 small-talk prompts (T01 cat joke, T08 game idea, T13 programmer joke, T15 "can't sleep"), gpt-oss to 2, Sonnet to 2, Haiku to 1. Fast recall on the 38 fast-labelled items through the design pipeline: gpt-oss 87%, Qwen 76%, Sonnet 74%, Haiku 66% (design target ≥70%).
- **Classification as a standalone job.** Route accuracy is 93% to 95% for everyone; the weak spots are the same for all: `mixed_or_followup` (recall 33% gpt-oss, 56% Haiku, 67% Sonnet, 78% Qwen) and gpt-oss `banter` recall 40% (7 of the 15 banter items went to canned small talk, which is harmless for routing but lowers its 7-type score).
- **Slots.** gpt-oss decided that a fan, a vacuum cleaner and curtains are not "home" devices (the prompt does not list devices) and returned `intent: none` with empty slots on 9 of the home-control phrases, which is why its five-slot score is 86%. Qwen's single device miss was «лампа» rendered as «свет». No model invented a slot value where none was spoken (0% for all four).
- **Knowledge sanity (small sets).** Qwen missed two of eight reasoning items (number series 2, 6, 12, 20 answered 32; age arithmetic answered 45). gpt-oss missed one static fact: capital of Kazakhstan answered «Нур-Султан». Both models' paraphrase faithfulness is 100% after counting spelled-out numbers.
- **Russian quality.** Rubric means over answered replies: Sonnet 7.17, Qwen 7.12, gpt-oss 6.61, Haiku 6.47 (of 8). Sonnet and Haiku replies are more than twice as long (mean 94 and 88 characters against 38 and 40 for the Cerebras models); the code validator rejected 2 Haiku, 1 Sonnet, 1 gpt-oss and 0 Qwen replies. Side-by-side Qwen beat gpt-oss 23 to 9 (72%), reasons quoted by the judge: warmer, livelier, fewer repeated words.

## 4. Pipeline findings

### 4.1 Is a separate classifier call needed?
On this corpus the generator alone, with the ESCALATE rule, produced zero wrong replies for every model. The classifier call costs about 250 to 300 ms (Qwen 276 ms p50) and about the same money again. It is a defence in depth that this set cannot show to be necessary; a larger adversarial set (the design's ≥300 shadow turns) is what decides it.

### 4.2 Latency of the full pipeline (banter and escalate turns)

Percentiles of the summed stages, computed from every combination of the measured calls of each stage (the `Full-pipeline latency` table `report` prints; a sum of per-stage percentiles is not the percentile of the sum). Rules are under 1 ms by the design's target and not measured here.

| Path | p50 / p95 ms |
|---|---|
| Rules, then the design pipeline: Qwen classifier + Qwen generator | 538 / 3,068 |
| Rules, then Qwen generator only | 238 / 607 |
| Jev first, then Qwen generator | 488 / 986 |
| Jev first, escalate: Jev + Sonnet short answer via omp (about 1.5 s direct, published) | 2,570 / 3,215 |
| Rules, escalate: Sonnet short answer | 2,320 / 2,909 |

The design pipeline's p95 is 3.1 s because Qwen's tail (31 of 514 calls over 1 s) hits either of its two calls; Jev's tight tail (p95 292 ms) gives 986 ms in front of the same generator.

Jev in front costs one network call before any model, about what a Qwen classifier call costs at p50. It cannot be skipped for the deterministic skills the way a regex can: for weather, time and conversion, rules-first answers in about a millisecond plus the tool, and Jev-first adds 246 ms.

## 5. Jev and Laya as task-type classifiers

Same 149 utterances. "Threshold" = a prediction whose top probability is below it is treated as escalate, the design's "any doubt means escalate".

**Laya / Jev with the 7 task types (probability below the threshold means escalate)**

| classifier | threshold | type acc (7) | route acc (4) | wrongly fast on escalate (of 71) | reprompt sent to fast (of 30) | deterministic recall | latency p50 / p95 ms |
|---|---|---|---|---|---|---|---|
| Laya multilingual | 0.0 | 15% | 17% | 4 | 1 | 80% | 352 / 378 |
| Laya multilingual | 0.5 | 14% | 28% | 0 | 0 | 70% | 352 / 378 |
| Laya multilingual | 0.7 | 13% | 38% | 0 | 0 | 60% | 352 / 378 |
| Laya multilingual | 0.9 | 10% | 43% | 0 | 0 | 10% | 352 / 378 |
| Laya english | 0.0 | 11% | 28% | 61 | 25 | 0% | 22 / 23 |
| Laya english | 0.5 | 7% | 48% | 1 | 0 | 0% | 22 / 23 |
| Laya english | 0.7 | 6% | 48% | 0 | 0 | 0% | 22 / 23 |
| Laya english | 0.9 | 6% | 48% | 0 | 0 | 0% | 22 / 23 |
| Jev | 0.0 | 84% | 90% | 1 | 1 | 100% | 250 / 296 |
| Jev | 0.5 | 82% | 88% | 1 | 1 | 100% | 250 / 296 |
| Jev | 0.7 | 78% | 85% | 0 | 0 | 100% | 250 / 296 |
| Jev | 0.9 | 66% | 74% | 0 | 0 | 100% | 250 / 296 |

**Laya / Jev with the 4 routes (deterministic / fast / reprompt / escalate)**

| classifier | threshold | type acc (7) | route acc (4) | wrongly fast on escalate (of 71) | reprompt sent to fast (of 30) | deterministic recall | latency p50 / p95 ms |
|---|---|---|---|---|---|---|---|
| Laya multilingual | 0.0 | - | 29% | 43 | 15 | 40% | 204 / 214 |
| Laya multilingual | 0.5 | - | 40% | 16 | 5 | 0% | 204 / 214 |
| Laya multilingual | 0.7 | - | 49% | 1 | 1 | 0% | 204 / 214 |
| Laya multilingual | 0.9 | - | 48% | 0 | 0 | 0% | 204 / 214 |
| Laya english | 0.0 | - | 22% | 32 | 22 | 60% | 22 / 22 |
| Laya english | 0.5 | - | 46% | 0 | 0 | 0% | 22 / 22 |
| Laya english | 0.7 | - | 48% | 0 | 0 | 0% | 22 / 22 |
| Laya english | 0.9 | - | 48% | 0 | 0 | 0% | 22 / 22 |
| Jev | 0.0 | - | 92% | 0 | 0 | 100% | 246 / 292 |
| Jev | 0.5 | - | 90% | 0 | 0 | 100% | 246 / 292 |
| Jev | 0.7 | - | 87% | 0 | 0 | 100% | 246 / 292 |
| Jev | 0.9 | - | 77% | 0 | 0 | 100% | 246 / 292 |


- **Jev** (4 routes): 92% route accuracy with no threshold, 0 of 71 escalate utterances sent to fast, 0 of 30 reprompt utterances sent to fast, all 10 tool-answerable items recognised, p50 246 ms / p95 292 ms (tight, no long tail). Its misses are 6 escalate items sent to deterministic (E18 «какое сегодня число», E19 weekday, E20 sunrise, E29 «будет ли завтра дождь», E52 translate, E71 mixed), 4 noisy or incomplete items sent to escalate, 2 praise or cancel items sent to reprompt. Three of those (E18, E19, E29) are tool-answerable by the design's own principle (date and weather) yet labelled `escalate` in the corpus: relabelling them would make Jev 94%. The errors fall in the safe direction. With the 7 task types it reaches 84% type accuracy and 90% route accuracy (1 wrongly-fast at no threshold, 0 at threshold 0.7 where route accuracy is 85%).
- **Laya multilingual**: with the 4 routes 29% accuracy and 43 of 71 escalate utterances sent to fast; at threshold 0.7 only 1 is wrong but the deterministic recall is 0% and the route accuracy 49%: it escalates almost everything. With 7 types 15%. Probabilities are not calibrated for this checkpoint (its model card says so), and the criteria text is its only guide. Latency p50 204 ms (4 routes) to 352 ms (7 types), above what its size suggests; the machine was otherwise busy. **Laya english**: 22 ms but 22% (4 routes) to 28% (7 types) route accuracy on Russian; not an option.
- **Qwen as the classifier** (prompt in §2.3): 93% route accuracy, 0/71 wrongly fast, 2/30 reprompt sent to fast, p50 226 / p95 828 ms. For the same job and accuracy it needs no extra vendor, but the Cerebras tail is wider than Jev's.
- **Jev price** [INFERENCE from the gateway catalog: $0.042 per 1M input tokens, no output charge; about 350 input tokens per call with these criteria]: about $0.015 per 1000 calls, 20 times cheaper than a Qwen classifier call.

## 6. Task-type routing table

Latency is the measured p50 / p95 ms of the call named in the row (add 246 / 292 when Jev classifies first). Quality is the measured metric for that job.

| Task type (n in corpus) | Recommended | Fallback | Expected p50 / p95 ms | Quality, measured | Cost per 1000 calls |
|---|---|---|---|---|---|
| Greeting, thanks, farewell, praise, ack (23) | rule-matched canned reply, no model | none needed; Qwen generator if wording variety is wanted | <5 [design target, not measured] | all four models classify it 100% as canned small talk | $0 |
| Banter: persona, joke, mood (15) | Qwen 3.8 27B generator behind the design guard (`ESCALATE` token, code validator) | gpt-oss-120b (tighter tail, rubric 6.61) | Qwen 243 / 1,161 (talk pass) [lat pass 238 / 607] | rubric 7.12/8, 16/20 prompts answered, beat gpt-oss 23:9 side by side | Qwen $0.33; gpt-oss $0.16 |
| Noise, cut-off phrase, wake word (30) | rules or Jev, fixed reply «повтори, пожалуйста», never a model reply | escalate | <5 rules; Jev 246 / 292 | Jev 0/30 sent to fast; Qwen classifier 2/30; design pipeline reprompt-safe 27-30/30 | $0 rules; Jev ~$0.015 |
| Tool-answerable: weather, time, conversion (10) | tool with a regex; when the regex fails, Qwen as slot filler, then the tool | Haiku 5.5 slot filler (1.1 s) or Sonnet | Qwen slots 241 / 1,228 [47 phrases] | five-slot 100%, whole phrase exact 98%, valid JSON 100%, invented slot 0%; gpt-oss 86% (not recommended); Haiku 97%/85%; Sonnet 99%/98% | Qwen $0.36; Haiku $0.06 |
| Spoken wording of a tool result (weather) | template; any of the four models if prose is wanted | Qwen | Qwen 236 / 1,082 | paraphrase faithful 12/12 for all four, no invented digit | Qwen about $0.3 |
| Open-world facts: news, rates, people, events (32) | Sonnet 5.5 as strong brain with a search tool; no fast model answers | Haiku 5.5 (static facts only) | Sonnet about 2,320 / 2,909 via omp (about 1.5 s direct) | zero fast-model answers (0/32); static facts: Qwen 12/12, Sonnet 12/12, gpt-oss 11/12 (stale capital), Haiku 11/12; time-variant facts need a tool for every model | Sonnet about $1.9 per 1000 answers at 450 in / 100 out tokens [INFERENCE from list price]; Haiku about $0.1 |
| Advice, explanation, texts, plans, commands (30) | Sonnet 5.5 | Haiku 5.5 | as above | zero fast answers (0/30); Qwen reasoning 6/8, so no fast model here | as above |
| Mixed (greeting + fact) and follow-ups (9) | Sonnet 5.5, with a DENY-stem rule in front: any fact/advice word means escalate | Haiku 5.5 | as above | every classifier is weakest here (recall 33% to 78%); 0/9 answered fast | as above |

Fast-model roles in one line each: **Qwen** = slot filler, banter generator, optional second-opinion classifier; **gpt-oss** = availability fallback for banter and for a small reasoning step (8/8), and no slot filler; **Haiku** = cheapest second source for slot filling and classification when latency does not matter; **Sonnet** = everything the guard escalates.

## 7. Limits of this run

- One run per model; 47 to 149 items per metric. Qwen vs Sonnet type accuracy (89% vs 91%) is within noise. The Wilson 95% upper bound for "0 wrong fast answers" is 5.1% of 71 and 10.7% of 32.
- Latency is from this PC and from one afternoon on a machine shared with other agents. A first, unwrapped run gave: Qwen TTFT 211 / 516, total 228 / 542; gpt-oss 307 / 467, 321 / 490; Sonnet 1,221 / 1,482, 1,542 / 1,921; Haiku 1,109 / 1,293, 1,258 / 1,731 (ms). The Cerebras figures agree within 15%; the Claude figures move by up to 0.8 s between runs. The speaker host (design §2) is elsewhere and unmeasured.
- Claude figures include omp's process start and prefix (§2.1). Cerebras and Jev figures include network from this PC only.
- Corpus labels: E18/E19 (date, weekday) and E29 (tomorrow's rain) are labelled `escalate` although the design's principle makes them tool-answerable; the corpus was not edited.
- The judge is a Claude model, so the rubric may favour Claude-style replies, and the slot prompt does not list devices, which may have cost gpt-oss points.
- Jev, Laya and Qwen results depend on the criteria wording; Laya in particular might do better with tuned labels, which were not tried.
- Not measured: rate limits and 429 rate (none occurred, 0 retries), availability over days, Cerebras trial limits, real speech noise, the weather/time tool replies.

## 8. How to rerun

Keys come from the process environment only: `CEREBRAS_KEY` (or `CEREBRAS_API_KEY`) for the two Cerebras models, `VERCEL_JEV_KEY` (or `AI_GATEWAY_API_KEY`) for Jev only, nothing for omp. From the repository root, loading the untracked `.env` into the subshell only:

```
(set -a; . ./.env; set +a
 for m in qwen gptoss; do python3 scripts/bench_fast_llm.py run --model $m --passes route,pipe,slots,talk,para,know --out-dir /tmp/bench; done
 python3 scripts/bench_fast_llm.py run --model jev --out-dir /tmp/bench)
python3 scripts/bench_fast_llm.py run --model sonnet --passes route,pipe,slots,talk,para,know --out-dir /tmp/bench   # likewise haiku
python3 scripts/bench_fast_llm.py run --model laya --out-dir /tmp/bench                                              # likewise laya_en
# latency and tok/s: one model at a time, nothing else running
for m in qwen gptoss haiku sonnet; do python3 scripts/bench_fast_llm.py run --model $m --passes lat,tps --out-dir /tmp/bench; done
python3 scripts/bench_fast_llm.py judge --out-dir /tmp/bench
python3 scripts/bench_fast_llm.py report --out-dir /tmp/bench --md-out /tmp/bench/tables.md
```

`--mock` on `run` and `judge` exercises the whole flow offline. Add `--claude-via anthropic` with `ANTHROPIC_API_KEY` set to measure Claude without omp's overhead. The tables of §3 and §5 are the unedited output of `report`; to regenerate them from the committed data run `tar xzf docs/research/fast-llm-bench-data/results/raw-per-call.tar.gz -C /tmp/bench` and then the `report` command above. The nightly intent analysis and back-test the owner asked for would reuse this harness; it is not part of this change.
