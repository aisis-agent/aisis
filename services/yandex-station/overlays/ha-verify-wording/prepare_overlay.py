#!/usr/bin/env python3
"""Build the quasar overlay that speaks what ha-cli OBSERVED after a Home Assistant command, and nothing it only guessed.

One file of the ACTIVE image changes (README.md in this directory): `backend/ha-intents.ts`. The input is the file taken from the
active image (`docker cp aisis-quasar-prod-<color>:/src/backend/ha-intents.ts`), or the one the correction-hook overlay produced
from it (both are accepted; each has its own pinned result):

    prepare_overlay.py --ha-intents ha-intents.ts --output-dir NEW_DIRECTORY

Each patch is an exact single-block replacement (no fuzz), the input SHA-256 must be a known one and the result must have the
pinned SHA-256. The output is `NEW_DIRECTORY/backend/ha-intents.ts`; it is a deploy.sh `--overlay SRC_DIR` (maps to /src) only when
the image already has the grammar-hook.ts the input imports: with the correction-hook input (`7a70be74…`) that overlay's patched
`backend/grammar-hook.ts` MUST be copied next to it (README.md). Nothing is deployed or restarted here.
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
    "f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3": "58a2aa0b34cded2284669e2b288fb4fffe21c5f9db3edddaa11b31a027a1cd48",
    "7a70be745f4f766e322377d17fa9f20707730d3e3285a5018b5a90abfbb9bc9c": "8065b4b5bf2990d6dbf3cd52e456eacaefe3cad823820ceb69c751109238d9e2",
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
// (X-Aisis-Down: refused|reset|dns|unreachable|starting|http_502|http_503|http_504), undefined when an older ha-cli queued the
// call on a timeout. etaSeconds (X-Aisis-Eta) is only logged: whether Home Assistant is "restarting" is not something anyone observed.
class HaQueuedError extends Error {
  constructor (message: string, readonly etaSeconds: number | undefined, readonly down: string | undefined) {
    super(message)
  }
}

function restartingError (response: Response): HaQueuedError {
  const eta = Number(response.headers.get('x-aisis-eta'))
  return new HaQueuedError('ha-cli queued the call', Number.isFinite(eta) && eta > 0 ? eta : undefined,
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

// Nothing was observed of a command that was sent but not answered in time (or whose read-back timed out).
const HA_NOT_ANSWERED = 'Home Assistant не ответил вовремя, результат команды не проверен.'

function isTimeout (error: unknown): boolean {
  return error instanceof Error && error.name === 'TimeoutError'
}

// A body read cut off by the fetch timeout rejects with AbortError, not TimeoutError; only used where no one else aborts.
function isTimeoutOrBodyAbort (error: unknown): boolean {
  return isTimeout(error) || (error instanceof Error && error.name === 'AbortError')
}

// A service call that did not get an answer in time (haFetch gives up): nothing was observed. Only the call itself is judged so:
// a read that timed out before any command was sent keeps the generic failure text.
async function haServiceFetch (config: { token: string, url: string }, path: string, body: unknown): Promise<Response> {
  try {
    return await haFetch(config, path, body)
  } catch (error) {
    if (isTimeout(error)) throw new HaCommandError(`HA ${path} timed out`, HA_NOT_ANSWERED)
    throw error
  }
}

// The layer's own read-back AFTER a command was sent and ha-cli did not confirm it (an older ha-cli, no X-Aisis-Verify: ok): a
// read that times out here leaves the command's outcome unknown, so it is not the generic failure of a read before any command.
async function readBackAfterCommand (readBack: () => Promise<boolean>): Promise<boolean> {
  try {
    return await readBack()
  } catch (error) {
    if (isTimeoutOrBodyAbort(error)) throw new HaCommandError('read-back after the command timed out', HA_NOT_ANSWERED)
    throw error
  }
}

// ha-cli read the device back after this call and it was in the state the call leads to (X-Aisis-Verify: ok).
function confirmedByHaCli (response: Response): boolean {
  return response.headers.get('x-aisis-verify') === 'ok'
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

CALL_SERVICE_SIGNATURE_BEFORE = """\
  turnOn: boolean): Promise<void> {
  const domain = entityId.split('.')[0]
"""

CALL_SERVICE_SIGNATURE_AFTER = """\
  turnOn: boolean): Promise<boolean> {
  const domain = entityId.split('.')[0]
"""

CALL_SERVICE_BEFORE = """\
  const response = await haFetch(config, path, body)
  if (response.status === 202) {
    // Accepted into the ha-cli delayed queue: HA is restarting.
    throw restartingError(response)
  }
  if (!response.ok) {
    throw new Error(`HA ${path} ${entityId} -> ${response.status}`)
  }
"""

CALL_SERVICE_AFTER = """\
  const response = await haServiceFetch(config, path, body)
  if (response.status === 202) throw restartingError(response)
  if (!response.ok) throw haFailure(response, `HA ${path} ${entityId}`)
  return confirmedByHaCli(response)
"""

WHOLE_DOMAIN_BEFORE = """\
  wholeDomain: 'light', turnOn: boolean): Promise<void> {
  // Domain-wide HA calls require entity_id "all".
  const response = await haFetch(config,
    `/api/services/${wholeDomain}/${turnOn ? 'turn_on' : 'turn_off'}`, { entity_id: 'all' })
  if (response.status === 202) throw restartingError(response)
  if (!response.ok) {
    throw new Error(`HA ${wholeDomain}/${turnOn ? 'turn_on' : 'turn_off'} -> ${response.status}`)
  }
"""

WHOLE_DOMAIN_AFTER = """\
  wholeDomain: 'light', turnOn: boolean): Promise<boolean> {
  // Domain-wide HA calls require entity_id "all".
  const response = await haServiceFetch(config,
    `/api/services/${wholeDomain}/${turnOn ? 'turn_on' : 'turn_off'}`, { entity_id: 'all' })
  if (response.status === 202) throw restartingError(response)
  if (!response.ok) throw haFailure(response, `HA ${wholeDomain}/${turnOn ? 'turn_on' : 'turn_off'}`)
  return confirmedByHaCli(response)
"""

SET_SERVICE_BEFORE = """\
device: DeviceIntent): Promise<void> {
  const domain = entityId.split('.')[0]
  let path = '/api/services/light/turn_on'
"""

SET_SERVICE_AFTER = """\
device: DeviceIntent): Promise<boolean> {
  const domain = entityId.split('.')[0]
  let path = '/api/services/light/turn_on'
"""

SET_SERVICE_CALL_BEFORE = """\
  const response = await haFetch(config, path, body)
  if (response.status === 202) throw restartingError(response)
  if (!response.ok) throw new Error(`HA ${path} ${entityId} -> ${response.status}`)
"""

SET_SERVICE_CALL_AFTER = """\
  const response = await haServiceFetch(config, path, body)
  if (response.status === 202) throw restartingError(response)
  if (!response.ok) throw haFailure(response, `HA ${path} ${entityId}`)
  return confirmedByHaCli(response)
"""

EXECUTE_BEFORE = """\
  if (device.kind === 'set') {
    await Promise.all(device.targets.map(entityId => callSetService(config, entityId, device)))
  } else if (device.wholeDomain) {
    await callWholeDomain(config, device.wholeDomain, turnOn)
  } else {
    await Promise.all(device.targets.map(entityId => callService(config, entityId, turnOn)))
  }

  if (await verifyExecution(config, device)) {
"""

EXECUTE_AFTER = """\
  let confirmed: boolean[]
  if (device.kind === 'set') {
    confirmed = await Promise.all(device.targets.map(entityId => callSetService(config, entityId, device)))
  } else if (device.wholeDomain) {
    confirmed = [await callWholeDomain(config, device.wholeDomain, turnOn)]
  } else {
    confirmed = await Promise.all(device.targets.map(entityId => callService(config, entityId, turnOn)))
  }

  // ha-cli already read every device back (X-Aisis-Verify: ok on each call): a second look from here would only add the stale
  // cached reads of a Home Assistant that went away in between, and report a failure nobody observed.
  if (confirmed.every(Boolean) || await readBackAfterCommand(() => verifyExecution(config, device))) {
"""

TV_SCRIPT_BEFORE = """\
          const response = await haFetch(config, '/api/services/script/turn_on', { entity_id: entityId })
          if (response.status === 202) throw restartingError(response)
          if (!response.ok) throw new Error('TV application launch was not accepted')
"""

TV_SCRIPT_AFTER = """\
          const response = await haServiceFetch(config, '/api/services/script/turn_on', { entity_id: entityId })
          if (response.status === 202) throw restartingError(response)
          if (!response.ok) throw haFailure(response, 'TV application launch')
"""

FIREPLACE_SCRIPT_BEFORE = """\
          const response = await haFetch(config, '/api/services/script/turn_on', { entity_id: entityId })
          if (response.status === 202) throw restartingError(response)
          if (!response.ok) throw new Error('Projector fireplace launch was not accepted')
"""

FIREPLACE_SCRIPT_AFTER = """\
          const response = await haServiceFetch(config, '/api/services/script/turn_on', { entity_id: entityId })
          if (response.status === 202) throw restartingError(response)
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
    if (error instanceof HaQueuedError) {
      logger.warn(`Fast intent '${s}' queued: Home Assistant unavailable (${error.down ?? 'no evidence'}, eta ${error.etaSeconds ?? '?'}s)`)
      return { kind: 'reply', quasarDirectives: [], text: haRecoveryText(error.down) }
    }
    logger.error(`Fast intent '${s}' failed:`, error)
    if (error instanceof HaCommandError) return { kind: 'reply', quasarDirectives: [], text: error.say }
"""

HA_INTENTS_PATCHES = (
    (RESTART_BEFORE, RESTART_AFTER, "the HaRestartingError block"),
    (CALL_SERVICE_SIGNATURE_BEFORE, CALL_SERVICE_SIGNATURE_AFTER, "the callService signature"),
    (CALL_SERVICE_BEFORE, CALL_SERVICE_AFTER, "the callService request and failure"),
    (WHOLE_DOMAIN_BEFORE, WHOLE_DOMAIN_AFTER, "the callWholeDomain request and failure"),
    (SET_SERVICE_BEFORE, SET_SERVICE_AFTER, "the callSetService signature"),
    (SET_SERVICE_CALL_BEFORE, SET_SERVICE_CALL_AFTER, "the callSetService request and failure"),
    (EXECUTE_BEFORE, EXECUTE_AFTER, "the calls and the read-back of executeDeviceIntent"),
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
