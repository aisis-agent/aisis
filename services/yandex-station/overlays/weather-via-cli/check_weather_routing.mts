// Run inside a quasar image with the overlay mounted over /src (README.md): a Home Assistant weather entity is
// present, yet weather questions must come back as "none" so the adapter (weather-cli) answers them.
//   docker run --rm --entrypoint /node_modules/.bin/tsx -v "$PWD/check_weather_routing.mts:/src/backend/check_weather_routing.mts:ro" \
//     -v "$PWD/diverted_phrases.json:/src/backend/diverted_phrases.json:ro" \
//     -v "$OVERLAY/backend/entity-index.ts:/src/backend/entity-index.ts:ro" IMAGE /src/backend/check_weather_routing.mts
import { readFileSync } from 'node:fs'

interface EntityLike {
  attributes: Record<string, unknown>
  entity_id: string
  state: string
}
interface Layer {
  matchEntityQuery: (text: string, entities: Map<string, EntityLike>, areas: Map<string, string>, now: Date) => { kind: string, text: string }
}
// The image compiles to CommonJS: named exports arrive on `default`.
const loaded = await import('./entity-index.ts') as Layer & { default?: Layer }
const { matchEntityQuery } = loaded.matchEntityQuery === undefined ? loaded.default! : loaded

const entities = new Map<string, EntityLike>([
  ['weather.forecast_home', { attributes: { friendly_name: 'Forecast Home', humidity: 71, temperature: 12.9, wind_speed: 20 },
    entity_id: 'weather.forecast_home', state: 'rainy' }],
  ['sensor.outdoor_humidity', { attributes: { friendly_name: 'Влажность на улице', unit_of_measurement: '%' },
    entity_id: 'sensor.outdoor_humidity', state: '71' }]
])
const areas = new Map<string, string>()
const ask = (text: string) => matchEntityQuery(text, entities, areas, new Date('2026-11-16T13:54:00Z'))

// Weather and outdoor temperature: not answered here. The same list is checked against the adapter's parser
// (scripts: test_weather_skills.py), so a phrase can never be diverted without an answer behind it.
const table = JSON.parse(readFileSync(new URL('./diverted_phrases.json', import.meta.url), 'utf8')) as { lead_ins: string[], phrases: string[] }
// Every entry, bare and with each lead-in word and punctuation.
const toAdapter: string[] = table.phrases.flatMap(phrase => [phrase, `${table.lead_ins[0]}, ${phrase}?`])
// Everything else keeps its entity answer: other measures, and weather phrasings outside the table.
const stays = ['какая влажность на улице', 'какая погода и влажность на улице', 'какое давление на улице',
  'какая погода на улице при этом']

let pass = 0
const failures: string[] = []
for (const text of toAdapter) {
  const result = ask(text)
  if (result.kind === 'none') pass++
  else failures.push(`«${text}» -> ${result.kind}: ${result.text}`)
}
for (const text of stays) {
  const result = ask(text)
  if (result.kind === 'answer') pass++
  else failures.push(`«${text}» expected an answer, got ${result.kind}`)
}
const total = toAdapter.length + stays.length
console.log(`ИТОГ ${pass}/${total}`)
for (const failure of failures) console.log('FAIL', failure)
process.exit(pass === total ? 0 : 1)
