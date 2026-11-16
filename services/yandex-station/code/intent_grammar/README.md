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
      level: {type: number, range: [0, 10], required: true, ask: "Какую громкость поставить?"}
    patterns: ["{set_verb}? громкост* (на|до)? {level}"]
    guards: [not_question]     # not_negated | not_question | is_question; acting tiers get not_negated by default
    allow_negation: false      # opt out of the default guard (for «не надо»)
    fuzzy_ok: false            # a fuzzy reading of an acting intent is downgraded to `confirm` unless true
    action: {executor: speaker.volume, args: {level: "$level"}}
    reply: {kind: earcon}      # canned (say: [..]) | template (say: "..{slot}..{duration_text}") | earcon | none
    examples: ["громкость на пять", {say: "сделай громкость семь", slots: {level: 7}}]   # pinned positives
    negatives: ["громкость 150", "не делай громкость пять"]                              # pinned: must not match
backtest:
  brain_labels: [CHITCHAT, KNOWLEDGE]            # corpus classes only a model answers (expected: abstain)
  unaddressed: {HALLUCINATION: drop, AMBIENT: no_action}   # speech not for the speaker: the correct handling
  holdout_percent: 20
gate:                          # see «Gate» below; a coverage floor on a missing or empty split fails
  max_false_triggers: 2        # totals over the whole corpus
  max_wrong_actions: 6
  max_p95_us: 25000
  min_coverage: {real/holdout: 0.85, real/all: 0.80, all/all: 0.85}
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
| `{name:type}` | inline slot: `number`, `ordinal`, `percent` (a number, the unit word «процентов» is optional: write `{p:number} процент*` when the unit is required), `delta` («на 10 процентов»), `duration` (seconds), `clock` (`{hour, minute, part, ambiguous, text}`), `text`, `word`, or a lexicon mapping name (value = its key) |
| `_` / `...` | exactly one / any number of arbitrary tokens |

Literals of a pattern and of a lexicon are folded like utterances (lower case, ё -> е), so `ещё раз` and `Алиса` match. A number is
digits (at most 9 per part) or words; a signed `-5` is no number and matches nothing. A past-tense `-л` form never shares a stem with
the imperative (`включил` is not `включи`: a report is not a command).

Cross-cutting passes, not per-intent rules: ё fold, `-ка`, `50%`, wake and courtesy strip with a span map back to the raw text,
negation and question flags, optional single-edit typo repair of `typo_vocabulary` (abstains when two words are as close), clause
split on `connectors` when the whole utterance has **no reading** (two readings that tie abstain, they are never split) and every
clause is read by an acting/query/canned intent. A compound decision has the tier of its most demanding clause (`confirm` beats
`execute` beats `query` beats `canned`), and the utterance's question mark applies to every clause.

Decision rules: **abstain over guess**. Two different intents at the same priority and specificity abstain (the brain takes the
turn); a negated utterance never reaches an `execute`/`confirm` intent; a required slot that is missing asks (`clarify`) when `ask`
is given and otherwise is no match.

Replies: `Grammar.say(decision, pick, values=None)` / `render_reply`. A template whose placeholder nobody filled (`Громкость {level}.`
without a level) gives `None`, never the literal text; the executor passes what it measured as `values={"level": 5}` (they win over
matched slots).

### Volume action args (executor `speaker.volume`)

Exactly one of: `step` (`1` / `-1`, relative), `level` (a plain number on the speaker's own 0..10 scale) or `percent` (0..100).
A bare number is a level and must be within 0..10, so «громкость 50» is not matched (the brain takes it); a percent needs its unit
(«50%», «пятьдесят процентов») and keeps it: «громкость 10%» is `{percent: 10}`, «громкость на максимум» is `{level: 10}`.

## Back-test

Corpus directory: `$AISIS_INTENT_CORPUS_DIR`, else `~/.local/share/aisis/corpus/` (private, never in git). Files:
`real-voice-*.jsonl` or `*-labelled.jsonl` (`{text, truth, ts?}`; source `real`) and `intents-ru.jsonl`
(`{text, expect: {kind, domain, tier?, action?}}`; source `synthetic`, truth = domain).

Per row the outcome is one of `correct`, `wrong`, `missed` (abstained on something an intent should answer), `overreach` (answered a
brain-only class), `brain_ok`, `dropped` / `no_action` (unaddressed speech handled), `false_trigger` (unaddressed speech got an answer),
`leak` (a hallucination fell through to the model). The report (`reports/backtest-<time>-<ns>-<pid>.json`, `reports/latest.json`) holds,
per `source/split`: rows, addressed, **coverage** (answered without a model / addressed), correct rate, **completed** rate (correct and not
a clarification/reprompt), wrong actions (over every clause of a compound), false triggers, latency p50/p95/max in microseconds; plus
per-truth and per-intent tables, dev misses, held-out misses (id and labels only), a row-id → outcome map, the split definition and
the comparison with the previous report.

**Expectations.** A synthetic row with `expect.tier` / `expect.action` is correct only when the label matches **and** the decision has
that tier and the action's essential arguments: `volume:N` → `speaker.volume` with `level == N`, `setpoint:N` → `ha.climate` with
`temperature == N`, `brightness:N` → `ha.light_brightness` with `percent == N`, `on:ENTITY` / `off:ENTITY` → `ha.light`/`ha.device` with
that `state` (the entity id is not compared: the grammar speaks in rooms and devices). Any other form (`brain:…`) and `tier: llm` have
no exact mapping: that row stays label-only. A compound is correct when one of its clauses carries the action. Rows without `tier` /
`action` are label-only as before.

**Row ids** are content-derived: sha1 of the file stem, `ts` (when present), the canonical text and the n-th exact duplicate. Inserting,
removing or reordering rows never renames the others, so the comparison with the previous report stays valid.

**Held-out split.** `holdout_percent` of the canonical texts (by hash; all copies of a phrase on one side) is never printed:
tune on `dev`, read `holdout` as the honest number. The canonical text is grammar-independent: lower case, ё -> е, punctuation
removed, whitespace collapsed (the grammar's fillers, wake words and typo list are NOT applied), so editing the grammar can never
move a row between `dev` and `holdout`. The definition is versioned (`SPLIT_VERSION`, recorded as `split.version` in every report);
changing it moves rows, and the comparison only trusts rows that stayed on the same side. It is still small for the real corpus, and
an intent list derived from the same utterances stays optimistic: new nightly rows are the unbiased test.

**Gate.** `backtest --gate` exits 1 when a `gate:` rule fails, when any pinned example/negative fails, when a row handled correctly in
the previous report is no longer handled, or when held-out coverage dropped. `max_wrong_actions`, `max_false_triggers` and `max_p95_us`
are totals over the whole corpus (`all/all`) and apply to a first report too, with no baseline. A `min_coverage` floor fails when its
split is absent or has nothing addressed (a synthetic-only corpus cannot pass a `real/holdout` floor). Regressions and the coverage /
correct / completed deltas are computed over the rows present in both reports (same side of the split), so adding unhandled rows
cannot fail the gate and removing rows cannot hide a drop. `--no-text` leaves utterance texts out of a report (use it for anything that
leaves the machine).

**Baseline.** `reports/latest.json` is the baseline of the next run and is promoted only by a run whose gate held: a failing run is
written and archived under its own name but the baseline stays the last passing report, so an identical rerun fails again. Reports are
written atomically (temp file, then replace) under a lock, names carry the clock in nanoseconds and the pid, and `--out` and the
reports folder are created independently. `--no-latest` never promotes. The shipped `gate:` floors are what the seed grammar meets and
rise with the grammar (see the comment in `grammar/ru.yaml`).

## Provenance

Canonicaliser (ё fold, edge-only courtesy strip, span map, single-edit typo repair), the suffix stemmer and the clock/duration
modelling follow HyperCalendarBot (`src/services/intent/canonical-input.ts`, `event-reference.ts`, `date-time-words.ts`; same owner, no
licence file). The code here is a Python re-implementation for spoken Russian (numerals in all cases, spoken clock idioms), not a copy.
