// Run inside a quasar image with the overlay mounted over /src/backend (README.md). The fast layer is the REAL one; the
// adapter and Home Assistant are stubs on a loopback port inside the throwaway container (no network):
//   docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
//     -v "$PWD/check_correction_hook.mts:/src/backend/check_correction_hook.mts:ro" \
//     -v "$OVERLAY/backend/ha-intents.ts:/src/backend/ha-intents.ts:ro" \
//     -v "$OVERLAY/backend/grammar-hook.ts:/src/backend/grammar-hook.ts:ro" "$IMAGE" /src/backend/check_correction_hook.mts
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'

type Result = null | { kind: string, listen?: boolean, quasarDirectives?: unknown[], text: null | string }
interface Options { deviceId?: string, dialogActive?: boolean, noCorrect?: boolean, speakerLevel?: null | number }
interface Layer { matchFastIntent: (text: string, options?: Options) => Promise<Result> }
interface Body { deviceId?: string, dialogActive?: boolean, noop?: { desired: string, label: string, targets: string[] }, rerun?: boolean, shadow?: boolean, text?: string }
interface Reply { body: unknown, delayMs?: number, status?: number }

process.env.AISIS_HA_TOKEN = 'check-only'

// Stub Home Assistant: two lights in two rooms (the default room of a station nobody places is «Гостиная»).
const lights: Record<string, { area: string, name: string, state: string }> = {
  'light.gostinaya': { area: 'Гостиная', name: 'Гостиная', state: 'on' },
  'light.kukhnya': { area: 'Кухня', name: 'Кухня', state: 'on' }
}
const serviceCalls: string[] = []
const adapterCalls: Body[] = []
let adapter: (body: Body) => Reply = () => ({ body: { route: 'none' } })

const entity = (id: string) => ({ attributes: { friendly_name: lights[id].name }, entity_id: id, state: lights[id].state })
const server = createServer((request, response) => {
  let raw = ''
  request.on('data', chunk => { raw += String(chunk) })
  request.on('end', () => {
    const url = request.url ?? ''
    const json = (value: unknown) => { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)) }
    if (url === '/intent/decide') {
      const body = JSON.parse(raw) as Body
      adapterCalls.push(body)
      // A runaway re-run loop must end as a failed check, not as a hang.
      const reply: Reply = adapterCalls.length > 40 ? { body: '', status: 500 } : adapter(body)
      setTimeout(() => {
        response.writeHead(reply.status ?? 200, { 'Content-Type': 'application/json' })
        response.end(typeof reply.body === 'string' ? reply.body : JSON.stringify(reply.body))
      }, reply.delayMs ?? 0)
      return
    }
    if (url === '/api/states') return json(Object.keys(lights).map(entity))
    if (url === '/api/template') {
      response.writeHead(200)
      response.end(Object.entries(lights).map(([id, light]) => `${id}\t${light.name}\t${light.area}\n`).join(''))
      return
    }
    const one = /^\/api\/states\/(light\.\w+)$/.exec(url)
    if (one !== null && lights[one[1]] !== undefined) return json(entity(one[1]))
    const service = /^\/api\/services\/light\/turn_(on|off)$/.exec(url)
    if (service !== null) {
      const serviceBody: { entity_id: string } = JSON.parse(raw) // the stub's own caller: ha-intents always sends entity_id
      const id = serviceBody.entity_id
      serviceCalls.push(`${service[1]} ${id}`)
      const targets = id === 'all' ? Object.keys(lights) : [id]
      for (const target of targets) lights[target].state = service[1]
      return json([])
    }
    response.writeHead(404)
    response.end('')
  })
})
await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
process.env.AISIS_HA_URL = base
process.env.AISIS_ADAPTER_INTENT_URL = `${base}/intent/decide`

const loaded = await import('./ha-intents.ts') as Layer & { default?: Layer }
const { matchFastIntent } = loaded.matchFastIntent === undefined ? loaded.default! : loaded

let pass = 0
let total = 0
const failures: string[] = []
const check = (name: string, ok: boolean, detail = '') => {
  total++
  if (ok) pass++
  else failures.push(`${name} ${detail}`.trim())
}
// The hook keeps per-station state (open context, unheard probe): every scenario runs on a station of its own.
let STATION = 'station-0'
let stationNumber = 0
const reset = (state: { gostinaya?: string, kukhnya?: string } = {}) => {
  STATION = `station-${++stationNumber}`
  lights['light.gostinaya'].state = state.gostinaya ?? 'on'
  lights['light.kukhnya'].state = state.kukhnya ?? 'on'
  serviceCalls.length = 0
  adapterCalls.length = 0
  adapter = () => ({ body: { route: 'none' } })
}
const verdict = (fields: Record<string, unknown>) => ({ context: false, intent: 'check', listen: false, ms: 1, text: null, volume: null, ...fields })
const ALREADY_ON = 'Свет в гостиной уже включен.'
const ASK = 'Мне послышалось «включи свет». Вы имели в виду «выключи свет»?'
const run = async (text: string, options: Options = {}) => await matchFastIntent(text, { deviceId: STATION, ...options })
const show = (result: Result) => JSON.stringify(result)

// ── baseline of the stub: no adapter answer, the old path ──────────────────────────────────────────────────────────
reset()
let result = await run('включи свет')
check('baseline: no-op without an adapter verdict says «уже включен»', result?.kind === 'reply' && result.text === ALREADY_ON, show(result))
check('baseline: the hook was asked once for the phrase, then once for the no-op', adapterCalls.length === 2 && adapterCalls[0].noop === undefined && adapterCalls[1].noop !== undefined, JSON.stringify(adapterCalls))

// ── request shape ──────────────────────────────────────────────────────────────────────────────────────────────────
check('hook body carries deviceId, rerun:true and dialogActive', adapterCalls[0].deviceId === STATION && adapterCalls[0].rerun === true && adapterCalls[0].dialogActive === false && adapterCalls[0].text === 'включи свет' && adapterCalls[0].shadow === undefined, JSON.stringify(adapterCalls[0]))
check('noop body carries desired, label and targets, plus deviceId and rerun:true', JSON.stringify(adapterCalls[1].noop) === JSON.stringify({ desired: 'on', label: 'свет в гостиной', targets: ['light.gostinaya'] }) && adapterCalls[1].deviceId === STATION && adapterCalls[1].rerun === true && adapterCalls[1].text === 'включи свет', JSON.stringify(adapterCalls[1]))
reset()
await matchFastIntent('сколько будет два плюс два', { dialogActive: true })
check('no station: no deviceId field, dialogActive is sent', adapterCalls[0].deviceId === undefined && adapterCalls[0].dialogActive === true && adapterCalls[0].rerun === true, JSON.stringify(adapterCalls[0]))

// ── no-op with an adapter reply ────────────────────────────────────────────────────────────────────────────────────
reset()
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ context: true, listen: true, route: 'reply', text: ASK }) }
result = await run('включи свет')
check('no-op with an adapter reply speaks its question and listens', result?.kind === 'reply' && result.text === ASK && result.listen === true && serviceCalls.length === 0, show(result))
reset()
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ route: 'reply', text: 'Свет уже горит, оставить?' }) }
result = await run('включи свет')
check('a reply without listen is spoken without listening', result?.text === 'Свет уже горит, оставить?' && result.listen === undefined, show(result))

// ── no-op with the adapter saying none, failing, or answering with something that is not a reply ───────────────────────
const noopAnswers: Array<[string, (body: Body) => Reply]> = [
  ['none', body => ({ body: verdict({ route: body.noop === undefined ? 'none' : 'none' }) })],
  ['timeout', body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ route: 'reply', text: ASK }), delayMs: 600 }],
  ['http 500', body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ route: 'reply', text: ASK }), status: 500 }],
  ['bad json', body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: 'not json' }],
  ['unknown route', body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ route: 'surprise', text: ASK }) }],
  ['reply with empty text', body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ route: 'reply', text: '  ' }) }],
  ['rerun answer to a probe', body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ route: 'rerun', text: 'выключи свет' }) }],
  ['silent', body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ route: 'silent' }) }]
]
for (const [label, handler] of noopAnswers) {
  reset()
  adapter = handler
  const started = Date.now()
  result = await run('включи свет')
  const elapsed = Date.now() - started
  check(`no-op, adapter ${label}: «уже включен» exactly as before, nothing executed`, result?.kind === 'reply' && result.text === ALREADY_ON && result.listen === undefined && serviceCalls.length === 0 && elapsed < 900, `${elapsed} ms ${show(result)}`)
}

// ── probe only where the old branch answered «уже» ─────────────────────────────────────────────────────────────────────
reset({ gostinaya: 'off' })
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ listen: true, route: 'reply', text: ASK }) }
result = await run('включи свет')
check('a real change is executed without a probe', result?.text === null && serviceCalls.join() === 'on light.gostinaya' && adapterCalls.every(call => call.noop === undefined), `${show(result)} ${JSON.stringify(adapterCalls.map(call => call.noop))}`)
reset()
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ listen: true, route: 'reply', text: ASK }) }
result = await run('включи весь свет')
check('whole-domain is not a single-target no-op: no probe', adapterCalls.every(call => call.noop === undefined) && result?.text !== ASK, `${show(result)} ${JSON.stringify(adapterCalls.map(call => call.noop))}`)
reset()
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ listen: true, route: 'reply', text: ASK }) }
result = await run('включи свет на кухне')
check('a no-op in another room carries that room\'s entity', result?.text === ASK && adapterCalls.at(-1)?.noop?.targets.join() === 'light.kukhnya' && adapterCalls.at(-1)?.noop?.label === 'свет на кухне', JSON.stringify(adapterCalls.at(-1)))
reset()
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ listen: true, route: 'reply', text: ASK }) }
result = await run('выключи свет')
check('«выключи свет» while on is a change: no probe', result?.text === null && serviceCalls.join() === 'off light.gostinaya' && adapterCalls.every(call => call.noop === undefined), show(result))
result = await run('выключи свет')
check('«выключи свет» while already off probes with desired off', result?.text === ASK && adapterCalls.at(-1)?.noop?.desired === 'off', JSON.stringify(adapterCalls.at(-1)))

// ── rerun route ────────────────────────────────────────────────────────────────────────────────────────────────────────
reset()
adapter = body => body.rerun === true ? { body: verdict({ route: 'rerun', text: 'выключи свет' }) } : { body: verdict({ route: 'none' }) }
result = await run('включи свет')
check('rerun executes the adapter\'s phrase through the fast layer', result?.kind === 'reply' && result.text === null && serviceCalls.join() === 'off light.gostinaya', `${show(result)} ${serviceCalls.join()}`)
check('the re-run asks the hook again WITHOUT announcing rerun, and never probes', adapterCalls.length === 2 && adapterCalls[1].text === 'выключи свет' && adapterCalls[1].rerun === undefined && adapterCalls[1].noop === undefined, JSON.stringify(adapterCalls))
reset()
adapter = () => ({ body: verdict({ route: 'rerun', text: 'выключи свет' }) })
result = await run('включи свет')
check('an adapter that answers rerun to everything cannot loop', result?.text === null && serviceCalls.join() === 'off light.gostinaya' && adapterCalls.length === 2, `${adapterCalls.length} calls ${show(result)}`)
reset()
adapter = body => body.rerun === true ? { body: verdict({ route: 'rerun', text: 'включи свет' }) } : { body: verdict({ route: 'none' }) }
result = await run('выключи свет')
check('a re-run that is itself a no-op answers «уже» and does not ask again', result?.text === ALREADY_ON && serviceCalls.length === 0 && adapterCalls.every(call => call.noop === undefined), `${show(result)} ${JSON.stringify(adapterCalls)}`)
reset()
adapter = body => body.rerun === true ? { body: verdict({ route: 'rerun', text: 'расскажи длинный анекдот про кота и собаку' }) } : { body: verdict({ route: 'none' }) }
result = await run('включи свет')
check('a rerun the fast layer cannot decide falls back to the old path (the no-op answer)', result?.text === ALREADY_ON && serviceCalls.length === 0, `${show(result)} ${serviceCalls.join()}`)
check('the fallback keeps the original phrase for the probe', adapterCalls.some(call => call.noop !== undefined && call.text === 'включи свет'), JSON.stringify(adapterCalls))
for (const [label, text] of [['empty text', ''], ['blank text', '   '], ['missing text', null]] as const) {
  reset()
  adapter = body => body.rerun === true ? { body: verdict({ route: 'rerun', text }) } : { body: verdict({ route: 'none' }) }
  result = await run('включи свет')
  check(`rerun with ${label} is invalid: old path`, result?.text === ALREADY_ON && serviceCalls.length === 0, show(result))
}
reset()
adapter = body => body.rerun === true ? { body: verdict({ route: 'rerun', text: 'громкость семь' }) } : { body: verdict({ route: 'none' }) }
result = await run('включи свет', { speakerLevel: 3 })
check('a rerun phrase owned by another tier (volume) executes there', JSON.stringify(result?.quasarDirectives ?? []).includes('sound_set_level') && serviceCalls.length === 0, show(result))

// ── modes ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
reset()
adapter = () => ({ body: verdict({ listen: true, route: 'reply', text: ASK }) })
process.env.GRAMMAR_HOOK = 'shadow'
result = await run('включи свет')
await new Promise(resolve => setTimeout(resolve, 100))
check('shadow: one classification-only call, exactly the old keys, «уже включен» as before', result?.text === ALREADY_ON && adapterCalls.length === 1 && JSON.stringify(Object.keys(adapterCalls[0]).sort()) === JSON.stringify(['dialogActive', 'shadow', 'text']), `${show(result)} ${JSON.stringify(adapterCalls)}`)
for (const value of ['off', 'OFF', '0', 'false', '']) {
  reset()
  process.env.GRAMMAR_HOOK = value
  result = await run('включи свет')
  check(`GRAMMAR_HOOK='${value}': no call at all, «уже включен»`, adapterCalls.length === 0 && result?.text === ALREADY_ON, show(result))
}
delete process.env.GRAMMAR_HOOK

// ── the owner's reply while the adapter's repair context is open ────────────────────────────────────────────────────────
// Which layer consumes each standard reply? 'quasar:<layer>' = before the hook (the adapter never sees it), 'hook' = the adapter
// gets it. dialogActive is true, like a reply inside the follow-up window.
const WORDS = ['да', 'нет', 'выключи', 'повтори', 'отмена', 'хватит', 'спасибо', 'не то', 'включи', 'забудь']
const table: Record<string, { closed: string, open: string }> = {}
const openContext = async () => {
  reset()
  adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ context: true, listen: true, route: 'reply', text: ASK }) }
  const asked = await run('включи свет')
  adapter = () => ({ body: verdict({ context: true, listen: true, route: 'reply', text: 'ответ адаптера' }) })
  return asked
}
const layerOf = (before: number, answered: Result): string => adapterCalls.length > before ? 'hook' : `quasar (${answered === null ? 'brain' : answered.text === null ? 'silent end' : 'speech'})`
// A turn that gave the hook the chance and the adapter said none: the old handlers answer after it.
for (const word of WORDS) {
  // context closed: the standard replies go the old way (a baseline of where each word lands today)
  reset()
  adapter = () => ({ body: verdict({ context: false, route: 'none' }) })
  await run('включи свет')
  let before = adapterCalls.length
  let answered = await run(word, { dialogActive: true })
  const closed = layerOf(before, answered)
  // context open: the previous verdict said context:true
  const asked = await openContext()
  check(`context opened by the no-op reply (${word})`, asked?.text === ASK && asked.listen === true, show(asked))
  before = adapterCalls.length
  answered = await run(word, { dialogActive: true })
  const open = layerOf(before, answered)
  table[word] = { closed, open }
  check(`context open: «${word}» reaches the hook`, open === 'hook' && answered?.text === 'ответ адаптера' && answered.listen === true, `${open} ${show(answered)}`)
}
console.log('СВОДКА: слово | контекст закрыт | контекст открыт')
for (const [word, row] of Object.entries(table)) console.log(`  «${word}» | ${row.closed} | ${row.open}`)
check('closed context: terminators and «повтори» are swallowed before the hook (the baseline the table documents)',
  ['нет', 'повтори', 'отмена', 'хватит', 'спасибо', 'забудь'].every(word => table[word].closed.startsWith('quasar')) && ['да', 'выключи', 'не то', 'включи'].every(word => table[word].closed === 'hook'), JSON.stringify(table))

// With the context open and the adapter saying none, the old handlers still answer: nothing is lost.
await openContext()
adapter = () => ({ body: verdict({ context: false, route: 'none' }) })
let before = adapterCalls.length
result = await run('отмена', { dialogActive: true })
check('context open, adapter none: «отмена» still ends the dialog silently', adapterCalls.length === before + 1 && result?.kind === 'reply' && result.text === null, show(result))
check('the verdict without context closes the context: the next «отмена» is swallowed before the hook', await (async () => {
  const calls = adapterCalls.length
  const out = await run('отмена', { dialogActive: true })
  return adapterCalls.length === calls && out?.text === null
})())
// «повтори» with the context closed is the quasar repeat handler, never the hook, and a repeated no-op is not probed again
reset()
await run('выключи свет') // changes the light: becomes the last executed command
lights['light.gostinaya'].state = 'on'
serviceCalls.length = 0
adapterCalls.length = 0
result = await run('повтори')
check('context closed: «повтори» runs the command again in quasar, the hook is not asked', serviceCalls.join() === 'off light.gostinaya' && adapterCalls.length === 0, `${serviceCalls.join()} ${adapterCalls.length} ${show(result)}`)
result = await run('повтори')
check('a repeated command that is a no-op says «уже выключен» without asking the adapter', result?.text === 'Свет в гостиной уже выключен.' && adapterCalls.length === 0, `${adapterCalls.length} ${show(result)}`)
reset()
const askedAgain = await (async () => {
  adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ context: true, listen: true, route: 'reply', text: ASK }) }
  return await run('включи свет')
})()
adapter = body => body.text === 'повтори' ? { body: verdict({ context: true, listen: true, route: 'reply', text: ASK }) } : { body: verdict({ route: 'none' }) }
serviceCalls.length = 0
result = await run('повтори', { dialogActive: true })
check('context open: «повтори» is answered by the adapter (the question again), the misheard command is not run again', askedAgain?.text === ASK && result?.text === ASK && result.listen === true && serviceCalls.length === 0, `${show(result)} ${serviceCalls.join()}`)
// a context of one station does not defer another station's replies
await openContext()
before = adapterCalls.length
result = await run('нет', { dialogActive: true, deviceId: 'other-station' })
check('another station\'s «нет» is not deferred', adapterCalls.length === before && result?.text === null, show(result))
// shadow and off modes ignore a stale context
await openContext()
process.env.GRAMMAR_HOOK = 'off'
before = adapterCalls.length
result = await run('нет', { dialogActive: true })
check('GRAMMAR_HOOK=off: the context is ignored, «нет» ends the dialog as before', adapterCalls.length === before && result?.text === null, show(result))
delete process.env.GRAMMAR_HOOK
// a quasar pending question still wins over the hook
reset()
adapter = () => ({ body: verdict({ route: 'none' }) })
result = await run('громкость')
check('a quasar pending question is asked', result?.listen === true, show(result))
adapter = () => ({ body: verdict({ context: true, listen: true, route: 'reply', text: 'не должно прозвучать' }) })
before = adapterCalls.length
result = await run('семь', { speakerLevel: 3 })
check('the answer to a quasar question is consumed before the hook', adapterCalls.length === before && JSON.stringify(result?.quasarDirectives ?? []).includes('sound_set_level'), show(result))

// ── a probe whose question was never heard cannot authorize a correction ─────────────────────────────────────────────────
// The adapter opened its repair context for the probe, but its reply came after the 250 ms budget: quasar said «уже включен» and the
// owner heard no question. The next turn must not announce `rerun`, so the adapter can only ask to say the phrase again.
reset()
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ context: true, listen: true, route: 'reply', text: ASK }), delayMs: 600 }
result = await run('включи свет')
check('a probe answered too late: «уже включен» as before', result?.text === ALREADY_ON && serviceCalls.length === 0, show(result))
adapter = body => body.rerun === true ? { body: verdict({ route: 'rerun', text: 'выключи свет' }) } : { body: verdict({ route: 'none' }) }
result = await run('да', { dialogActive: true })
const yes = adapterCalls.find(call => call.text === 'да')
check('the next «да» does not announce rerun and cannot execute the phrase nobody heard', yes !== undefined && yes.rerun === undefined && serviceCalls.length === 0, `${JSON.stringify(yes)} ${serviceCalls.join()} ${show(result)}`)
adapter = () => ({ body: verdict({ route: 'none' }) })
await run('сколько будет два плюс два')
check('an answer does not lift the guard: the station still does not announce rerun', adapterCalls.at(-1)?.text === 'сколько будет два плюс два' && adapterCalls.at(-1)?.rerun === undefined, JSON.stringify(adapterCalls.at(-1)))
// The adapter's window (30 s, the hook forgets at 35 s) has run out: nothing can still be waiting for that question.
const realNow = Date.now
Date.now = () => realNow() + 36_000
await run('сколько будет два плюс два')
Date.now = realNow
check('after the adapter\'s window the station announces rerun again', adapterCalls.at(-1)?.text === 'сколько будет два плюс два' && adapterCalls.at(-1)?.rerun === true, JSON.stringify(adapterCalls.at(-1)))
reset()
adapter = body => body.noop === undefined ? { body: verdict({ route: 'none' }) } : { body: verdict({ context: true, listen: true, route: 'reply', text: ASK }) }
await run('включи свет')
adapter = body => body.rerun === true ? { body: verdict({ route: 'rerun', text: 'выключи свет' }) } : { body: verdict({ route: 'none' }) }
result = await run('да', { dialogActive: true })
check('a probe that WAS heard still lets the «да» run the repaired phrase', adapterCalls.find(call => call.text === 'да')?.rerun === true && serviceCalls.join() === 'off light.gostinaya', `${JSON.stringify(adapterCalls)} ${serviceCalls.join()} ${show(result)}`)

// ── answers that arrive out of order ────────────────────────────────────────────────────────────────────────────────────
// An older turn's slow `none` lands after the newer probe's `context: true`: it must not close the context.
reset()
adapter = body => body.noop !== undefined
  ? { body: verdict({ context: true, listen: true, route: 'reply', text: ASK }) }
  : { body: verdict({ route: 'none' }), delayMs: body.text === 'сколько будет два плюс два' ? 200 : 0 }
const older = run('сколько будет два плюс два')
await new Promise(resolve => setTimeout(resolve, 20))
result = await run('включи свет')
await older
check('the newer probe opened the context', result?.text === ASK, show(result))
before = adapterCalls.length
adapter = () => ({ body: verdict({ context: true, listen: true, route: 'reply', text: 'ответ адаптера' }) })
result = await run('нет', { dialogActive: true })
check('an older classification\'s late «none» did not close the context: «нет» reaches the adapter', adapterCalls.length === before + 1 && result?.text === 'ответ адаптера', `${adapterCalls.length - before} ${show(result)}`)

console.log(`ИТОГ ${pass}/${total}`)
for (const failure of failures) console.log('FAIL', failure)
server.close()
process.exit(pass === total ? 0 : 1)
