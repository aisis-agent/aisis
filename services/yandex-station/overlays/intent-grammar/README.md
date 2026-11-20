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
   250 ms timeout and **fails open**: timeout, non-200, bad JSON or an unknown route (own properties of the route table
   only) return `null` and a warn line with the elapsed ms. `AISIS_ADAPTER_TOKEN`, when the container has it, goes as
   `Authorization: Bearer` (the production adapter has none configured, like the other quasar helpers
   `asr-correct.ts`, `music_play`). `GRAMMAR_HOOK=on|shadow|off` (default `on`; any other value is `off`): `shadow`
   sends `{text, dialogActive, shadow: true}` without awaiting it, so the adapter classifies and executes nothing and the
   turn never waits, and only logs the answer; `off` makes no call.
2. PATCH `backend/ha-intents.ts` (two exact blocks): the import, and in `matchFastIntentCore` a call after the
   «A question is pending: this turn is the answer» block (a pending yes/no or slot answer is still handled first;
   `end-dialog` and `repeat` stay before it) AND after the «а на кухне?» follow-up (it needs the previous device command,
   which the grammar never sees), right before the arithmetic and the catalog. The verdict is mapped there, next to the
   code it reuses; a null result continues through the old tiers untouched.
3. PATCH `backend/asr-correct.ts`: `clarify` leaves `ELIGIBLE_TIERS` (and the header comment says why).

Adapter response contract:

```
request: {"text": string, "dialogActive": boolean, "shadow"?: true}
{"route": "reply"|"silent"|"volume"|"dialog_end"|"none", "text": string|null, "listen": boolean,
 "volume": null | {"target"?: 0..1, "steps"?: signed int, "deltaLevels"?: signed int}, "intent": string|null, "ms": number}
```

| route        | result in quasar                                                                                  |
|--------------|---------------------------------------------------------------------------------------------------|
| `reply`      | `{kind:'reply', quasarDirectives: [], text}`, `listen: true` only when the adapter says so; empty text is ignored |
| `silent`     | the existing `DIALOG_END` (reply with `text: null`)                                               |
| `dialog_end` | the pending question is cleared, then `DIALOG_END`                                                |
| `volume`     | the existing `volumeResult`: `target` is a fraction of the scale; `steps` are multiples of `volumeStepPct()` (read like `executeResolved` does); `deltaLevels` becomes `target = clamp((speakerLevel + deltaLevels) / 10)` when the speaker reported its level, else `steps = sign(deltaLevels)`. The turn is recorded, but «повтори» repeats the level it reached (a target), not the change in levels: the recorded command cannot carry a delta (known gap, ticket) |
| `none`, unknown | `null`: the phrase goes through the old tiers                                                  |

## Build, check, deploy

Run the full checks in CI or RunPod. Prebuild the reviewed overlay as one
immutable candidate, export its exact closure, execute all original corpus and
dialog cases with the maintained layer checks through native RunPod, and collect the real evidence using the
[corpus protocol](../../corpus-runner/README.md). Set `VERIFIED_IMAGE_ID` and
`CORPUS_RECEIPT` from that execution; an older receipt cannot authorize a newly
built image. Run the deployment command from the clean isolated release checkout.

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
  -v "$OVERLAY/backend/grammar-hook.ts:/src/backend/grammar-hook.ts:ro" "$IMAGE" /src/backend/check_grammar_hook.mts   # ИТОГ 34/34
# Only after the prebuilt candidate and its offloaded evidence are verified:
../../bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"
```

Overlay preparation and checks do not deploy or restart anything; the final
deployment command belongs to the deploy owner. `check_grammar_hook.mts` serves a stub adapter and a
stub Home Assistant on a loopback port inside the throwaway container (no network): reply, silent, dialog_end, volume
(target, steps going up, deltaLevels with and without the speaker level), none, timeout (the old path still answers), HTTP 500,
bad JSON, an unknown and a prototype route name, `GRAMMAR_HOOK=shadow` (not awaited, `shadow: true`, never acts) and every
`off` spelling (`off`, `OFF`, `0`, `false`, `disabled`, empty: no call), the bearer header with and without a token, and that a pending
clarification («Громкость.» → «семь») is answered before the hook is called (`ИТОГ 34/34`). The adapter must serve `/intent/decide` before the flip: without it the hook fails
open after 250 ms on every turn (correct, but slower than before).

**Rollout order.** The hook is `on` by default (`GRAMMAR_HOOK` unset = `on`), so the first flip already lets the grammar act on live turns.
Safe order: (1) the adapter serves `/intent/decide` (curl it with `"shadow": true`), (2) deploy the image, (3) watch the first turns (adapter
journal, the quasar `Grammar ...` log lines). To observe first, set `GRAMMAR_HOOK=shadow` in `runtime.env` BEFORE the deploy (the container reads it
once at creation) and switch to `on` with a second recreate of the active color. The 2026-11-17 deploy went straight to `on`; rollback is
`GRAMMAR_HOOK=off` plus a recreate (see Rollback). Image tags use dashes: docker tags cannot contain `+`.

`compose.bluegreen.yaml` passes `GRAMMAR_HOOK` to the prod colors (`${GRAMMAR_HOOK-on}` from `runtime.env`: only an UNSET variable means `on`, an empty
`GRAMMAR_HOOK=` is `off`) and sets `off` for stage: `/intent/decide` executes timers on the adapter it reaches, and stage containers share the host
network with the real adapter, so a stage grammar call could cancel real timers.
The deployment corpus and dialog cases execute only through native RunPod: stage mode is `off`,
and production mode is the freshly recorded `runtime.env` value. The closed adapter endpoints
prevent calls to the real adapter. The [corpus protocol](../../corpus-runner/README.md) checks the
three reminder contracts against the isolated maintained grammar before their strict fast-layer
fallback; all original cases are retained. At stage and idle-production gates, `deploy.sh` verifies
the actual native receipt and the running image's source, dependencies, runtime and mode; it does
not execute those suites in service containers. The dashboard's separate corpus workflow retains
its own runner and container-mode behavior and is not covered by this deployment receipt.
Nothing in these deployment checks calls a real `/intent/decide`: the hook's code is checked by
`check_grammar_hook.mts` against a stub; the real endpoint is the live smoke of the adapter.
A container reads its environment once: changing `GRAMMAR_HOOK` means recreating the active color
(a deploy with the new value in `runtime.env`).

The adapter side of a clarification question («поставь таймер» → «На сколько поставить таймер?», `listen: true`): the adapter remembers the
question for a short time and joins the next phrase to it («на пять минут» → «поставь таймер на пять минут»), so the
answer is not lost although quasar holds no pending state for a grammar question.

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
# Prebuild this combined overlay, export it, run the native suites, and collect
# its own receipt before invoking the deployment command:
../../bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"
```

The in-container check passes on the combined `ha-intents.ts` too (`ИТОГ 34/34`, with the speaker-room `speaker-room.ts`
mounted next to it): the hook blocks and the speaker-room hunks do not overlap.

## Rollback

Switch the mode without a rebuild: `GRAMMAR_HOOK=off` in `runtime.env` and recreate the active color (no call at all) or `shadow` (classification-only calls, not awaited,
logged as `Grammar shadow '<s>' -> <intent> (<ms> ms)`, never acts). Full rollback is the deploy script's: redeploy the
previous image tag (`../../bluegreen/deploy.sh` rolls back on a failed smoke test by itself).

## Provenance

The baselines are `/src/backend/ha-intents.ts` and `/src/backend/asr-correct.ts` of the active image
(`aisis-quasar:bg-tv-fireplace-contract-20261113`):

| file             | baseline SHA-256                                                   | patched SHA-256                                                    |
|------------------|--------------------------------------------------------------------|--------------------------------------------------------------------|
| `ha-intents.ts`  | `245c920baafb0709493e30547c147ebc50d475513595c10c4663708478a3cca9` | `782c69c48e5b3174e86e015bab1dd0f2b076314e5490f7d12ea18e84b26b39e1` |
| `ha-intents.ts` after the speaker-room overlay | `81fac5ca320c639471382b58fbf466f701b88ccf50beda895e75a443737e405b` | `f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3` |
| `asr-correct.ts` | `6587bc9624ff8268efd59589a73197882228e7ec12c20145ede656c75e27e5c5` | `1f337435e1c4cebd6edf43480330229e128ad035afb3c80fa6664cd319afc48d` |

`grammar-hook.ts` is new (SHA-256 of the file next to the script). The quasar source is versioned in
[aisis-quasar](https://github.com/alex-mextner/aisis-quasar) (`grammar-hook.ts`, `asr-correct.ts` and the patched `ha-intents.ts` are in the tree at tag `aisis-pre-weather-20261117`); this overlay is the audited patch folded into it, so the patches apply only to the older baselines instead of copying 1,400 lines here.
After a later deploy rebuilt either file the baseline hash changes and `prepare_overlay.py` refuses until the patch is
rebased: it must never be applied blind.

## Tests

`python3 -m unittest discover -s services/yandex-station/overlays/intent-grammar` (patch applier; set
`AISIS_QUASAR_BASELINE_DIR` to a directory holding the captured files to also check the pinned hashes);
`check_grammar_hook.mts` (inside the image, last run: `ИТОГ 34/34`); adapter side:
`services/yandex-station/code/` (`intent_grammar`, `intent_route.py` tests).
