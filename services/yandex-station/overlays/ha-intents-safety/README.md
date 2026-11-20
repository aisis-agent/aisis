# The fast-intent layer does not act on a negated phrase and keeps each station's questions and memory to itself

Two defects of `backend/ha-intents.ts` (the quasar fast-intent layer, part of the image; its source is the public fork
[aisis-quasar](https://github.com/alex-mextner/aisis-quasar)), found by the retro-review of the aisis-quasar patch range
(https://github.com/alex-mextner/aisis/issues/186) and fixed under https://github.com/alex-mextner/aisis/issues/202. The fix ships twice,
identical in logic: as a patch commit in the fork, and as this overlay on the exact sources of the running image.

## Finding 2: a negated phrase is run, and a negated answer confirms

The layer reads a verb and a noun and nothing else:

* «не включай чайник» switches the kettle plug (`switch.kettle_plug_local`) on, «не выключай свет» switches the lights off,
  «не включай кондиционер» starts the AC in cool, «не тише» and «не убавляй громкость» lower the volume, «нельзя включать чайник»
  asks «Включить чайник?», «включи не чайник, а свет» boils the kettle.
* A pending yes/no question is confirmed by any answer that holds a yes word: «не делай», «не давай», «не точно», «не верно», «не ок» and
  «нет, давай» are all a «да» (`resolveClarification` tests the yes words before the no words and anywhere in the phrase). «не свет, а
  музыку» answers «Свет или музыку?» with «свет» (it holds the word) and the choice is **remembered** in `ha.ambiguity.json`.
  «не семь» fills the volume slot with seven.

**Verdict: exploitable, by speaking naturally.** Nobody has to attack anything. Reproduced on the unpatched image sources
(`run_check.sh`, «without the overlay»): each of those phrases executes or confirms, with the adapter down and with the adapter answering
`none`. It is not limited to the adapter being down: the adapter's grammar (`intent_grammar`) answers most negated phrases itself
(`negation.noop`, «Хорошо, не буду.», and `asr.garbage_short` for «свет не включай»), but returns **no verdict** for «лучше не включай чайник»,
«чайник не включай пока», «ни за что не включай чайник», «никогда не включай чайник», «не вздумай включать чайник», «да не включай же чайник»
(measured with `python3 -m intent_grammar match`), and then the quasar layer runs the action. The hook also has a 250 ms budget and fails open.
What the layer can reach in this home bounds the impact: lights, the kettle plug, the ACs, the TV and projector, the TV-app and fireplace
scripts, volume and dashboard settings by voice; no locks, alarms, garage doors or covers exist. The worst direct result is the kettle plug
or an AC started by a phrase that forbade it. A confirmed pending setting change (the registry's `confirm` risk) takes the same «да»
path (`executeSetting` → `confirmed: true`); that path is read from the code, the check has no dashboard registry to exercise it.

### The change

* `backend/negation.ts` (new, pure): the negators of the adapter grammar (не, ни, нельзя, никогда; plus the glued ненадо, ненужно, неа and
  the negative pronoun «никакого», «никакой»…), the scope of one clause (a negator anywhere in the clause of a word, before or after it:
  «не включай пожалуйста этот свет», «свет не включай»; «а», «но», «зато» start a new clause: «не свет, а музыку»), and one carve-out that
  stays a plain command, because the layer ran it before and the adapter has no rule for it either: a polite request to the speaker that
  opens the utterance («Алиса, не мог бы ты включить свет», «Алиса, ты не могла бы включить свет», «не можешь включить свет», «нельзя ли
  включить свет»). Statements are not requests and stay negated: «я не мог включить свет» («мог» without «бы»), «я не могла бы включить чайник», «я знаю, что ты не можешь
  включить чайник».
* `decideSentence`: a negated sentence is **no match** (`llm` tier) for everything that would act (run, confirm, ask to fill in, ask to
  repeat). Read-only decisions stay: answers, state and setting reads, math, entity questions («что не включено» is still answered).
  Per sentence, so «Включи свет на кухне. Не знаю.» still runs the first one.
* `resolveClarification`: a choice word under a negator is not the choice («не свет, а музыку», «никакого света», «не хочу сейчас
  включать свет»); a yes word next to a negation or a «нет» is not «да» («не делай», «нет, давай» cancel, the question is consumed). A
  refusal beats a slot fill («Включить или выключить чайник?» → «включить нет» runs nothing, «не семь» sets nothing), and an explicit
  negated answer to the legacy «Ты про чайник?» cancels instead of sending the phrase on to the brain (that is left to «нет, я про другое»).
* A negated phrase is not offered to the ASR corrector (a repair could only turn it into its opposite), and a dismissed or refused question
  («ничего», «хватит», «не делай») is dropped from the corrector's context at once, so a «гавай» heard after it is not read as its answer.
* What a negated phrase now does: the layer returns no answer and the turn goes on to the brain, exactly as for any phrase it does not
  understand. With a healthy adapter the grammar has answered most of them before that.

Known limits (fail safe, nothing runs): a contrast without «а» («свет, не музыку»: the commas are gone after tokenizing) leaves both words
negated and cancels the question; say «свет, а не музыку».

Not covered, on purpose: «включи не чайник, а свет» and «не включай свет в гостиной, а включи на кухне» are **not executed** (the
negator makes the whole sentence unsafe to read with a verb-and-noun grammar; the adapter grammar does the same); the owner says it again
without the correction. A purpose clause («включи свет, чтобы не было темно») is not exempt either: it ran before, but an exemption that
keeps «чтобы не включать чайник» and «главное, чтобы не свет» from slipping through needs a grammar of its own; say «включи свет». Mixed polarity in an answer («нет, давай») is a «нет». «да, не вопрос» is a «нет» as well: a refusal costs one
repeated word, a wrong «да» runs an appliance.

## Finding 3: pending questions, turn memory and the corrector's context are process-global

`pendingClarification`, the turn memory (`turn-memory.ts`: «повтори», «а на кухне?»), the reprompt guard (`lastRepromptAt`) and the
corrector's last-question context (`asr-correct.ts`) were one value per process, and this home has three stations. Reproduced on the
unpatched sources: a question asked on station A («Повторить: включить чайник?», «Какую громкость поставить?») is answered and consumed
by a «да» / «семь» from station B, B's «нет» cancels A's question, «повтори» on B repeats A's answer (also the brain's), «а на кухне?» on
B continues A's last command, and A's question is sent to the corrector as the context of B's «давай».

**Verdict: exploitable, with two speakers.** A «да» (or TV chatter holding one) heard by B within 90 s of a question to A confirms A's action.
«повтори» on B discloses what A heard or was told, the brain's answers included. The context leak is certain (it is in the request body the
check reads); that it turns into an action depends on the corrector (the check uses a stand-in that reads its context; the real one is the
adapter's model and was not run).

### The change

* Every one of those is now a small map keyed by the station's device id (`stationKey`: trimmed, lower case; `''` for a caller without
  one, which is the single shared bucket the corpus runner and old callers get). The turn of `matchFastIntent` carries the key in its trace
  (`AsyncLocalStorage`, as before), so the deep call sites (`executeSetting`, `repeatLast`, the correction) need no new parameters.
* Bounded: each map holds at most `MAX_STATIONS` = 32 stations (a home has a handful) and forgets the station written to longest ago
  (`rememberFor`); a client that invents device ids cannot grow them, and cannot make a station answer for another. The TTLs are as
  before: question 90 s, «повтори» 10 min, «а на кухне?» 3 min, corrector context 30 s.
* `input-handler.ts` records the brain's answers with the station of its connection (`brainTurn.station`), otherwise «повтори» after a
  brain answer would have no station to belong to.
* **Stays global, on purpose:** the persisted ambiguity memory (`AISIS_HA_AMBIGUITY_FILE`, `ha.ambiguity.json`). It maps a *phrase* to what
  the owner meant by it («включи глобус» = the light), not a station to anything; per-station learning would ask the same question three
  times and change the file. Its format and loader are untouched and old and new images read each other's file. Likewise the registry cache
  and the corrector's per-minute call budget (a cost limit, not a conversation).

## Build, check, deploy

The input of every patched file is either the active image's own file (SHA-256 pinned, and the result's too) or **another overlay's
output** (every anchor must still occur exactly once and nothing else is checked; the result's SHA-256 is printed). One block has two
known shapes: the terminator handler of `matchFastIntent` («ничего», «хватит», «отмена» clear the open question) sits one level deeper after
the `correction-hook` overlay moved it into `endOrRepeat`; exactly one of them must be present.

```
python3 prepare_overlay.py --src IMAGE_SRC --output-dir NEW_DIRECTORY [--ha-intents OTHER_OVERLAY_OUTPUT/backend/ha-intents.ts] [--input-handler FILE]
                                                   # IMAGE_SRC = docker cp aisis-quasar-prod-<active colour>:/src/. DIR (four files are read)
./run_check.sh [--ha-intents FILE [--grammar-hook FILE]]
                                                   # builds from the active colour in a scratch dir, then in scratch containers of its image, no network:
                                                   # check_ha_intents_safety.mts WITH the overlay (ИТОГ 170/170), the patched input-handler.ts
                                                   # loads, and the same check WITHOUT it, which must fail (ИТОГ 86/170: the old behaviour)
python3 -m unittest discover -s .                  # patch applier (CI); AISIS_QUASAR_BASELINE_DIR=<IMAGE_SRC> adds the real-file hash test
```

The output (`NEW_DIRECTORY`, a `deploy.sh --overlay SRC_DIR`) holds exactly five files: `backend/ha-intents.ts`, `backend/turn-memory.ts`,
`backend/asr-correct.ts`, `backend/negation.ts` (new) and `routers/uniproxy/input-handler.ts`. It is byte-identical to the same files of
the aisis-quasar fork commit that carries the patch (checked, hashes below).

**Order in the combined build: this overlay goes LAST.** The overlays that also patch `backend/ha-intents.ts` (`correction-hook`,
`ha-verify-wording`, `ac-defer-to-adapter`) pin their inputs to the image's file or to one another's output and cannot take this one's.
Build their combined `ha-intents.ts` first (their documented chain), then run this script with `--ha-intents <that file>` and let its five
files **replace** the combined directory's `ha-intents.ts` (this output already contains theirs) and add the other four. Checked against
every shape those overlays produce (`run_check.sh --ha-intents …`, with the `correction-hook` overlay's `grammar-hook.ts` where its
`ha-intents.ts` imports it): the image's file, `correction-hook`, `correction-hook` + `ha-verify-wording`, `ha-verify-wording`,
`ac-defer-to-adapter`, and all of them together (the last one built by applying the text of the `ac-defer-to-adapter` block to the
`correction-hook` + `ha-verify-wording` output, as their pins forbid chaining them): the blocks never overlap theirs (the one shared line, the terminator handler's
`pendingClarification = null`, is the two-shape block above), and `ИТОГ 170/170` with the overlay, 86/170 or 88/170 without (the
`ac-defer-to-adapter` input already hands the AC phrases to the adapter). If another overlay patches `input-handler.ts`, pass its output
with `--input-handler`.

Deploy: part of the one combined flip, by the deploy owner. Nothing here deploys or restarts anything. `deploy.sh` needs a prebuilt
immutable image and a corpus receipt for exactly that image (`../../corpus-runner/README.md`): build the combined overlay directory,
prebuild the image, run the corpus on it, then `deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"`. The
receipt's corpus (469 rows) and dialogs (195) are the original ones; this change adds none to them and breaks none of them (the fork's
runners: 466 original corpus rows and 195 dialogs pass unchanged). The 36 new corpus rows and `run_safety.mts` live in the fork.

After the flip, `docker logs aisis-quasar-prod-<color>` after a negated phrase (try «лучше не включай чайник») shows no
`Fast intent executed: on [switch.kettle_plug_local]` line (the old image logs one) and no `Fast intent room for …` line (the layer made no
decision).

## Rollback

`deploy.sh rollback` flips back to the previous colour (the previous image). Nothing is migrated: the ambiguity file is unchanged in both
directions, and the per-station memories live in the process (a flip empties them, as it always did).

## Provenance

Image: `aisis-quasar:volume-zero-e859a37b` (running colour at the time of writing: green, image
`sha256:de0480613e23dadad99952f2e54ec86ed2f31e844841a6a1b9103859762523fe`), the same bytes as tag `aisis-volume-zero-e859a37b` of the fork.

| file | image SHA-256 | patched SHA-256 |
|---|---|---|
| `backend/ha-intents.ts` | `f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3` | `60733dc71ea252cc018f6854f58c836282ccd6ef7116e1ea7e679bcbb93d33a2` |
| `backend/turn-memory.ts` | `0c2ee7e4be189e6d5c159f23a5492459c2d4b850102b1b4690bfb64c84ef3c19` | `5db6d505165f07571a01233b26756a2ab738100f205cfd03f7b45464128597df` |
| `backend/asr-correct.ts` | `1f337435e1c4cebd6edf43480330229e128ad035afb3c80fa6664cd319afc48d` | `9eca3a1cf0975c6e7bdd37925b6aedbc8227f628ae9fed5a52ec9fe8e0d44131` |
| `routers/uniproxy/input-handler.ts` | `d01c3d5ef1a7959c4b3c866354963bf656d0e8a311ae9e613db9028703b432ba` | `495b43f99c666169aee3328babee81087210a5144d04842e7ed56298c2dd0f5f` |
| `backend/negation.ts` (new) | none | `32c222c1f1bc2ebcff90f1439e490ce139b47c88a0b81116a2e89809e9928ac2` |

After a later deploy changes one of the files its hash changes and `prepare_overlay.py` refuses (pristine input) or checks the anchors
(another overlay's output) until the patch is rebased: it must never be applied blind.

## Tests

`python3 -m unittest discover -s services/yandex-station/overlays/ha-intents-safety` (patch applier, run by CI); `check_ha_intents_safety.mts`
(inside the image, `run_check.sh`: 170 checks, the fork's `src/backend/corpus/run_safety.mts` with two edits, listed in its header).
