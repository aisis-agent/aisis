# The speaker says what ha-cli observed, not what it guessed

Since ticket #190 `ha-cli` verifies every service call against the device state and answers with what it saw
(`services/yandex-station/README.md`, «A command is done when the device says so»):

| ha-cli answer | meaning | header |
|---|---|---|
| 200 | the device did it | `X-Aisis-Verify: ok` |
| 502 | Home Assistant answered, the device did not do it | `X-Aisis-Verify: failed`, `X-Aisis-Verify-Reason` |
| 504 | nothing could be read, outcome unknown | `X-Aisis-Verify: unknown`, `X-Aisis-Verify-Reason` |
| 202 | Home Assistant was OBSERVED gone; the call is queued | `X-Aisis-Down: refused\|reset\|dns\|unreachable\|starting\|http_502\|http_503\|http_504` |

## Why this overlay exists

The quasar fast-intent layer (`backend/ha-intents.ts`, part of the image, not of this repository) still answered
every 202 with «Умный дом перезагружается» (the evidence is only «connection refused»; a reboot was never observed) and every
502/504 with «Не удалось выполнить команду умного дома» (for a 504 that claims a failure nobody saw).

## The change (one file, ten exact blocks, `prepare_overlay.py`)

* `HaRestartingError` (renamed `HaQueuedError`: nothing says Home Assistant restarts) carries `down` (`X-Aisis-Down`); `haRecoveryText(down)`: `starting` → «Home Assistant запускается, команду
  выполню, когда он вернётся.»; any other evidence → «Home Assistant недоступен, команду выполню, когда он вернётся.»; no header
  (an old ha-cli queued the call on a timeout) → «Не получилось проверить ответ Home Assistant, команда ждёт в очереди.» No ETA is
  spoken any more (it is still in the log line), and the word «перезагружается» is gone.
* `HaCommandError` + `haFailure(response, what)` in the five failure sites (`callService`, `callWholeDomain`, `callSetService`,
  the TV-application and fireplace `callScript`): `verify=failed` → «Команду отправил, но не вижу, чтобы устройство её выполнило.»
  (`device_unavailable` → «Home Assistant видит это устройство недоступным.»); `verify=unknown` → «Не получилось проверить ответ
  Home Assistant.»; a plain HTTP error without a verdict keeps «Не удалось выполнить команду умного дома.».
* `haServiceFetch` wraps the five service-call requests: a call that got no answer within `haFetch`'s 3 s says «Home Assistant не
  ответил вовремя, результат команды не проверен.» The same text covers the layer's own read-back after a command was sent
  (`readBackAfterCommand`, below): a read that times out THERE leaves the outcome unknown. A read that timed out before any command was
  sent (registry, «уже включено» check, climate pre-read, a state question) keeps the generic failure text: no command is in doubt there.
* `callService`, `callWholeDomain` and `callSetService` return whether ha-cli confirmed the call (`X-Aisis-Verify: ok`).
  `executeDeviceIntent` skips its own read-back when every call was confirmed: a Home Assistant that went away right after
  would only serve stale cache and the speaker would announce a failure nobody observed. Without the header (an old ha-cli) the
  layer's own read-back (`verifyExecution` / `verifySet`) runs as before, also when only some of several calls were confirmed.
* The final catch logs `queued: Home Assistant unavailable (<evidence>, eta …)` and speaks `HaCommandError.say`; the full error is
  still logged.

## What it does not cover

* Commands the adapter itself executes (`ha.light_brightness`, `ha.light_color`, `ha.climate`, `ha.climate_delta` in
  `ha_actions.py`) are spoken by the adapter, before this layer: the ticket #190 change to `ha_actions.py` gives them the same
  header-driven wording, so deploy the adapter together with ha-cli.
* ha-cli answers within its own 2.0 s budget (`CLIENT_TIMEOUT_S`) plus a 0.2 s last look, under the layer's 3 s timeout; the timeout wording above is
  for a ha-cli that does not answer at all.

## Build, check, deploy

The input is the active image's `ha-intents.ts` (SHA-256 pinned, no fuzz). Two inputs are accepted, each with its own pinned
result: the active image's file (`f4ab25c3…`), and the file the `correction-hook`
overlay produces from it (`7a70be74…`, built from `grammar-hook.ts` `f6f6021e…`). **With the correction-hook input, that overlay's
patched `backend/grammar-hook.ts` MUST ship in the same overlay directory**: its `ha-intents.ts` imports `grammarNoop` and
`grammarContextOpen` from it.

```
./run_check.sh        # builds from the active colour, runs check_ha_verify_wording.mts in a scratch container, no network: ИТОГ 51/51;
                      # then the same check without the overlay, which must fail (20/51: the old wording)
./run_check.sh --ha-intents CORRECTION_HOOK_OUT/backend/ha-intents.ts --grammar-hook CORRECTION_HOOK_OUT/backend/grammar-hook.ts
                      # the same for the second accepted input (the correction-hook variant) with its grammar-hook.ts
python3 -m unittest discover -s . -p "test_*.py"      # AISIS_QUASAR_BASELINE_DIR=<dir with ha-intents.ts> adds the real-file hash test
```

Deploy (the deploy owner; nothing here deploys or restarts anything). `deploy.sh` needs a prebuilt immutable image and a corpus
receipt (`../../corpus-runner/README.md`); follow the one-image flow of `../intent-grammar/README.md`, «Together with … (one
image, one flip)», and `../volume-zero/README.md`, «Deployment order»: build the combined overlay directory, prebuild the image,
run the corpus on it, then `deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"`. Rollback is
`deploy.sh rollback`.

```
BASE=$(mktemp -d)
docker cp aisis-quasar-prod-<active colour>:/src/. "$BASE"
OVERLAY=$(mktemp -d)/overlay
mkdir -p "$OVERLAY/backend"
# correction-hook first when it is merged: its output grammar-hook.ts goes into $OVERLAY/backend, then
python3 prepare_overlay.py --ha-intents <correction-hook ha-intents.ts, or $BASE/backend/ha-intents.ts> --output-dir "$OVERLAY.verify"
cp "$OVERLAY.verify/backend/ha-intents.ts" "$OVERLAY/backend/ha-intents.ts"
```

Order: this overlay MUST land after the ha-cli/adapter ticket #190 PR (it reads headers only that ha-cli sends): without them every
202/502/504 gets the old, vaguer fallback wording. Any other overlay that touches a different file of the image (for example one for
`routers/uniproxy/uniproxy-connection.ts`) goes into the same `$OVERLAY` unchanged.
