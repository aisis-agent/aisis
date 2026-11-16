# Intent grammar and corpus back-tests

Ticket #107 (audit: `docs/research/intent-coverage-audit-2026-11.md`, §7.3 and §7.5). One declarative grammar for what the speaker
answers **without a language model**, a deterministic matcher, and a back-test that measures every grammar change against the
labelled corpora before it ships.

```
python3 -m intent_grammar check                       # validate, run pinned examples/negatives, list conflicting readings
python3 -m intent_grammar match "громкость на пять"   # the decision for one utterance, as JSON
python3 -m intent_grammar backtest --gate --misses    # numbers, report file, exit 1 on a gate failure or regression
python3 -m intent_grammar compile [--check]           # grammar/ru.yaml -> grammar/ru.json
```

Run from `services/yandex-station/code/`. The YAML is the authoring source (`grammar/ru.yaml`, PyYAML needed at authoring time
and in CI); the speaker loads the committed JSON bundle `grammar/ru.json`, so the runtime venv needs no new dependency. CI fails
when the bundle is stale.

## Grammar file

```yaml
version: 1
locale: ru
lexicon:                       # shared vocabulary; a list is a set of forms, a mapping is key -> forms
  wake: [алиса, яндекс]        # reserved: wake, lead_in, fillers, negation, question_words, connectors, typo_vocabulary
  lead_in: [ну, слушай]        #   lead_in: dropped at the edges only; fillers: dropped anywhere
  negation: [не, нельзя]
  rooms: {kitchen: [кухн*], bedroom: [спальн*]}
intents:
  - id: volume.set
    tier: execute              # execute | confirm | clarify | query | canned | reprompt | drop
    labels: [VOLUME]           # corpus classes this intent is the correct answer to (back-test truth)
    priority: 0                # higher wins; equal priority and specificity with two intents = abstain
    slots:
      value: {type: percent, range: [0, 100], required: true, ask: "Какую громкость поставить?"}
    patterns: ["{set_verb}? громкост* (на|до)? {value}"]
    guards: [not_question]     # not_negated | not_question | is_question; acting tiers get not_negated by default
    allow_negation: false      # opt out of the default guard (for «не надо»)
    fuzzy_ok: false            # a fuzzy reading of an acting intent is downgraded to `confirm` unless true
    action: {executor: speaker.volume, args: {level: "$value"}}
    reply: {kind: earcon}      # canned (say: [..]) | template (say: "..{slot}..{duration_text}") | earcon | none
    examples: ["громкость на пять", {say: "сделай громкость семь", slots: {value: 7}}]   # pinned positives
    negatives: ["громкость 150", "не делай громкость пять"]                              # pinned: must not match
backtest:
  brain_labels: [CHITCHAT, KNOWLEDGE]            # corpus classes only a model answers (expected: abstain)
  unaddressed: {HALLUCINATION: drop, AMBIENT: no_action}   # speech not for the speaker: the correct handling
  holdout_percent: 20
gate: {max_wrong_actions: 0, max_false_triggers: 0, max_p95_us: 25000, min_coverage: {"real/holdout": 0.5}}
```

### Patterns

Whole utterance must be consumed (after wake words and courtesy words are stripped); words match by **stem**, so write base forms.

| Syntax | Meaning |
|---|---|
| `слово` | any inflection (`кухня` matches кухне, кухню) |
| `=слово` / `слов*` | exactly this token / any token that starts with it |
| `~слово` | like a word, and in the fuzzy pass (only if nothing matched exactly) one edit or one sound class away |
| `(а\|б в\|г)` `X?` | alternatives (each a token sequence), optional |
| `{name}` | a lexicon set, or a slot declared under `slots:` |
| `{name:type}` | inline slot: `number`, `ordinal`, `percent` (number + «процентов»), `delta` («на 10 процентов»), `duration` (seconds), `clock` (`{hour, minute, part, ambiguous, text}`), `text`, `word`, or a lexicon mapping name (value = its key) |
| `_` / `...` | exactly one / any number of arbitrary tokens |

Cross-cutting passes, not per-intent rules: ё fold, `-ка`, `50%`, wake and courtesy strip with a span map back to the raw text,
negation and question flags, optional single-edit typo repair of `typo_vocabulary` (abstains when two words are as close), clause
split on `connectors` when the whole utterance has no reading and every clause is read by an acting/query/canned intent.

Decision rules: **abstain over guess**. Two different intents at the same priority and specificity abstain (the brain takes the
turn); a negated utterance never reaches an `execute`/`confirm` intent; a required slot that is missing asks (`clarify`) when `ask`
is given and otherwise is no match.

## Back-test

Corpus directory: `$AISIS_INTENT_CORPUS_DIR`, else `~/.local/share/aisis/corpus/` (private, never in git). Files:
`real-voice-*.jsonl` or `*-labelled.jsonl` (`{text, truth, ts?}`; source `real`) and `intents-ru.jsonl` (`{text, expect: {kind, domain}}`;
source `synthetic`, truth = domain).

Per row the outcome is one of `correct`, `wrong`, `missed` (abstained on something an intent should answer), `overreach` (answered a
brain-only class), `brain_ok`, `dropped` / `no_action` (unaddressed speech handled), `false_trigger` (unaddressed speech got an answer),
`leak` (a hallucination fell through to the model). The report (`reports/backtest-<time>.json`, `reports/latest.json`) holds, per
`source/split`: rows, addressed, **coverage** (answered without a model / addressed), correct rate, **completed** rate (correct and not
a clarification/reprompt), wrong actions, false triggers, latency p50/p95/max in microseconds; plus per-truth and per-intent tables,
dev misses, held-out misses (id and labels only), a row-id → outcome map, and the comparison with the previous report.

**Held-out split.** `holdout_percent` of the canonical texts (by hash; all copies of a phrase on one side) is never printed:
tune on `dev`, read `holdout` as the honest number. It is still small for the real corpus, and an intent list derived from the
same utterances stays optimistic: new nightly rows are the unbiased test.

**Gate.** `backtest --gate` exits 1 when a `gate:` rule fails, when any pinned example/negative fails, when a row handled correctly in
the previous report is no longer handled, or when held-out coverage dropped. `--no-text` leaves utterance texts out of a report
(use it for anything that leaves the machine).

## Provenance

Canonicaliser (ё fold, edge-only courtesy strip, span map, single-edit typo repair), the suffix stemmer and the clock/duration
modelling follow HyperCalendarBot (`src/services/intent/canonical-input.ts`, `event-reference.ts`, `date-time-words.ts`; same owner, no
licence file). The code here is a Python re-implementation for spoken Russian (numerals in all cases, spoken clock idioms), not a copy.
