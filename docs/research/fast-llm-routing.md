# Fast LLM tier and safe routing for the voice speaker

Ticket: alex-mextner/aisis#88 (relates to #37, response latency). Research date: 2026-11-15. Owner ask: how to get ~1000 tok/s, find a provider with similar speed, and make sure the fast model never answers hard questions or fact lookups. **Amendment (owner review of PR #91):** the first draft sent weather and other tool-answerable lookups to the strong brain. That was wrong; the router has three routes and tool-answerable questions never reach an LLM (§6).

Labels used for every number: **[AA]** independent measurement by Artificial Analysis (median, 10k-token input, quoted through a third-party page), **[OR]** OpenRouter live-traffic median, **[vendor]** the provider's own claim, **[here]** measured from the speaker host during this research, **[INFERENCE]** my reasoning, not observed.

## 1. Conclusions

1. **For short voice replies, TTFT matters more than 1000 tok/s.** The adapter sends the whole reply as one `partialResponse` (`adapter.py`, `_process_turn`). For an illustrative 60-token reply, 143 → 1000 tok/s saves ~0.36 s of decoding. This is not a measured end-to-end improvement: reasoning, network, routing, STT and TTS still count (§2).
2. **Cerebras is the clearest independently reported ≥1000 tok/s option:** gpt-oss-120b at 1,694 tok/s, 0.38 s to first *chunk*, excluding reasoning [AA via Novita]. Its vendor claims ~3,000 for gpt-oss-120b and ~1,850 for Qwen 3.8 27B. Groq advertises 1,000 tok/s on gpt-oss-20b. Fireworks added an Ultra-fast DeepSeek endpoint on November 3 (6–8× generation-speed claim); newer Inco/LithosAI endpoints also exist. None is a verified short-Russian-voice benchmark here. Mercury 3's 1,068 tok/s claim contrasts with 160–254 [OR] at launch. No usable local 1000 tok/s configuration was found (§5).
3. **Recommendation.** Evaluate Cerebras `qwen-3.8-27b` with `reasoning_effort: "none"` first; compare `gpt-oss-120b` at `low` as the same-provider alternative. Availability fallback: Groq (`qwen/qwen3.8-27b`, currently preview, or `openai/gpt-oss-120b`). On any doubt or provider problem, escalate to the existing strong brain (§6.5). Russian speech quality and actual answer latency remain unverified because no candidate key exists; the offline and shadow gates must pass before production use.
4. **The fast LLM has a small job.** A private sample of 36 brain-bound turns included greetings, thanks, banter, ASR noise, incomplete commands and knowledge questions. This small convenience sample cannot establish household traffic percentages. Canned greetings and deterministic tools avoid network inference altogether; the fast LLM covers only approved banter.
5. **Safety is a requirement, not a proved property of this design.** Rules gate access before generation; strict classification precedes a separate generation call, and every uncertainty escalates (§6). Prompt instructions and output filters alone cannot guarantee no factual hallucination. Target: zero wrong fast answers on the eval, then zero on ≥300 shadow-mode escalation cases (§7); keep production disabled until demonstrated.
6. **Owner must provide:** a Cerebras API key (§8) and a yes to sending final transcripts of fast-eligible turns to Cerebras (US cloud, the same class of third party as Anthropic today). A Groq key is optional.

## 2. Voice latency budget

Reply latency ≈ endpointing (VAD hangover `AISIS_STT_SILENCE_MS=800` in `runtime.env`) + final STT (target p50 ≤ 300 ms, spec 140; not re-measured here) + **[route + network + TTFT + tokens/speed]** + TTS first audio (Fish/Alice, **not measured**). Only the bracket changes with the LLM.

Illustrative 60-token reply, **not an apples-to-apples latency benchmark**: the older provider snapshots measure first *chunk*, excluding reasoning, whereas current model-level snapshots report first *answer*, including reasoning. Thus the gpt-oss totals below are optimistic lower bounds, not expected time to speech.

| Brain / tier | tok/s | Published latency | Latency + 60 tok (illustration only) | Source |
|---|---|---|---|---|
| Claude Sonnet 5.5 (live brain, non-streaming) | 143 | 1.5 s | **1.92 s** | BenchLeader/AA 2026-11-15 |
| Claude Haiku 5.5 | 255 | 1.1 s | 1.34 s | BenchLeader/AA |
| SambaNova gpt-oss-120b | 701 | 1.18 s | 1.27 s | [AA] |
| Groq gpt-oss-120b | 473 | 0.63 s | 0.76 s | [AA] |
| Cerebras gpt-oss-120b | 1,694 | 0.38 s | **0.42 s** | [AA] |
| Hypothetical infinite speed, TTFT 0.38 s | ∞ | 0.38 s | 0.38 s | arithmetic |

At equal 0.38 s startup, 1,694 tok/s → infinite speed saves just 0.04 s. **Optimize first answer and short responses before decode speed.** From the speaker host (Serbia), unauthenticated `/models` requests returned headers in Cerebras 0.22–0.38 s (HTTP 403), Groq 0.23–0.29 s (401), Anthropic ~0.21 s (401), 3 runs each [here]. TLS setup was roughly 30–60 ms. These are edge/error-response timings, **not model TTFT or evidence of origin location**; Cerebras's 403 requires investigation with an authenticated key. Keep a warm connection in a persistent adapter client.

Reasoning models: AA's Groq gpt-oss-20b row has a 7.55 s time-to-first-*answer* token at "high" effort because thinking counts. gpt-oss cannot switch reasoning off (low/medium/high only); Qwen 3.8 27B on Cerebras can (`reasoning_effort: "none"`), which is why it is primary.

Illustrative cost for 100 API requests/day at 400 input + 60 output tokens each: Cerebras Qwen ~$1.5/month; gpt-oss-120b ~$0.56/month, excluding thinking. The two-call design at 100 BANTER turns/day makes 200 requests (~$3/$1.1 at those per-request sizes), plus any billed reasoning; count both calls and actual usage when budgeting.

## 3. Provider comparison

| Provider / model | Output tok/s | TTFT | Price in/out per 1M | Context | OpenAI-compatible | Tools / JSON | Russian | Source |
|---|---|---|---|---|---|---|---|---|
| **Cerebras** `gpt-oss-120b` | **1,694** [AA]; ~3,000 [vendor] | 0.38 s [AA] | $0.35 / $0.75 | 131k paid, 65k free | yes | tools, strict JSON schema, caching | weak–medium [INFERENCE]: anecdotal reports of literal translation from English; effort low/med/high only | [Cerebras models](https://inference-docs.cerebras.ai/models/overview), [AA via Novita](https://novita.ai/compare/best-fastest-llm-api) |
| **Cerebras** `qwen-3.8-27b` | ~1,850 [vendor], no independent Cerebras row found | unknown | $0.99 / $1.49 | 128k paid, 64k trial | yes | strict JSON schema, tools; `reasoning_effort: none` | multilingual Qwen family is promising [INFERENCE]; no Russian voice evaluation found | [Cerebras Qwen](https://inference-docs.cerebras.ai/models/qwen-3.8-27b.md), [reasoning](https://inference-docs.cerebras.ai/capabilities/reasoning.md) |
| **Groq** `openai/gpt-oss-20b` | ~1,000 [vendor]; no independent speed row | 7.55 s at high effort [AA]; error rate 4.0% | $0.075 / $0.30 | 131k | yes | tools, JSON | as gpt-oss | [Groq models](https://console.groq.com/docs/models), [AA gpt-oss-20b](https://artificialanalysis.ai/models/gpt-oss-20b/providers) |
| **Groq** `openai/gpt-oss-120b` | 473 [AA]; 500 [vendor] | 0.63 s [AA] | $0.15 / $0.60 | 131k | yes | tools, JSON | as gpt-oss | AA via Novita |
| **Groq** `qwen/qwen3.8-27b` (preview) | 450 [vendor] | n/a | $0.80 / $4.00 | 131k, max out 16k | yes | tools, JSON | as Qwen | Groq models |
| Groq Llama 3.1 8B (560) / 3.3 70B (280) | [vendor] | n/a | contact sales only | 131k | yes | yes | Llama: medium | Groq models |
| **SambaNova** `gpt-oss-120b` | 701 [AA] | 1.18 s [AA] | $0.22 / $0.59 | 128k | yes | yes | as gpt-oss | AA via Novita, [SambaCloud models](https://docs.sambanova.ai/docs/en/models/sambacloud-models) |
| **Baseten** Fast, DeepSeek V4.1 Flash 1017 | 837 [AA older snapshot] (standard 281) | 0.21 s to first chunk; current AA first answer at Max is 9.21 s | $0.60 / $2.40 | 1,048k | yes | tools, JSON, schema | untested Russian; multilingual model [INFERENCE] | [Baseten catalog](https://docs.baseten.co/inference/model-apis/overview.md), [current AA](https://artificialanalysis.ai/models/deepseek-v4-1-flash-1017/providers) |
| **Together** DeepSeek V4.1 Flash 1017 | 365 [AA older snapshot] | 0.32 s first chunk, excludes reasoning | $0.30 / $1.20 | 1M | yes | tools / JSON per AA | untested Russian | [Together model](https://www.together.ai/models/deepseek-v4-1-flash-1017) |
| **Fireworks** DeepSeek V4.1 Flash 1017 Standard / Ultra-fast Preview | 210 Standard [AA older snapshot]; Ultra-fast 6–8× [vendor], no readable current tok/s figure found | 1.15 s first chunk Standard; Ultra-fast unknown | Standard $0.30 / $1.20; Ultra-fast **$2.40 / $9.60** | 1M per AA | yes | tools / JSON per AA | untested Russian | [Nov 3 release](https://docs.fireworks.ai/updates/changelog.md), [pricing](https://docs.fireworks.ai/serverless/pricing.md) |
| **Inception** Mercury 3 (diffusion LLM) | 1,068 [vendor]; **160–254 [OR]** (median, launch week) | ~1 s [OR] | $0.20 / $0.70 (launch −50%) | 1M | yes (`mercury-3`) | tools, structured outputs | unknown | [Inception models](https://docs.inceptionlabs.ai/get-started/models.md), [eesel](https://www.eesel.ai/blog/inception-mercury-3) |
| Inception Mercury Voice (TTFT < 170 ms [vendor]) | n/a | enterprise only | $0.40 / $1.50 | 128k | yes | yes | unknown | same |
| Celeris-1 | 818 [AA] | 0.68 s [AA] | $0.20 / $0.70 | 131k | not checked | not checked | unknown; AA Intelligence Index 6.3 (very weak) | [BenchLeader](https://www.benchleader.com/models/celeris-1) |
| Gemini 3.5 Flash Lite | 297 | 0.7 s | not checked | n/a | no (Google API) | yes | strong [INFERENCE] | [BenchLeader speed board](https://www.benchleader.com/leaderboards/speed) |

Notes that decide the table:

- **Free tier / limits.** Cerebras: no permanent free tier; $5 one-time credit valid 30 days after adding a payment method; paid Qwen 3.8 27B 300 RPM/150k uncached TPM, gpt-oss-120b 1k RPM/500k uncached TPM. Trial gpt-oss is 5 RPM ([limits](https://inference-docs.cerebras.ai/support/rate-limits.md)). Groq's gpt-oss-20b, gpt-oss-120b and Qwen 3.8 27B free-plan limits are 30 RPM, 1k requests/day, 8k TPM, 200k tokens/day ([limits](https://console.groq.com/docs/rate-limits)); classifier and generator consume separate requests. Inception's reported new-key 100M-token promotion is secondary-source information (eesel), not a verified ongoing allowance.
- **Privacy / residency.** Cerebras and Groq are US companies; US incorporation alone does not establish serving location. Groq retains no inference content by default, offers a ZDR setting, and stores retained content in US GCP buckets; limited reliability/abuse logs may remain up to 30 days ([primary policy](https://console.groq.com/docs/your-data)). Cerebras [primary privacy policy §6](https://www.cerebras.ai/privacy-policy) says it does not retain training/inference/chatbot inputs or outputs; shared inference serving residency is unconfirmed. Spec 140 permits only final accepted transcripts to reach model providers: audio, voiceprints and speaker-identity hints stay local. This research changes no audio/TTS privacy policy.
- **Tool calling is not used by the fast tier** (§6): it must not act. Strict JSON schema output is used instead and exists on both Cerebras models.
- **Provisionally not selected:** Groq Llama 8B/70B (sales only), SambaNova (older snapshot has slower startup), Fireworks Ultra-fast (new preview, 8× Standard price; Russian/answer latency unmeasured), Together (older measured route <400 tok/s), Mercury 3 (launch speed claim not reproduced), Celeris-1 (quality/API documentation incomplete). Model size or speed does not itself make a model cautious: routing policy is required for every provider.

### Other vendors, speculative decoding, quotas and privacy

- **Baseten:** Fast variants use the same weights, separate capacity/slugs/limits, and may fall back to base capacity. Synchronous API inputs/outputs are not stored; shared serving regions may change, and regional restriction requires support. Basic defaults: 15 RPM/100k TPM unverified, 120 RPM/500k TPM verified; no ongoing free allowance confirmed. [Catalog/privacy](https://docs.baseten.co/inference/model-apis/overview.md), [limits](https://docs.baseten.co/inference/model-apis/pricing-and-limits.md).
- **SambaNova:** gpt-oss-120b Free: 20 RPM, **20 requests/day**, 200k tokens/day; Developer: 60 RPM/12k requests/day, account cap 20M tokens/day. [Primary limits](https://docs.sambanova.ai/docs/en/models/rate-limits). No verified residency or retention commitment found for this specific public plan: obtain terms before sending private transcripts.
- **Fireworks:** ZDR by default per [pricing page](https://fireworks.ai/pricing); global serverless is not a promised resident region. Select US-only endpoints carry 50% premium. Limits are adaptive per model, Ultra-fast is lower-limit Medium tier; inspect account response headers, not maximum advertised ceilings. No permanent free inference allowance verified. Fireworks [describes](https://fireworks.ai/blog/best-llms-for-coding) ~1000 tok/s adaptive speculative decoding on Cursor's fine-tuned Llama-3-70B: a workload-specific vendor result, **not a stock Russian chat endpoint**.
- **Together:** [current pricing](https://www.together.ai/pricing) confirms the table rates; its DeepSeek model card explicitly lists DSpark speculative decoding. No independently verified 1000 tok/s stock endpoint found. Trial balance, account quotas, retention and deployment region were not established from the retrieved primary pages; do not assume free or EU-resident.
- **Inception:** [model catalog](https://docs.inceptionlabs.ai/get-started/models.md) confirms Mercury 3 `instant` mode, 1M context, tools/schema, and launch rates. Mercury Voice is enterprise-only; Mercury Decide is classification-only (not Chat Completions). Privacy policy is [here](https://inceptionlabs.ai/privacy-policy); API-specific residency/ZDR and RPM quotas remain unconfirmed. Diffusion generates blocks in parallel; it is not ordinary draft-and-verify speculative decoding.
- **New entrants:** current [AA DeepSeek provider list](https://artificialanalysis.ai/models/deepseek-v4-1-flash-1017/providers) includes LithosAI Ultra Chat and Inco Fast. A November [Vellum snapshot](https://www.vellum.ai/llm-leaderboard) reports Inco Fast ~1,075 tok/s; independent primary tok/s and current TTFT/prices/limits/privacy were not readable here, so neither is recommended. Celeris-1's aggregator data is likewise insufficient to establish a production API/privacy contract.
- **Hardware, not a deployable plan:** reporting on Taalas HC1 cites ~17,000 tok/s for hardwired Llama 3.1 8B; the [primary site](https://taalas.com/) does not supply a self-serve API rate card or Russian-quality evidence. [Etched](https://www.etched.com/progress/frontier-inference-clusters) markets inference clusters; no public priced model API verified. Do not compare hardware demos or aggregate batched throughput to per-user streaming speed.
- **Self-host speculative offerings:** llama.cpp draft-model speculation, vLLM/SGLang draft/MTP, and TensorRT-LLM/ExLlama-class quantized serving can improve decode when draft acceptance is high. Draft storage/prefill cost and a 60-token reply can erase the gain; no honest 1000 tok/s estimate follows from a claimed 2× or 3× speedup. Engines/models were not installed during this research.


## 4. Interfaces the design must fit (read from the repo)

- **Committed main at research base `ff69417`:** `_process_turn` calls `calc_evaluate(text)` then `self.turn(station,text)`. The inspected active workspace also contains the uncommitted #85 work adding `voice_skills.py`, `self.skills.answer(text)` and `user_prompt.CAPABILITIES` (weather/nameless music). The proposed order below assumes #85 lands; until then insert after `calc_evaluate`. This research does not modify or commit that other task's files.
- `Adapter.turn` runs the brain through `AISIS_CHATGPT_COMMAND`. On the speaker host that is `claude_primary.py` (Anthropic Messages API, `claude-sonnet-5-5`, three-key failover, history `AISIS_CLAUDE_HISTORY_PAIRS=8` in `router.sqlite3`) with the ChatGPT browser bridge (`chatgpt_pm.py` → `pm_cdp_server.py` `POST /turn` on :31118, CDP :9226) as the guarded last reserve. So the brief's "ChatGPT/CDP" path is today the *reserve*, not the primary. `claude_primary.py` is in the open PR #31 / release `claude-primary-da9779c`, not on `main` (`main` has only the `_claude_module()` loader).
- Before the adapter, quasar runs regex fast-intents, `/laya` semantics and `asr_correct` (Claude Haiku, 1.2 s budget). Recognized smart-home commands execute there; malformed ones can reach the adapter. The inspected #85 `user_prompt.CAPABILITIES` addition asks the brain to clarify instead of pretend to act. Confirmations must come from tool receipts; voice never authorizes sensitive writes (spec 140).
- Fillers: `/src/backend/fillers.ts` in the quasar container has `FillerPlan {immediate, late}`; `llm` is immediate today, `math`/`music` are late after 1.5 s. Existing adapter helper endpoints are `/laya` and `/asr/correct`; proposed `/route` follows that authenticated helper pattern.
- Turn bookkeeping: `Store.begin/finish`, `memory.record_episode`, `dialog_context` (recent-dialogs block). History of the brain is `claude_primary`'s own table.

## 5. Self-hosting 1000 tok/s on the home PC

Measured on `ubuntu-pc` (read-only commands): NVIDIA GeForce RTX 4060, 8,188 MiB VRAM, **6,106 MiB already used** (wyoming whisper large-v3-turbo 2.2 GB, a second wyoming process 1.4 GB, `hyper-task` python 2.7 GB, desktop apps), memory clock 8,501 MHz on a 128-bit bus = 272 GB/s [derived], 115 W; CPU Ryzen 5 5600 (6c/12t), 60 GB RAM. No `vllm`, `llama-server`, `llama-cli`, `ollama` or `llama_cpp` installed, nothing was installed or run, so **the figures below are estimates, not measurements**.

Single-stream decoding is memory-bandwidth bound: tok/s ≤ 272 GB/s ÷ weight bytes, and real engines reach ~40–60% of that [INFERENCE].

| Model on this GPU | Weights (Q4) | Ceiling | Realistic | Fits in ~2 GB free VRAM? |
|---|---|---|---|---|
| Qwen2.5-0.5B-Instruct Q4_K_M | ~0.35 GB | ~780 tok/s | 300–450 | yes; Russian suitability untested |
| Qwen2.5-1.5B-Instruct Q4_K_M | ~1.0 GB | ~270 | 120–160 | yes, short KV cache; Russian quality untested |
| Qwen2.5-3B-Instruct Q4_K_M | ~2.0 GB | ~135 | 60–80 | no comfortable KV headroom at current usage |
| Llama-3.1-8B-Instruct Q4_K_M | ~5.0 GB | ~54 | 25–35; speculation speedup unmeasured | no |
| 27B (Qwen 3.8) | ~15 GB | ~18 | does not fit | public community best is 156.8 tok/s on high-end GPUs ([llm-bench.io](https://llm-bench.io/models/qwen3-8-27b)) |

These estimates assume **llama.cpp CUDA, batch 1, all layers offloaded, ~2k context**, not vLLM batched aggregate throughput. GGUF metadata/KV buffers add to weights. Ollama wraps an engine rather than changing memory bandwidth. For actual ≥1000 tok/s at useful multilingual quality, use multi-accelerator/SRAM/wafer-scale serving or specialized model-parallel/speculative infrastructure; buying one larger consumer GPU is not a guaranteed route.

CPU-only bandwidth is unmeasured: assuming ~50 GB/s dual-channel DDR4 gives a ~50 tok/s weight-read ceiling for a 1 GB model, with ~20–30 tok/s as a rough practical estimate. **No useful local 1000 tok/s configuration was found.** A 1.5B Q4 model might yield ~100–150 tok/s with short context; its Russian quality still needs evaluation. Spec 140 reserves only ~3 GB for the speaker stack. More GPU memory helps model fit but does not establish single-stream latency.

## 6. Three-route routing design

**Principle: if a local tool can produce the answer, use the tool, not an LLM.** Facts that come from a tool (weather, device state, time/date) are *not* fact lookups for escalation purposes. Routes: **(1) deterministic skills, no LLM:** smart-home commands, timers, volume/media, weather, time/date, unit conversions, music by name: regex/intent match, tool call, templated reply from the tool's structured output. **(2) fast/lite model:** only slot extraction (city, day) when the regex cannot parse it, paraphrase of a tool result, small talk and simple confirmations; strict classification precedes generation and any doubt means ESCALATE. **(3) strong brain:** only open-world knowledge/facts no local tool can answer (prices, rates, people, places, events), complex reasoning, multi-step questions, or when a skill returns «cannot answer» (tool error, unknown city, no data).


```
utterance ─► [existing] fast-intent / laya / asr_correct (matched commands consumed; unmatched goes onward)
          ─► adapter._process_turn
               ROUTE 1 (no LLM): calc_evaluate ─► voice_skills.answer (weather, music) ─► [new] clock skill ─┐
                                                                                            ▼ none matched
               fast_tier.decide(text, context)
                 CANNED -> fixed reply
                 ROUTE 2: BANTER / slot extraction -> strict classifier -> fast generator -> validator
                 ROUTE 3: uncertain, open-world or skill 'cannot answer' -> Adapter.turn -> claude_primary -> pm reserve
```

Proposed module `services/yandex-station/code/fast_tier.py` owns decision, prompt, validator and client; call after `skills.answer` once #85 lands, otherwise after `calc_evaluate`. Switch `AISIS_FAST_TIER=off|shadow|canned|on` (default `off`).

### 6.1 Pre-router (a): deterministic, runs before any generation

Use the committed `calc.py` normalization precedent (lower-case, ё→е). After #85 lands, reuse its tokenizer rather than fork conventions; normalize **both rules and input**. All phrase matching is anchored to the complete utterance after stripping one approved wake-name prefix.

1. **Context gate overrides everything:** after an escalated turn, a rejected candidate, or an unresolved clarification, send follow-ups to the strong path for 120 s. `voice_scores.score` is speaker verification, not ASR confidence. Unknown or incomplete input never triggers a content guess.
2. **Existing tools run first.** Smart-home actions remain in the quasar fast-intent layer; confirm only from successful tool receipts. Add fixed-unit conversions to `calc.py` with a parsed number/unit table (F39–F42), before this router. There is **no CONVERSION route inside `fast_tier`**. Failed parses escalate.
3. **Canonical allow-list:** one implementation table contains the normalized exact phrases F01–F38 from the corpus, their route, and their exact DENY-word exemptions. F01–F23 are CANNED (F02 «Привет, как дела?» has a fixed greeting); F24–F38 are BANTER. No example elsewhere adds aliases. «Как дела у Дурова?» cannot match «Привет, как дела?». Whole-utterance matching, not capitalization, is the safety boundary.
4. **DENY rules:** information-seeking stems (`кто`, `что`, `когда`, `почему`, `сколько`, `найди`, `новост`, `курс`, `цен`, `счет`), advice/safety/action stems, multiple requests or >8 words escalate unless the entire text is precisely an exempt canonical phrase. A greeting followed by a fact request is never exempt. Unknown text always escalates; missed stems cannot create permission.
5. **Acknowledgements are contextual:** the adapter has no pending-approval state machine. Clarification confirmation exists upstream in quasar/asr_correct; do not intercept it. Without trusted upstream confirmation that no question/action is pending, F16–F23 escalate rather than receive a misleading «Хорошо». Voice never authorizes sensitive writes. Reprompt is allowed only for a clearly truncated phrase or wake-only input, otherwise escalate.

The deterministic rule router grants **eligibility only**. A separate strict LLM classifier must still approve BANTER before answer generation. The classifier cannot override DENY/UNKNOWN.

### 6.2 Fast tier (b): classify first, then generate, no tools

- Endpoint for both calls: Cerebras `https://api.cerebras.ai/v1/chat/completions`, `qwen-3.8-27b`, `reasoning_effort: "none"`, one persistent session, no automatic retries. Combined deadline target 1.0 s; two calls may exceed it, so measure before enablement rather than relax fail-closed routing.
- **Availability fallback is configuration, not an in-turn retry chain:** `AISIS_FAST_PROVIDER=cerebras|groq` selects one persistent client for subsequent turns. For Groq use `https://api.groq.com/openai/v1/chat/completions`, `openai/gpt-oss-120b`, `reasoning_effort: low`, with sufficient thinking budget (e.g. 512 classifier / 1024 generator tokens, still the same wall deadline). Validate its Russian/schema/latency separately. Provider failure on a turn escalates immediately to the strong brain; it never attempts another fast provider and adds another network wait. An operator can switch providers after outage detection.
- **Classification call, before answer generation:** only for a BANTER candidate approved by rules. `temperature: 0`, `max_completion_tokens: 48`, schema `{"route":"fast"|"escalate","reason":<enum>}`, strict, all fields required, `additionalProperties: false`. Reason enum: `banter|fact|complex|command|ambiguous`. Classifier instruction: «Не отвечай пользователю. Только классифицируй: fast допустим лишь для точно распознанной болтовни без фактов, советов, действий и неоднозначности; иначе escalate». Allow generation only on `fast/banter`; malformed output, doubt, or timeout escalates. The reason is metadata, never spoken.
- **Generation call:** separate, after a successful classifier result; `temperature: 0.3`, `max_completion_tokens: 80`. Small fixed persona plus current utterance, no untrusted historical instructions. At most two recent safe-banter exchanges, only when necessary. No factual history enters the generator. Output is plain short text **or exactly `ESCALATE`**, not JSON. Strict schema is needed for the classifier, not the generator ([Cerebras support](https://inference-docs.cerebras.ai/capabilities/structured-outputs.md)).

System prompt (Russian, sent verbatim; the fixed token is `ESCALATE`):

```
Ты Брачо, голосовой собеседник на домашней колонке. Ты отвечаешь только на болтовню: шутки, настроение, вопросы о тебе самом, короткие реплики вежливости. Если нужна информация о мире, совет, действие или есть сомнение, ответь ровно одним токеном ESCALATE. Не пытайся ответить частично.
Правила:
1. Ты не знаешь фактов о мире: новости, цены, курсы, время и дату, людей, места, события, определения. Проверяемый или меняющийся ответ: ESCALATE.
2. Сомневаешься, о чём речь, или фраза похожа на команду, обрывок, шум: ESCALATE.
3. Не давай советов (здоровье, деньги, право, отношения, безопасность), не объясняй, не сравнивай, не пиши тексты, не решай задачи: ESCALATE.
4. Ты ничего не включаешь, не ищешь, не запоминаешь и не проверяешь; не обещай этого.
5. Ответ: одно-два коротких предложения по-русски, без списков, без цифр, без разметки.
Ответ — только короткая русская реплика или ровно ESCALATE. Никогда не объясняй пользователю внутреннюю маршрутизацию.
```

- **Post-validator (code, not model):** require rule verdict BANTER and preceding classifier `fast/banter`, reply ≤220 chars, ≤2 sentences, ≤30 words, no numbers/URLs/Latin script, no action-success claims, no case-insensitive `escalate` substring, no obvious factual/lookup content. Sentence-initial capitalization is permitted; heuristic name detection cannot prove factual safety. On failure escalate and emit **no fast text**. If free-form jokes invent factual-looking claims in shadow review, use reviewed canned lines instead.

### 6.3 Escalation (c)

Preserve `Adapter.turn` → `AISIS_CHATGPT_COMMAND` (`claude_primary` → pm reserve), station locks, authenticated loopback PM envelope `{station_id,text,conversation_id,turn_id}` → `{text,conversation_id}`, and no retry of uncertain dispatch. Fast responses use the same station lock and turn-status ledger, without updating browser `conversation_id` or marking the strong persona delivered. **Existing quasar log ingestion into `dialogs` and `dialog_context.recent_block` is the sole transcript path.** Do not append another copy into that history or mutate `claude_primary`'s database; test that the next strong turn sees each spoken fast reply once.

**Strong-tool design:** current `claude_primary` supplies no tools, so keep static knowledge on that path but add a trusted strong-only tool broker for lookups. Proposed schema-validated read-only functions: `search(query)` → a configured search API returning timestamped source URLs/snippets; `music_search(query)` → existing Music Assistant. Music execution and all home-control writes stay in existing authorized deterministic handlers; model text cannot trigger them. For news/prices/current events the broker requires a successful fresh tool result before a factual spoken answer, including requested location/time. On unavailable key, timeout, stale/no sources: «Не получилось проверить», not a memory-based fallback. A chat-only CDP reserve with no verified tools may answer static questions but must not be used to invent live facts. This is a concrete integration requirement for the later implementation, not something the current deployment already does.

### 6.4 Latency masking (d)

- Proposed authenticated `POST /route` is a **filler hint**, not permission to execute or generate. It takes station/turn identity; `fast_tier.decide` always checks current state again at execution.
- Return enum `strong|canned|candidate`. In `off` and `shadow`, always return `strong` (preserve today's filler and reply). In `canned`, return `canned` only for an exact eligible canned phrase; all others return `strong`. In `on`, return `candidate` for BANTER only when provider/key/circuit/deadline allow inference, `canned` for eligible canned phrases, else `strong`.
- Overlay mapping: `strong` → today's immediate llm filler; `canned` → `{immediate:null,late:null}`; `candidate` → no immediate filler, schedule «Минутку.» only **after the 1.0 s candidate deadline** if a final response has not arrived. Cancel the timer on final answer, interruption or disconnect. On fast failure, emit one filler when handing to strong, reusing/cancelling the timer so it cannot play twice. Filler estimates are not measured end-to-end latency guarantees.
- A 150 ms `/route` failure or stale hint defaults to today's `strong` filler; execution-time routing remains fail-closed. Mode/circuit changes between hint and execution may cause an unnecessary filler, never permission to speak unvalidated text.

### 6.5 Fail-closed rules

Mode has priority over key/circuit state:

| Mode | New CANNED speech | Candidate model calls | Spoken answer |
|---|---|---|---|
| `off` | no | none | unchanged existing path |
| `shadow` | no | read-only evaluation if eligible and key/circuit permit | strong path only |
| `canned` | exact eligible phrases only | none | canned or strong |
| `on` | exact eligible phrases only | only after rule/classifier approval | validated fast reply or strong |

In `shadow`/`on`, HTTP errors, 429, invalid schema, explicit or case-insensitive `ESCALATE`, empty/invalid output, and a combined candidate timeout >1.0 s reject the candidate. Open the model-call circuit for 60 s after three provider failures; canned speech remains governed solely by mode. Unknown text, rejected-candidate follow-ups and all classifier doubt escalate. Empty STT preserves the adapter's existing no-speech handler.

Ordinary telemetry contains only route/reason/timing/verdict. **Shadow text goes to a separate private `fast-shadow.sqlite3`**, not `dialogs`, `heard`, `answered`, the brain context table, memory index or dashboard transcript. Candidate text is never added to spoken history; the owner labels this separate evaluation record. Test that candidate-only text cannot appear in `recent_block` or history search.

## 7. Eval plan

Data: [`fast-llm-eval.jsonl`](fast-llm-eval.jsonl): 149 labelled utterances. Core classes: fast-eligible 38, reprompt 30, fact 32, complex 30 (30–50 each). Extras: 10 deterministic-skill items (4 conversions, 4 weather, 2 time), 8 traps, 1 contextual follow-up. IDs are stable; appended test batches are intentionally not renumbered.

| Label | Count | Expected |
|---|---|---|
| `fast` | 38 | CANNED or BANTER when context permits; escalation is safe but reduces recall |
| `reprompt` | 30 | noise/cut-off phrases: request repetition or safely escalate, never invent content; wake-only: «Слушаю.» |
| `escalate` | 71 (32 open-world fact with no local tool, 30 complex/advice/safety/tools, 8 traps, 1 follow-up) | **no fast-model content**; strong path |
| `deterministic` | 10 (4 conversions, 4 weather, 2 time) | route 1, no LLM: `calc.py` converter, weather-cli skill, clock skill; templated reply from structured tool output; recompute/verify values exactly. Failure or «cannot answer» escalates |

Real traffic is not committed: the corpus above is synthetic, modelled on the adapter log's categories.

Weather/time utterances (E01, E17, E28, E61, E67, E70) were relabelled from `escalate` to `deterministic` per owner review: they are served by tools, so sending them to the strong brain would be a routing defect. Currency rates, prices, traffic and sun times have no local tool and stay `escalate`.

Labels describe a **router-only fixture** with no unresolved context unless `prev` is set. End-to-end evaluation requires deterministic clock/weather/music answers to be served before the router and records `deterministic|fast_model|strong` separately. No deterministic tool receipt counts as a fast-model answer. A later harness must additionally cover all four modes, lower-case equivalents, upstream pending-clarification acknowledgements, cancellation, shadow-history isolation and sticky follow-ups without question words («подробнее», «а он»).

Metrics and gates:

1. **Wrong-fast-answer rate:** zero content answers from the fast model on the 71 escalation-labelled open-world cases; record false-fast eligibility separately. Also count factual hallucinations generated for fast-eligible prompts. Future robustness work should add manually labelled dropped-syllable/homophone/truncation variants; those variants are **not yet in this corpus** and no generated result is claimed. Shadow gate: zero failures on ≥300 distinct owner-labelled escalation turns (including denied turns). Rule of three: 0/300 gives a ~1% one-sided 95% upper bound, not proof of zero.
2. Reprompt safety: 30/30 produce only a repetition request, wake acknowledgement, or escalation; zero content guesses. Record repetition recall separately, since unknown safely escalates.
3. Fast recall: fraction of the 38 fast-eligible items answered without strong escalation, with context preconditions met; target ≥70%, secondary to safety.
4. Deterministic-skill gate: 10/10 `deterministic` items are answered with **zero LLM calls** (skill-route rate 10/10), values match a recomputed conversion or a mocked tool output (weather/time), and none escalates to the strong brain unless the tool reports failure; excluded from router recall and model tok/s. Any LLM call on a tool-answerable item is a routing defect. A slot-extraction call is allowed only after regex failure and is counted separately.
5. Latency: p50/p95 of rules (<1 ms), CANNED (<5 ms), BANTER (target p50 ≤ 0.7 s, p95 ≤ 1.0 s from the speaker host).
6. Russian quality, scored by the owner on 20 banter replies, Qwen 3.8 27B vs gpt-oss-120b (natural / awkward / wrong): the tie-breaker for §1.3.

Rollout: `off` → `shadow` until **both** ≥1 week and ≥300 distinct owner-labelled escalation cases are collected, plus all offline gates → `canned` → `on`. Collection may require substantially longer than a week; the small observed sample cannot predict its duration. A wrong fast-model answer returns to `shadow` and becomes a new eval case.

## 8. Owner setup

No candidate-provider key was found: metadata-only `keychain list` ran on both workstation and speaker host, and candidate-name/environment-variable checks were empty. Production Claude credentials are configured through a private source list; they were not read or used. **No authenticated model benchmark was run.** This research supplies the requested signup/keychain instructions for the no-key case.

1. **Cerebras key (primary).** Sign up at <https://cloud.cerebras.ai>, then **API Keys** in the left nav, **Generate API Key**, as the [primary console guide](https://inference-docs.cerebras.ai/console/api-keys.md) instructs (no deep-link URL is supplied there). Adding a payment method grants the $5 / 30-day trial credit. Store it in your terminal with the hidden prompt; never paste it into chat:

   ```
   keychain set aisis/cerebras --label "Cerebras API key (aisis fast tier)"
   ```

   For a future benchmark after a key exists, use ten synthetic short Russian prompts (greeting, thanks, joke, good-night wish, mood acknowledgement, persona/name, hearing confirmation, farewell, unit conversion, short repetition). Exclude one warm-up; report first answer, first delta, total completion, token usage, errors and cold reconnects separately from the speaker host. Measure decode only when usage and chunk coverage permit; never report chunk count as token count, or hidden reasoning tokens over a visible-only interval. Pace gpt-oss trial requests below **5 RPM**, including warm-up, and allow enough completion budget for reasoning. Enforce a wall deadline and sanitize all exception output. These measurements remain gated on the owner creating the key.
2. **Groq key (optional fallback).** <https://console.groq.com/keys>, then `keychain set aisis/groq --label "Groq API key (aisis fast tier fallback)"`. Use `https://api.groq.com/openai/v1`, models `openai/gpt-oss-120b` (`reasoning_effort: low`) or `qwen/qwen3.8-27b`.
3. **Decision:** send the final transcript of banter turns (not audio) to Cerebras, a US cloud (§3), or keep every turn on the brain.

## 9. Open points and what I did not verify

- Russian quality of `qwen-3.8-27b` and `gpt-oss-120b` on Cerebras, and Cerebras TTFT for Qwen: only vendor figures; the benchmark and §7 item 6 settle it.
- The older AA snapshot uses 10k-token inputs and High/Max effort. A short prompt with reasoning disabled needs a separate first-answer measurement; no predicted latency reduction is established here.
- Provider policies and account limits can change; recheck the linked primary pages when activating a paid account.
- The `/route` endpoint, overlay filler change and brain-history hook are design, not tested against the overlay source beyond `fillers.ts`'s `FillerPlan` shape.
- Implementation scope after this research: deterministic clock/date skill; strong-path search broker; `fast_tier.py`, shared history/filler integration and the §7 harness. Nothing in this report enables a new production model.

## Sources

- Cerebras: [models](https://inference-docs.cerebras.ai/models/overview), [Qwen 3.8 27B](https://inference-docs.cerebras.ai/models/qwen-3.8-27b.md), [gpt-oss](https://inference-docs.cerebras.ai/models/openai-oss.md), [rate limits](https://inference-docs.cerebras.ai/support/rate-limits.md), [reasoning](https://inference-docs.cerebras.ai/capabilities/reasoning.md), [structured outputs](https://inference-docs.cerebras.ai/capabilities/structured-outputs.md), [pricing](https://www.cerebras.ai/pricing)
- Groq: [models](https://console.groq.com/docs/models), [rate limits](https://console.groq.com/docs/rate-limits), [data](https://console.groq.com/docs/your-data)
- Artificial Analysis (via [Novita comparison, 2026-10-26](https://novita.ai/compare/best-fastest-llm-api)); [AA gpt-oss-20b providers](https://artificialanalysis.ai/models/gpt-oss-20b/providers); [AA Qwen3.8 27B providers](https://artificialanalysis.ai/models/qwen3-8-27b/providers); [BenchLeader speed board 2026-11-15](https://www.benchleader.com/leaderboards/speed)
- SambaNova: [SambaCloud models](https://docs.sambanova.ai/docs/en/models/sambacloud-models). Inception: [models and pricing](https://docs.inceptionlabs.ai/get-started/models.md), [Mercury 3 vs OpenRouter](https://www.eesel.ai/blog/inception-mercury-3). Celeris-1: [BenchLeader](https://www.benchleader.com/models/celeris-1)
- Repo base `ff69417`: adapter, calculator, reply guard, user prompt, `chatgpt_pm.py`, `pm_cdp_server.py`; inspected #85 workspace additions: `voice_skills.py` and capability prompt; branch `feat/claude-primary-api`: `claude_primary.py`; spec 140; deployed quasar `/src/backend/fillers.ts`.
