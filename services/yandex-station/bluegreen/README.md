# Blue/green source artifact deployment

`deploy.sh deploy --artifact REMOTE_OUTPUT` deploys the two-file voice overlay prepared and tested by native CI or the managed RunPod runner. It uses the existing immutable image recorded in `proof.json`. This mode performs no local compilation or Docker image build.

The input directory must contain `fireplace-artifact.tar` and `proof.json`. The archive contains only `backend/ha-intents.ts` and `backend/tv-app-intents.ts`; both file hashes, the archive hash, the source commit, the required existing image ID and the remote runtime input digest are recorded in the proof. Import rejects changed archives, extra entries, links and mismatched source bytes.

The import copies the verified files into an immutable directory under `AISIS_BG_STATE/artifacts`. The downloaded input can then be removed. Image and artifact directory are stored separately for each stage and production color. Every normal `bgctl.py compose` call validates the stored files and includes `source-artifacts.compose.json`, which mounts the two files read-only over the matching image. Normal recreation therefore retains the selected artifact without a one-off `-f` argument. An image deployment without `--artifact` clears only the selected color's mounts.

The artifact proof establishes integrity and remote test provenance. It does not establish production acceptance. Deployment still runs both stage corpus/dialog checks, stage API and handshake checks, the stage switch check, the idle production color's corpus/dialog and health checks, the switch checks after the flip, and the existing speaker reconnection check. Each corpus gate exports the actual container before and after its managed RunPod run, including effective mounted source bytes and runtime flags. The managed corpus job uses the supported `cpu3c-4` target and has no local fallback.

If the idle color fails, deployment restores its previous image and artifact together. The active production color and its existing source files remain unchanged until the verified flip. `deploy.sh rollback` selects the warm previous color with its own recorded image and mounts.

The deployment scripts and helper must be installed together before using this mode: `deploy.sh`, `bgctl.py`, `source_artifacts.py`, `corpus_runpod.py` and the existing Compose/export/smoke dependencies. All normal automation that recreates a color must use this version of `bgctl.py`. An older copy cannot interpret artifact state. Do not remove an artifact directory while either color references it.

`deploy.sh` records a private timestamped log under `/tmp` and streams it to the terminal. Source artifact mode keeps production configuration and credentials on the local host; they are not part of the source archive or remote corpus runtime.
