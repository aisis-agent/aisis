# Intent grammar and corpus back-tests

Ticket #107 (audit: `docs/research/intent-coverage-audit-2026-11.md`, §7.3 and §7.5). One declarative grammar for what the speaker
answers **without a language model**, a deterministic matcher, and a back-test that measures every grammar change against the
labelled corpora before it ships.

```
python3 -m intent_grammar check                       # validate, run pinned examples/negatives, list conflicting readings
python3 -m intent_grammar match "громкость на пять"   # the decision for one utterance, as JSON
python3 -m intent_grammar backtest --route combined --gate --misses   # numbers, report file, exit 1 on a gate failure or regression
python3 -m intent_grammar compile [--check]           # grammar/ru.yaml -> grammar/ru.json
```

After every grammar edit: `check`, `compile`, then `backtest --route combined --gate` (the nightly loop does the same and opens a PR only when the gate holds).

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
    priority: 0                # higher wins; equal priority and specificity with two intents = abstain; below 0 = a catch-all
    phase: post                # pre: may answer before the quasar container's own handlers; post (default): only when they found nothing
    max_tokens: 4              # the utterance has at most this many words (catch-alls keep to short phrases)
    slots:
      level: {type: number, range: [0, 10], required: true, ask: "Какую громкость поставить?"}
    patterns: ["{set_verb}? громкост* (на|до)? {level}"]
    guards: [not_question]     # not_negated | not_question | is_question | has:<set> | not_has:<set> (any word in a lexicon set);
                               # acting tiers (execute, confirm) get not_negated by default
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
  min_coverage: {real/all: 0.80, all/all: 0.85}
  holdout: {splits: [real/holdout], ci_min_addressed: 20, floor_min_addressed: 60, floor: {coverage: 0.85, correct: 0.80}, legacy_floor: {...}}
  goal: {split: real/all, coverage: 0.90, correct: 0.90}
  safety: {intent: refuse.emergency, phrases: [...]}   # must-pass emergency phrases
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

## Route: where the grammar sits

`intent_route.IntentRouter` (code/) is the adapter's route, and what `backtest --route adapter|combined` measures. `classify(text, phase)` is pure:
`phase="pre"` = the pre-phase intents only (what the quasar overlay may ask before the container's own handlers, via `/intent/decide`);
otherwise real intents (priority >= 0, compounds included) -> arithmetic -> weather -> catch-alls (priority < 0). `route()` executes the decision
(canned/reprompt lines, clock, timers, Home Assistant climate and colour, weather, music) and returns `reply | silent | volume | dialog_end | none`; every
failure, a guessed (fuzzy/confirm) reading, or an executor owned by the quasar container is `none`, so the old path continues untouched.
Reprompts are said once a minute per station: a second miss goes to the brain. The catch-alls (priority < 0) are also skipped when the speaker's last
line to that station was a question: «да», «про собак» answer the brain's «Рассказать подробнее?» and are the brain's to continue. A tie between two real
intents is an abstain, never split into a compound (a chain of eight or more clauses is never split: the search is quadratic); `Grammar.decide` with
the default bands does not hand it to the catch-alls either (the router's explicit bands do).

A compound runs only when every clause is something the adapter itself can run (`executable`), checked before the first clause runs: a compound
that ends in `none` has changed nothing. A timer or alarm **start** is deduplicated for five seconds (the quasar hook and the speaker's own turn can
both carry the phrase; duplicates in flight share one execution; a cancel clears it; a failure is never replayed); nothing else is: Home Assistant
actions, cancels and queries run every time. A Home Assistant command without a room is about the room of the speaker that heard it (the HA area of
the station's `media_player`, the way the overlay's speaker-room layer reads it); a speaker whose room Home Assistant does not tell is asked for the
room, never given the living room.

Replies: `Grammar.say(decision, pick, values=None)` / `render_reply`. A template whose placeholder nobody filled (`Громкость {level}.`
without a level) gives `None`, never the literal text; the executor passes what it measured as `values={"level": 5}` (they win over
matched slots).

### Volume action args

Executor `speaker.volume`: exactly one of `step` (`1` / `-1`, relative), `level` (a plain number on the speaker's own 0..10 scale) or `percent`
(0..100). A bare number is a level and must be within 0..10, so no volume intent reads «громкость 50» (no action: the catch-all reprompt or the
brain takes it); a percent needs its unit («50%», «пятьдесят процентов») and keeps it: «громкость 10%» is `{percent: 10}`, «громкость на
максимум» is `{level: 10}`.

A relative change by an amount is the separate executor `speaker.volume_delta`: `direction` (`1` / `-1`) and exactly one of `levels` (a plain
number, 1..10 like `volume.set`) or `percent` (1..100, the unit is required). Any other unit is not volume: «прибавь два градуса» is
`climate.warmer`'s, and a bare «прибавь на тридцать» is no number of levels. `intent_route.volume_from` turns the args into the overlay's
`target` (level / 10 or percent / 100), `steps` or `deltaLevels`; an action that carries none or several of the units does nothing.

## Back-test

Corpus directory: `$AISIS_INTENT_CORPUS_DIR`, else `~/.local/share/aisis/corpus/` (private, never in git). Files:
`real-voice-*.jsonl` or `*-labelled.jsonl` (`{text, truth, ts?}`; source `real`) and `intents-ru.jsonl`
(`{text, expect: {kind, domain, tier?, action?}}`; source `synthetic`, truth = domain).

`quasar-replay.jsonl` (optional, written by `tools/replay_quasar.mts` in a throwaway container of the active quasar image) says which phrases the deployed
container already answers without a model, and how (`via`); with it `--route quasar` is the baseline before the grammar and `--route combined` the production order
(grammar pre-phase -> quasar -> grammar rest). Without it the flag falls back to the corpus' own record.

`--route combined` counts a grammar decision only where the runtime acts on it (`IntentRouter.executable`, the check `route()` itself uses): the adapter
turn cannot change the volume (only the overlay's pre-phase hook can), a guessed reading waits for the owner's «да», and an executor that is not
configured is declined. The measurement assumes the full production configuration (timers, Home Assistant actions, skills bound:
`intent_route.measured_router`); a deployment without `AISIS_TIMER_TTS_ENTITY` / `AISIS_TIMER_PLAYER` answers its timer phrases «не умею» instead.

Per row the outcome is one of `correct`, `wrong`, `missed` (abstained on something an intent should answer), `overreach` (answered a
brain-only class), `brain_ok`, `dropped` / `no_action` (unaddressed speech handled), `false_trigger` (unaddressed speech got an answer),
`leak` (a hallucination fell through to the model). The report (`$AISIS_INTENT_REPORT_DIR`, default `~/.local/share/aisis/intent-reports/`:
`backtest-<time>-<ns>-<pid>.json` and `latest.json`; kept out of the corpus directory because `deploy.sh` copies that directory into the quasar
containers) holds, per `source/split`: rows, addressed, **coverage** (answered without a model / addressed), correct rate, **completed** rate
(correct and not a clarification/reprompt), wrong actions (over every clause of a compound), false triggers, latency p50/p95/max in
microseconds; plus per-truth and per-intent tables, dev misses, held-out misses (id and labels only), a row-id → outcome map, the split
definition and the comparison with the previous report.

**Expectations.** A synthetic row with `expect.tier` / `expect.action` is correct only when the label matches **and** the decision has
that tier and the action's essential arguments: `volume:N` → `speaker.volume` with `level == N`, `setpoint:N` → `ha.climate` with
`temperature == N`, `brightness:N` → `ha.light_brightness` with `percent == N`, `on:ENTITY` / `off:ENTITY` → `ha.light`/`ha.device` with
that `state` (the entity id is not compared: the grammar speaks in rooms and devices). Any other form (`brain:…`) and `tier: llm` have
no exact mapping: that row stays label-only. A decision of the quasar container (`quasar.handled` in `--route quasar|combined`) carries the
tier it answered with (the replay's `via`: `fast-confirm` is a confirmation question, `fast-clarify` a clarification, neither a completed action),
no labels and no arguments (the replay records neither): it is judged on that tier against `expect.tier` alone, never on a label copied from the
row. A compound is correct when one of its clauses carries the action. Rows without `tier` / `action` are label-only as before.

**Row ids** are content-derived: sha1 of the file stem, `ts` (when present), the canonical text and the n-th exact duplicate. Inserting,
removing or reordering rows never renames the others, so the comparison with the previous report stays valid.

**Held-out split.** `holdout_percent` of the canonical texts (by hash; all copies of a phrase on one side) is never printed:
tune on `dev`, read `holdout` as the honest number. The canonical text is grammar-independent: lower case, ё -> е, punctuation
removed, whitespace collapsed (the grammar's fillers, wake words and typo list are NOT applied), so editing the grammar can never
move a row between `dev` and `holdout`. The definition is versioned (`SPLIT_VERSION`, recorded as `split.version` in every report);
changing it moves rows, and the comparison only trusts rows that stayed on the same side. It is still small for the real corpus, and
an intent list derived from the same utterances stays optimistic: new nightly rows are the unbiased test.

**Gate.** `backtest --gate` exits 1 when a `gate:` rule fails, when any pinned example/negative fails, or when the **safety set** fails
(`gate.safety`: phrases that must always read as `refuse.emergency`, independent of the corpus and of every coverage number).
`max_wrong_actions`, `max_false_triggers` and `max_p95_us` are totals over the whole corpus (`all/all`). A `min_coverage` floor fails when its
split is absent or has nothing addressed.

**Regression ratchet (every split, held-out included).** Against the previous promoted report the run compares row by row over the rows both
reports have (same id, same label, same side of the split). A row may get better, never worse: handled → missed/wrong and missed → wrong
fail, wrong → abstained is an improvement. Nothing is sampled (same phrases, deterministic engine), so there is no tolerance; a one-row
allowance would let a 5.6-point loss through at 18 rows. A baseline row gone from the corpus (`lost_rows`) or relabelled fails too, and a
baseline of another route, split definition or schema is `incompatible`: it fails instead of silently comparing nothing. No usable baseline
is itself a failure with `--gate`; the first run of a route (or an explicit rebaseline) is `--init-baseline REASON`, recorded in the report.

**The owner's rebaseline.** A corrected label, an edited row text or a renamed corpus file (row ids include the file name) turns the gate red
for every later run, because a lost or relabelled row can never come back. The way out is the owner's explicit
`backtest --route combined --gate --init-baseline "why"`: it is accepted against a **compatible** baseline exactly when the run reports lost or
relabelled rows, writes the reason and the counts into the report (`baseline.init_reason`, `baseline.rebaselined: {lost_rows, relabelled}`) and
evaluates the gate again without those two checks only: every other check, the per-row regressions included, still applies, and the report
becomes the new baseline only when the gate then holds. A compatible baseline with nothing lost or relabelled refuses `--init-baseline` (every row is
compared with it). The nightly loop never passes the flag.

**Held-out floor by n.** The independent held-out split is small (19 real rows, 18 addressed at the time of writing), so no hand-picked absolute
rate is gated on it today. `gate.holdout.floor` (coverage, correct) switches on with n: from `ci_min_addressed` rows the split fails when the
whole 95 % Wilson interval of a rate is below the floor (clearly under it: one phrase cannot flip that), from `floor_min_addressed` rows the point
floor applies too. The superseded 0.85 held-out floor is printed in every run (`legacy_floor`) so a green gate is never read as that floor being
met. The 90 % product target is `gate.goal`, reported with its Wilson interval and gap, never gated. The corpus grows by new labelled real
rows (the nightly loop appends them, append-only); they sit on a stable side of the hash split and join the row-by-row check once a promoted
report contains them.

**Baseline.** `latest.json` in the report directory is the baseline of the next run and is promoted only by a run whose gate held: a failing run is
written and archived under its own name but the baseline stays the last passing report, so an identical rerun fails again. Reports are
written atomically (temp file, then replace) under a lock, names carry the clock in nanoseconds and the pid, and `--out` and the
report folder are created independently. `--no-latest` never promotes. The shipped `gate:` floors are ratchets: raise them when a change
holds a higher number, never lower them to make a change pass (see the comment in `grammar/ru.yaml`).

## Provenance

Canonicaliser (ё fold, edge-only courtesy strip, span map, single-edit typo repair), the suffix stemmer and the clock/duration
modelling follow HyperCalendarBot (`src/services/intent/canonical-input.ts`, `event-reference.ts`, `date-time-words.ts`; same owner, no
licence file). The code here is a Python re-implementation for spoken Russian (numerals in all cases, spoken clock idioms), not a copy.
