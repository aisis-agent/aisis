// What the deployed quasar layer would answer WITHOUT a model for each phrase of the labelled corpora, as one JSON line
// per phrase: {"text", "handled", "via", "action"}; `action` is what the handler resolved the phrase to (the device and state, the volume
// step, the media key, ...) so a reader can tell «включи свет» from «выключи свет». The grammar back-test reads the result (quasar-replay.jsonl next to the
// corpora) to know which phrases the container already serves, so the "combined route" number is the production route
// and not a guess from an older build's log.
//
// Pure text, no network, no Home Assistant, no device: the same stateless parsers the audit replay used (no pending
// question, no repeat history, no music-library lookup: a phrase the music parser accepts counts as handled, which is
// optimistic when the artist is not in the library). Run it in a throwaway container of the ACTIVE image:
//
//   IMAGE=$(docker inspect --format '{{.Config.Image}}' aisis-quasar-prod-<active colour>)
//   docker run --rm --network none --entrypoint /node_modules/.bin/tsx \
//     -v "$PWD/replay_quasar.mts:/src/backend/replay_quasar.mts:ro" -v "$CORPUS:/corpus:ro" "$IMAGE" \
//     /src/backend/replay_quasar.mts /corpus > "$CORPUS/quasar-replay.jsonl"
//
// Phrases come from real-voice-*.jsonl, *-labelled.jsonl and intents-ru.jsonl in the directory (argument 1).
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
// Default imports: the backend is CommonJS to tsx, and a named import of its exports fails in an ES module script.
import endDialog from './end-dialog'
import ha from './ha-intents'
import musicParse from './music-parse'
import tvApps from './tv-app-intents'

// Fixture registry: the rooms and devices of the real instance (mirrors corpus/run_corpus.mts).
const areaBy: Array<[string, string[]]> = [
  ['Гостиная', ['light.gostinaia_potolok', 'light.liustra', 'light.girlianda', 'light.gostinaia_globus_socket_1',
    'media_player.televizor', 'media_player.gostinaia', 'climate.kondei_1', 'media_player.mini_gostinaia', 'switch.globus3']],
  ['Кухня', ['light.kukhnia_svetilnik', 'switch.kettle_plug_local']],
  ['Спальня', ['light.spalnia_nochnik']],
  ['Студия', ['light.studiia_lampa']],
  ['Прихожая', ['light.prikhozhaia']],
  ['Второй этаж', ['light.vtoroi_etazh', 'climate.kondei_2', 'media_player.projector']]
]
const areaByEntity = new Map<string, string>()
const entities = new Map<string, unknown>()
for (const [area, ids] of areaBy) {
  for (const id of ids) {
    areaByEntity.set(id, area)
    entities.set(id, { attributes: { friendly_name: id }, entity_id: id, state: 'off' })
  }
}
const reg = { areaByEntity, entities, fetchedAt: Date.now() } as never

function handledBy (text: string): { via: string, action: unknown } | null {
  const endDialogAction = endDialog.parseEndDialog(text)
  if (endDialogAction !== null) return { via: 'end-dialog', action: endDialogAction }
  const math = ha.evaluateMath(text.toLowerCase().replace(/ё/g, 'е'))
  if (math !== null) return { via: 'math', action: math }
  const tvApp = tvApps.parseTvAppIntent(text)
  if (tvApp !== null) return { via: 'tv-app', action: tvApp }
  const fireplace = tvApps.parseFireplaceIntent(text)
  if (fireplace !== null) return { via: 'fireplace', action: fireplace }
  const mediaKey = musicParse.parseMediaControl(text)
  if (mediaKey !== null) return { via: 'media-key', action: mediaKey }
  const decision = ha.decideFastIntent(text, reg)
  if (decision.tier !== 'llm' && decision.tier !== 'math') return { via: `fast-${decision.tier}`, action: decision }   // the whole decision: a state query carries its scope outside `resolved`
  const music = musicParse.parseMusicIntent(text)
  const bareName = music !== null ? music : musicParse.parseBareMusicName(text)
  if (music !== null || bareName !== null) return { via: 'music', action: bareName }
  return null
}

const dir = process.argv[2] ?? '.'
const seen = new Set<string>()
for (const name of readdirSync(dir).filter(file => file.endsWith('.jsonl') && !file.startsWith('quasar-replay')).sort()) {
  for (const line of readFileSync(join(dir, name), 'utf8').split('\n')) {
    if (line.trim() === '') continue
    const text = String((JSON.parse(line) as { text?: unknown }).text ?? '').trim()
    if (text === '' || seen.has(text)) continue
    seen.add(text)
    let handled: { via: string, action: unknown } | null = null
    try { handled = handledBy(text) } catch { handled = null }
    console.log(JSON.stringify({ handled: handled !== null, text, via: handled?.via ?? null, action: handled?.action ?? null }))
  }
}
