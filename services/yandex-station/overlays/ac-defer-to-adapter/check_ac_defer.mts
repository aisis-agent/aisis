// Run inside the active quasar image with the overlay mounted over /src (README.md): every air-conditioner phrase of the
// 2026-11-20 11:18 incident (and its relatives) must go on to the brain tier (the adapter) with NO Home Assistant call and NO
// question from the container; everything else (lights, state answers) keeps its quasar answer. Mocked Home Assistant, no
// network. Against the UNPATCHED image the AC cases fail: that is the incident.
import { readFileSync, rmSync } from 'node:fs'

const phrases = JSON.parse(readFileSync('/src/backend/ac-phrases.json', 'utf8')) as { handed_over: string[], kept_by_quasar: string[] }

const AMBIGUITY_FILE = `/tmp/ha-ambiguity-ac-defer-${process.pid}.json`
process.env.AISIS_HA_URL = 'http://ha.test'
process.env.AISIS_HA_TOKEN = 'test-token'
process.env.AISIS_HA_AMBIGUITY_FILE = AMBIGUITY_FILE
process.env.SEMANTIC_INTENTS = 'off'
rmSync(AMBIGUITY_FILE, { force: true })

type Attrs = Record<string, unknown>
interface Ent { entity_id: string, state: string, attributes: Attrs }

const areaOf: Record<string, string> = {
  'light.gostinaia_globus_socket_1': 'Гостиная', 'light.studiia': 'Студия',
  'climate.kondei_1': 'Гостиная', 'climate.kondei_2': 'Второй этаж',
  'media_player.aisis_speaker_m00zrw3008rajg': 'Студия'
}
const STUDIO = 'M00ZRW3008RAJG'
const entities = new Map<string, Ent>()
for (const id of Object.keys(areaOf)) {
  const attrs: Attrs = { friendly_name: id }
  if (id.startsWith('climate.')) Object.assign(attrs, { hvac_modes: ['cool', 'heat', 'dry', 'auto', 'off'], max_temp: 30, min_temp: 16, temperature: 30 })
  entities.set(id, { attributes: attrs, entity_id: id, state: id.startsWith('media_player.') ? 'idle' : 'off' })
}
const calls: { path: string, body: Attrs }[] = []

globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
  const json = (value: unknown): Response => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } })
  if (url.pathname === '/api/states') return json([...entities.values()])
  if (url.pathname === '/api/template') {
    return new Response([...Object.entries(areaOf)].map(([id, area]) => `${id}\t${id}\t${area}`).join('\n'))
  }
  if (url.pathname.startsWith('/api/states/')) {
    const entity = entities.get(url.pathname.slice('/api/states/'.length))
    return entity === undefined ? new Response('', { status: 404 }) : json(entity)
  }
  if (url.pathname.startsWith('/api/services/')) {
    const body = JSON.parse(String(init?.body ?? '{}')) as Attrs
    calls.push({ body, path: url.pathname })
    const entity = entities.get(String(body.entity_id))
    if (entity !== undefined) {
      if (url.pathname.endsWith('/turn_on')) entity.state = 'on'
      if (url.pathname.endsWith('/turn_off')) entity.state = 'off'
      if (url.pathname.endsWith('/set_hvac_mode')) entity.state = String(body.hvac_mode)
    }
    return json([])
  }
  return new Response('', { status: 404 })
}) as typeof fetch

interface Layer {
  matchFastIntent: (text: string, options?: { deviceId?: string }) => Promise<null | { kind: string, listen?: boolean, text: null | string }>
}
const loaded = await import('/src/backend/ha-intents.ts') as Layer & { default?: Layer }
const { matchFastIntent } = loaded.matchFastIntent === undefined ? loaded.default! : loaded

let failed = 0
let total = 0
function check (name: string, ok: boolean, detail: unknown): void {
  total++
  if (!ok) { failed++; console.log(`  ✗ ${name}: ${JSON.stringify(detail)}`) }
}
async function say (text: string) {
  calls.length = 0
  for (const entity of entities.values()) entity.state = entity.entity_id.startsWith('media_player.') ? 'idle' : 'off'
  return await matchFastIntent(text, { deviceId: STUDIO })
}

// The incident phrases and their relatives (phrases.json, the same list the unit test proves against the adapter grammar): the
// brain tier (the adapter) decides, the container neither acts nor asks.
for (const phrase of phrases.handed_over) {
  const result = await say(phrase)
  check(`«${phrase}» goes on to the adapter`, result === null || result.kind === 'llm', result)
  check(`«${phrase}» makes no Home Assistant call`, calls.length === 0, calls)
}

// What the quasar layer keeps: turning the AC OFF (the adapter has no such rule), the cut-off reprompt, the bare question.
{
  calls.length = 0
  for (const entity of entities.values()) entity.state = entity.entity_id.startsWith('media_player.') ? 'idle' : 'off'
  entities.get('climate.kondei_2')!.state = 'heat'
  const off = await matchFastIntent('выключи кондиционер наверху', { deviceId: STUDIO })
  check('«выключи кондиционер наверху» still switches the unit off here', off?.kind === 'reply' &&
    calls.length === 1 && calls[0].path === '/api/services/climate/set_hvac_mode' && calls[0].body.entity_id === 'climate.kondei_2' &&
    calls[0].body.hvac_mode === 'off', { calls, off })
  const cut = await say('включи кондиционер на')
  check('«включи кондиционер на» (cut off) is still asked again, not handed over', cut?.kind === 'reply' && cut.text !== null && calls.length === 0, cut)
  const mixed = await say('Включи кондиционер на втором этаже на обогрев. Включи свет.')
  check('a two-sentence turn keeps the sentence loop (known remainder: still decided by quasar)', mixed?.kind === 'reply', mixed)
  for (const phrase of phrases.kept_by_quasar) {
    const kept = await say(phrase) // shapes the adapter grammar cannot parse whole (a clause with a light, a room before the AC word, extra words)
    check(`«${phrase}» keeps the quasar path (known remainder)`, kept?.kind === 'reply', kept)
    check(`«${phrase}» is not handed to the adapter`, kept !== null && kept.kind !== 'llm', kept)
  }
  const lights = await say('включи свет')
  check('«включи свет» still runs here', lights?.kind === 'reply' && calls.some(call => call.path === '/api/services/light/turn_on'), { calls, lights })
  const state = await say('что включено')
  check('«что включено» is still answered here', state?.kind === 'reply' && state.text !== null && calls.length === 0, state)
  // Last: it leaves a pending question behind.
  const bare = await say('Кондиционер на втором этаже.')
  check('the bare «Кондиционер на втором этаже» keeps its question (no adapter clarification yet)',
    bare?.kind === 'reply' && bare.listen === true && calls.length === 0, bare)
}

console.log(`ИТОГ ${total - failed}/${total}`)
process.exit(failed === 0 ? 0 : 1)
