# Weather goes to weather-cli, not to the Home Assistant entity

Every weather answer the speaker gives comes from `weather-cli` (`weather say --now|--tomorrow|--week|--umbrella|--dress`,
Home Assistant + Open-Meteo, one decision for «now» and «umbrella»), spoken by the adapter's `voice_skills.py`.

## Why this overlay exists

The quasar fast-intent layer used to answer «какая погода», «что на улице», «сколько градусов на улице» itself. Its
last local resort before the brain, `matchEntityQuery` in `backend/entity-index.ts`, read the Home Assistant `weather.*`
entity and spoke it with its own template: «На улице 12,9 градуса, дождь, влажность 71 процент, ветер 20 километров
в час.» (dialog `cbcb90bc-…`, 2026-11-16 13:54, log line `Fast intent for 'Алиса, какая сейчас погода?'`). The turn never
reached the adapter, so `voice_skills.py` and weather-cli never ran, and the answer carried a humidity percentage that
weather-cli deliberately never speaks. The intent corpus already expects these phrases to be `llm` turns
(`weather_time`); they only came out as answers because a weather entity exists on the real instance.

## The change

Two anchors of `backend/entity-index.ts`: the weather branch of `matchEntityQuery`, and the entry-point banner above it where a
generated table is inserted. For a weather word (`погод`) or an outdoor question («на улице», «за окном») the branch returns
"none" when the whole utterance (lead-in words like «алиса», «скажи» removed, normalized) is in the **handoff table**
`diverted_phrases.json`. The turn then continues to the brain tier, where the adapter answers it with weather-cli before the
brain is asked. Anything not in the table keeps its old entity answer: outdoor humidity and pressure («какая влажность на
улице»), mixed questions («какая погода и влажность на улице»), unusual phrasings. The guarantee is exactly that: **a
question in the table is answered by weather-cli; a question outside it keeps its previous route**, so a phrase the adapter
cannot parse is never taken away from the entity answer (review of the earlier broad divert found five such phrases:
воздуха, Цельсия, балкон, «и», humidity mixes).

To admit a phrase: add its normalized form to `phrases` (sorted, lower case, ё folded, no punctuation, no lead-in words),
run the tests below (the adapter must answer it with a mode or the qualifier hint), update `PATCHED_SHA256` in
`prepare_overlay.py` (the table is embedded in the patched file), run `check_weather_routing.mts`.

Known limit: «какой ветер на улице» is answered by `weather say --now`, which speaks wind only when it matters
(a category, never a speed); the old entity template always read the speed out. Time of day («вечером») or «сегодня и завтра»
in an otherwise plain weather question gets a fixed hint instead («Так не умею…»). The home city (Белград) is vocabulary; any other
city («погода в Париже») is not and goes to the brain, which refuses.

## Build, check, deploy

Order matters: **adapter first, overlay second.** The overlay hands exact phrases to the adapter, and the running
`aisis-station-adapter` keeps the `voice_skills.py` it imported at start. Deploy this branch's `voice_skills.py` and
restart the unit (`systemctl --user restart aisis-station-adapter.service`), then ask the running adapter a phrase only the
new parser accepts (`Какая погода и сколько градусов на улице`, expect mode `now`) before the cutover below. An old adapter
answers `None` to the newly diverted phrases.

```
OVERLAY=$(mktemp -d)/overlay
docker cp aisis-quasar-prod-<active color>:/src/backend/entity-index.ts /tmp/entity-index.baseline.ts
python3 prepare_overlay.py --baseline /tmp/entity-index.baseline.ts --output-dir "$OVERLAY"   # SHA-256 checked, no fuzz
docker run --rm --entrypoint /node_modules/.bin/tsx \
  -v "$PWD/check_weather_routing.mts:/src/backend/check_weather_routing.mts:ro" \
  -v "$PWD/diverted_phrases.json:/src/backend/diverted_phrases.json:ro" \
  -v "$OVERLAY/backend/entity-index.ts:/src/backend/entity-index.ts:ro" <active image> /src/backend/check_weather_routing.mts   # ИТОГ 1372/1372
# After prebuilding the immutable overlay image and collecting its own native
# offloaded corpus evidence, from the clean isolated release checkout:
../../bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"
```

The same `docker run` without the entity-index mount shows the old behaviour (567/1372).
Full checks run in CI or RunPod. Follow the [corpus protocol](../../corpus-runner/README.md):
prebuild one immutable candidate, export its exact closure, execute the unchanged
intent corpus and dialogs through native RunPod, and collect the real receipt.
Set `VERIFIED_IMAGE_ID` and `CORPUS_RECEIPT` from that execution. `deploy.sh`
verifies the evidence and actual candidate on stage and the idle color before it
flips, and retains rollback on a failed smoke test; it runs no local full suite.

## Provenance

The baseline is `/src/backend/entity-index.ts` of the active image (`aisis-quasar:bg-tv-fireplace-contract-20261113`),
SHA-256 `73474b5c8e60daa38f62f700cdbe787cd1badb77690b3733a7c93315ba41235f`; the patched file is
`c24efb900b0c9ad4ad74aaf30723add07c55cd639799882e2430117f96308d10` (it includes the embedded table). The quasar overlay source is not versioned in this
repository (it lives in the images), so the patch is applied to the captured file instead of copying 880 lines here.
After a later deploy rebuilt `entity-index.ts` the baseline hash changes and `prepare_overlay.py` refuses until the patch
is rebased: it must never be applied blind.

## Tests

`python3 -m unittest discover -s services/yandex-station/overlays/weather-via-cli` (patch applier);
`check_weather_routing.mts` (inside the image, a weather entity present); adapter side:
`services/yandex-station/code/test_weather_skills.py` (stdlib only, run by CI). `diverted_phrases.json` is the one list
of questions the overlay hands over: the `.mts` proves the quasar returns "none" for each, and `test_weather_skills.py`
proves the adapter answers each (with any lead-in), so a diverted phrase can never be left without an answer behind it.
