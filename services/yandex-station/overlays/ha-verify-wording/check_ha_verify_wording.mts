// Run inside a quasar image with the overlay mounted over /src/backend (README.md). The fast layer is the REAL one; ha-cli (the
// "Home Assistant" the layer talks to) is a stub on a loopback port inside the throwaway container (no network) that answers
// a service call the way the new ha-cli does: status + X-Aisis-Verify / -Verify-Reason / -Down headers.
//   docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
//     -v "$PWD/check_ha_verify_wording.mts:/src/backend/check_ha_verify_wording.mts:ro" \
//     -v "$OVERLAY/backend/ha-intents.ts:/src/backend/ha-intents.ts:ro" "$IMAGE" /src/backend/check_ha_verify_wording.mts
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'

type Result = null | { kind: string, listen?: boolean, quasarDirectives?: unknown[], text: null | string }
interface Layer { haRecoveryText?: (down: string | undefined) => string, matchFastIntent: (text: string, options?: object) => Promise<Result> }
interface Reply { delayMs?: number, headers?: Record<string, string>, status: number }

process.env.AISIS_HA_TOKEN = 'check-only'

const lights: Record<string, { area: string, name: string, state: string }> = {
  'light.gostinaya': { area: 'Гостиная', name: 'Гостиная', state: 'on' },
  'light.kukhnya': { area: 'Кухня', name: 'Кухня', state: 'on' }
}
const serviceCalls: string[] = []
let serviceReply: Reply & { delayMs?: number } = { status: 200 }

const entity = (id: string) => ({ attributes: { friendly_name: lights[id].name }, entity_id: id, state: lights[id].state })
const server = createServer((request, response) => {
  let raw = ''
  request.on('data', chunk => { raw += String(chunk) })
  request.on('end', () => {
    const url = request.url ?? ''
    const json = (value: unknown) => { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)) }
    if (url === '/intent/decide') return json({ route: 'none' })
    if (url === '/api/states') return json(Object.keys(lights).map(entity))
    if (url === '/api/template') {
      response.writeHead(200)
      response.end(Object.entries(lights).map(([id, light]) => `${id}\t${light.name}\t${light.area}\n`).join(''))
      return
    }
    const one = /^\/api\/states\/(light\.\w+)$/.exec(url)
    if (one !== null && lights[one[1]] !== undefined) return json(entity(one[1]))
    if (/^\/api\/services\//.test(url)) {
      serviceCalls.push(`${url} ${raw}`)
      // A verified call really changes the light (what a 200 from ha-cli means); every other answer leaves it alone.
      const turn = /\/light\/turn_(on|off)$/.exec(url)
      const body = raw === '' ? {} : JSON.parse(raw) as { entity_id?: string }
      if (serviceReply.status === 200 && turn !== null) {
        const ids = body.entity_id === 'all' ? Object.keys(lights) : [body.entity_id ?? '']
        for (const id of ids) if (lights[id] !== undefined) lights[id].state = turn[1]
      }
      const reply = serviceReply
      setTimeout(() => {
        response.writeHead(reply.status, { 'Content-Type': 'application/json', ...reply.headers })
        response.end('[]')
      }, reply.delayMs ?? 0)
      return
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
const layer = loaded.matchFastIntent === undefined ? loaded.default! : loaded
const { matchFastIntent } = layer

let pass = 0
let total = 0
const failures: string[] = []
const check = (name: string, ok: boolean, detail = '') => {
  total++
  if (ok) pass++
  else failures.push(`${name} ${detail}`.trim())
}
const show = (result: Result) => JSON.stringify(result)
const spoken: string[] = []
const ask = async (reply: Reply, phrase = 'выключи свет'): Promise<Result> => {
  lights['light.gostinaya'].state = 'on'
  lights['light.kukhnya'].state = 'on'
  serviceCalls.length = 0
  serviceReply = reply
  const result = await matchFastIntent(phrase)
  if (result?.text) spoken.push(result.text)
  return result
}
const says = (result: Result, text: string) => result?.kind === 'reply' && result.text === text && result.listen === undefined

const STARTING = 'Home Assistant запускается, команду выполню, когда он вернётся.'
const DOWN = 'Home Assistant недоступен, команду выполню, когда он вернётся.'
const NO_EVIDENCE = 'Не получилось проверить ответ Home Assistant, команда ждёт в очереди.'
const UNKNOWN = 'Не получилось проверить ответ Home Assistant.'
const NOT_DONE = 'Команду отправил, но не вижу, чтобы устройство её выполнило.'
const UNAVAILABLE = 'Home Assistant видит это устройство недоступным.'
const TIMEOUT = 'Home Assistant не ответил вовремя, результат команды не проверен.'
const REAL_ERROR = 'Не удалось выполнить команду умного дома.'

// ── 202: ha-cli OBSERVED Home Assistant gone ───────────────────────────────────────────────────────────────────────
for (const down of ['refused', 'reset', 'dns', 'unreachable']) {
  const result = await ask({ headers: { 'x-aisis-down': down, 'x-aisis-eta': '40' }, status: 202 })
  check(`202 down=${down}: says unavailable, no ETA, no reboot`, says(result, DOWN), show(result))
  check(`202 down=${down}: the service call was made once`, serviceCalls.length === 1, serviceCalls.join())
}
let result = await ask({ headers: { 'x-aisis-down': 'starting' }, status: 202 })
check('202 down=starting: says it is starting', says(result, STARTING), show(result))
result = await ask({ status: 202 })
check('202 without evidence (an old ha-cli queued on a timeout): says it could not check', says(result, NO_EVIDENCE), show(result))
check('haRecoveryText is exported: three texts', layer.haRecoveryText?.('starting') === STARTING && layer.haRecoveryText('refused') === DOWN && layer.haRecoveryText(undefined) === NO_EVIDENCE)

// ── 504 / 502: the verdict of the verification, not a guess ────────────────────────────────────────────────────────
result = await ask({ headers: { 'x-aisis-verify': 'unknown', 'x-aisis-verify-reason': 'no_state' }, status: 504 })
check('504 unknown: says it could not check, not «не удалось выполнить»', says(result, UNKNOWN), show(result))
result = await ask({ headers: { 'x-aisis-verify': 'failed', 'x-aisis-verify-reason': 'state_unchanged' }, status: 502 })
check('502 failed: says the command was sent but not seen done', says(result, NOT_DONE), show(result))
result = await ask({ headers: { 'x-aisis-verify': 'failed', 'x-aisis-verify-reason': 'device_unavailable' }, status: 502 })
check('502 failed device_unavailable: says Home Assistant sees the device unavailable', says(result, UNAVAILABLE), show(result))
result = await ask({ headers: { 'x-aisis-verify': 'failed' }, status: 502 })
check('502 failed without a reason: still the observed wording', says(result, NOT_DONE), show(result))
result = await ask({ status: 500 })
check('a plain HTTP error without a verdict keeps «не удалось выполнить»', says(result, REAL_ERROR), show(result))
result = await ask({ headers: { 'x-aisis-verify': 'skipped' }, status: 400 })
check('a rejected request (verify skipped) keeps «не удалось выполнить»', says(result, REAL_ERROR), show(result))

// ── 200: verified, the earcon as before ────────────────────────────────────────────────────────────────────────────
result = await ask({ headers: { 'x-aisis-verify': 'ok' }, status: 200 })
check('200 ok: a confirming earcon and no text', result?.kind === 'reply' && result.text === null && (result.quasarDirectives?.length ?? 0) > 0, show(result))

// ── the other call sites ───────────────────────────────────────────────────────────────────────────────────────────
const SITES: Array<[string, string, string]> = [
  ['выключи весь свет', 'whole domain', '/api/services/light/turn_off {"entity_id":"all"}'],
  ['яркость 50', 'set-point', '/api/services/light/turn_on'],
  ['включи ютуб на телевизоре', 'TV application', '/api/services/script/turn_on'],
  ['включи камин на проекторе', 'projector fireplace', '/api/services/script/turn_on']
]
for (const [phrase, label, expected] of SITES) {
  result = await ask({ headers: { 'x-aisis-down': 'refused' }, status: 202 }, phrase)
  check(`${label}: 202 says unavailable`, says(result, DOWN), `${phrase}: ${show(result)}`)
  check(`${label}: reached the expected call site`, serviceCalls.some(call => call.startsWith(expected)), `${phrase}: ${serviceCalls.join(' | ')}`)
  result = await ask({ headers: { 'x-aisis-verify': 'unknown', 'x-aisis-verify-reason': 'no_state' }, status: 504 }, phrase)
  check(`${label}: 504 unknown says it could not check`, says(result, UNKNOWN), `${phrase}: ${show(result)}`)
}

// ── haFetch gives up (3 s): nothing was observed, no failure is claimed ───────────────────────────────────────────
result = await ask({ delayMs: 3500, status: 200 })
check('a timed-out call says it did not answer in time, not «не удалось выполнить»', says(result, TIMEOUT), show(result))

check('nothing the speaker said claims a reboot', spoken.every(text => !/перезагруж/i.test(text)), spoken.join(' | '))

server.close()
console.log(`ИТОГ ${pass}/${total}`)
for (const failure of failures) console.log(`ПРОВАЛ ${failure}`)
process.exit(failures.length === 0 ? 0 : 1)
