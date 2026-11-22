# Intent grammar and corpus back-tests

Ticket #107 (audit: `docs/research/intent-coverage-audit-2026-11.md`, §7.3 and §7.5). One declarative grammar for what the speaker
answers **without a language model**, a deterministic matcher, and a back-test that measures every grammar change against the
labelled corpora before it ships.

```
python3 -m intent_grammar check                       # validate, run pinned examples/negatives, list conflicting readings
python3 -m intent_grammar match "громкость на пять"   # the decision for one utterance, as JSON
python3 -m intent_grammar match --context repair.confirm --value alt="выключи свет" "да"   # ... inside a dialog context
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
      # a `text` slot may also say `no_time: true` (a value holding «через <duration>» is no reading) and
      # `reject: [word, ..]` (a value made only of those words is no reading): reminder.in uses both
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

### Contexts: replies that mean something only inside a dialog

```yaml
contexts:                      # top level; an intent joins one or several with `context:`
  - id: repair.confirm
    values: [alt, heard]       # names of the per-turn phrases the caller supplies; patterns reference them as {@alt}
    sample: {alt: ["выключи свет", "выключи"], heard: ["включи свет", "включи"]}   # what `check`, `match --context` and the back-test pass by default
intents:
  - id: repair.accept
    tier: execute
    context: repair.confirm    # one id or a list; no `priority:` = 900
    patterns: ["(да|ага|угу)", "{@alt}"]
    action: {executor: repair.accept}
```

`Grammar.decide(text, context=ActiveContext("repair.confirm", {"alt": ("выключи свет", "выключи")}))` (or just the id): the intents of that
context join the normal ones and outrank them (priority 900 unless given; **a context intent can never be given 1000 or more, the emergency
intent's priority, so `refuse.emergency` wins inside every context and no context argument can suppress it**). Without the argument, and for
every intent that has no `context`, nothing changes: a context intent is invisible to a plain `decide`, and a normal intent never sees the
argument. The values are the dialog's turn-specific words, passed by the caller and matched like literal words (by stem, the whole phrase, no
phrase = no match); the YAML stays static. `{@name}` is allowed only in intents of a context that lists `name` under `values:`. A context
intent ignores `phase` (a dialog's replies are decided before and after the quasar layer alike), and a compound is split into normal clauses
only: «да и громче» is never an accept plus a volume change. `check` and `Grammar.self_check` read every example and negative of a context
intent inside each of its contexts with the context's `sample` values.

## Route: where the grammar sits

`intent_route.IntentRouter` (code/) is the adapter's route, and what `backtest --route adapter|combined` measures. `classify(text, phase)` is pure:
`phase="pre"` = the pre-phase intents only (what the quasar overlay may ask before the container's own handlers, via `/intent/decide`);
otherwise real intents (priority >= 0, compounds included) -> arithmetic -> weather -> catch-alls (priority < 0). `route()` executes the decision
(canned/reprompt lines, clock, timers, Home Assistant climate and colour, weather, music) and returns `reply | silent | volume | dialog_end | none`; every
failure, a guessed (fuzzy/confirm) reading, or an executor owned by the quasar container is `none`, so the old path continues untouched.
Reprompts are said once a minute per station: a second miss goes to the brain. The catch-alls (priority < 0) are also skipped while the last line to the
station was a question the **brain** asked («Рассказать подробнее?»): «да», «про собак» are the brain's to continue. The mark lasts five minutes
(`BRAIN_QUESTION_WINDOW_S`: the brain's text is ready long before it is spoken; lines the hook or the quasar layer answer never reach
`note_reply`, so only a clock bounds it) or until the next line of any kind replaces it. The adapter tells the router which
replies are the brain's (`note_reply(..., brain=True)`); the grammar's own questions («Да?», «На сколько поставить таймер?») never count. A tie between two real
intents is an abstain, never split into a compound (nine or more clauses, counted by their real boundaries, are never split: the search is quadratic);
`Grammar.decide` with the default bands does not hand it to the catch-alls either (the router's explicit bands do).

**The repair dialog (`AISIS_CORRECTION=off|shadow|on`, default off; `correction.py`).** Before the classification above, `route()` first reads the utterance as the answer to a
question the speaker asked and still has open for that device (30 s; the grammar contexts `repair.confirm|choice|repeat` and their `repair.*` executors), then asks the correction
engine whether this utterance deserves a question (a second opinion the gateway stored, or the quasar layer's no-op cue from the request's `noop`). The request body of
`/intent/decide` may carry `deviceId`, `rerun: true` and `noop`; the response may carry route `rerun` (`text` = the corrected phrase the overlay runs as if spoken) and `context: true`
(the dialog is open). An accepted alternative is run here when this adapter owns its executor; handed to the overlay as `rerun` when it announced support and the action is not risky; and
otherwise asked for again («Скажите ещё раз: выключи свет.»). An unrelated command, an unmatched reply, an emergency phrase and a timeout all leave the dialog and take the normal route
(the unmatched reply reaches the brain with `[Колонка только что спросила «…»; пользователь отвечает]`). `docs/specs/140-local-voice-speaker.md` has the decision record.

A compound runs only when every clause is something the adapter itself can run (`executable`), checked before the first clause runs: a compound
that ends in `none` has changed nothing. Without timers configured a timer phrase, alone or in a compound made only of timer clauses, answers «не умею»;
a compound with any other clause goes to the old path whole.
Timer and alarm starts, sets and cancels run in arrival order, and one **utterance** delivered twice within five seconds runs once (the quasar hook and the
speaker's own turn can both carry it; so the second «отмени таймер» does not answer «Таймеров нет» to a cancel that worked). One slot remembers the last
utterance that ran such an action with its reply (a compound unless one of its clauses is a Home Assistant one or «повтори»: those depend on the speaking station, and Home Assistant may fail behind a reply that still says «готово», so a repeated one runs again, timer clause included); it is replayed only for the same utterance, after the actions in flight have finished;
any other utterance that runs an action empties it, and a failed, declined or queued result is never stored; duplicates in flight share one execution (a compound with a Home Assistant or «повтори» clause only within the same station).
Nothing else is deduplicated: Home Assistant actions and queries run every time. A voice reminder is cancelled by `reminder.cancel` («отмени напоминание»);
«отмени таймер» with only a reminder left says so and names that phrase. A Home Assistant command without a room is about the room of the speaker
that heard it (the HA area of the station's `media_player`, the way the overlay's speaker-room layer reads it); a speaker whose room Home Assistant does
not tell is asked for the room, never given the living room. Area names reach the Home Assistant template only as an escaped string literal.

An air conditioner that is **off** is switched on by every climate command that reaches the adapter (`ha.climate`, `ha.climate_delta`; ticket
195): a mode phrase sets that mode, a setpoint or «теплее»/«холоднее» starts the unit first (heat or cool by where the setpoint sits against the
room temperature when that is a degree or more away, else the mode the unit reports as `last_hvac_mode`, else automatic), and «включи кондиционер»
without a mode or temperature starts it in its last mode (a unit that already runs gets no call and the answer says so). The unit is read before
the command (the cached list of states may be a minute old) and read back after it: the answer is spoken from the state read back («Кондиционер
включён на обогрев, 23 градуса.»), a unit that does not show the command is «команду отправил, но не вижу, чтобы устройство её выполнило», an
unreadable one «не получилось проверить». These intents are `phase: post`: the quasar container answers its own AC phrases first («включи
кондиционер …» in cool mode, a bare «кондиционер» with its fixed question) and the pre-phase hook has a 250 ms budget, too short for a Home
Assistant call, so the rule applies to the phrases the container leaves alone.

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

### Information intents

`assistant.capabilities*` (post phase) and `home.climate_status` / `home.climate_bare` (`pre`) are `tier: query` intents whose executors (`skill.capabilities`, `ha.climate_status`)
build the answer from live data in the adapter (`capabilities.py`, `home_status.py`). They carry no `reply`. The air conditioner in every case («у кондея»,
«с кондеем») is the lexicon set `ac_forms`; `ac_words` stays as it is, the quasar overlay `ac-defer-to-adapter` pins it. `home.climate_bare` is declined by the router
until `AISIS_STATUS_BARE=on`. The phrase fixture `grammar/info-ru.jsonl` is read by `test_info_intents.py`; it is not part of the private corpus.

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

**The quasar corpus runner (`run_corpus.mts`, the `deploy.sh` pre-flip check) is versioned in `services/yandex-station/corpus-runner/`**;
only the private data (`intents-ru.jsonl`) and the dialog runner stay in the private corpus dir.
The [isolated corpus protocol](../../corpus-runner/README.md) hashes those fixtures
together with the maintained runner, `expected_kind.mjs` and the isolated adapter
checker. It runs all original cases through native RunPod, checking the three
exact reminder execute contracts in the adapter before requiring their strict
fast-layer fallback. `deploy.sh` verifies the real receipt and exact
source/runtime/image bindings at each stage and idle-production gate; it neither
copies fixtures into running service containers nor executes the full suites on
the workstation. (The dashboard's own corpus run uses a separate corpus tree with
its own runner and is not covered.) Its one exemption for `canned` rows: the quasar layer has no
`canned` tier, the adapter's route answers emergency phrases («позвони в полицию», domain `EMERGENCY_REFUSAL`) as `refuse.emergency`, and the runner reaches
no adapter (closed port), so exactly the `EMERGENCY_REFUSAL` rows are expected to come out as `llm` there. A `canned` row of any other domain is NOT
exempt and fails the check. The run prints the rule (`правило: canned -> llm только для домена EMERGENCY_REFUSAL (N строк)`); the emergency answer itself is
pinned by `gate.safety` (26 phrases, one miss fails `backtest --gate` and the nightly run). The rule has node tests (`node --test corpus-runner/*.test.mjs`, CI).

Per row the outcome is one of `correct`, `wrong`, `missed` (abstained on something an intent should answer), `overreach` (answered a
brain-only class), `brain_ok`, `dropped` / `no_action` (unaddressed speech handled), `false_trigger` (unaddressed speech got an answer),
`leak` (a hallucination fell through to the model). The report (`$AISIS_INTENT_REPORT_DIR`, default `~/.local/share/aisis/intent-reports/`:
`backtest-<time>-<ns>-<pid>.json` and `latest.json`; kept out of the corpus directory
so only explicitly declared fixtures enter the offloaded bundle) holds, per `source/split`: rows, addressed, **coverage** (answered without a model / addressed), correct rate, **completed** rate
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

**Dialog rows (repair contexts, `gate.dialog`).** A row with a `context` field says how a text reads INSIDE an open dialog context
(`Grammar.decide(text, context=ActiveContext(context, values))`): `{text, context, values?, expect: {intent: <id>|null}}` or
`expect: {outside: true}` (no reply of the dialog: it leaves the context and takes the normal route); a real-corpus row may say `truth: <intent id>|outside`
instead. `values` default to the context's `sample`. Dialog rows are **not** part of the real/synthetic/all splits (a reply is meaningless outside its dialog,
and the coverage numbers must stay comparable), they are reported in a `dialog` block per kind and context and gated as a unit: `gate.dialog: {max_wrong: 0}`
(every row decided as labelled) plus every `gate.safety` phrase decided as the emergency intent **inside every declared context**. The committed
`grammar/dialog-ru.jsonl` carries them for the shipped grammar (kind `synthetic`: the generated matrix of every standard reply in every context, with
paraphrases and negatives; kind `authored`: phrases written from the owner's wording; no recorded real reply to a repair question exists yet, so no row is
called `real`); any `context` row in the private corpus files joins them with the kind of its file, and is left out of the normal rows. A row of an
undeclared context fails, it is never skipped. The back-test summary prints `dialog rows n=…` per kind.

**Regression ratchet (every split, held-out included).** Against the previous promoted report the run compares row by row over the rows both
reports have (same id, same label, same side of the split). A row may get better, never worse: handled → missed/wrong and missed → wrong
fail, wrong → abstained is an improvement. Nothing is sampled (same phrases, deterministic engine), so there is no tolerance; a one-row
allowance would let a 5.6-point loss through at 18 rows. A baseline row gone from the corpus (`lost_rows`) or relabelled fails too, and a
baseline of another route, split definition or schema is `incompatible`: it fails instead of silently comparing nothing (`REPORT_SCHEMA` is also the
version of how rows are scored: it is bumped with any scoring change, so an older baseline needs the owner's `--init-baseline REASON`, which accepts an
incompatible baseline). No usable baseline
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

## Nightly loop

```
python3 -m intent_grammar nightly prepare [--hours 24] [--jev] [--dry-run]   # corpus growth, measurement, clusters, the brief: the day's private working directory
python3 -m intent_grammar nightly run     [--hours 24] [--jev] [--dry-run]   # prepare, then launch the omp session that proposes and lands changes
python3 -m intent_grammar nightly status                                      # summary (and handoff) of the newest run
python3 -m intent_grammar nightly morning                                     # hand the night's hatch requests to the owner through the harness
python3 -m intent_grammar nightly label "<phrase>" <TRUTH>                    # queue an owner label for the real corpus
```

Every night at 03:30 (`systemd/aisis-intent-nightly.timer`, `Persistent=true` so a missed night runs at the next boot; `systemd/aisis-intent-nightly.service` runs `nightly run`):

1. **Extract** the last 24 h of spoken utterances from the dialog store with the layer that answered each (quasar fast layer / brain / nothing).
2. **Grow the corpus** (see below), then **measure**: the production route over the labelled corpora with the previous promoted report as baseline (`--route combined`,
   the grammar's `gate:` with the held-out ratchet `gate.holdout`, the emergency safety set `gate.safety`, and the reported 90 % `gate.goal`). The report is written to the
   day's directory (mode 0600: it holds the dev utterances) and becomes `latest.json` only when the gate held, as in `backtest`. The nightly run never makes a baseline itself: without a usable
   `latest.json` (none yet, another route, split definition or schema) the gate is blocked with `no baseline`. The owner starts it once with `backtest --route combined --gate --no-text --init-baseline REASON`.
   The summary carries the per-row comparison (`regressed`, `lost_rows`, `relabelled`) and the superseded held-out floor as `legacy_floor`.
3. **Cluster** what only the brain answered (stem-set similarity); with `--jev` ask Jev for the nearest existing intent of each cluster.
4. **Brief** an omp session through the sanctioned detached launcher (skill `rig-detached-omp`, environment stripped of keys and tokens).
   - Gate **green**: the session proposes new/merged/improved intents, only if the gate still holds, and lands them through the **normal merge path**: its own worktree, tests,
     the back-test gate, ONE `review diff --staged --task 107 --commit --detach` (shared board and queue; no hatch), a pull request with `Refs #107`, then
     `gh ship <PR> --task '#107'` from the repository root. Nothing is merged any other way and no gate flag is skipped. It comments the aggregates on the open issue
     «Nightly intent loop reports», sends **one** Telegram message and writes `handoff.json`. Nothing survives the gate, or the diff is trivial: nothing is proposed and the report says why.
   - Gate **red** before the night's change (a failed rule, a failed safety phrase, or no corpus to measure on): the brief is the blocked one. The session runs
     `review brainstorm "<failed rules>" --task 107`, works the fix out in its worktree and leaves `proposal.patch`, `brainstorm.md` and `report.md`; it makes no commit, push, PR or
     `gh ship`. The loop sends the owner one Telegram line (a safety failure is called out as such). A gate that fails is satisfied, never bypassed.
   - A gate that blocks the session later (back-test, test, review finding, CI, `gh ship`): it does not bypass it and does not stop; it runs `review brainstorm`, satisfies the gate the way
     that discussion settles, a refactor included, and records the outcome in the PR body. Only a gate that is demonstrably broken (refuses a correct state, command and output as evidence)
     gets a ticket (`task new`) and a line in `<date>/hatch-requests.md`. The session never sets a `RIG_HATCH_REQUEST_*` variable and the `require-review-before-commit` hatch is never an option.
5. **Morning** (`systemd/aisis-intent-morning.timer`, 08:30): `nightly morning` turns each night's `hatch-requests.md` into one harness ticket (`harness tasks new`), once per file (a `hatch-asked` marker
   beside it; a refused ask is reported and repeated the next morning). The owner decides there; the loop grants nothing.

**Corpus growth.** Each `prepare` appends the newly labelled real rows to `real-voice-nightly-labelled.jsonl` in the corpus directory (a real source by name; the owner's hand-labelled files are never touched).
Sources: the owner's feedback inbox `<out>/owner-labels.jsonl` (lines `{"text", "truth", "ts"?}`, filled by `nightly label`; `labeller: "owner"`), and, only with `--jev`, the cluster
representatives Jev maps to an existing intent with confidence ≥ 0.8 (`truth` = that intent's first label, `labeller: "jev"`, the choice and confidence in `note`). A row is added only when no real
file already has its canonical text (`backtest.split_key`: case, ё, punctuation, whitespace), so the file is append-only and deduplicated; no row is ever rewritten or removed, the file is
mode 0600, and new rows get `verdict: "unverified"` so they never count as handled by the quasar layer. The held-out side of a row is the stable hash of its text, computed by the loader. An inbox line
that cannot be used (not JSON, no text or truth, unknown label) is listed in the summary and on stderr with its line number and stays where it is. A corpus with an unreadable line is not appended to at all:
the run stops with the lines named, because the line could be the duplicate of a candidate.

**Jev is opt-in and off by default**: it sends private household utterances to Vercel. The unit does not pass `--jev`. To enable it put `AISIS_NIGHTLY_ARGS=--jev` and `VERCEL_JEV_KEY=...` in
`~/.config/aisis/intent-nightly.env` (mode 0600; the unit reads it as an optional `EnvironmentFile` and expands `$AISIS_NIGHTLY_ARGS` in `ExecStart`), and delete the line to switch it off. `--jev` without the key,
or without the Jev module, stops the run with exit 2 and a Telegram line; failed Jev calls are counted in the summary and warned about on stderr; none of it is silent. The key stays in this process.

**`--dry-run`** prepares and writes the brief and nothing else outside the day's private directory: no corpus row (the summary says how many would be added), no baseline promotion, no report lock,
no Jev call (with `--jev` only the key and module are checked), no session, no Telegram. Read `brief.md`, `summary.json` and `backtest.json` in `<out>/<date>/` to see what a live night would do.

A merged grammar takes effect at the next restart of the station adapter (it loads the committed JSON bundle at start); the nightly session does not restart the adapter. The service sets `KillMode=process` so
the detached omp session is not killed when the oneshot unit ends.

All working files (`~/.local/share/aisis/intent-nightly/<date>/`, mode 0600) hold real utterances and never leave the machine; only aggregates and intent ids reach git, a PR, an issue or Telegram.

**Why a systemd timer.** omp has no scheduling facility: `omp --help`, the omp docs (CLI reference, slash commands, SDK, settings, tools), the installed skills and `~/.omp` contain only an interactive loop mode and advisor
review cadences, nothing that starts a session at a time. A user timer that launches a detached omp session is the established pattern on this host, and `aisis-whisper-prompt.timer` is the in-repo precedent for a nightly job.

## Provenance

Canonicaliser (ё fold, edge-only courtesy strip, span map, single-edit typo repair), the suffix stemmer and the clock/duration
modelling follow HyperCalendarBot (`src/services/intent/canonical-input.ts`, `event-reference.ts`, `date-time-words.ts`; same owner, no
licence file). The code here is a Python re-implementation for spoken Russian (numerals in all cases, spoken clock idioms), not a copy.
