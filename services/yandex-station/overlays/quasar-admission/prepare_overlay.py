#!/usr/bin/env python3
"""Build the quasar overlay that admits only the clients it should: loopback colours, an address allow-list, known stations.

The running quasar answers every client that can reach its ports: a WebSocket with any `x-uprx-device-id` is a speaker whose text
turns run the Home Assistant fast-intent layer, `GET /quasar.yandex.net/glagol/token?device_id=` mints a control token for any device
and `GET /quasar.yandex.net/glagol/device_list` returns every speaker's private key (README.md in this directory, ticket #202). Three
files of the image change: NEW `admission.ts` (policy, Express middleware, WebSocket gate), PATCH `main.ts` (admission is the first
middleware; the HTTP server and the function-state server bind to `BIND_HOST`, default 127.0.0.1) and PATCH
`routers/uniproxy.alice.yandex.net.ts` (the upgrade is admitted before it becomes a connection). The same change is the commit
"fix: admit only known clients to the UniProxy and HTTP surface" of the public fork alex-mextner/aisis-quasar.

    prepare_overlay.py --baseline BASELINE_DIR --output-dir NEW_DIRECTORY [--router FILE]

`--baseline` is a copy of the ACTIVE image's sources (`docker cp aisis-quasar-prod-<color>:/src/. BASELINE_DIR`). The router is taken
from it unless `--router` names the file the `uniproxy-close-log` overlay produced from it (both inputs are accepted, each with its own
pinned result; the close-log blocks do not overlap these). Every patch is an exact single-block replacement (no fuzz): an anchor that is
not found exactly once, or an input or result whose SHA-256 is not the pinned one, refuses the build. The output directory is a deploy.sh
`--overlay SRC_DIR` (it maps to /src) holding only the three files. Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

ADMISSION_TS = "admission.ts"
MAIN_TS = "main.ts"
ROUTER_TS = "routers/uniproxy.alice.yandex.net.ts"

# SHA-256 of an accepted input -> SHA-256 its patched result must have.
ADMISSION_SHA256 = "75ebda5b8949d995e45b3c726e7d6467095ab53cb31c30e4a3b8f8b93f41efbd"
MAIN_SHA256 = {
    "dc56a983d9cb766f7df1f58ec8f41031d11cb8b16a2a00bdedfd2701238711a6": "d4b49d9978fefc6021f06507528dd4f8d0c3dc9bb81b0c7f54cf54996d7d4661",
}
ROUTER_SHA256 = {
    # the active image (aisis-quasar:volume-zero-e859a37b), and the same file after the uniproxy-close-log overlay
    "84a656535de98f8a239c1d54aa70821a8d8f420d4f7424d77377b9764958da71": "ea1c6ba38f917213a16785d9c99e7c3d7afcd168ba33a1684d9da9ebfc7377b9",
    "34f1c54d257a05e35c48404942d33d49c96d3d01a853fc25443b9db77a5e69ba": "5fd84c5e9245d58d4630cd890de7feebbd6f7a8ade624f611a53000d8d0370fc",
}

MAIN_PATCHES = (
    ("""\
import { BufferedAudioMetadataBackend } from './backend/audio-metadata/buffered'
""", """\
import { admissionMiddleware, knownStationIds, parseAdmissionPolicy } from './admission'
import { BufferedAudioMetadataBackend } from './backend/audio-metadata/buffered'
""", "the import block"),
    ("""\
const app = express()

""", """\
const app = express()

// A colour is reached through the blue/green switch on the same host, so it listens on loopback only (BIND_HOST=0.0.0.0
// restores upstream's behaviour for a container that publishes ports). Admission (src/admission.ts) is the first
// middleware: nothing below it (body parsing, the glagol token and device-list routes) runs for a refused client.
const bindHost = process.env.BIND_HOST ?? '127.0.0.1'
app.use(admissionMiddleware(parseAdmissionPolicy(), async () => await knownStationIds(storage)))

""", "the Express application"),
    ("""\
const server = app.listen(environment.PORT, error => {
""", """\
const server = app.listen(environment.PORT, bindHost, error => {
""", "the HTTP listener"),
    ("""\
  functionStateServer.listen(environment.FUNCTION_STATE_SERVER_PORT, () => {
""", """\
  // Never wider than loopback, whatever BIND_HOST says: this server has no admission of its own.
  functionStateServer.listen({ hostname: '127.0.0.1', port: environment.FUNCTION_STATE_SERVER_PORT }, () => {
""", "the function-state-server listener"),
)

ROUTER_PATCHES = (
    ("""\
import { getLogger } from '../logger'
""", """\
import { AdmissionPolicy, admitUpgrade, parseAdmissionPolicy, refuseUpgrade } from '../admission'
import { getLogger } from '../logger'
""", "the import block"),
    ("""\
  server: Server): UniProxyRouter {
  const wsServer = new WSServer({ noServer: true })
""", """\
  server: Server, admission: AdmissionPolicy = parseAdmissionPolicy()): UniProxyRouter {
  const wsServer = new WSServer({ noServer: true })
  const knownStations = async (): Promise<Set<string>> => {
    const infos = await parameters.storage.getStationInfos()
    return new Set(infos.map(info => info.duid))
  }
""", "the router function"),
    ("""\
      wsServer.handleUpgrade(request, socket, head, client => {
        wsServer.emit('connection', client, request)
      })
""", """\
      // Admission runs before the upgrade: a refused client never becomes a connection and none of its frames is
      // decoded. The http module drops its own socket error handler on upgrade and ws adds one only in
      // handleUpgrade, so until then (and for a refused client) a reset must be ignored here: unhandled, it stops
      // the server.
      const ignoreSocketError = (): void => {}
      socket.on('error', ignoreSocketError)
      admitUpgrade(admission, request, knownStations)
        .then(admitted => {
          if (admitted) {
            socket.removeListener('error', ignoreSocketError)
            wsServer.handleUpgrade(request, socket, head, client => {
              wsServer.emit('connection', client, request)
            })
          } else {
            refuseUpgrade(socket)
          }
        })
        .catch((error: unknown) => {
          logger.warn(`UniProxy upgrade failed: ${String(error)}`)
          refuseUpgrade(socket)
        })
""", "the WebSocket upgrade"),
)


class OverlayError(Exception):
    """An input is not the file a patch was written for, or a patch no longer applies exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_patches(source: str, patches: tuple[tuple[str, str, str], ...]) -> str:
    """Replace each block; every block must occur exactly once, and no replacement may be a block of the original already."""
    for before, _, label in patches:
        if source.count(before) != 1:
            raise OverlayError(f"{label} was not found exactly once")
    for before, after, label in patches:
        if after in source:
            raise OverlayError(f"{label} is already patched")
        source = source.replace(before, after)
    return source


def patch_file(data: bytes, patches: tuple[tuple[str, str, str], ...], accepted: dict[str, str], *, name: str,
               check_hashes: bool) -> bytes:
    if check_hashes and sha256(data) not in accepted:
        raise OverlayError(f"{name}: sha256 {sha256(data)} is not an accepted input ({', '.join(sorted(accepted))})")
    patched = apply_patches(data.decode("utf-8"), patches).encode("utf-8")
    if check_hashes and sha256(data) in accepted and sha256(patched) != accepted[sha256(data)]:
        raise OverlayError(f"{name}: patched sha256 {sha256(patched)} is not the pinned {accepted[sha256(data)]}")
    return patched


def prepare(baseline: Path, output_dir: Path, *, router: Path | None = None, check_hashes: bool = True) -> list[Path]:
    admission = (HERE / "src" / ADMISSION_TS).read_bytes()
    if check_hashes and sha256(admission) != ADMISSION_SHA256:
        raise OverlayError(f"{ADMISSION_TS}: sha256 {sha256(admission)} is not the pinned {ADMISSION_SHA256}")
    files = {
        ADMISSION_TS: admission,
        MAIN_TS: patch_file((baseline / MAIN_TS).read_bytes(), MAIN_PATCHES, MAIN_SHA256, name=MAIN_TS,
                            check_hashes=check_hashes),
        ROUTER_TS: patch_file((router or baseline / ROUTER_TS).read_bytes(), ROUTER_PATCHES, ROUTER_SHA256,
                              name=ROUTER_TS, check_hashes=check_hashes),
    }
    written = []
    for name, data in files.items():
        target = output_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise OverlayError(f"{target} already exists")
        target.write_bytes(data)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True, help="a copy of the active image's /src")
    parser.add_argument("--router", type=Path, help="routers/uniproxy.alice.yandex.net.ts after the uniproxy-close-log overlay")
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory for deploy.sh --overlay")
    args = parser.parse_args(argv)
    try:
        written = prepare(args.baseline, args.output_dir, router=args.router)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    for target in written:
        print(f"{sha256(target.read_bytes())[:12]}  {target.relative_to(args.output_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
