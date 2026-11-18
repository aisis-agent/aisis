# Restore a silent speaker after remote announcements

Refs https://github.com/alex-mextner/aisis/issues/168.
The dependent Home Assistant guard change is tracked at
https://github.com/alex-mextner/ha-configs-rs-bg-spomenik/issues/78.

The existing Quasar volume directive rejects zero with Zod before it reaches the
speaker. The converter returns null on that failure, and the HTTP push handler
can still return success. An HTTP acknowledgement therefore does not prove that
silence was restored.

## Regression and provenance

The fixtures are exact copies of the current provider, converter and protobuf
encoder, captured read-only from a production image on 2026-11-18. Their hashes
also matched the other production color. `fixtures/provenance.json` records the
hashes and the installed dependency versions. The `.txt` suffix distinguishes
captured sources from maintained overlay code; the checker loads their unchanged
bytes as TypeScript in an isolated temporary directory.

`check_volume_zero.mts` uses the real Zod schema, directive conversion and numeric
protobuf encoder. Only the logger and the unrelated alarm serializer needed at
module initialization are substituted. The checker never starts a server or
contacts a device. It writes private automatic logs under `/tmp`, reports their
paths, and removes its temporary module tree.

The first test-only revision intentionally runs `npm run check:baseline` in CI.
It must fail with `integer zero must produce a volume directive` against the
unchanged captured source. The runtime overlay is added only after that real
failure is observed. The final CI command will exercise both the recorded baseline
rejection and the prepared overlay, including invalid input and byte-drift checks.
