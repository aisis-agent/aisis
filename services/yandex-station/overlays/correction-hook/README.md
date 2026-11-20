# The speaker asks back instead of «Свет в студии уже включен.»

The correction engine of the adapter (ticket #182) needs three things of the quasar fast-intent layer, which cannot be done
from the adapter's side: it must know WHICH station spoke, it must be able to ask the adapter before it answers a no-op, and
it must run a phrase the adapter repaired as if it had been spoken. This overlay is those changes. It is prepared and
stage-checked; **it is not deployed** (the deploy owner flips it).

The case: the owner said «Выключи свет», the on-device spotter heard «включи свет», the light was on, quasar answered
«Свет в студии уже включен.» and the owner had no way to correct it.

## The change (two files in the image)

The ACTIVE image already carries the speaker-room, weather-via-cli and intent-grammar overlays, so both inputs are the files
THEY produced (`/src/backend/ha-intents.ts`, `/src/backend/grammar-hook.ts` of the active color); this overlay patches them, not
the original sources. Nothing else changes.

1. PATCH `backend/grammar-hook.ts` (nine exact blocks)
   - the request body gains `deviceId` (the station, when the caller has one) and `rerun: true` (announces that this caller can run a
     `rerun` route); `GRAMMAR_HOOK=shadow` bodies are untouched (`{dialogActive, shadow: true, text}`, never `rerun`).
   - route `rerun` (own-property check like the others) carries `text`; a `rerun` without non-empty text is invalid -> `null`, and a
     `rerun` is also invalid in answer to a request that did not announce `rerun: true` (the re-run below asks without it: no loop).
   - `context: true` on any verdict (new boolean field of the route JSON, see below).
   - NEW exports `grammarNoop(text, {deviceId, dialogActive}, {desired, label, targets})` (a probe: `/intent/decide` with
     `{text, dialogActive, deviceId, rerun: true, noop}`; only a `reply` is returned, `GRAMMAR_HOOK=on` only, the same 250 ms,
     fail-open) and `grammarContextOpen(deviceId)`.
2. PATCH `backend/ha-intents.ts` (eight exact blocks)
   - the verdict `rerun` is executed with `matchFastIntent(grammar.text, { ...options, noCorrect: true })` (one repair per utterance,
     the re-run's own hook call does not announce `rerun`, and the re-run never probes a no-op); a re-run the fast layer cannot
     decide (`null`) continues through the old path with the original phrase.
   - `executeDeviceIntent`: the «уже включен» branch (single target, on/off, not whole-domain, i.e. the existing condition) asks the
     adapter once before returning that text (`askAboutNoop`): a `reply` verdict (with its `listen`) is returned instead;
     `none`, timeout, HTTP error, bad JSON, any other route: `alreadyText` exactly as today. The phrase and station reach that function
     through the turn's trace (`TurnTrace.hook`, set by `matchFastIntentCore`): no signature changed. A «повтори» of a command and a
     re-run never probe.
   - while the adapter's repair context is open for the station, the end-of-dialog and «повтори» handlers wait for the hook (next section).

### Adapter response contract (additions)

```
request: {"text": string, "dialogActive": boolean, "deviceId"?: string, "rerun"?: true,
          "noop"?: {"desired": "on"|"off", "label": string, "targets": [entity ids]}, "shadow"?: true}
response route kinds gain "rerun": {"route": "rerun", "text": "выключи свет", ...}   # the phrase is executed as if spoken
response field    "context": true     # the adapter holds a repair context open for this station: its next reply must reach it first
```

`context` is optional and false by default. Any verdict WITHOUT `context: true` (including `none`) closes the flag; the flag also
expires 35 s after the verdict that set it (the adapter's window is 30 s), so a context the adapter closed silently on a timeout
costs at most one extra hook call. The adapter signals it on its question («Мне послышалось…», `listen: true`) and on every reply
while the context stays open, and omits it on the verdict that closes it. The no-op probe carries `rerun: true` as well, but its
answer is only used when it is a `reply`.

Invariant of the guards below: the adapter may run a repaired phrase (`rerun`) only for an owner's «да» that was sent when no other
request of the station was in flight and no request of the station has failed within the adapter's window, and whose answer arrives
while nothing else of the station overlapped it. The state is per station (`deviceId`).

(1) Ordering. Requests are numbered when sent and an answer is applied only when no NEWER request of the station was applied before
it: an older classification's late `none` cannot close the context a newer no-op probe opened (and a late `context: true` cannot
reopen one a newer verdict closed; such a stale question is not returned to the caller either, because its context was not recorded:
it counts as a failure). A request that FAILED is ordered like an answer: it advances the same watermark.

(2) Unheard. A request that got no usable answer (timeout, HTTP error, bad JSON, invalid route; for the no-op probe also any answer
but a `reply` or a `none` without a context; a stale question) leaves the station «unheard» for 35 s, whatever the order of the
failure: the adapter may have opened a repair context whose question never reached the speaker (the no-op probe's reply came after
250 ms and quasar said «уже включен»), and an OLDER request that fails after a newer one was answered may have done the same.
Requests of an unheard station do NOT announce `rerun`, so a later «да» can never make the adapter execute that unspoken correction
through the overlay: the adapter answers «Скажите ещё раз…» for a quasar-owned action instead (its `rerun` route is only taken when
`rerun: true` was announced). An adapter-owned action it executes itself is outside this overlay. NO later answer lifts the guard
early: a client timeout does not stop the adapter's work, request numbers are not the adapter's processing order, and neither a
`none` nor a `reply` that keeps the context open says the question was spoken; only the window running out (the adapter's is 30 s)
is certain. The price is a «Скажите ещё раз…» instead of a rerun for up to 35 s after a failed request of that station.

(3) Fence around the confirmation. A request announces `rerun` only when no other request of the station is in flight at that moment
(a no-op probe's question may still be on its way, so a «да» sent before it was delivered is not an answer to it). The permission is
re-checked when the answer arrives, before a `rerun` verdict is acted on: it is refused (the verdict becomes `none`, the old path
continues with the original phrase) when a newer request of the station settled first (the answer is stale; this includes a newer
failure, e.g. a probe that timed out while the confirmation was in flight) or when another request of the station is still in flight
(the adapter may answer the confirmation before delivering that question).

Not covered by a client-side guard (an adapter protocol matter, ticket #242): a request that timed out here and is only
processed by the adapter after the window the hook assumes (the adapter would need to echo a per-station generation), and a
question the adapter returned successfully but the speaker never voiced (delivery acknowledgement).

## Which layer consumes the owner's standard reply (measured, stage run `check_correction_hook.mts`)

Order in `matchFastIntentCore` today: end-of-dialog terminators -> «повтори» -> a quasar pending question (yes/no, slot) -> room
follow-up «а на кухне?» -> **hook** -> arithmetic, catalog, brain. Each word was sent with `dialogActive: true` after the no-op
question was asked and after a normal turn; «контекст закрыт» is how it is today:

| reply | context closed (today) | context open (this overlay) |
|---|---|---|
| «да», «ага», «угу», «конечно», «именно», «верно», «точно», «так», «давай» | hook | hook |
| «нет», «неа» | **quasar** terminator `no`: silent `DIALOG_END`, pending question cleared | hook |
| «отмена», «отмени», «забудь», «ладно», «все», «ничего», «не надо» | **quasar** terminator `dismiss`: silent end | hook |
| «хватит», «стоп» | **quasar** terminator `stop`: silent end (and, when `dialogActive` is false, stops the music) | hook |
| «спасибо», «пока» | **quasar** terminator `thanks`: silent end | hook |
| «повтори», «ещё раз» | **quasar** repeat handler: says the last answer again or runs the last command again | hook |
| «выключи», «включи», «выключи свет», «не то», «не так», «неверно» | hook | hook |
| any word while a quasar question is pending («Громкость?» -> ...) | the pending answer is consumed by quasar first (slot fill, or its own yes/no) | same: a quasar pending question still wins |

(`n=10` words x 2 states in the check for the rows above; the other words of the rows are the same code paths: `end-dialog.ts`
PHRASES and `turn-memory.ts` repeat patterns, checked with `parseEndDialog` / `parseRepeat` in the same image.)

Consequence without the deferral: a «нет» or «отмена» to the adapter's question never reaches the adapter (the context stays open
until its own 30 s timeout, so a later «да» in an unrelated chat could be taken as the answer), and «повтори» would re-run the
mis-heard command or say a stale answer. With the overlay, while `context: true` is the latest verdict for the station, the
terminator and repeat handlers run AFTER the hook and only when the hook did not take the turn (`none`/failure), so an adapter that
answers `none` gets exactly the old behaviour. The deferral is per station (`deviceId`), only in `GRAMMAR_HOOK=on`.

## Build, check, deploy (do not run before the preconditions)

Preconditions, in this order:

1. The adapter PRs of #182 (gateway layer, correction FSM and `context: true`) are merged and the adapter units restarted: the
   adapter must serve `/intent/decide` with the new request fields (`deviceId`, `rerun`, `noop`) and the `rerun`/`context` answers.
   An old adapter ignores unknown request fields and never answers `rerun`; the overlay then only adds one fail-open call before
   each «уже включен» (≤ 250 ms, only on a no-op).
2. `deploy.sh status` shows the active color you are building from (below it is read from `bluegreen/state.env`).

```
cd services/yandex-station/overlays/correction-hook
ACTIVE=$(sed -n 's/^PROD_ACTIVE=//p' ~/.local/share/aisis/bluegreen-state/state.env)     # blue | green
IMAGE=$(docker inspect --format '{{.Config.Image}}' aisis-quasar-prod-$ACTIVE)
BASE=$(mktemp -d)
docker cp aisis-quasar-prod-$ACTIVE:/src/backend/ha-intents.ts "$BASE/ha-intents.ts"
docker cp aisis-quasar-prod-$ACTIVE:/src/backend/grammar-hook.ts "$BASE/grammar-hook.ts"
OVERLAY=$(mktemp -d)/overlay
python3 prepare_overlay.py --ha-intents "$BASE/ha-intents.ts" --grammar-hook "$BASE/grammar-hook.ts" --output-dir "$OVERLAY"   # SHA-256 checked, no fuzz
# stage checks, scratch container without network: the new overlay, then the intent-grammar overlay's own checks on top of it
docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
  -v "$PWD/check_correction_hook.mts:/src/backend/check_correction_hook.mts:ro" \
  -v "$OVERLAY/backend/ha-intents.ts:/src/backend/ha-intents.ts:ro" \
  -v "$OVERLAY/backend/grammar-hook.ts:/src/backend/grammar-hook.ts:ro" "$IMAGE" /src/backend/check_correction_hook.mts   # ИТОГ 73/73
docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
  -v "$PWD/../intent-grammar/check_grammar_hook.mts:/src/backend/check_grammar_hook.mts:ro" \
  -v "$OVERLAY/backend/ha-intents.ts:/src/backend/ha-intents.ts:ro" \
  -v "$OVERLAY/backend/grammar-hook.ts:/src/backend/grammar-hook.ts:ro" "$IMAGE" /src/backend/check_grammar_hook.mts       # ИТОГ 34/34
# Only after the prebuilt candidate and its offloaded evidence are verified (next paragraph); never from a mutable overlay directory:
../../bluegreen/deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"
```

Deploy is the intent-grammar overlay's immutable-candidate flow, not repeated here: see [`../intent-grammar/README.md`](../intent-grammar/README.md)
(«Build, check, deploy»). Prebuild `$OVERLAY` as ONE immutable candidate on the active color's image (the two files replace the active
image's under `/src`; the speaker-room, weather and intent-grammar files stay), export its exact closure, run all original corpus and
dialog cases with the maintained layer checks natively through RunPod ([corpus protocol](../../corpus-runner/README.md)), and set
`VERIFIED_IMAGE_ID` and `CORPUS_RECEIPT` from that execution. `deploy.sh deploy` refuses without `--corpus-receipt` (or
`AISIS_CORPUS_RECEIPT`), takes an immutable `sha256:` image ID with `--image` (not combinable with `--overlay`/`--tag`), and an older
receipt cannot authorize a newly built image. Run the deployment command from the clean isolated release checkout. Nothing here
deploys or restarts anything.

Flags: no new flag. `GRAMMAR_HOOK=on` (default) enables everything; `shadow` sends the old classification-only request and nothing
new (no `deviceId`, no `rerun`, no probe, no deferral); `off` makes no call. A container reads its environment once: changing the mode means
recreating the active color (`GRAMMAR_HOOK` in `runtime.env`, then a deploy). Shipping with `GRAMMAR_HOOK=shadow` first is possible
but proves little: the new behaviour exists only in `on`.

After the flip watch the quasar log lines `Grammar noop '...' -> ...`, `Grammar route '...' -> ... (rerun, N ms)` and
`Grammar rerun '...' ... matched nothing locally`, and the adapter's journal for `/intent/decide` bodies with `noop`.

## Rollback

Fast: `GRAMMAR_HOOK=off` in `runtime.env` and recreate the active color (no hook call at all, so no probe, no rerun, no deferral).
Full: redeploy the previous image tag (`aisis-quasar:volume-zero-e859a37b` at the time of writing; `deploy.sh` also rolls back on
a failed smoke test by itself).

## Risks

- An adapter that does not yet serve the new fields: the probe costs one more call on a no-op (fail-open, ≤ 250 ms; HTTP 200 with
  `none` is a few ms). Hence the deploy order: adapter first.
- The probe only runs on a no-op of a single light/switch target; every other turn is as before. A no-op the adapter answers with a
  reply is spoken instead of «уже включен» and quasar then expects the answer through the adapter's context: the adapter must set
  `context: true` on that reply, otherwise «нет»/«отмена»/«повтори» are consumed by quasar (table above) and the adapter's dialog is
  left open until its own timeout.
- A context flag stays up to 35 s after a silently closed adapter context: «нет»/«отмена»/«хватит»/«спасибо» then cost one extra hook
  call before the old handler answers; «хватит» on a fresh turn still stops the music afterwards.
- The re-run asks the hook again for the repaired phrase (without `rerun`); a grammar-owned repaired phrase («громче») thus runs in the grammar.
- Known limit: `trace.phrase` is the normalized phrase, so the adapter sees `включи свет`, not the raw transcript (it canonicalizes anyway).
- The corpus and dialogs run of `deploy.sh` is unchanged by the overlay: in a scratch container the corpus gives `ИТОГ: 466/469` and the
  dialogs `195/195` on the active image's sources AND on the overlay (3 reminder phrases miss in that scratch setup with a closed adapter port;
  identical before and after, so not caused here; the real deploy compares against 469).

## Provenance

The baselines are `/src/backend/ha-intents.ts` and `/src/backend/grammar-hook.ts` of the active production color
(`aisis-quasar:volume-zero-e859a37b`, green, 2026-11-19; the blue color has the same two files, same SHA-256):

| file | baseline SHA-256 | patched SHA-256 |
|---|---|---|
| `ha-intents.ts` | `f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3` | `7a70be745f4f766e322377d17fa9f20707730d3e3285a5018b5a90abfbb9bc9c` |
| `grammar-hook.ts` | `f6f6021e87bdc57b5e1115148187d02fab88b8a5de3d6b7fe7b3643291d01c7f` | `c4bd50c68050b8733398cd5af1ddc3bcc70274cd35fc2faadf5b76263b0bb444` |

After a later deploy rebuilt either file the baseline hash changes and `prepare_overlay.py` refuses until the patch is rebased: it
must never be applied blind.

## Tests

`python3 -m unittest discover -s services/yandex-station/overlays/correction-hook` (patch applier, plus the patched `grammar-hook.ts` of
the intent-grammar overlay run with `bun` and a hand-driven fetch: stale and unheard answers; set `AISIS_QUASAR_BASELINE_DIR` to a
directory holding the captured files to also check the pinned hashes); `check_correction_hook.mts` (inside the image, `ИТОГ 73/73`):
hook body (`deviceId`, `rerun`, `noop`), the `rerun` route (executes the phrase through the fast layer; invalid text; an adapter that
answers `rerun` to everything cannot loop; a failed re-run falls back; a re-run that is itself a no-op does not ask again), the no-op
probe (reply with and without `listen`; `none`, timeout, HTTP 500, bad JSON, unknown route, empty reply, `rerun` or `silent` answer
to a probe: «уже включен» exactly as before and nothing executed; no probe for a real change, a whole-domain command or a repeated
no-op), shadow and every `off` spelling, the context table above, a probe answered after the 250 ms budget (the next «да» does not
announce `rerun`, not even after an answer arrived; once the window has run out it does again; a heard probe still lets «да» run the
repaired phrase) and an older classification's late `none` after a newer probe's context. The bun-driven unit tests also cover
(out-of-order answers by hand, a movable clock): an older `none` after a newer FAILED probe, an OLDER probe that fails after a newer
answer, no later answer (in flight at the failure, sent after it, a `none` or a `reply` that keeps the context open) lifts the guard
before the window ends, a stale question (not returned, no context, no `rerun` announced afterwards), a probe answer `none` that
claims a context (unheard; a plain `none` is an ordinary answer), a «да» sent while a probe is in flight (no `rerun` announced, a
`rerun` answer is refused even after the probe's question arrived), and a confirmation whose `rerun` answer arrives after the probe
timed out, while a newer probe is still in flight, or after a newer request was answered (each refused: nothing runs).
Mutation-checked (each fails a check): removing the deferral, the probe skip on repeat, the announced-rerun guard, the phrase restore
after a failed re-run, the unheard guard (its recording, its window), the per-station answer numbering, the failure's watermark
advance (order and stale), the stale-question discard, the probe-answer filter, or any term of the confirmation fence (other request
in flight at send time, stale at answer time, other request in flight at answer time).
