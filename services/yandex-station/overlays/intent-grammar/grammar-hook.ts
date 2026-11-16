// Grammar-first client (adapter /intent/decide; grammar, tiers and replies: services/yandex-station/code/intent_grammar).
// Most addressed phrases of the speaker are decided by a declarative grammar in the adapter without a language model:
// «который час», «громче», «не включай свет», wake-only and TTS-echo noise. The fast-intent layer asks it once per
// turn, after a pending yes/no answer was handled and before anything else, and acts on the verdict.
// GRAMMAR_HOOK=on|shadow|off (default on). shadow: call and log the answer, never act. off: no call at all.
// This file only does HTTP and validation: the mapping to FastIntentResult (reply, volume directives) lives in
// ha-intents.ts next to the code it reuses, so there is no import cycle.
import { getLogger } from '../logger'

const logger = getLogger()

// The adapter answers in a few ms; this guards a hung connection. A slow adapter must never delay the speaker.
const TIMEOUT_MS = 250

export type GrammarRouteKind = 'dialog_end' | 'none' | 'reply' | 'silent' | 'volume'

export interface GrammarVolume {
  // Signed speaker levels on the 0..10 scale.
  deltaLevels?: number
  // Signed multiples of the configured volume step.
  steps?: number
  // Fraction of the full scale, 0..1.
  target?: number
}

export interface GrammarRoute {
  intent: null | string
  listen: boolean
  ms: number
  route: GrammarRouteKind
  text: null | string
  volume: GrammarVolume | null
}

const ROUTES: Record<string, GrammarRouteKind | undefined> = {
  dialog_end: 'dialog_end', none: 'none', reply: 'reply', silent: 'silent', volume: 'volume'
}

function finite (value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function parseVolume (value: unknown): GrammarVolume | null {
  if (typeof value !== 'object' || value === null) return null
  const raw = value as Record<string, unknown>
  const volume: GrammarVolume = {}
  if (finite(raw.target) && raw.target >= 0 && raw.target <= 1) volume.target = raw.target
  if (finite(raw.steps) && raw.steps !== 0) volume.steps = raw.steps
  if (finite(raw.deltaLevels) && raw.deltaLevels !== 0) volume.deltaLevels = raw.deltaLevels
  return volume.target === undefined && volume.steps === undefined && volume.deltaLevels === undefined ? null : volume
}

/** The adapter's verdict as a validated plain object; null on any failure, off or shadow: the caller then runs the old path. */
export async function grammarRoute (
  text: string,
  ctx: { dialogActive: boolean, speakerLevel: null | number },
  fetchImpl: typeof fetch = fetch
): Promise<GrammarRoute | null> {
  // Read per call, not at load: the mode can be switched without reloading the module.
  const current = (process.env.GRAMMAR_HOOK ?? 'on').toLowerCase()
  if (current === 'off') return null
  const started = Date.now()
  try {
    const response = await fetchImpl(process.env.AISIS_ADAPTER_INTENT_URL ?? 'http://127.0.0.1:31117/intent/decide', {
      body: JSON.stringify({ dialogActive: ctx.dialogActive, text }),
      headers: { 'Content-Type': 'application/json' },
      method: 'POST',
      signal: AbortSignal.timeout(TIMEOUT_MS)
    })
    if (!response.ok) {
      logger.warn(`Grammar route http ${response.status} for '${text}' after ${Date.now() - started}ms`)
      return null
    }
    const data = await response.json() as Record<string, unknown>
    const route = typeof data.route === 'string' ? ROUTES[data.route] : undefined
    if (route === undefined) return null
    const verdict: GrammarRoute = {
      intent: typeof data.intent === 'string' ? data.intent : null,
      listen: data.listen === true,
      ms: finite(data.ms) ? data.ms : Date.now() - started,
      route,
      text: typeof data.text === 'string' && data.text.trim() !== '' ? data.text : null,
      volume: route === 'volume' ? parseVolume(data.volume) : null
    }
    if (current === 'shadow') {
      logger.info(`Grammar shadow '${text}' -> ${verdict.intent ?? 'none'} (${verdict.route}, ${verdict.ms} ms)`)
      return null
    }
    if (route === 'reply' && verdict.text === null) return null
    if (route === 'volume' && verdict.volume === null) return null
    return verdict
  } catch (error) {
    logger.warn(`Grammar route failed for '${text}' after ${Date.now() - started}ms: ${String(error)}`)
    return null
  }
}
