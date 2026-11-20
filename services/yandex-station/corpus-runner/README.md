# Isolated corpus evidence

The full intent corpus and dialog suites run through the native `runpod-run`
wrapper. Deployment verifies their evidence and the current candidate at each
stage/prod color. It never executes those suites on the workstation or a running
home-service container, and missing evidence has no local fallback.

This v1 protocol is deliberately tied to the reviewed source-equivalent export:
24 source/config files, 11 installed package trees and their 13 literal links,
the unchanged 469-row private corpus and dialog fixture, the maintained runners,
and the hash-pinned Node 25.9.0 runtime. A separate eight-file maintained adapter
grammar closure checks the three explicitly identified reminder contracts.
The source closure includes `src/backend/negation.ts`, the module the fast-intent
layer imports to refuse negated phrases (overlay `ha-intents-safety`). It was
added to the closure rather than folded into `end-dialog.ts`, so the fork keeps one
file per concern and the receipt proves the module's exact bytes. The declared
closure is exact: a manifest that omits that module, or lists any source file
not declared here, is refused (`source closure differs`). The check covers the
manifest and the archive; it does not enumerate other files under `/src` in the image.
The Docker image is **not executed remotely**. The proof covers these exact
source/dependency bytes, then deployment independently verifies the immutable
image, runtime, source closure and nonsecret mode flags in each running color.
The existing API, handshake, switch, reconnect and rollback checks remain in place.

## Prepare the private inputs

Use an owned input directory within an isolated Git worktree. It contains only
`offloaded_corpus.py`, `manifest.json` and `payload.tar.gz`. Keep that directory
0700 and the JSON/archive/logs 0600. The helper must be copied from the reviewed
maintained revision, and its hash must match the manifest. Do not add the private
bundle or fixtures to Git, public CI artifacts or a public container registry.

The tar archive has no wrapper directory. Each declared inventory path has its
leading slash removed. The five fixture/runner files are under `src/backend/corpus/`:
`intents-ru.jsonl`, `run_dialogs.mts`, `run_corpus.mts`, `expected_kind.mjs`, and
`check_reminder_layers.py`. The separate adapter source files are under
`verification/adapter/intent_grammar/`; they are not Quasar image members.
Original file modes and literal dependency symlinks are retained; hard links,
extra files, traversal, missing members and changed bytes are rejected. Privacy
comes from the enclosing 0700 directory, not from changing hash-relevant modes.

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
  "fixtures": [{"name": "intents-ru.jsonl", "sha256": "<64 hex>", "bytes": 50661, "mode": 420}],
  "adapter_layer": {
    "schema": "aisis-adapter-layer-source/v1",
    "source_repo": "https://github.com/alex-mextner/aisis",
    "source_commit": "2d01d514bdf89d81a892871b42851f68a330ec93",
    "source_tree": "6b224aaa8f6363ce554969c2e9aff73b0d9cb261",
    "files": [{"repo_path": "services/yandex-station/code/intent_grammar/__init__.py", "archive_path": "verification/adapter/intent_grammar/__init__.py", "sha256": "<64 hex>", "bytes": 322, "mode": 420, "git_blob": "<40 hex>"}]
  }
}
```

Include all five fixture/runner rows and all eight adapter files. The complete `inventory` is the original candidate
metadata object, including source files, package entries/tree hashes, literal
links/edges and overlay provenance. Source modes are integers (for example 420 is
0644), not Git mode strings. The adapter block contains only the six reviewed
stdlib modules plus compiled `grammar/ru.json` and its `grammar/ru.yaml`
provenance. Both file hashes and Git blob identities are checked; the compiled
bundle's `source_sha256` must match that exact YAML file, which is not compiled or
executed in this run. Keep the collector's package-entry order: its tree
hash uses `JSON.stringify` rows ordered with `localeCompare(path, 'en')`.

The maintained overlay commit and the Quasar source/image provenance are separate
identities. `runner_revision` binds this helper and deployment source; a normal
squash merge is acceptable only if its complete tree equals the reviewed tree.
The pre-squash commit is retained as provenance; it need not remain reachable in
a fresh clone. The current complete tree and clean tracked/untracked working tree
are checked independently, as are the actual current private fixture hashes.
An updated source, dependency, runtime, fixture, mode or image requires new evidence.

Run the deployment from a clean, isolated checkout of that complete reviewed
tree. Keep its private inputs and receipts outside the checkout. The existing
`deploy.sh unit` command defaults to the live `~/xp/aisis/services/yandex-station/code`
directory; that mutable unit-install target must be separate from this release
checkout. Concurrent edits, unit installs or unrelated untracked files in the
release checkout intentionally stop the gate. Do not discard them or relax the
check: use the preserved clean release checkout. The operator must not mark
release paths `assume-unchanged` or `skip-worktree`; Git's index flags are within
the stated same-user trust boundary.

## Execute once through the native wrapper

Use the existing native admission/budget/credential guards, a 900-second maximum,
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
both archive and binary hashes. It runs all original corpus and dialog cases
in two clean environments: stage with `GRAMMAR_HOOK=off`, then the freshly observed
prod mode. No host environment or token is inherited. All five adapter/settings
endpoints are explicitly set to their audited paths on `http://127.0.0.1:9`.
The dialog fixture retains its existing mocked fetch and dummy HA credentials.
Ambient ancestor module directories are rejected. Node's documented
[`--no-global-search-paths`](https://nodejs.org/download/release/v25.9.0/docs/api/cli.html#--no-global-search-paths)
option prevents global module lookup. The deployed inspector rejects symlinked
source/package ancestors and undeclared nested dependencies; the two recorded
generated pnpm shims are recognized by their exact paths, bytes and hashes.
The test runtime explicitly sets `NODE_ENV=production`, matching the observed
serving child, and `TSX_DISABLE_CACHE=1`, the pinned tsx loader's explicit
in-memory-cache input. Together with `NODE_OPTIONS`, these exact values are
recorded in the native result and checked by its consumer; the runner hash binds
their implementation. The host environment cannot replace them.

The serving pnpm bootstrap has a standard `NODE_PATH` fallback into its shared
hoist. That is not exported or represented as absent. Preparation must resolve
the seven declared dependency edges from their actual caller contexts with that
serving environment and verify their inventoried target roots. The observed
snapshot resolves all seven edges from 42 literal caller/metadata contexts to
those exact roots. Type-only edges use package metadata and esbuild's platform
edge uses its actual binary path. The inspector still rejects package-local
shadows and extra non-root source `package.json` or `tsconfig.json` files. This is
evidence for the declared tested closure, not execution of every module in the
production image or a claim that the image has no other packages.

### Reminder layer contract

The original corpus remains byte-identical at 469 rows, including the three
reminder `cmd/execute` expectations. The previous direct fast-layer-only runner
reported 466/469 because that layer deliberately returns `llm/llm` for reminders.
The maintained runner now invokes its adjacent checker synchronously, with no
shell, using the actual absolute `sys.executable` supplied as
`AISIS_CORPUS_PYTHON` and the fixed `AISIS_CORPUS_ADAPTER_DIR` payload path.
The isolated stdlib-only child loads the eight hash-bound maintained grammar
files. It must actually prove `reminder.in`, the original 600/600/7200-second
slots and note digests, and exactly the `timer.start` action for those three
exact row hashes. It executes no action. Only after that real proof does the
Node runner require strict `llm/llm` from the pure fast layer for each of them;
the remaining 466 rows retain their original fast-layer checks. Missing, changed,
duplicate or failed checker evidence cannot count as a passing corpus case.

No precomputed pass sidecar is accepted. The native result records the actual
CPython implementation, version, absolute executable and binary SHA256 used for
the checker, and the executable is rehashed after the run. This identifies the
real test interpreter; it does not claim that Python belongs to the Quasar image
or that the live adapter process was executed. The current maintained adapter
files and original private fixture hashes are also rechecked by deployment.

Every process must actually exit 0 and print exactly one complete summary:
469 of 469 corpus cases and 195 of 195 dialog cases, in **each** mode. A partial total,
missing/duplicate summary, timeout or nonzero process exit fails. The true process
exit is retained. A single `AISIS_CORPUS_RESULT_V1=` marker binds the successful
results to manifest/archive/runner/Node hashes. The unchanged package/source bytes
are checked after the run as well. Native private logs retain suite output.

## Collect and consume the real evidence

Capture the native wrapper's original combined stdout/stderr in a private raw
lifecycle log. After it exits, retain that actual transcript: the canonical run
ledger records completion before cleanup and does not contain a termination row.
Collection requires the ordered waiting-for-pod, exact runner command, complete
canonical run output, exit 0 naming its actual native log, and termination or
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
Only the row with that exact raw-line hash is parsed, so a concurrent incomplete
append or an unrelated malformed row cannot invalidate the selected evidence.
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
