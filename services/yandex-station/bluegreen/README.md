# Blue/green source artifact deployment

`deploy.sh deploy --artifact REMOTE_OUTPUT` deploys a two-file voice overlay over the existing immutable image recorded in `proof.json`. This mode performs no local compilation or Docker image build. Its runtime identity is the image ID together with the exact mounted source hashes.

The public fireplace workflow compiles the isolated helper, runs its contract tests and packages the overlay inputs. The separate managed candidate assembly runs the actual prepared matcher and the full corpus/dialog suites, then produces `fireplace-artifact.tar` and `proof.json`. These are distinct artifacts and checks. A candidate assembly result does not satisfy the later stage or idle production gates.

## Artifact integrity and persistent mounts

The input directory must contain `fireplace-artifact.tar` and `proof.json`. The archive contains only `backend/ha-intents.ts` and `backend/tv-app-intents.ts`. The proof records both source hashes, the archive hash, source commit, required existing image ID and remote runtime input digest. Import rejects changed archives, extra entries, links and mismatched source bytes.

The proof is integrity metadata, not a signature or a production acceptance record. The reviewed remote run and its authoritative artifact identity must be checked when retrieving it. The source revision used for candidate assembly remains separate from any newer deployment-helper revision.

Import copies the verified files into a read-only directory under `AISIS_BG_STATE/artifacts`. The downloaded input can then be removed. Each stage and production color stores its own image and artifact directory. Every normal `bgctl.py compose` creation or configuration call validates the stored bytes and read-only permissions and includes `source-artifacts.<environment>.compose.json`, which mounts both files read-only over the matching image.

A production-only recreation does not depend on an unused stage artifact. Teardown commands (`down`, `rm`, `stop`) omit artifact mounts and can clean up stage even if an unused artifact was removed. Normal recreation retains the selected artifact without a one-off `-f` argument.

An image deployment without `--artifact` clears only the selected color's mounts. If the active color has a source artifact, an ordinary `--overlay` deployment must give an explicit base or use `--artifact`. Implicit image inheritance fails before stage or production mutation because it cannot preserve the active mounted sources.

## Fresh runtime gates

Deployment retains both stage corpus/dialog checks, stage API and handshake checks, the stage switch check, the idle production color's corpus/dialog and health checks, switch checks after the flip, and the speaker reconnection check.

Each corpus gate exports the actual container before and after its remote run, including effective mounted source bytes and runtime flags. It verifies the same complete commands, full counts, logs and input digest. Neither backend can accept a precomputed candidate proof or a receipt for another runtime.

The default backend uses the supported managed RunPod `cpu3c-4` target. The explicit alternative is:

```sh
AISIS_CORPUS_BACKEND=private-actions ./deploy.sh deploy --artifact REMOTE_OUTPUT
```

The private Actions backend is fixed in `private_corpus_ci.py` to one private repository, numeric repository identity, workflow ID, immutable release ref and reviewed commit. There is no arbitrary executor, workflow or result override. It uploads only a freshly prepared `manifest.json` and `rootfs.tar.gz`, inside the request's `private-input.tar`. The workflow executes its checked-in, hash-pinned corpus helper.

Each request dispatches once. An uncertain acknowledgement is observed without redispatch. Completion requires a successful first attempt for that request and workflow, the exact corpus artifact name, the artifact's authoritative digest, matching repository/run/source/input identities, and the original receipt with full counts. The saved gate evidence includes the run URL, run and artifact IDs, workflow pin, input hashes and provenance. Input releases are removed only after their request ownership is rechecked. Credential-store guards remain in effect; an unavailable store stops the operation without a token fallback.

Both backends have no local test fallback. Production configuration and credentials remain local and are excluded from the filtered remote runtime.

## Maintenance and recovery

Deployment acquires an exclusive maintenance owner before reading the active color. A direct child renews only that owner's marker, every 30 seconds, with an absolute two-hour limit. The watchdog's existing 900-second expiry remains enabled. Normal exit stops renewal and releases only the matching owner's marker. A killed parent stops renewal through reparent detection; the marker then expires normally. A stale or replaced owner cannot renew or remove its replacement.

Mutating deployment commands check the lease, and production selection, recreation and flip recheck the active color. A changed active color stops the operation. If the lease expires or its heartbeat stops after an idle replacement, EXIT cleanup can quarantine and remove only the captured inactive container while the lease still belongs to this deployment. It does not renew an expired lease. A replacement lease owner, changed container identity or newly active color refuses cleanup. Promotion and quarantine share the maintenance coordination lock. The active production color remains unchanged until the verified flip.

Before corpus checks, deployment verifies each candidate container's actual image ID and exact read-only source mounts. Before selecting the idle candidate, it records the original idle container ID; an unreadable identity stops the operation before changing production state.

If the idle color fails, including a Compose failure after partial replacement, deployment restores its previous image and artifact, recreates the container, waits for health and verifies its actual image and mounts. If both recreation and restoration fail before replacing the original container, that original is preserved.

Restoration failure after replacement is explicit. Cleanup records a persistent quarantine and the previous selection, rechecks inactive color and captured container identity, then removes only that replacement by ID. An untouched original container is preserved. The quarantine blocks normal Compose creation/configuration, ordinary selection changes and switch promotion; teardown remains possible. The watchdog skips quarantined colors while continuing its normal expiry and recovery for other components. Thus a later normal recreation cannot silently bring back a failed candidate.

Explicit `bgctl.py recover-selection ENV COLOR OWNER` requires a fresh lease, an inactive color, the recorded previous image and a fully valid read-only artifact. It verifies that the image exists, restores that recorded selection and clears quarantine only after those checks pass. Normal recreation and runtime verification can then restore the previous color. Invalid previous bytes keep the quarantine in place; they are never treated as accepted merely to remove the block.

The candidate identity remains owned until switch smoke and speaker reconnection succeed. A failed post-switch candidate is quarantined before a guarded reverse switch. While it is still active, rejection preserves its running container; after confirmed reversal, cleanup removes only the captured inactive candidate. A refused or expired-lease reverse switch is reported as unconfirmed. A replacement owner or container prevents both rejection and reversal. The quarantine still prevents those rejected bytes from becoming a later automatically promoted standby.

`deploy.sh rollback` selects the warm previous color with its own image and mounts.

## Installation and logs

Install the deployment helpers together: `deploy.sh`, `bgctl.py`, `source_artifacts.py`, `maintenance_lease.py`, `corpus_runpod.py`, `private_corpus_ci.py`, `failover.py`, and the existing Compose/export/smoke dependencies. All normal automation that recreates a color must use this version of `bgctl.py`; an older copy cannot interpret artifact state. Reload the watchdog through its normal service lifecycle after installing the helpers so it uses the matching quarantine and coordination code. Do not remove an artifact directory while either color references it.

`deploy.sh` and the corpus entry point stream safe progress and results to the terminal and automatically keep private execution logs under `/tmp`. Source artifact deployment keeps configuration and credentials out of the source archive and remote corpus inputs.
