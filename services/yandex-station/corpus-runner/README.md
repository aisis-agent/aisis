# Isolated corpus evidence

The full intent corpus and dialog suites run through the native `runpod-run`
wrapper. Deployment verifies their evidence and the current candidate at each
stage/prod color. It never executes those suites on the workstation or a running
home-service container, and missing evidence has no local fallback.

This v1 protocol is deliberately tied to the reviewed source-equivalent export:
23 source/config files, 11 installed package trees and their 13 literal links,
four unchanged fixture/runner files, and the hash-pinned Node25.9.0 runtime.
The Docker image is **not executed remotely**. The proof covers these exact
source/dependency bytes, then deployment independently verifies the immutable
image, runtime, source closure and nonsecret mode flags in each running color.
The existing API, handshake, switch, reconnect and rollback checks remain in place.

## Prepare the private inputs

Use an owned input directory within an isolated Git worktree. It contains only
`offloaded_corpus.py`, `manifest.json` and `payload.tar.gz`. Keep that directory
0700 and the JSON/archive/logs0600. The helper must be copied from the reviewed
maintained revision, and its hash must match the manifest. Do not add the private
bundle or fixtures to Git, public CI artifacts or a public container registry.

The tar archive has no wrapper directory. Each declared inventory path has its
leading slash removed. The four fixtures are under `src/backend/corpus/`.
Original file modes and literal dependency symlinks are retained; hard links,
extra files, traversal, missing members and changed bytes are rejected. Privacy
comes from the enclosing0700 directory, not from changing hash-relevant modes.

`manifest.json` has this structure (identities must be actual, not placeholders):

```json
{
  "schema": "aisis-corpus-offload/v1",
  "execution_kind": "runpod-source",
  "image_executed_remote": false,
  "candidate_image": "sha256:<immutable candidate ID>",
  "baseline_image": "sha256:<immutable base ID>",
  "baseline_layers": ["sha256:<actual base layer ID>"],
  "candidate_layers": ["<all base layers>", "sha256:<one overlay layer>"],
  "overlay_revision": {"commit": "<40 hex>", "tree": "<40 hex>"},
  "runner_revision": {"commit": "<40 hex>", "tree": "<40 hex>", "sha256": "<64 hex>"},
  "runtime": {"version": "v25.9.0", "archive_sha256": "<pinned archive hash>", "binary_sha256": "<pinned binary hash>"},
  "prod_grammar_hook": "on",
  "default_flags": {"ASR_CORRECT": null, "SEMANTIC_INTENTS": null, "AISIS_FAST_EARCON": null},
  "inventory": {"schema": "aisis-candidate-source-metadata/v1", "...": "complete verified inventory"},
  "fixtures": [{"name": "intents-ru.jsonl", "sha256": "<64 hex>", "bytes": 50661, "mode": 420}]
}
```

Include all four fixture rows. The complete `inventory` is the original candidate
metadata object, including source files, package entries/tree hashes, literal
links/edges and overlay provenance. Source modes are integers (for example420 is
0644), not Git mode strings. Keep the collector's package-entry order: its tree
hash uses `JSON.stringify` rows ordered with `localeCompare(path, 'en')`.

The maintained overlay commit and the Quasar source/image provenance are separate
identities. `runner_revision` binds this helper and deployment source; a normal
squash merge is acceptable only if its complete tree equals the reviewed tree.
The pre-squash commit is retained as provenance; it need not remain reachable in
a fresh clone. The current complete tree and clean tracked/untracked working tree
are checked independently, as are the actual current private fixture hashes.
An updated source, dependency, runtime, fixture, mode or image requires new evidence.

## Execute once through the native wrapper

Use the existing native admission/budget/credential guards, a900-second maximum,
the unprivileged execution user, no keep/TTL override, and normal termination.
The sync directory must contain only the three prepared inputs. The command
recorded by the native ledger must be exactly this argv (substitute actual hashes):

```sh
python3 offloaded_corpus.py run --manifest manifest.json --bundle payload.tar.gz \
  --manifest-sha256 MANIFEST_SHA256 --bundle-sha256 BUNDLE_SHA256 \
  --runner-sha256 RUNNER_SHA256
```

The helper has an inactive default and refuses `run` without the native
unprivileged RunPod context. It verifies the archive, source/dependency closure,
fixture and runner hashes, downloads the fixed official Node archive and checks
both archive and binary hashes. It runs the unchanged corpus and dialog programs
in two clean environments: stage with `GRAMMAR_HOOK=off`, then the freshly observed
prod mode. No host environment or token is inherited. All five adapter/settings
endpoints are explicitly set to their audited paths on `http://127.0.0.1:9`.
The dialog fixture retains its existing mocked fetch and dummy HA credentials.
Ambient ancestor module directories are rejected. Node's documented
[`--no-global-search-paths`](https://nodejs.org/download/release/v25.9.0/docs/api/cli.html#--no-global-search-paths)
option prevents global module lookup. The deployed inspector rejects symlinked
source/package ancestors and undeclared nested dependencies; the two recorded
generated pnpm shims are recognized by their exact paths, bytes and hashes.

Every process must actually exit0 and print exactly one complete summary:
469 of469 corpus cases and195 of195 dialog cases, in **each** mode. A partial total,
missing/duplicate summary, timeout or nonzero process exit fails. The true process
exit is retained. A single `AISIS_CORPUS_RESULT_V1=` marker binds the successful
results to manifest/archive/runner/Node hashes. The unchanged package/source bytes
are checked after the run as well. Native private logs retain suite output.

## Collect and consume the real evidence

Capture the native wrapper's original combined stdout/stderr in a private raw
lifecycle log. After it exits, retain that actual transcript: the canonical run
ledger records completion before cleanup and does not contain a termination row.
Collection requires the ordered waiting-for-pod, exact runner command, complete
canonical run output, exit0 naming its actual native log, and termination or
confirmed already-gone message. A termination failure rejects the receipt.

```sh
python3 services/yandex-station/corpus-runner/offloaded_corpus.py collect \
  --manifest /private/input/manifest.json --bundle /private/input/payload.tar.gz \
  --run-log /home/OWNER/.cache/runpod-run/runs/ACTUAL_NATIVE_LOG.log \
  --termination-log /private/ACTUAL_OUTER_WRAPPER.log \
  --pod-id ACTUAL_POD_ID --ledger-line-sha256 ACTUAL_LEDGER_LINE_SHA256 \
  --output /private/input/corpus-receipt.json
```

The collector and consumer reopen the canonical account-home native ledger and
run log, matching the actual command, successful exit, explicit pod and ledger-line
identity, result marker and log hashes. The ledger line hash includes its original
newline. A failed or separate earlier attempt cannot be selected accidentally.
A copied success JSON or a standalone termination line is insufficient. Native
evidence must remain private and owned by the current operator; the helper never
changes native files or permissions. The receipt stays beside the original
`manifest.json`; both are needed for later verification.
This is an audit of the owner's native execution records, not cryptographic
attestation against an owner who can rewrite the ledger and every matching log.

Prefer the prebuilt immutable candidate, so deploy cannot rebuild a different ID:

```sh
services/yandex-station/bluegreen/deploy.sh deploy \
  --image sha256:ACTUAL_TESTED_IMAGE --corpus-receipt /private/input/corpus-receipt.json
```

`AISIS_CORPUS_RECEIPT` supplies the same mandatory receipt for `stage-check` and
`adopt`. Legacy build inputs may still be used, but only a build that reproduces the
already-tested immutable ID (for example an unchanged cache hit) can continue.
A new image ID requires its own export and isolated execution; deployment does
not relabel old evidence for it. The prebuilt `--image` path avoids that ambiguity.
At each stage-blue, stage-green and idle-prod gate, verification hashes current
source/dependency bytes and Node, checks that source-altering mounts are absent,
and compares explicitly reported grammar/default flags. A missing grammar value
does not masquerade as stage-off. The known `/src/fillers` asset mount is outside
the fixed export; other mounts overlapping source/dependency/config paths fail.
This metadata-only inspection performs
no corpus, API or device request. The individual existing smoke/handshake/rollback
gates then run normally.
