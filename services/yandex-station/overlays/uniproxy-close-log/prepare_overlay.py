#!/usr/bin/env python3
"""Build the quasar overlay that logs a closed UniProxy WebSocket with its close code, at the level the code deserves.

The router (`routers/uniproxy.alice.yandex.net.ts`) logs `UniProxy WebSocket closed` at WARN for every connection that ends, with no
code, no device and no age. The speaker ends a connection after every exchange and after every wake word, the deploy smoke checks end
theirs: about 30 lines a day, none of them a failure, and the dashboard and session pages showed each one that fell into a dialog as an
error. The one change: a close the peer made itself (code 1000, 1001, 1005) is logged at INFO as `UniProxy WebSocket closed (normal):
code=… device=… age=…ms`; any other close (1006: the link dropped without a close frame, protocol and server errors) stays at WARN as
`UniProxy WebSocket closed abnormally: code=… device=… age=…ms`. dialogstore.py reads these two texts (and the old one without a code).

    prepare_overlay.py --baseline routers/uniproxy.alice.yandex.net.ts --output-dir NEW_DIRECTORY

`--baseline` is the file taken from the ACTIVE image (`docker cp aisis-quasar-prod-<color>:/src/routers/uniproxy.alice.yandex.net.ts`);
its SHA-256 must be the known one, the patch is three exact single-block replacements (no fuzz), and the result is checked against its own
SHA-256. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to /src): `NEW_DIRECTORY/routers/uniproxy.alice.yandex.net.ts`.
Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

BASELINE_SHA256 = "84a656535de98f8a239c1d54aa70821a8d8f420d4f7424d77377b9764958da71"
PATCHED_SHA256 = "34f1c54d257a05e35c48404942d33d49c96d3d01a853fc25443b9db77a5e69ba"

LOGGER_ANCHOR = """\
const logger = getLogger()
"""

WITH_CODES = """\
const logger = getLogger()

// The peer ended the connection itself: 1000 normal closure, 1001 going away, 1005 closed with no status code.
// The speaker closes a connection after every exchange and every wake word (its spotter-log socket half a second
// after it opens) and the deploy checks close theirs, so those are not warnings: the dashboard flags every WARN
// that falls into a dialog. Anything else (1006: the link dropped without a close frame; 1002, 1003, 1007, 1008,
// 1009, 1011: protocol, payload or server trouble) still is.
const NORMAL_CLOSE_CODES = new Set([1000, 1001, 1005])
"""

REGISTER_ANCHOR = """\
export function registerUniproxyAliceYandexNetRouter ("""

WITH_HELPER = """\
export function closeLogLine (code: number, reason: Buffer, deviceId: string,
  ageMs: number): { level: 'info' | 'warn', message: string } {
  const normal = NORMAL_CLOSE_CODES.has(code)
  // The device id is a request header and the reason is peer text: neither goes into the log as it came.
  const device = deviceId.replaceAll(/[^\\w.-]/g, '?').slice(0, 40)
  const why = reason.length > 0 ? ` reason=${JSON.stringify(reason.toString('utf8').slice(0, 80))}` : ''
  return {
    level: normal ? 'info' : 'warn',
    message: `UniProxy WebSocket closed${normal ? ' (normal)' : ' abnormally'}: code=${code} device=${device}${why} ` +
      `age=${ageMs}ms`
  }
}

export function registerUniproxyAliceYandexNetRouter ("""

BEFORE_CLOSE = """\
    const connection = new UniProxyConnection(websocket, deviceId, platform, parameters)
    websocket.on('close', () => {
      set.delete(connection)
      logger.warn('UniProxy WebSocket closed')
    })
"""

AFTER_CLOSE = """\
    const connection = new UniProxyConnection(websocket, deviceId, platform, parameters)
    const openedAt = Date.now()
    websocket.on('close', (code, reason) => {
      set.delete(connection)
      const line = closeLogLine(code, reason, deviceId, Date.now() - openedAt)
      logger[line.level](line.message)
    })
"""


class OverlayError(Exception):
    """The baseline is not the file this patch was made for, or the patch no longer applies exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_patch(baseline: str) -> str:
    """Add the close codes after the logger, the line builder before the router, and replace the close handler; each anchor must
    occur exactly once."""
    for anchor, what in ((LOGGER_ANCHOR, "the logger declaration"), (REGISTER_ANCHOR, "the router function"),
                         (BEFORE_CLOSE, "the WebSocket close handler")):
        if baseline.count(anchor) != 1:
            raise OverlayError(f"{what} was not found exactly once")
    return (baseline.replace(LOGGER_ANCHOR, WITH_CODES).replace(REGISTER_ANCHOR, WITH_HELPER)
            .replace(BEFORE_CLOSE, AFTER_CLOSE))


def prepare(baseline_path: Path, output_dir: Path, *, check_hashes: bool = True) -> Path:
    data = baseline_path.read_bytes()
    if check_hashes and sha256(data) != BASELINE_SHA256:
        raise OverlayError(f"baseline sha256 {sha256(data)} is not the expected {BASELINE_SHA256}")
    patched = apply_patch(data.decode("utf-8")).encode("utf-8")
    if check_hashes and sha256(patched) != PATCHED_SHA256:
        raise OverlayError(f"patched sha256 {sha256(patched)} is not the expected {PATCHED_SHA256}")
    target = output_dir / "routers" / "uniproxy.alice.yandex.net.ts"
    target.parent.mkdir(parents=True, exist_ok=False)
    target.write_bytes(patched)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="directory to write into; its routers/ subdirectory must not exist yet")
    args = parser.parse_args(argv)
    try:
        target = prepare(args.baseline, args.output_dir)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
