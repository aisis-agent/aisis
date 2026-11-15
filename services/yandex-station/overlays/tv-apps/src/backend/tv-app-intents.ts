/** Explicit living-room launches. Speaker location does not change the target. */
export interface TvAppIntent {
  app: 'netflix' | 'youtube'
  scriptEntity: string
  packageId: string
  label: string
}

export interface FireplaceIntent {
  scriptEntity: 'script.living_room_fireplace'
  label: 'камин'
}

interface NativePlayer {
  state: string
  attributes?: { app_id?: unknown, media_title?: unknown }
}

interface ObservedActions<T> {
  callScript: (entityId: string, variables?: { request_id: string }) => Promise<void>
  readNative: () => Promise<T | undefined>
  sleep?: (milliseconds: number) => Promise<void>
}

type AppActions = ObservedActions<NativePlayer>
type LaunchStatus = 'confirmed' | 'pending' | 'starting' | 'failed'

interface FireplaceAttributes {
  owned?: unknown
  file_matches?: unknown
  repeat?: unknown
  observed_at?: unknown
  request_id?: unknown
  launch_started_at?: unknown
  source_kind?: unknown
  source_id?: unknown
  title?: unknown
  progressing?: unknown
  error?: unknown
}

interface FireplaceReadiness {
  state: string
  attributes?: FireplaceAttributes
}

interface FireplaceActions extends AppActions {
  readReadiness: () => Promise<FireplaceReadiness | undefined>
}

interface FireplaceObservation {
  native: NativePlayer | undefined
  proof: FireplaceReadiness | undefined
}

const APPS: Record<TvAppIntent['app'], TvAppIntent> = {
  netflix: {
    app: 'netflix', scriptEntity: 'script.living_room_netflix',
    packageId: 'com.netflix.ninja', label: 'Netflix'
  },
  youtube: {
    app: 'youtube', scriptEntity: 'script.living_room_youtube',
    packageId: 'com.google.android.youtube.tv', label: 'YouTube'
  }
}
const ACTIVE_STATES = new Set(['on', 'idle', 'playing', 'paused', 'buffering'])
const DEFINITIVE_FIREPLACE_ERRORS = new Set([
  'selection_unreadable', 'no_distinct_source', 'sources_unavailable', 'invalid_target', 'stopped'
])
const FIREPLACE: FireplaceIntent = {
  scriptEntity: 'script.living_room_fireplace', label: 'камин'
}

function imperativeWords (input: unknown): string | null {
  if (typeof input !== 'string' || input.length > 240) return null
  const text = input.normalize('NFKC').toLowerCase().replace(/ё/g, 'е')
  if (text.includes('?')) return null
  return text.trim().replace(/[.!]+$/, '').replace(/,/g, ' ').replace(/\s+/g, ' ').trim()
}

/** Match a complete imperative, never a substring inside a question or music request. */
export function parseTvAppIntent (input: unknown): TvAppIntent | null {
  const words = imperativeWords(input)
  if (words === null) return null
  const match = /^(?:алиса )?(?:пожалуйста )?(?:открой|включи|запусти) (?:пожалуйста )?(?:на (?:телевизоре|тв|телеке|телике) )?(netflix|нетфликс|youtube|ютуб|ютюб|ютьюб)(?: на (?:телевизоре|тв|телеке|телике))?(?: пожалуйста)?$/.exec(words)
  if (match === null) return null
  return { ...APPS[/^(netflix|нетфликс)$/.test(match[1]) ? 'netflix' : 'youtube'] }
}

/** The controller chooses the projector video; the caller cannot supply a URL. */
export function parseFireplaceIntent (input: unknown): FireplaceIntent | null {
  const words = imperativeWords(input)
  if (words === null) return null
  const match = /^(?:алиса )?(?:пожалуйста )?(?:открой|включи|запусти) (?:пожалуйста )?(?:на проекторе )?камин(?: на проекторе)?(?: пожалуйста)?$/.test(words)
  return match ? { ...FIREPLACE } : null
}

async function readWithinBudget<T> (actions: ObservedActions<T>, remaining: number): Promise<T | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      actions.readNative(),
      new Promise<undefined>(resolve => { timer = setTimeout(() => resolve(undefined), remaining) })
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

/** One launch and an absolute reply budget. Uncertainty never causes a retry. */
async function executeObserved<T, S extends LaunchStatus> (
  scriptEntity: string, actions: ObservedActions<T>, observed: (native: T) => S
): Promise<{ status: S | 'pending' }> {
  const deadline = Date.now() + 4000
  await actions.callScript(scriptEntity)
  const sleep = actions.sleep ?? (async milliseconds => await new Promise(resolve => setTimeout(resolve, milliseconds)))
  let status: S | 'pending' = 'pending'
  for (let attempt = 0; attempt < 12 && Date.now() < deadline; attempt++) {
    let native: T | undefined
    try {
      native = await readWithinBudget(actions, deadline - Date.now())
    } catch {
      // A failed observation does not prove that an accepted launch failed.
      return { status: 'pending' }
    }
    if (Date.now() >= deadline) return { status }
    if (native !== undefined) {
      status = observed(native)
      if (status === 'confirmed' || status === 'failed') return { status }
    }
    if (attempt < 11) await sleep(Math.min(250, Math.max(0, deadline - Date.now())))
  }
  return { status }
}

export async function executeTvAppIntent (
  intent: TvAppIntent, actions: AppActions
): Promise<{ status: 'confirmed' | 'pending' }> {
  return await executeObserved(intent.scriptEntity, actions, native =>
    ACTIVE_STATES.has(native.state) && native.attributes?.app_id === intent.packageId ? 'confirmed' : 'pending')
}

function currentFireplaceRequest (
  attrs: FireplaceAttributes | undefined, requestId: string, launchStarted: number
): boolean {
  if (attrs?.request_id !== requestId || typeof attrs.launch_started_at !== 'string') return false
  const startedAt = Date.parse(attrs.launch_started_at)
  return Number.isFinite(startedAt) && startedAt >= launchStarted - 2000 && startedAt <= Date.now() + 2000
}

function fireplaceReady (proof: FireplaceReadiness): boolean {
  const attrs = proof.attributes
  if (proof.state !== 'playing' || attrs?.owned !== true || attrs.file_matches !== true) return false
  if (attrs.repeat !== 'one' || attrs.progressing !== true) return false
  if (attrs.error !== undefined && attrs.error !== null) return false
  if (attrs.source_kind !== 'youtube' && attrs.source_kind !== 'local') return false
  if (typeof attrs.source_id !== 'string' || attrs.source_id.trim() === '') return false
  if (typeof attrs.title !== 'string' || attrs.title.trim() === '') return false
  if (typeof attrs.observed_at !== 'string' || typeof attrs.launch_started_at !== 'string') return false
  const observedAt = Date.parse(attrs.observed_at)
  const startedAt = Date.parse(attrs.launch_started_at)
  return Number.isFinite(observedAt) && observedAt >= startedAt && observedAt <= Date.now() + 2000
}

function fireplaceOutcome (
  observation: FireplaceObservation, requestId: string, launchStarted: number
): LaunchStatus {
  const { native, proof } = observation
  if (proof === null || typeof proof !== 'object' || Array.isArray(proof)) return 'pending'
  if (!currentFireplaceRequest(proof.attributes, requestId, launchStarted)) return 'pending'
  if (proof.state === 'starting') return 'starting'
  if (proof.state === 'error' && typeof proof.attributes?.error === 'string' && DEFINITIVE_FIREPLACE_ERRORS.has(proof.attributes.error)) return 'failed'
  if (!fireplaceReady(proof)) return 'pending'
  return native?.state === 'playing' && native.attributes?.media_title === proof.attributes?.title ? 'confirmed' : 'pending'
}

export async function executeFireplaceIntent (
  _intent: FireplaceIntent, actions: FireplaceActions
): Promise<{ status: LaunchStatus }> {
  const launchStarted = Date.now()
  const requestId = globalThis.crypto.randomUUID()
  return await executeObserved(FIREPLACE.scriptEntity, {
    callScript: async entityId => await actions.callScript(entityId, { request_id: requestId }),
    readNative: async () => {
      const proof = await actions.readReadiness().catch(() => undefined)
      if (proof === null || typeof proof !== 'object' || Array.isArray(proof)) return undefined
      const needsNative = currentFireplaceRequest(proof.attributes, requestId, launchStarted) && fireplaceReady(proof)
      const native = needsNative ? await actions.readNative().catch(() => undefined) : undefined
      return { native, proof }
    },
    sleep: actions.sleep
  }, observation => fireplaceOutcome(observation, requestId, launchStarted))
}
