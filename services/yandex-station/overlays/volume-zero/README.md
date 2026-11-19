# Restore a silent speaker after remote announcements

Refs https://github.com/alex-mextner/aisis/issues/168.
The dependent Home Assistant guard change is tracked at
https://github.com/alex-mextner/ha-configs-rs-bg-spomenik/issues/78.

The existing Quasar volume directive rejects zero with Zod before it reaches the
speaker. The converter returns null on that failure, and the HTTP push handler
can still return success. An HTTP acknowledgement therefore does not prove that
silence was restored.

## Contract and scope

The overlay changes only `set_sound_volume`'s integer lower bound from 1 to 0.
The supported native range becomes **0..10**. The existing directive already
encodes `new_level = volume` and `new_percent_level = volume * 10`, including zero.
Normalized HA levels 0 and 0.5 map to native levels 0 and 5. Fractional native
values, negative values, values above 10 and nonnumeric inputs remain invalid.

This permits restoration after briefly raising an unmuted speaker from volume
zero. It does not add a mute command or change true reported-mute protection.

## Prepare the overlay

`prepare_overlay.py` accepts only the exact reviewed source and verifies the
exact result. It reads the baseline without modifying it, refuses any existing
output directory, and writes one private file:

```text
NEW_DIRECTORY/routers/alice/base-provider.ts
```

The output directory's parent must already exist. Preparation does not build an
image, deploy, restart a service or contact a speaker. For example:

```sh
python3 prepare_overlay.py --baseline /private/baseline/base-provider.ts --output-dir /private/volume-zero-overlay
```

| File identity | SHA-256 |
| --- | --- |
| Baseline | `9d67c34303b94b9d0ca90c778036ad1afddab1d92d4ca1e24f0c62279e7605b2` |
| Prepared result | `3721f9160a307ef5eea1f3f034810183c66eb4a304a23b39cd96ad9578cb9f5d` |

Both the preparer and regression write automatic private logs under `/tmp` and
report their paths and completion status.

## Regression and provenance

The fixtures are exact copies of the provider, converter and protobuf encoder,
captured read-only from a production image on 2026-11-18. Their hashes also
matched the other production color. `fixtures/provenance.json` records the hashes
and installed dependency versions. The `.txt` suffix distinguishes captured
source from maintained overlay code; the checker loads the unchanged bytes as
TypeScript in an isolated temporary directory.

`check_volume_zero.mts` uses the real Zod schema, directive conversion and numeric
protobuf encoder. Only logging and the unrelated alarm serializer needed at
module initialization are substituted. No server or device is contacted.

The baseline was reproduced on GitHub-hosted CI at commit
`71bdc0d861b04333dc940f7463673b5e65c453fa`:
https://github.com/alex-mextner/aisis/actions/runs/44406296656/job/135162302188.
After a successful level-five control, it observed the real Zod `too_small`
error for `volume` with minimum 1, then failed exactly at
`integer zero must produce a volume directive`.

The final CI command, `npm run check`, retains that baseline check and exercises
the prepared overlay: exact payloads for 0, 1, 5 and 10; invalid arguments;
unchanged input; byte-for-byte preservation of unrelated source; private output
permissions; and rejection of unknown, already patched or existing output inputs.
The checker removes its temporary module tree. Run the full verification in CI
or an approved isolated test executor.

## Deployment order

The generated directory matches the existing blue/green overlay layout, so
prebuilding an immutable image from the active baseline preserves other overlays.
Follow the [corpus protocol](../../corpus-runner/README.md) to export its exact
source and dependencies, run all original corpus and dialog cases through native
RunPod, and collect the genuine execution receipt. Deploy that same immutable
image with `deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"`
from the clean release checkout. Stage and idle-production gates verify the receipt
and actual image; no full suite executes in the target service containers. Health,
switch, reconnect and rollback gates remain required, with no skip or local fallback.

Install and verify the Quasar change before relaxing the matching HA guard.
Acceptance must observe actual native level-zero state after the command and
restore the fresh prior state. A successful HTTP response alone is insufficient.
The source regression does not establish firmware behavior, audible timing or
production deployment.
