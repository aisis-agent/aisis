// Run inside a quasar image with the overlay mounted over /src/backend (README.md): the fast-intent layer asks the
// adapter's grammar first, maps its verdict, fails open, and never lets the hook run before a pending answer.
//   docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
//     -v "$PWD/check_grammar_hook.mts:/src/backend/check_grammar_hook.mts:ro" \
//     -v "$OVERLAY/backend/ha-intents.ts:/src/backend/ha-intents.ts:ro" \
//     -v "$OVERLAY/backend/asr-correct.ts:/src/backend/asr-correct.ts:ro" \
//     -v "$OVERLAY/backend/grammar-hook.ts:/src/backend/grammar-hook.ts:ro" IMAGE /src/backend/check_grammar_hook.mts
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'

interface Directive { Name?: null | string, Payload?: unknown }
type Result = null | { kind: string, listen?: boolean, quasarDirectives?: Directive[], text: null | string }
interface Layer {
  matchFastIntent: (text: string, options?: { dialogActive?: boolean, speakerLevel?: null | number }) => Promise<Result>
}
interface Protobuf { encodeProtobufStruct: (value: Record<string, unknown>) => unknown }

// The HA client of the fast layer is configured by env and pointed at the stub below (an empty home): nothing here
// ever reaches a real Home Assistant.
process.env.AISIS_HA_TOKEN = 'check-only'

interface Call { body: { dialogActive?: boolean, text?: string } }
const calls: Call[] = []
let answer: { delayMs?: number, body: unknown, status?: number } = { body: { route: 'none' } }
const server = createServer((request, response) => {
  if (request.url?.startsWith('/api/') === true) {
    response.writeHead(200, { 'Content-Type': 'application/json' })
    response.end(request.url === '/api/states' ? '[]' : '')
    return
  }
  let raw = ''
  request.on('data', chunk => { raw += String(chunk) })
  request.on('end', () => {
    calls.push({ body: JSON.parse(raw) as Call['body'] })
    setTimeout(() => {
      response.writeHead(answer.status ?? 200, { 'Content-Type': 'application/json' })
      response.end(typeof answer.body === 'string' ? answer.body : JSON.stringify(answer.body))
    }, answer.delayMs ?? 0)
  })
})
await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
process.env.AISIS_HA_URL = base
process.env.AISIS_ADAPTER_INTENT_URL = `${base}/intent/decide`

// The image compiles to CommonJS: named exports arrive on `default`.
const loaded = await import('./ha-intents.ts') as Layer & { default?: Layer }
const { matchFastIntent } = loaded.matchFastIntent === undefined ? loaded.default! : loaded
const protobuf = await import('../protobuf.ts') as Protobuf & { default?: Protobuf }
const { encodeProtobufStruct } = protobuf.encodeProtobufStruct === undefined ? protobuf.default! : protobuf

const route = (fields: Record<string, unknown>, extra: Partial<typeof answer> = {}) => {
  answer = { body: { intent: 'check', listen: false, ms: 1, text: null, volume: null, ...fields }, ...extra }
}
const names = (result: Result) => (result?.quasarDirectives ?? []).map(directive => directive.Name)
const level = (n: number) => JSON.stringify({ Name: 'sound_set_level', Payload: encodeProtobufStruct({ new_level: n, new_percent_level: n * 10 }) })
const levelsOf = (result: Result) => (result?.quasarDirectives ?? [])
  .filter(directive => directive.Name === 'sound_set_level').map(directive => JSON.stringify({ Name: directive.Name, Payload: directive.Payload }))
const MATH = 'сколько будет два плюс два'

let pass = 0
let total = 0
const failures: string[] = []
const check = (name: string, ok: boolean, detail = '') => {
  total++
  if (ok) pass++
  else failures.push(`${name} ${detail}`.trim())
}
const mathText = ((await (async () => { route({ route: 'none' }); return await matchFastIntent(MATH) })()) as Result)?.text
check('baseline math answer', typeof mathText === 'string' && mathText !== '', JSON.stringify(mathText))

// reply / silent / dialog_end / none
route({ route: 'reply', text: 'Сейчас десять часов.' })
let result = await matchFastIntent('который час')
check('reply', result?.kind === 'reply' && result.text === 'Сейчас десять часов.' && result.listen === undefined && names(result).length === 0, JSON.stringify(result))
route({ listen: true, route: 'reply', text: 'Какую комнату?' })
result = await matchFastIntent('включи')
check('reply listen', result?.kind === 'reply' && result.listen === true && result.text === 'Какую комнату?', JSON.stringify(result))
route({ route: 'reply', text: '  ' })
result = await matchFastIntent(MATH)
check('reply with empty text is ignored', result?.text === mathText, JSON.stringify(result))
route({ route: 'silent' })
result = await matchFastIntent('алиса')
check('silent', result?.kind === 'reply' && result.text === null && names(result).length === 0, JSON.stringify(result))
route({ route: 'dialog_end' })
result = await matchFastIntent('отстань')
check('dialog_end', result?.kind === 'reply' && result.text === null && names(result).length === 0, JSON.stringify(result))
route({ route: 'none' })
result = await matchFastIntent(MATH)
check('none keeps the old path', result?.text === mathText, JSON.stringify(result))
route({ route: 'surprise' })
result = await matchFastIntent(MATH)
check('unknown route keeps the old path', result?.text === mathText, JSON.stringify(result))

// volume: target, steps, deltaLevels with and without the speaker level
route({ route: 'volume', volume: { target: 0.7 } })
result = await matchFastIntent('громкость семь', { speakerLevel: 3 })
check('volume target', JSON.stringify(levelsOf(result)) === JSON.stringify([level(7)]) && result?.text === null, JSON.stringify(names(result)))
route({ route: 'volume', volume: { steps: 1 } })
result = await matchFastIntent('громче', { speakerLevel: 4 })
check('volume steps with speaker level', levelsOf(result).length === 1 && JSON.stringify(levelsOf(result)) !== JSON.stringify([level(4)]), JSON.stringify(names(result)))
route({ route: 'volume', volume: { steps: -1 } })
result = await matchFastIntent('тише', { speakerLevel: null })
check('volume steps without speaker level', names(result).filter(name => name === 'sound_quiter').length >= 1 && levelsOf(result).length === 0, JSON.stringify(names(result)))
route({ route: 'volume', volume: { deltaLevels: 2 } })
result = await matchFastIntent('чуть громче', { speakerLevel: 4 })
check('volume deltaLevels with speaker level', JSON.stringify(levelsOf(result)) === JSON.stringify([level(6)]), JSON.stringify(names(result)))
route({ route: 'volume', volume: { deltaLevels: 5 } })
result = await matchFastIntent('намного громче', { speakerLevel: 8 })
check('volume deltaLevels clamps to the scale', JSON.stringify(levelsOf(result)) === JSON.stringify([level(10)]), JSON.stringify(names(result)))
route({ route: 'volume', volume: { deltaLevels: -2 } })
result = await matchFastIntent('чуть тише', { speakerLevel: null })
check('volume deltaLevels without speaker level is a relative step', names(result).filter(name => name === 'sound_quiter').length >= 1 && levelsOf(result).length === 0, JSON.stringify(names(result)))
route({ route: 'volume', volume: null })
result = await matchFastIntent(MATH)
check('volume without a payload keeps the old path', result?.text === mathText, JSON.stringify(result))

// request shape
check('request body', calls.at(-1)?.body.text === MATH && calls.at(-1)?.body.dialogActive === false, JSON.stringify(calls.at(-1)))
route({ route: 'none' })
await matchFastIntent(MATH, { dialogActive: true })
check('dialogActive is sent', calls.at(-1)?.body.dialogActive === true, JSON.stringify(calls.at(-1)))

// fail open: timeout, http error, bad json
route({ route: 'reply', text: 'не должно прозвучать' }, { delayMs: 600 })
let started = Date.now()
result = await matchFastIntent(MATH)
let elapsed = Date.now() - started
check('timeout fails open and the old path runs', result?.text === mathText && elapsed >= 200 && elapsed < 560, `${elapsed} ms ${JSON.stringify(result)}`)
answer = { body: { route: 'reply', text: 'x' }, status: 500 }
result = await matchFastIntent(MATH)
check('http 500 fails open', result?.text === mathText, JSON.stringify(result))
answer = { body: 'not json' }
result = await matchFastIntent(MATH)
check('bad json fails open', result?.text === mathText, JSON.stringify(result))

// modes
route({ route: 'reply', text: 'shadow answer' })
process.env.GRAMMAR_HOOK = 'shadow'
let before = calls.length
result = await matchFastIntent(MATH)
check('shadow calls but never acts', calls.length === before + 1 && result?.text === mathText, JSON.stringify(result))
process.env.GRAMMAR_HOOK = 'off'
before = calls.length
result = await matchFastIntent(MATH)
check('off makes no call', calls.length === before && result?.text === mathText, JSON.stringify(result))
delete process.env.GRAMMAR_HOOK

// A pending clarification is answered BEFORE the hook: the hook is not called for the answer.
route({ route: 'none' })
result = await matchFastIntent('громкость')
check('incomplete slot asks a question', result?.kind === 'reply' && result.listen === true && typeof result.text === 'string', JSON.stringify(result))
route({ route: 'reply', text: 'hook must not answer this' })
before = calls.length
result = await matchFastIntent('семь', { speakerLevel: 3 })
check('pending answer runs before the hook', calls.length === before && JSON.stringify(levelsOf(result)) === JSON.stringify([level(7)]), `${calls.length - before} calls ${JSON.stringify(result)}`)

console.log(`ИТОГ ${pass}/${total}`)
for (const failure of failures) console.log('FAIL', failure)
server.close()
process.exit(pass === total ? 0 : 1)
