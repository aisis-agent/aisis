// A phrase without a room is about the room of the speaker that heard it: matchFastIntent() end to end against a
// mocked Home Assistant that has the real home's areas (no network, no device actions, no sound).
//   overlays/speaker-room/run_tests.sh   (applies the overlay to the active image's sources, runs every tests/*.mts in a
//   scratch container of that image)
import { rmSync, writeFileSync } from 'node:fs'

const AMBIGUITY_FILE = `/tmp/ha-ambiguity-speaker-room-${process.pid}.json`
const HA_STORE_FILE = `/tmp/ha-store-speaker-room-${process.pid}.json`
writeFileSync(HA_STORE_FILE, JSON.stringify({ auth: { kind: 'long_lived', token: 'test-token' }, client_id: '', connected_at: 0, name: 'test', url: 'http://ha.test', uuid: 'test', version: 1 }))
process.env.AISIS_HA_STORE = HA_STORE_FILE
process.on('exit', () => rmSync(HA_STORE_FILE, { force: true }))
process.env.AISIS_HA_AMBIGUITY_FILE = AMBIGUITY_FILE
process.env.SEMANTIC_INTENTS = 'off'
rmSync(AMBIGUITY_FILE, { force: true })

type Attrs = Record<string, unknown>
interface Ent { entity_id: string, state: string, attributes: Attrs }

// The home as Home Assistant reports it on 2026-11-16 (areas of the entities that matter here).
const areaOf: Record<string, string> = {
  'light.gostinaia_globus_socket_1': 'Гостиная', 'light.liustra': 'Спальня', 'light.kukhnia': 'Кухня',
  'light.studiia': 'Студия', 'light.studio_sw': 'Студия',
  'light.k1se_85b6_light': 'Студия', 'light.snapmaker_u1_led_cavity_led': 'Студия', // junk lights: never part of «свет»
  'light.motion_light_motion_light': 'Прихожая',
  'climate.kondei_1': 'Гостиная', 'climate.kondei_2': 'Второй этаж',
  // one player per station, named after its device id
  'media_player.aisis_speaker_m00zrw3008rajg': 'Студия',
  'media_player.yandex_station_m0011hm00z86hg': 'Гостиная',
  'media_player.stantsiia_mini_gostinaia': 'Гостиная',
  'media_player.yandex_station_m10dst310hxnpk': 'Спальня',
  'media_player.yandex_station_m0floor2': 'Второй этаж',
  'media_player.yandex_station_m0corner0': 'Угол',
  // the Studio speaker's player also exists under the Yandex integration, in another area: the AISIS one must win
  'media_player.yandex_station_m00zrw3008rajg': 'Кухня'
}
const STUDIO = 'M00ZRW3008RAJG'
const LIVING = 'M0011HM00Z86HG'
const BEDROOM = 'M10DST310HXNPK'
const SECOND_FLOOR = 'M0FLOOR2'
const CORNER = 'M0CORNER0'

const entities = new Map<string, Ent>()
for (const id of Object.keys(areaOf)) {
  const attrs: Attrs = { friendly_name: id }
  if (id.startsWith('climate.')) Object.assign(attrs, { min_temp: 16, max_temp: 30, temperature: 25 })
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
      if (url.pathname.endsWith('/turn_on')) {
        entity.state = 'on'
        if (typeof body.brightness_pct === 'number') entity.attributes.brightness = Math.round(body.brightness_pct * 2.55)
      }
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
const STUDIO_LIGHTS = ['light.studiia', 'light.studio_sw']
const touched = (): string[] => [...new Set(calls.map(call => String(call.body.entity_id)))].sort()
// Every light starts off, or on for a «выключи» (switching off what is already off is answered, not executed).
async function say (text: string, deviceId?: string) {
  calls.length = 0
  for (const entity of entities.values()) if (entity.entity_id.startsWith('light.')) entity.state = text.startsWith('выключи') ? 'on' : 'off'
  return await matchFastIntent(text, deviceId === undefined ? {} : { deviceId })
}

// 1. The reported bug: «включи свет» said to the Studio speaker lights the studio, never the living room.
{
  const result = await say('включи свет', STUDIO)
  check('Studio: «включи свет» is silent', result?.kind === 'reply' && result.text === null, result)
  check('Studio: «включи свет» turns on exactly the studio lights', JSON.stringify(touched()) === JSON.stringify(STUDIO_LIGHTS), touched())
  check('Studio: every call is turn_on', calls.every(call => call.path === '/api/services/light/turn_on'), calls)
  // The second try (the owner repeated it): the studio lights are on, it runs again and never claims the living room.
  calls.length = 0
  const again = await matchFastIntent('включи свет', { deviceId: STUDIO })
  check('Studio: the repeat never speaks about the living room', !(again?.text ?? '').includes('гостиной'), again)
  check('Studio: the repeat still targets only the studio lights', calls.every(call => STUDIO_LIGHTS.includes(String(call.body.entity_id))), calls)
  const off = await say('выключи свет', STUDIO)
  check('Studio: «выключи свет» turns the studio lights off', off?.text === null && JSON.stringify(touched()) === JSON.stringify(STUDIO_LIGHTS) &&
    calls.every(call => call.path === '/api/services/light/turn_off'), calls)
}

// 2. Every other speaker acts in its own room.
{
  await say('включи свет', LIVING)
  check('Living room speaker: the globus', JSON.stringify(touched()) === '["light.gostinaia_globus_socket_1"]', touched())
  await say('включи свет', BEDROOM)
  check('Bedroom speaker: the bedroom chandelier', JSON.stringify(touched()) === '["light.liustra"]', touched())
}

// 3. An explicit room in the phrase beats the room of the speaker.
{
  await say('включи свет на кухне', STUDIO)
  check('Studio speaker + «на кухне»: the kitchen light', JSON.stringify(touched()) === '["light.kukhnia"]', touched())
  await say('выключи свет в гостиной', STUDIO)
  check('Studio speaker + «в гостиной»: the living room globus', JSON.stringify(touched()) === '["light.gostinaia_globus_socket_1"]', touched())
  await say('включи свет в студии', LIVING)
  check('Living room speaker + «в студии»: the studio lights', JSON.stringify(touched()) === JSON.stringify(STUDIO_LIGHTS), touched())
}

// 4. No station, a station HA does not know, or one in an area that is not a room of the home: the previous default.
{
  await say('включи свет')
  check('no station: the living room (previous default)', JSON.stringify(touched()) === '["light.gostinaia_globus_socket_1"]', touched())
  await say('включи свет', 'M0UNKNOWN0')
  check('unknown station: the living room (previous default)', JSON.stringify(touched()) === '["light.gostinaia_globus_socket_1"]', touched())
  await say('включи свет', CORNER)
  check('station in a non-room area: the living room (previous default)', JSON.stringify(touched()) === '["light.gostinaia_globus_socket_1"]', touched())
  await say('включи свет', 'm00zrw3008rajg')
  check('device id case does not matter', JSON.stringify(touched()) === JSON.stringify(STUDIO_LIGHTS), touched())
}

// 5. The catalog path (a set-point instead of a bare switch-on) uses the speaker's room too.
{
  await say('яркость 30', STUDIO)
  check('Studio: «яркость 30» sets the studio lights', calls.length > 0 && calls.every(call => STUDIO_LIGHTS.includes(String(call.body.entity_id))) &&
    calls.every(call => call.body.brightness_pct === 30), calls)
  await say('яркость 30', LIVING)
  check('Living room speaker: «яркость 30» sets the globus', JSON.stringify(touched()) === '["light.gostinaia_globus_socket_1"]', calls)
  await say('яркость на кухне 30', STUDIO)
  check('Studio speaker + «на кухне»: «яркость на кухне 30» sets the kitchen', JSON.stringify(touched()) === '["light.kukhnia"]', calls)
}

// 6. The AC follows the same rule: the speaker's room when it has one, else the first AC.
{
  await say('включи кондиционер', SECOND_FLOOR)
  check('Second floor speaker: the second floor AC', JSON.stringify(touched()) === '["climate.kondei_2"]', touched())
  await say('включи кондиционер', LIVING)
  check('Living room speaker: the living room AC', JSON.stringify(touched()) === '["climate.kondei_1"]', touched())
  await say('включи кондиционер', STUDIO)
  check('Studio speaker (no AC in the studio): the first AC as before', JSON.stringify(touched()) === '["climate.kondei_1"]', touched())
  await say('включи кондиционер в гостиной', SECOND_FLOOR)
  check('Second floor speaker + «в гостиной»: the living room AC', JSON.stringify(touched()) === '["climate.kondei_1"]', touched())
}

// 7. speakerArea(): how a station id finds its room in the registry.
{
  const speakerRoom = await import('/src/backend/speaker-room.ts') as { speakerArea?: typeof import('/src/backend/speaker-room.ts').speakerArea, default?: { speakerArea: typeof import('/src/backend/speaker-room.ts').speakerArea } }
  const speakerArea = speakerRoom.speakerArea ?? speakerRoom.default!.speakerArea
  const areas = new Map(Object.entries(areaOf))
  check('speakerArea: the AISIS speaker player wins over the Yandex integration one', speakerArea(areas, STUDIO) === 'Студия', speakerArea(areas, STUDIO))
  check('speakerArea: the Yandex integration player is used when it is the only one', speakerArea(areas, BEDROOM) === 'Спальня', speakerArea(areas, BEDROOM))
  check('speakerArea: lower-case id', speakerArea(areas, STUDIO.toLowerCase()) === 'Студия', null)
  check('speakerArea: unknown station', speakerArea(areas, 'M0NOBODY0') === undefined, null)
  check('speakerArea: no id', speakerArea(areas, undefined) === undefined && speakerArea(areas, '') === undefined && speakerArea(areas, '  ') === undefined, null)
  check('speakerArea: only media players count (a sensor named after the station is not its room)',
    speakerArea(new Map([['sensor.aisis_speaker_m0sensor0_connected', 'Кухня'], ['sensor.yandex_station_m0sensor0', 'Кухня']]), 'M0SENSOR0') === undefined, null)
  check('speakerArea: an id is matched whole, never as the tail of a longer one',
    speakerArea(new Map([['media_player.yandex_station_xm00zrw3008rajg', 'Кухня']]), STUDIO) === undefined, null)
}

rmSync(AMBIGUITY_FILE, { force: true })
console.log(`\nКОМНАТА ГОВОРЯЩЕГО: ${total - failed}/${total}`)
process.exit(failed === 0 ? 0 : 1)
