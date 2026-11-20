#!/usr/bin/env python3
"""Build the quasar overlay that speaks what ha-cli OBSERVED after a Home Assistant command, and nothing it only guessed.

One file of the ACTIVE image changes (README.md in this directory): `backend/ha-intents.ts`. The input is the file taken from the
active image (`docker cp aisis-quasar-prod-<color>:/src/backend/ha-intents.ts`), or the one the correction-hook overlay produced
from it (both are accepted; each has its own pinned result):

    prepare_overlay.py --ha-intents ha-intents.ts --output-dir NEW_DIRECTORY

Each patch is an exact single-block replacement (no fuzz), the input SHA-256 must be a known one and the result must have the
pinned SHA-256. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to /src): `NEW_DIRECTORY/backend/ha-intents.ts`.
Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# SHA-256 of the accepted input -> SHA-256 its patched result must have. Inputs: the active image
# (aisis-quasar:volume-zero-e859a37b: speaker-room, weather-via-cli, intent-grammar applied) and the same file after the
# correction-hook overlay (its blocks do not overlap these).
HA_INTENTS_PATCHED_SHA256 = {
    "f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3": "c246f55c30d6f33615b9fd8570f29d23136d65182790ec63c9544ed691d63c23",
    "7a70be745f4f766e322377d17fa9f20707730d3e3285a5018b5a90abfbb9bc9c": "290506059b1a8639ed85903b23eead89b5580ba9c0487131f1a3469fe310ce7e",
}

RESTART_BEFORE = """\
// Thrown when a service call was accepted into the ha-cli queue because HA is
// restarting. etaSeconds is ha-cli's measured typical recovery time (X-Aisis-Eta:
// median of real HA down->up windows), undefined if the header is missing.
class HaRestartingError extends Error {
  constructor (message: string, readonly etaSeconds: number | undefined) {
    super(message)
  }
}

function restartingError (response: Response): HaRestartingError {
  const eta = Number(response.headers.get('x-aisis-eta'))
  return new HaRestartingError('ha-cli queued the call', Number.isFinite(eta) && eta > 0 ? eta : undefined)
}

function secondsWord (n: number): string {
  if (n % 10 === 1 && n % 100 !== 11) return 'секунду'
  if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) return 'секунды'
  return 'секунд'
}

export function haRecoveryText (etaSeconds: number | undefined): string {
  if (etaSeconds === undefined) return 'Умный дом перезагружается, скоро всё придёт в норму.'
  return `Умный дом перезагружается, обычно примерно через ${etaSeconds} ${secondsWord(etaSeconds)} всё придёт в норму.`
}
"""

RESTART_AFTER = """\
// Thrown when ha-cli OBSERVED Home Assistant gone (HTTP 202) and queued the call. `down` is the evidence ha-cli saw
// (X-Aisis-Down: refused|reset|dns|unreachable|starting), undefined when an older ha-cli queued the call on a timeout.
// etaSeconds (X-Aisis-Eta) is only logged: whether Home Assistant is "restarting" is not something anyone observed.
class HaRestartingError extends Error {
  constructor (message: string, readonly etaSeconds: number | undefined, readonly down: string | undefined) {
    super(message)
  }
}

function restartingError (response: Response): HaRestartingError {
  const eta = Number(response.headers.get('x-aisis-eta'))
  return new HaRestartingError('ha-cli queued the call', Number.isFinite(eta) && eta > 0 ? eta : undefined,
    response.headers.get('x-aisis-down') ?? undefined)
}

export function haRecoveryText (down: string | undefined): string {
  if (down === 'starting') return 'Home Assistant запускается, команду выполню, когда он вернётся.'
  if (down !== undefined) return 'Home Assistant недоступен, команду выполню, когда он вернётся.'
  return 'Не получилось проверить ответ Home Assistant, команда ждёт в очереди.'
}

// A service call whose outcome ha-cli checked against the device state and did not confirm. `say` is what the speaker says:
// only what was observed (X-Aisis-Verify: failed = the device did not do it; unknown = nothing could be read).
class HaCommandError extends Error {
  constructor (message: string, readonly say: string) {
    super(message)
  }
}

function haFailure (response: Response, what: string): Error {
  const verify = response.headers.get('x-aisis-verify')
  const reason = response.headers.get('x-aisis-verify-reason')
  if (verify === 'failed') {
    return new HaCommandError(`${what} not confirmed: ${reason ?? 'no reason'}`, reason === 'device_unavailable'
      ? 'Home Assistant видит это устройство недоступным.'
      : 'Команду отправил, но не вижу, чтобы устройство её выполнило.')
  }
  if (verify === 'unknown') {
    return new HaCommandError(`${what} unverified: ${reason ?? 'no reason'}`, 'Не получилось проверить ответ Home Assistant.')
  }
  return new Error(`${what} -> ${response.status}`)
}
"""

CALL_SERVICE_BEFORE = """\
  if (!response.ok) {
    throw new Error(`HA ${path} ${entityId} -> ${response.status}`)
  }
"""

CALL_SERVICE_AFTER = """\
  if (!response.ok) throw haFailure(response, `HA ${path} ${entityId}`)
"""

WHOLE_DOMAIN_BEFORE = """\
  if (!response.ok) {
    throw new Error(`HA ${wholeDomain}/${turnOn ? 'turn_on' : 'turn_off'} -> ${response.status}`)
  }
"""

WHOLE_DOMAIN_AFTER = """\
  if (!response.ok) throw haFailure(response, `HA ${wholeDomain}/${turnOn ? 'turn_on' : 'turn_off'}`)
"""

SET_SERVICE_BEFORE = """\
  if (response.status === 202) throw restartingError(response)
  if (!response.ok) throw new Error(`HA ${path} ${entityId} -> ${response.status}`)
"""

SET_SERVICE_AFTER = """\
  if (response.status === 202) throw restartingError(response)
  if (!response.ok) throw haFailure(response, `HA ${path} ${entityId}`)
"""

TV_SCRIPT_BEFORE = """\
          if (!response.ok) throw new Error('TV application launch was not accepted')
"""

TV_SCRIPT_AFTER = """\
          if (!response.ok) throw haFailure(response, 'TV application launch')
"""

FIREPLACE_SCRIPT_BEFORE = """\
          if (!response.ok) throw new Error('Projector fireplace launch was not accepted')
"""

FIREPLACE_SCRIPT_AFTER = """\
          if (!response.ok) throw haFailure(response, 'Projector fireplace launch')
"""

CATCH_BEFORE = """\
    if (error instanceof HaRestartingError) {
      logger.warn(`Fast intent '${s}' queued: Home Assistant is restarting (eta ${error.etaSeconds ?? '?'}s)`)
      return { kind: 'reply', quasarDirectives: [], text: haRecoveryText(error.etaSeconds) }
    }
    logger.error(`Fast intent '${s}' failed:`, error)
"""

CATCH_AFTER = """\
    if (error instanceof HaRestartingError) {
      logger.warn(`Fast intent '${s}' queued: Home Assistant unavailable (${error.down ?? 'no evidence'}, eta ${error.etaSeconds ?? '?'}s)`)
      return { kind: 'reply', quasarDirectives: [], text: haRecoveryText(error.down) }
    }
    logger.error(`Fast intent '${s}' failed:`, error)
    if (error instanceof HaCommandError) return { kind: 'reply', quasarDirectives: [], text: error.say }
    // haFetch gave up (AbortSignal.timeout): nothing was observed, so no failure is claimed.
    if (error instanceof Error && (error.name === 'TimeoutError' || error.name === 'AbortError')) {
      return { kind: 'reply', quasarDirectives: [], text: 'Home Assistant не ответил вовремя, результат команды не проверен.' }
    }
"""

HA_INTENTS_PATCHES = (
    (RESTART_BEFORE, RESTART_AFTER, "the HaRestartingError block"),
    (CALL_SERVICE_BEFORE, CALL_SERVICE_AFTER, "the callService failure"),
    (WHOLE_DOMAIN_BEFORE, WHOLE_DOMAIN_AFTER, "the callWholeDomain failure"),
    (SET_SERVICE_BEFORE, SET_SERVICE_AFTER, "the callSetService failure"),
    (TV_SCRIPT_BEFORE, TV_SCRIPT_AFTER, "the TV application script failure"),
    (FIREPLACE_SCRIPT_BEFORE, FIREPLACE_SCRIPT_AFTER, "the fireplace script failure"),
    (CATCH_BEFORE, CATCH_AFTER, "the final catch of matchFastIntent"),
)


class OverlayError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_patches(baseline: str, patches: tuple[tuple[str, str, str], ...]) -> str:
    """Replace every BEFORE by its AFTER; each block must occur exactly once."""
    for before, after, label in patches:
        if baseline.count(before) != 1:
            raise OverlayError(f"{label} was not found exactly once")
        baseline = baseline.replace(before, after)
    return baseline


def patch_file(source: Path, patches: tuple[tuple[str, str, str], ...], accepted: dict[str, str], *, check_hashes: bool) -> bytes:
    """`accepted` maps the SHA-256 of each known input to the SHA-256 its patched result must have."""
    data = source.read_bytes()
    patched_sha = accepted.get(sha256(data))
    if check_hashes and patched_sha is None:
        raise OverlayError(f"{source.name} sha256 {sha256(data)} is not one of the expected {', '.join(accepted)}")
    patched = apply_patches(data.decode("utf-8"), patches).encode("utf-8")
    if check_hashes and sha256(patched) != patched_sha:
        raise OverlayError(f"patched {source.name} sha256 {sha256(patched)} is not the expected {patched_sha}")
    return patched


def prepare(ha_intents: Path, output_dir: Path, *, check_hashes: bool = True) -> Path:
    if output_dir.exists():
        raise OverlayError(f"output directory already exists: {output_dir}")
    patched = patch_file(ha_intents, HA_INTENTS_PATCHES, HA_INTENTS_PATCHED_SHA256, check_hashes=check_hashes)
    target = output_dir / "backend"
    target.mkdir(parents=True, mode=0o700)
    (target / "ha-intents.ts").write_bytes(patched)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ha-intents", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="must not exist yet")
    args = parser.parse_args(argv)
    try:
        target = prepare(args.ha_intents, args.output_dir)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
