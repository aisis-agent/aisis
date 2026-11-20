# Air conditioner phrases go to the adapter, not to the container's own handlers

Incident 2026-11-20 11:18 (owner clips «Кондиционер на втором этаже» → «Включить.» and «Включи кондиционер на втором этаже на
обогрев»): the unit upstairs was started in **cool**, the speaker said «Умный дом перезагружается», the owner finished it by hand.

## Why this overlay exists

The quasar fast-intent layer (`backend/ha-intents.ts`, `decideSentence`) answers AC phrases before the adapter ever sees them, and
gets them wrong:

- every «включи кондиционер» starts the unit with `set_hvac_mode cool` (`callService`: `turnOn ? 'cool' : 'off'`), a set-point on
  an off unit starts it in cool (`callSetService`); «на обогрев» is not understood at all (no `heat` anywhere in the layer);
- a bare «кондиционер на втором этаже» is answered with the fixed question «Включить или выключить кондиционер наверху?», and the
  bare «Включить.» that follows executes it;
- a phrase without a floor acts on the AC of the speaker's room, else on `kondei_1` (floor 1): the Studio speaker has no AC, so
  «включи кондиционер» reaches the first-floor unit (reproduced by `check_ac_defer.mts` on the unpatched image);
- a call that outlives the container's 2 s timeout is queued by ha-cli and reported as «перезагружается» (fixed in the ha-cli
  verification work, #190).

The adapter has the right behaviour already: `ha.climate` / `ha.climate_delta` (`intent_grammar/grammar/ru.yaml` `climate.*`,
`ha_actions.py`): the floor from the phrase, the mode from the phrase («обогрев» = heat), a warm-up mode for a set-point on an
off unit chosen by the setpoint, never another floor, one verified call. `python3 -m intent_grammar match "Включи кондиционер на
втором этаже на обогрев."` gives `ha.climate {room: upstairs, mode: heat}`.

## The change

One block of `backend/ha-intents.ts` (`decideFastIntent`): a single-sentence turn that would turn **on** or **set** a climate
entity becomes the `llm` tier, so the turn continues to the brain tier, where the adapter's grammar route runs it, only when the
adapter grammar parses the WHOLE phrase. The adapter's `climate.set` rule matches whole utterances, and anything else holding an AC
word ends in «Такого устройства я не знаю» with no call to the unit, so the patched layer hands over exactly the shapes of that
rule: `[verb] кондиционер|кондей [в|на] [room] [на|в] [mode] [temperature [градусов]] [пожалуйста]` (a turn-on verb with any of
the optional parts; a set verb or none with a mode or a temperature), with the room, mode and number words the grammar knows and
a temperature of 16 to 30 (the grammar's range: «на 31» is answered «Такого устройства я не знаю» there, so it stays with the
container) (`adapterOwnsAcPhrase`). Everything else keeps the quasar path (known remainders): extra words («включи кондиционер сейчас»,
«… в режиме обогрева»), a second clause («… и включи свет», «… потом включи свет»: the adapter declines a compound holding a clause
it does not own), a room before the AC word («включи наверху кондиционер»), and multi-sentence turns. The adapter drops its lexicon's
fillers anywhere («пожалуйста», «плиз», «вот», «же», …) and its lead-in and wake words at the edges before matching, so the patched
layer drops the same words before it compares («включи, пожалуйста, кондиционер на втором этаже на обогрев» is handed over);
a unit test pins both word lists and the verb lists to the compiled grammar's lexicon.

`phrases.json` lists the handed-over and the kept phrases; `check_ac_defer.mts` runs them through the real layer in the image, and
`test_prepare_overlay.py` proves in CI that every handed-over phrase matches the patched shape **and** is executed by the adapter's
compiled grammar (`climate.set`, execute tier), and that no kept phrase matches a shape.

Only the words `кондиционер` and `кондей`: they are the ones the adapter grammar knows **and** the ones `music-parse.ts` keeps out
of music searches (`DEVICE_STEMS`). «сплит», «кондер», «климат» or a misheard «кондицинер» handed over would be taken for a music
title, so they keep the old quasar behaviour.

Deliberately **not** handed over (kept, with their known remainders):

- turning the AC **off** («выключи кондиционер наверху»): the adapter grammar has no AC-off rule (`intent_grammar match` gives
  `refuse.unknown_device`), so quasar keeps executing it (`set_hvac_mode off`, correct);
- the cut-off reprompt («включи кондиционер на»): quasar's own guard still asks again;
- the bare «кондиционер на втором этаже» question and its «Включить.» answer: the adapter has no two-turn clarification for it, so
  quasar keeps the question; its answer still starts the unit in cool. With the first-word loss fixed (wake-window trim) the
  owner's phrase arrives whole and is handed over;
- a middle-band ASR correction (`correctionFor`, mode `confirm`) that turns a garbled phrase into one of the handed-over AC commands:
  that path accepts only an `execute` decision, so the corrected phrase is no longer offered back for confirmation; the original
  decision stands and the owner repeats the command, which is then handed over whole (a high-confidence correction re-runs the
  corrected phrase; when the garbled phrase itself was a brain-tier turn, the corrected text is handed on);
- state answers, lights, volume, music, and any phrase without those two words.

Follow-ups on the adapter side that would let the overlay widen: an AC-off rule in the grammar/executor and a bare-AC question
with a remembered floor.

HARD PREREQUISITES on the adapter side (deploy and restart the adapter first; this overlay must NOT go live before them): `ha-verify`
(#190: verified calls, no false «перезагружается») and the AC turn-on rule (#195): «включи кондиционер [на втором этаже]» without a
mode must turn the unit on. The committed grammar alone answers «Не понял, что сделать» to that phrase without any HA call.

## Build, check, deploy

```
docker cp aisis-quasar-prod-<active color>:/src/backend/ha-intents.ts /tmp/ha-intents.baseline.ts
python3 prepare_overlay.py --baseline /tmp/ha-intents.baseline.ts --output-dir "$(mktemp -d)/overlay"   # SHA-256 checked, no fuzz
./run_check.sh [CONTAINER]                  # patched file in a scratch container of the active image, mocked HA: ИТОГ 52/52
./run_check.sh [CONTAINER] --baseline-only  # the same check on the UNPATCHED file: ИТОГ 25/52 (that is the incident)
python3 -m unittest discover -s . -p "test_*.py"
```

Deploy like the other overlays: prebuild one immutable image with `--overlay $OVERLAY`, collect its corpus receipt, then
`../../bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"`. Corpus caveat: the maintained
intent corpus expects a quasar `execute` for AC phrases; the deferred ones become `llm` turns, and `offloaded_corpus.py` pins the
original fixture hash, so the corpus contract has to learn «handed to the adapter» before this image can get an accepted receipt.
Stacks with `ha-verify-wording`, `uniproxy-close-log` and the correction hook: they touch other files; this one only
`backend/ha-intents.ts` (rebase `BEFORE` if another overlay edits the same lines; the baseline SHA-256 refuses a stale base).

## Provenance

Baseline: `/src/backend/ha-intents.ts` of `aisis-quasar-prod-green` on 2026-11-20 (image id
`sha256:de0480613e23dadad99952f2e54ec86ed2f31e844841a6a1b9103859762523fe`), SHA-256
`f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3`; patched file
`de2ed07891e34ca5ceec68eca55c0dea1fb415399884926ec7545f10ccb56f92`.
