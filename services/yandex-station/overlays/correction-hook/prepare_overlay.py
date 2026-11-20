#!/usr/bin/env python3
"""Build the quasar overlay the speaker's correction engine needs (repair dialog instead of «Свет уже включен»).

Two files of the ACTIVE image change (README.md in this directory; the active image already carries the speaker-room,
weather-via-cli and intent-grammar overlays, so both inputs are the files THEY produced):

  backend/grammar-hook.ts  the request carries `deviceId` and `rerun: true` (and `noop` for a no-op probe); the route `rerun`;
                           the adapter's `context: true` flag; `grammarNoop()` and `grammarContextOpen()`
  backend/ha-intents.ts    `rerun` runs the adapter's phrase through the fast layer (never the hook's `rerun` again); a single-target
                           on/off command that finds its target already in the desired state asks the adapter before saying
                           «уже включен»; while the adapter's repair context is open the terminator and «повтори» handlers
                           wait for the hook

    prepare_overlay.py --ha-intents ha-intents.ts --grammar-hook grammar-hook.ts --output-dir NEW_DIRECTORY

The inputs are files taken from the ACTIVE image (`docker cp aisis-quasar-prod-<color>:/src/backend/<file>`); each SHA-256 must
be the known one, every patch is an exact single-block replacement (no fuzz), and every result is checked against its own
SHA-256. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to /src): `NEW_DIRECTORY/backend/*.ts`. Nothing is
deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# SHA-256 of the accepted input -> SHA-256 its patched result must have. Inputs: the active image
# (aisis-quasar:volume-zero-e859a37b: speaker-room + intent-grammar already applied).
HA_INTENTS_PATCHED_SHA256 = {
    "f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3": "7a70be745f4f766e322377d17fa9f20707730d3e3285a5018b5a90abfbb9bc9c",
}
GRAMMAR_HOOK_PATCHED_SHA256 = {
    "f6f6021e87bdc57b5e1115148187d02fab88b8a5de3d6b7fe7b3643291d01c7f": "c4bd50c68050b8733398cd5af1ddc3bcc70274cd35fc2faadf5b76263b0bb444",
}

# ── grammar-hook.ts ─────────────────────────────────────────────────────────────────────────────────────────────────

GH_HEADER_BEFORE = """\
// This file only does HTTP and validation: the mapping to FastIntentResult (reply, volume directives) lives in
// ha-intents.ts next to the code it reuses, so there is no import cycle.
"""

GH_HEADER_AFTER = GH_HEADER_BEFORE + """\
// Correction engine: a non-shadow request also carries `deviceId` (the station) and `rerun: true` (this caller can run a
// `rerun` route: a phrase the adapter wants executed as if spoken); grammarNoop() sends the same plus `noop`, the answer
// to «включи свет» when the light is already on. `context: true` on a verdict says the adapter holds a repair context open
// for that station: its next reply («да», «нет», «повтори», «отмена») must reach the adapter first (grammarContextOpen).
"""

GH_KIND_BEFORE = """\
export type GrammarRouteKind = 'dialog_end' | 'none' | 'reply' | 'silent' | 'volume'
"""

GH_KIND_AFTER = """\
export type GrammarRouteKind = 'dialog_end' | 'none' | 'reply' | 'rerun' | 'silent' | 'volume'
"""

GH_INTERFACE_BEFORE = """\
export interface GrammarRoute {
  intent: null | string
"""

GH_INTERFACE_AFTER = """\
// What the fast layer found when a single-target on/off command was already satisfied.
export interface NoopProbe {
  desired: 'off' | 'on'
  label: string
  targets: string[]
}

export interface GrammarRoute {
  // The adapter keeps a repair context open for the station that asked: its next reply must reach the adapter first.
  context: boolean
  intent: null | string
"""

GH_ROUTES_BEFORE = """\
  dialog_end: 'dialog_end', none: 'none', reply: 'reply', silent: 'silent', volume: 'volume'
"""

GH_ROUTES_AFTER = """\
  dialog_end: 'dialog_end', none: 'none', reply: 'reply', rerun: 'rerun', silent: 'silent', volume: 'volume'
"""

GH_ASK_BEFORE = """\
async function ask (text: string, ctx: { dialogActive: boolean }, shadow: boolean, fetchImpl: typeof fetch): Promise<GrammarRoute | null> {
"""

GH_ASK_AFTER = """\
interface AskContext {
  deviceId?: string | undefined
  dialogActive: boolean
  noop?: NoopProbe
  rerun?: boolean
}

// A shadow request is the old classification-only one: it never announces `rerun` and carries nothing new.
function requestBody (text: string, ctx: AskContext): Record<string, unknown> {
  const body: Record<string, unknown> = { dialogActive: ctx.dialogActive, text }
  if (ctx.deviceId !== undefined && ctx.deviceId !== '') body.deviceId = ctx.deviceId
  if (ctx.rerun === true) body.rerun = true
  if (ctx.noop !== undefined) body.noop = ctx.noop
  return body
}

async function ask (text: string, ctx: AskContext, shadow: boolean, fetchImpl: typeof fetch): Promise<GrammarRoute | null> {
"""

GH_BODY_BEFORE = """\
      body: JSON.stringify(shadow ? { dialogActive: ctx.dialogActive, shadow: true, text } : { dialogActive: ctx.dialogActive, text }),
"""

GH_BODY_AFTER = """\
      body: JSON.stringify(shadow ? { dialogActive: ctx.dialogActive, shadow: true, text } : requestBody(text, ctx)),
"""

GH_RETURN_BEFORE = """\
    return {
      intent: typeof data.intent === 'string' ? data.intent : null,
"""

GH_RETURN_AFTER = """\
    return {
      context: data.context === true,
      intent: typeof data.intent === 'string' ? data.intent : null,
"""

GH_CTX_BEFORE = """\
  ctx: { dialogActive: boolean, speakerLevel: null | number },
"""

GH_CTX_AFTER = """\
  ctx: { deviceId?: string | undefined, dialogActive: boolean, rerun?: boolean, speakerLevel: null | number },
"""

GH_TAIL_BEFORE = """\
  const verdict = await ask(text, ctx, false, fetchImpl)
  if (verdict === null) return null
  if (verdict.route === 'reply' && verdict.text === null) return null
  if (verdict.route === 'volume' && verdict.volume === null) return null
  return verdict
}
"""

GH_TAIL_AFTER = """\
  return await decide(text, ctx, fetchImpl)
}

// A verdict the caller can act on: a reply needs text, a volume needs a payload, a `rerun` needs text AND a rerun permission
// that still holds when the answer arrives (it is never accepted from an adapter that was not told this caller can run it: no
// loop through the re-run; see rerunStillHeard).
function valid (verdict: GrammarRoute | null, rerunAllowed: boolean): GrammarRoute | null {
  if (verdict === null) return null
  if (verdict.route === 'reply' && verdict.text === null) return null
  if (verdict.route === 'volume' && verdict.volume === null) return null
  if (verdict.route === 'rerun' && (verdict.text === null || !rerunAllowed)) return null
  return verdict
}

// The adapter's window for the owner's answer is 30 s; a flag older than this is forgotten even if no later verdict cleared it
// (every verdict without `context: true` clears the context flag), so a lost reply cannot defer the handlers for long.
const CONTEXT_TTL_MS = 35_000

// Per station. `issued`/`applied` number the decision requests: a response is applied only when no newer request of the station
// was applied before it (the adapter's answers can arrive out of order: an old classification's `none` must not close the
// context a newer no-op probe just opened). A request that got no usable answer counts as applied too (it is ordered like an
// answer): an older answer that arrives after it must not undo what the failure recorded. `unheardUntil`: a request of the
// station got no usable answer, so the adapter may have opened a repair context whose question was never spoken; for the
// adapter's whole window (CONTEXT_TTL_MS) the station does not announce `rerun`, and the adapter then asks the owner to say the
// phrase again instead of running it. NO later answer lifts it early: a client timeout does not stop the adapter's work, request
// numbers are not its processing order, and a `none` or a `reply` that keeps the context open does not say the question was
// spoken; only the window running out is certain. A failure is recorded whatever its order. `pending`: the requests in flight.
interface StationState { applied: number, contextUntil: number, issued: number, pending: Set<number>, unheardUntil: number }
const stations = new Map<string, StationState>()

function stationState (deviceId: string | undefined): StationState {
  const key = deviceId ?? ''
  let state = stations.get(key)
  if (state === undefined) {
    state = { applied: 0, contextUntil: 0, issued: 0, pending: new Set(), unheardUntil: 0 }
    stations.set(key, state)
  }
  return state
}

function settle (state: StationState, turn: number, verdict: GrammarRoute | null): void {
  state.pending.delete(turn)
  if (verdict === null) {
    state.applied = Math.max(state.applied, turn)
    state.unheardUntil = Date.now() + CONTEXT_TTL_MS
    return
  }
  if (turn < state.applied) return
  state.applied = turn
  state.contextUntil = verdict.context ? Date.now() + CONTEXT_TTL_MS : 0
}

// Whether a `rerun` answer to request `turn` may still be executed NOW, i.e. after the await. At send time nothing else of the
// station was in flight and it was not unheard (decide); from then on any other request of the station that settled has a higher
// number (a failure advances `applied` too, so a newer failure makes this answer stale), and one still in flight, newer, may be a
// probe whose question has not been spoken and the adapter may answer this confirmation before delivering it. None of that
// authorizes a correction.
function rerunStillHeard (state: StationState, turn: number): boolean {
  return state.applied <= turn && state.pending.size <= 1
}

// The answer the caller will act on, or null (which counts as a failure: the station becomes unheard). An answer the caller
// does not use but that says it opened a context, and a question that arrived after a newer answer was applied (its context
// was not recorded and the caller would speak it without the deferral that goes with it), are failures too.
function usable (state: StationState, turn: number, verdict: GrammarRoute | null, accept: (verdict: GrammarRoute) => boolean): GrammarRoute | null {
  if (verdict === null || !accept(verdict)) return null
  return turn < state.applied && verdict.context ? null : verdict
}

// One numbered, non-shadow decision request of a station. `accept`: the verdicts the caller can use at all.
async function decide (text: string, ctx: AskContext, fetchImpl: typeof fetch, accept: (verdict: GrammarRoute) => boolean = () => true): Promise<GrammarRoute | null> {
  const state = stationState(ctx.deviceId)
  // A confirmation is only an answer to a question that was delivered before it was spoken: no other request in flight when it is
  // sent (a probe's question may still be on its way), and none overlapping it later (rerunStillHeard).
  const announce = ctx.rerun === true && Date.now() >= state.unheardUntil && state.pending.size === 0
  state.issued += 1
  const turn = state.issued
  state.pending.add(turn)
  const answer = await ask(text, { ...ctx, rerun: announce }, false, fetchImpl)
  const verdict = usable(state, turn, valid(answer, announce && rerunStillHeard(state, turn)), accept)
  settle(state, turn, verdict)
  return verdict
}

/** The adapter said (on its latest verdict for this station) that a repair context is open: the next reply goes to it first. */
export function grammarContextOpen (deviceId: string | undefined): boolean {
  if (mode() !== 'on') return false
  return stationState(deviceId).contextUntil > Date.now()
}

/**
 * «Включи свет» found the light already on: asks the adapter once (same 250 ms budget) whether it wants to ask the owner
 * back («Мне послышалось «включи свет»…»). Only a reply is returned; none, any other route, timeout, error or any mode but
 * `on`: null, and the caller says «уже включен» as before. Only a reply, or a `none` that opened no context, is an answer the
 * caller can use: any other answer (a `rerun`, a context claimed by a route the caller ignores) leaves the station unheard.
 */
export async function grammarNoop (
  text: string,
  ctx: { deviceId?: string | undefined, dialogActive: boolean },
  noop: NoopProbe,
  fetchImpl: typeof fetch = fetch
): Promise<GrammarRoute | null> {
  if (mode() !== 'on') return null
  const verdict = await decide(text, { ...ctx, noop, rerun: true }, fetchImpl,
    answer => answer.route === 'reply' || (answer.route === 'none' && !answer.context))
  return verdict !== null && verdict.route === 'reply' ? verdict : null
}
"""

GRAMMAR_HOOK_PATCHES = (
    (GH_HEADER_BEFORE, GH_HEADER_AFTER, "the header comment"),
    (GH_KIND_BEFORE, GH_KIND_AFTER, "GrammarRouteKind"),
    (GH_INTERFACE_BEFORE, GH_INTERFACE_AFTER, "the GrammarRoute interface"),
    (GH_ROUTES_BEFORE, GH_ROUTES_AFTER, "the ROUTES table"),
    (GH_ASK_BEFORE, GH_ASK_AFTER, "the ask signature"),
    (GH_BODY_BEFORE, GH_BODY_AFTER, "the request body"),
    (GH_RETURN_BEFORE, GH_RETURN_AFTER, "the parsed verdict"),
    (GH_CTX_BEFORE, GH_CTX_AFTER, "the grammarRoute context"),
    (GH_TAIL_BEFORE, GH_TAIL_AFTER, "the end of grammarRoute"),
)

# ── ha-intents.ts ───────────────────────────────────────────────────────────────────────────────────────────────────

HA_IMPORT_BEFORE = """\
import { grammarRoute } from './grammar-hook'
"""

HA_IMPORT_AFTER = """\
import { grammarContextOpen, grammarNoop, grammarRoute } from './grammar-hook'
"""

HA_TRACE_BEFORE = """\
interface TurnTrace {
  executed: ActionRecord | null
  phrase: string
"""

HA_TRACE_AFTER = """\
interface TurnTrace {
  executed: ActionRecord | null
  // What the grammar hook needs from the turn when a device intent finds a no-op deep inside executeDeviceIntent: the station,
  // the dialog flag and whether this turn may ask the adapter about it (not the re-run of a repaired phrase, not a «повтори»).
  hook?: { deviceId: string | undefined, dialogActive: boolean, probe: boolean }
  phrase: string
"""

HA_NOOP_BEFORE = """\
// Executes a device intent against HA, then VERIFIES the resulting state.
"""

HA_NOOP_AFTER = """\
// The target is already in the state the phrase asked for. The phrase may be a misheard opposite («выключи» heard as
// «включи»): the adapter's correction engine is asked once, inside the hook's own 250 ms, and may ask the owner back. Its
// reply is returned; none, a timeout, any failure or a turn that must not probe (the re-run of a repaired phrase, «повтори»)
// is null and the caller answers «уже включен» exactly as before.
async function askAboutNoop (device: DeviceIntent, desired: 'off' | 'on'): Promise<FastIntentResult | null> {
  const trace = turnTrace.getStore()
  const hook = trace?.hook
  if (trace === undefined || hook === undefined || !hook.probe || trace.phrase === '') return null
  const verdict = await grammarNoop(trace.phrase, { deviceId: hook.deviceId, dialogActive: hook.dialogActive },
    { desired, label: device.label, targets: device.targets })
  if (verdict === null || verdict.text === null) return null
  logger.info(`Grammar noop '${trace.phrase}' (${desired} [${device.targets.join(', ')}]) -> ${verdict.intent ?? 'none'} (${verdict.ms} ms)`)
  return verdict.listen
    ? { kind: 'reply', listen: true, quasarDirectives: [], text: verdict.text }
    : { kind: 'reply', quasarDirectives: [], text: verdict.text }
}

// Executes a device intent against HA, then VERIFIES the resulting state.
"""

HA_ALREADY_BEFORE = """\
      return { kind: 'reply', quasarDirectives: [], text: alreadyText(device.label, desired) }
"""

HA_ALREADY_AFTER = """\
      return await askAboutNoop(device, desired) ?? { kind: 'reply', quasarDirectives: [], text: alreadyText(device.label, desired) }
"""

HA_SETUP_BEFORE = """\
  if (trace !== undefined) trace.phrase = s

  try {
    // «ничего», «хватит», «спасибо», «отмена», «пока»: silent end of the dialog, before any pending question and
    // before the brain. Only the stop class on a fresh turn (no dialog in progress) reaches the music.
    const end = parseEndDialog(text)
    if (end !== null) {
      pendingClarification = null
      if (end === 'stop' && options.dialogActive !== true) return await runMediaControl(config, 'stop') ?? DIALOG_END
      return DIALOG_END
    }

    // «повтори», «ещё раз», «прошлую команду»: answered from the last turns, never by a clarifying brain question.
    // Without a recent turn the phrase goes on (to the brain, which has the adapter's dialog list).
    const repeat = parseRepeat(tokens)
    if (repeat !== null) {
      const repeated = await repeatLast(config, repeat, speakerLevel)
      if (repeated !== null) {
        if (trace !== undefined) trace.skipRecording = true
        logger.info(`Fast intent repeat (${repeat}) for '${s}': ${repeated.kind === 'reply' ? repeated.text ?? '(command again)' : repeated.text}`)
        return repeated
      }
    }

"""

HA_SETUP_AFTER = """\
  const hookTurn = { deviceId: options.deviceId, dialogActive: options.dialogActive === true, probe: options.noCorrect !== true }
  if (trace !== undefined) {
    trace.phrase = s
    trace.hook = hookTurn
  }

  try {
    // While the adapter holds a repair context open for this station (its last verdict said `context: true`) the owner's reply
    // («нет», «отмена», «повтори»…) belongs to the adapter: the terminator and repeat handlers below would swallow it, and
    // «повтори» would run the misheard command again. They wait for the hook and run after it when it does not take the turn.
    const deferToHook = grammarContextOpen(options.deviceId)
    const endOrRepeat = async (): Promise<FastIntentResult | null> => {
      // «ничего», «хватит», «спасибо», «отмена», «пока»: silent end of the dialog, before any pending question and
      // before the brain. Only the stop class on a fresh turn (no dialog in progress) reaches the music.
      const end = parseEndDialog(text)
      if (end !== null) {
        pendingClarification = null
        if (end === 'stop' && options.dialogActive !== true) return await runMediaControl(config, 'stop') ?? DIALOG_END
        return DIALOG_END
      }

      // «повтори», «ещё раз», «прошлую команду»: answered from the last turns, never by a clarifying brain question.
      // Without a recent turn the phrase goes on (to the brain, which has the adapter's dialog list).
      const repeat = parseRepeat(tokens)
      if (repeat !== null) {
        // A repeated command that is a no-op again is not asked about a second time.
        if (trace?.hook !== undefined) trace.hook.probe = false
        const repeated = await repeatLast(config, repeat, speakerLevel)
        if (repeated !== null) {
          if (trace !== undefined) trace.skipRecording = true
          logger.info(`Fast intent repeat (${repeat}) for '${s}': ${repeated.kind === 'reply' ? repeated.text ?? '(command again)' : repeated.text}`)
          return repeated
        }
      }
      return null
    }
    if (!deferToHook) {
      const early = await endOrRepeat()
      if (early !== null) return early
    }

"""

HA_CALL_BEFORE = """\
    const grammar = await grammarRoute(text, { dialogActive: options.dialogActive === true, speakerLevel })
"""

HA_CALL_AFTER = """\
    const grammar = await grammarRoute(text, { deviceId: options.deviceId, dialogActive: options.dialogActive === true,
      rerun: options.noCorrect !== true, speakerLevel })
"""

HA_RERUN_BEFORE = """\
      } else if (grammar.route === 'silent') {
        grammarResult = DIALOG_END
"""

HA_RERUN_AFTER = """\
      } else if (grammar.route === 'rerun' && grammar.text !== null) {
        // The adapter repaired the phrase (a misheard «включи» was «выключи»): execute its text as if spoken. The re-run is
        // one repair per utterance (noCorrect), asks the hook WITHOUT announcing `rerun` (so it can never answer `rerun`
        // again) and never probes a no-op. Nothing local matched it: the phrase continues through the old tiers.
        grammarResult = await matchFastIntent(grammar.text, { ...options, noCorrect: true })
        if (trace !== undefined) {
          trace.phrase = s
          trace.hook = hookTurn
        }
        if (grammarResult === null) logger.warn(`Grammar rerun '${grammar.text}' for '${s}' matched nothing locally; the old path continues`)
      } else if (grammar.route === 'silent') {
        grammarResult = DIALOG_END
"""

HA_LATE_BEFORE = """\
        return grammarResult
      }
    }

    // Pure arithmetic answers instantly: no HA roundtrip, no brain.
"""

HA_LATE_AFTER = """\
        return grammarResult
      }
    }

    // The hook did not take a turn that was deferred to it: the terminator and repeat handlers answer it now.
    if (deferToHook) {
      const late = await endOrRepeat()
      if (late !== null) return late
    }

    // Pure arithmetic answers instantly: no HA roundtrip, no brain.
"""

HA_INTENTS_PATCHES = (
    (HA_IMPORT_BEFORE, HA_IMPORT_AFTER, "the grammar-hook import"),
    (HA_TRACE_BEFORE, HA_TRACE_AFTER, "the TurnTrace interface"),
    (HA_NOOP_BEFORE, HA_NOOP_AFTER, "the comment of executeDeviceIntent"),
    (HA_ALREADY_BEFORE, HA_ALREADY_AFTER, "the «уже включен» return"),
    (HA_SETUP_BEFORE, HA_SETUP_AFTER, "the terminator and repeat handlers"),
    (HA_CALL_BEFORE, HA_CALL_AFTER, "the grammarRoute call"),
    (HA_RERUN_BEFORE, HA_RERUN_AFTER, "the silent route branch"),
    (HA_LATE_BEFORE, HA_LATE_AFTER, "the end of the grammar block"),
)


class OverlayError(Exception):
    """A baseline is not the file this patch was made for, or the patch no longer applies exactly."""


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


def prepare(ha_intents: Path, grammar_hook: Path, output_dir: Path, *, check_hashes: bool = True) -> Path:
    if output_dir.exists():
        raise OverlayError(f"{output_dir} already exists")
    files = {
        "ha-intents.ts": patch_file(ha_intents, HA_INTENTS_PATCHES, HA_INTENTS_PATCHED_SHA256, check_hashes=check_hashes),
        "grammar-hook.ts": patch_file(grammar_hook, GRAMMAR_HOOK_PATCHES, GRAMMAR_HOOK_PATCHED_SHA256, check_hashes=check_hashes),
    }
    target = output_dir / "backend"
    target.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (target / name).write_bytes(content)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ha-intents", type=Path, required=True)
    parser.add_argument("--grammar-hook", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="must not exist yet")
    args = parser.parse_args(argv)
    try:
        target = prepare(args.ha_intents, args.grammar_hook, args.output_dir)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
