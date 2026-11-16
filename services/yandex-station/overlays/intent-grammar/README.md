# The speaker asks the intent grammar first, not the language model

Most addressed phrases of the speaker are decided by the adapter's declarative grammar
(`services/yandex-station/code/intent_grammar/`, `grammar/ru.yaml`) without a language model, instantly. This overlay
makes the quasar fast-intent layer ask it first and act on the answer.

## Why this overlay exists

`docs/research/intent-coverage-audit-2026-11.md`:

- §4.3: an incomplete-slot phrase («Громкость.») is classified `clarify`, and the `clarify` tier was eligible for the
  Haiku ASR corrector (`backend/asr-correct.ts`). The speaker waited ~0.96 s for a model call that times out
  (850 ms), only to ask «Какую громкость поставить?» afterwards.
- §3.1 / §4.5: «который час», negations («не включай свет»), hallucinated phrases and wake-only turns («Алиса.»)
  reached the brain or ended in a Home Assistant error: no local layer owned them.

## The change (three files in the image)

1. NEW `backend/grammar-hook.ts`: `grammarRoute(text, { dialogActive, speakerLevel }, fetchImpl = fetch)` POSTs
   `{text, dialogActive}` to `AISIS_ADAPTER_INTENT_URL` (default `http://127.0.0.1:31117/intent/decide`) with a hard
   250 ms timeout and **fails open**: timeout, non-200, bad JSON or an unknown route return `null` and a warn line with
   the elapsed ms. `GRAMMAR_HOOK=on|shadow|off` (default `on`): `shadow` calls and logs, never acts; `off` makes no call.
2. PATCH `backend/ha-intents.ts` (two exact blocks): the import, and in `matchFastIntentCore` a call right after the
   «A question is pending: this turn is the answer» block (a pending yes/no or slot answer is still handled first;
   `end-dialog` and `repeat` stay before it) and before the «а на кухне?» follow-up. The verdict is mapped there, next
   to the code it reuses; a null result continues through the old tiers untouched.
3. PATCH `backend/asr-correct.ts`: `clarify` leaves `ELIGIBLE_TIERS` (and the header comment says why).

Adapter response contract:

```
{"route": "reply"|"silent"|"volume"|"dialog_end"|"none", "text": string|null, "listen": boolean,
 "volume": null | {"target"?: 0..1, "steps"?: signed int, "deltaLevels"?: signed int}, "intent": string|null, "ms": number}
```

| route        | result in quasar                                                                                  |
|--------------|---------------------------------------------------------------------------------------------------|
| `reply`      | `{kind:'reply', quasarDirectives: [], text}`, `listen: true` only when the adapter says so; empty text is ignored |
| `silent`     | the existing `DIALOG_END` (reply with `text: null`)                                               |
| `dialog_end` | the pending question is cleared, then `DIALOG_END`                                                |
| `volume`     | the existing `volumeResult`: `target` is a fraction of the scale; `steps` are multiples of `volumeStepPct()` (read like `executeResolved` does); `deltaLevels` becomes `target = clamp((speakerLevel + deltaLevels) / 10)` when the speaker reported its level, else `steps = sign(deltaLevels)`. The turn is recorded so «повтори» repeats it |
| `none`, unknown | `null`: the phrase goes through the old tiers                                                  |

## Build, check, deploy

```
OVERLAY=$(mktemp -d)/overlay
IMAGE=$(docker inspect --format '{{.Config.Image}}' aisis-quasar-prod-<active color>)
mkdir -p /tmp/q && docker cp aisis-quasar-prod-<active color>:/src/backend/ha-intents.ts /tmp/q/ha-intents.ts
docker cp aisis-quasar-prod-<active color>:/src/backend/asr-correct.ts /tmp/q/asr-correct.ts
python3 prepare_overlay.py --ha-intents /tmp/q/ha-intents.ts --asr-correct /tmp/q/asr-correct.ts --output-dir "$OVERLAY"   # SHA-256 checked, no fuzz
docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
  -v "$PWD/check_grammar_hook.mts:/src/backend/check_grammar_hook.mts:ro" \
  -v "$OVERLAY/backend/ha-intents.ts:/src/backend/ha-intents.ts:ro" \
  -v "$OVERLAY/backend/asr-correct.ts:/src/backend/asr-correct.ts:ro" \
  -v "$OVERLAY/backend/grammar-hook.ts:/src/backend/grammar-hook.ts:ro" "$IMAGE" /src/backend/check_grammar_hook.mts   # ИТОГ 24/24
../../bluegreen/deploy.sh deploy --overlay "$OVERLAY" --tag intent-grammar      # base = the active color's image
```

The owner runs the deploy; nothing here deploys or restarts anything. `check_grammar_hook.mts` serves a stub adapter and a
stub Home Assistant on a loopback port inside the throwaway container (no network): reply, silent, dialog_end, volume
(target, steps, deltaLevels with and without the speaker level), none, timeout (the old path still answers), HTTP 500,
bad JSON, `GRAMMAR_HOOK=shadow|off`, and that a pending clarification («Громкость.» → «семь») is answered before the
hook is called. `deploy.sh` runs the intent corpus and dialogs on stage and on the idle color before it flips, and
rolls back on a failed smoke test. The adapter must serve `/intent/decide` before the flip: without it the hook fails
open after 250 ms on every turn (correct, but slower than before).

### Together with the weather and speaker-room overlays (one image, one flip)

`weather-via-cli` patches `backend/entity-index.ts`; `speaker-room` patches `backend/ha-intents.ts` (other blocks than this
overlay), `routers/uniproxy/input-handler.ts` and `uniproxy-connection.ts` and adds two files; this overlay patches
`ha-intents.ts`, `asr-correct.ts` and adds `grammar-hook.ts`. `deploy.sh --overlay` takes ONE directory (it maps to `/src`)
and only one blue/green flip may run at a time, so the final image must be built from all merged overlays at once. Only
`ha-intents.ts` is shared: build the speaker-room overlay first and give its `backend/ha-intents.ts` to this script
(accepted as a second known input; its result has its own pinned SHA-256), then merge the weather file and the new files:

```
BASE=$(mktemp -d)                                   # the active image's sources
docker cp aisis-quasar-prod-<active color>:/src/. "$BASE"
OVERLAY=$(mktemp -d)/overlay
python3 ../speaker-room/prepare_overlay.py --baseline "$BASE" --output-dir "$OVERLAY"             # speaker-room: 3 patched + 2 new files
python3 ../weather-via-cli/prepare_overlay.py --baseline "$BASE/backend/entity-index.ts" --output-dir "$OVERLAY.weather"
python3 prepare_overlay.py --ha-intents "$OVERLAY/backend/ha-intents.ts" --asr-correct "$BASE/backend/asr-correct.ts" --output-dir "$OVERLAY.intent"
cp "$OVERLAY.weather/backend/entity-index.ts" "$OVERLAY.intent/backend/asr-correct.ts" "$OVERLAY.intent/backend/grammar-hook.ts" "$OVERLAY/backend/"
cp "$OVERLAY.intent/backend/ha-intents.ts" "$OVERLAY/backend/ha-intents.ts"                      # speaker-room + grammar hook
../../bluegreen/deploy.sh deploy --overlay "$OVERLAY" --tag intent-grammar+speaker-room+weather
```

The in-container check passes on the combined `ha-intents.ts` too (`ИТОГ 24/24`, with the speaker-room `speaker-room.ts`
mounted next to it): the hook blocks and the speaker-room hunks do not overlap.

## Rollback

Switch the mode without a rebuild: `GRAMMAR_HOOK=off` in the quasar environment (no call at all) or `shadow` (calls and
logs `Grammar shadow '<s>' -> <intent> (<route>, <ms> ms)`, never acts). Full rollback is the deploy script's: redeploy the
previous image tag (`../../bluegreen/deploy.sh` rolls back on a failed smoke test by itself).

## Provenance

The baselines are `/src/backend/ha-intents.ts` and `/src/backend/asr-correct.ts` of the active image
(`aisis-quasar:bg-tv-fireplace-contract-20261113`):

| file             | baseline SHA-256                                                   | patched SHA-256                                                    |
|------------------|--------------------------------------------------------------------|--------------------------------------------------------------------|
| `ha-intents.ts`  | `245c920baafb0709493e30547c147ebc50d475513595c10c4663708478a3cca9` | `09ab55d9adb150f9584b37520ff36297ce9f89432dfae15175a4aa149d112353` |
| `ha-intents.ts` after the speaker-room overlay | `81fac5ca320c639471382b58fbf466f701b88ccf50beda895e75a443737e405b` | `4f03a41d98ff9c32817d2c5daa8d9a29eae4b6004847e86e2409826a2d59e1c9` |
| `asr-correct.ts` | `6587bc9624ff8268efd59589a73197882228e7ec12c20145ede656c75e27e5c5` | `1f337435e1c4cebd6edf43480330229e128ad035afb3c80fa6664cd319afc48d` |

`grammar-hook.ts` is new (SHA-256 of the file next to the script). The quasar overlay source is not versioned in this
repository (it lives in the images), so the patches are applied to the captured files instead of copying 1,400 lines here.
After a later deploy rebuilt either file the baseline hash changes and `prepare_overlay.py` refuses until the patch is
rebased: it must never be applied blind.

## Tests

`python3 -m unittest discover -s services/yandex-station/overlays/intent-grammar` (patch applier; set
`AISIS_QUASAR_BASELINE_DIR` to a directory holding the captured files to also check the pinned hashes);
`check_grammar_hook.mts` (inside the image, last run: `ИТОГ 24/24`); adapter side:
`services/yandex-station/code/` (`intent_grammar`, `intent_route.py` tests).
