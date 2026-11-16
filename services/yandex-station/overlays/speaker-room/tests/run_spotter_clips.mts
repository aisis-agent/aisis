// The recordings of commands recognised on the speaker (spotter-clips.ts): which streams become a clip, which never do,
// and that a dead gateway cannot break a turn.
//   overlays/speaker-room/run_tests.sh   (applies the overlay to the active image's sources, runs every tests/*.mts in a
//   scratch container of that image)
import type { SpotterClip } from '/src/routers/uniproxy/spotter-clips.ts'

interface Module {
  SpotterClipRecorder: new (deviceId: string, sink: (clip: SpotterClip) => Promise<void>, now?: () => number) => {
    addAudio: (streamId: number, frame: Buffer) => void
    announce: (streamId: number, spotter: { source: string, transcript: string }) => void
    flush: () => void
  }
  gatewayClipUrl: (env: NodeJS.ProcessEnv) => string
}
const loaded = await import('/src/routers/uniproxy/spotter-clips.ts') as Module & { default?: Module }
const { SpotterClipRecorder, gatewayClipUrl } = loaded.SpotterClipRecorder === undefined ? loaded.default! : loaded

let failed = 0
let total = 0
function check (name: string, ok: boolean, detail: unknown): void {
  total++
  if (!ok) { failed++; console.log(`  ✗ ${name}: ${JSON.stringify(detail)}`) }
}
const COMMANDS = 'ru-RU-quasar-commands-10Feb25-08-25-shrd-bt-likes-q16-rt-lower-fah-lower-stops-dislikes-fah-dqmm'
const WAKE = 'ru-RU-minispeakers-alisa-11Jun25-mini2-i8-new-flags'
const sent: SpotterClip[] = []
const settle = async (): Promise<void> => await new Promise(resolve => setImmediate(resolve))
function recorder (sink: (clip: SpotterClip) => Promise<void> = async clip => { sent.push(clip) }, now?: () => number) {
  sent.length = 0
  return new SpotterClipRecorder('M00ZRW3008RAJG', sink, now)
}

// 1. The 2026-11-16 15:53 shape: one LogSpotter per mic channel (streams 1 and 3), then the audio of each.
{
  const spotter = recorder()
  spotter.announce(1, { source: COMMANDS, transcript: 'включи свет' })
  spotter.addAudio(1, Buffer.alloc(841, 1))
  spotter.announce(3, { source: COMMANDS, transcript: 'включи свет' })
  spotter.addAudio(3, Buffer.alloc(841, 3))
  spotter.addAudio(1, Buffer.alloc(300, 1))
  spotter.addAudio(1, Buffer.alloc(300, 1))
  spotter.addAudio(3, Buffer.alloc(100, 3))
  spotter.flush()
  await settle()
  check('one clip for the command, not one per channel', sent.length === 1, sent.length)
  check('the channel with the most audio wins', sent[0]?.audio.length === 1441 && sent[0].audio[0] === 1, sent[0]?.audio.length)
  check('the clip carries the transcript, the station and the frame count', sent[0]?.transcript === 'включи свет' &&
    sent[0].deviceId === 'M00ZRW3008RAJG' && sent[0].frames === 3 && sent[0].source === COMMANDS, sent[0])
  spotter.flush()
  await settle()
  check('a flushed command is not posted twice', sent.length === 1, sent.length)
}

// 2. Two different commands on one connection stay two clips.
{
  const spotter = recorder()
  spotter.announce(1, { source: COMMANDS, transcript: 'включи свет' })
  spotter.addAudio(1, Buffer.alloc(500))
  spotter.announce(5, { source: COMMANDS, transcript: 'громче' })
  spotter.addAudio(5, Buffer.alloc(400))
  spotter.flush()
  await settle()
  check('two commands: two clips', sent.map(clip => clip.transcript).sort().join('|') === 'включи свет|громче', sent.map(clip => clip.transcript))
}

// 3. Never a clip: the wake word (its voice turn is recorded by the gateway), an empty transcript, a stream nobody announced,
//    an announced stream the station sent no audio for.
{
  const spotter = recorder()
  spotter.announce(1, { source: WAKE, transcript: 'алиса' })
  spotter.addAudio(1, Buffer.alloc(800))
  spotter.announce(2, { source: COMMANDS, transcript: '   ' })
  spotter.addAudio(2, Buffer.alloc(800))
  spotter.addAudio(7, Buffer.alloc(800))
  spotter.announce(9, { source: COMMANDS, transcript: 'пауза' })
  spotter.flush()
  await settle()
  check('wake word, empty transcript, unannounced stream and an audio-less announce: nothing posted', sent.length === 0, sent)
}

// 4. The audio may arrive before its LogSpotter event.
{
  const spotter = recorder()
  spotter.addAudio(1, Buffer.alloc(700, 7))
  spotter.announce(1, { source: COMMANDS, transcript: 'выключи свет' })
  spotter.flush()
  await settle()
  check('audio first, announce second: still one clip', sent.length === 1 && sent[0].audio.length === 700, sent)
}

// 5. A gateway that is down or refuses never throws into the turn.
{
  const spotter = recorder(async () => { throw new Error('gateway answered 503') })
  spotter.announce(1, { source: COMMANDS, transcript: 'включи свет' })
  spotter.addAudio(1, Buffer.alloc(500))
  let threw = false
  try { spotter.flush(); await settle() } catch { threw = true }
  check('a failing sink does not throw', !threw, null)
}

// 6. One stream is capped (a runaway stream cannot eat the memory), the clip is still posted.
{
  const spotter = recorder()
  spotter.announce(1, { source: COMMANDS, transcript: 'включи свет' })
  for (let i = 0; i < 40; i++) spotter.addAudio(1, Buffer.alloc(32 * 1024))
  spotter.flush()
  await settle()
  check('capped stream: posted at most 512 KiB', sent.length === 1 && sent[0].audio.length <= 512 * 1024 && sent[0].frames === 40, sent[0]?.audio.length)
}

// 7. The idle timer flushes by itself (the station keeps the websocket open after a command).
{
  const spotter = recorder()
  spotter.announce(1, { source: COMMANDS, transcript: 'включи свет' })
  spotter.addAudio(1, Buffer.alloc(500))
  check('nothing is posted before the stream goes idle', sent.length === 0, sent.length)
  await new Promise(resolve => setTimeout(resolve, 1700))
  check('idle for 1.2 s: posted', sent.length === 1, sent.length)
}

// 7b. The same command twice before the idle flush is two utterances, each with its own mic channels: two clips, the
//     louder channel of each.
{
  let clock = 1_000
  const spotter = recorder(undefined, () => clock)
  spotter.announce(1, { source: COMMANDS, transcript: 'громче' })
  spotter.addAudio(1, Buffer.alloc(300, 1))
  spotter.announce(3, { source: COMMANDS, transcript: 'громче' })
  spotter.addAudio(3, Buffer.alloc(500, 3))
  clock += 700
  spotter.announce(5, { source: COMMANDS, transcript: 'громче' })
  spotter.addAudio(5, Buffer.alloc(400, 5))
  spotter.announce(7, { source: COMMANDS, transcript: 'громче' })
  spotter.addAudio(7, Buffer.alloc(200, 7))
  spotter.flush()
  await settle()
  check('repeated command: two clips', sent.length === 2, sent.length)
  check('each utterance keeps its own loudest channel', sent.map(clip => clip.audio[0]).sort().join() === '3,5', sent.map(clip => clip.audio[0]))
}

// 7c. A burst of commands that never goes idle (every frame re-arms the timer): the stream limit flushes what is complete
//     instead of dropping it, so nine commands with two mic channels each are nine clips.
{
  let clock = 1_000
  const spotter = recorder(undefined, () => clock)
  for (let command = 0; command < 9; command++) {
    spotter.announce(command * 2, { source: COMMANDS, transcript: `команда ${command}` })
    spotter.addAudio(command * 2, Buffer.alloc(300, command + 1))
    spotter.announce(command * 2 + 1, { source: COMMANDS, transcript: `команда ${command}` })
    spotter.addAudio(command * 2 + 1, Buffer.alloc(200, command + 1))
    clock += 700
    await settle()
  }
  spotter.flush()
  await settle()
  check('a burst of nine commands: nine clips', sent.length === 9, sent.map(clip => clip.transcript))
}

// 7d. Empty frames carry nothing: not retained, not counted, and they cannot hold a stream open or fill memory.
{
  const spotter = recorder()
  spotter.announce(1, { source: COMMANDS, transcript: 'включи свет' })
  for (let i = 0; i < 100_000; i++) spotter.addAudio(1, Buffer.alloc(0))
  spotter.addAudio(1, Buffer.alloc(500))
  for (let i = 0; i < 5_000; i++) spotter.addAudio(1, Buffer.alloc(1))
  spotter.flush()
  await settle()
  check('empty frames are ignored, chunks are capped', sent.length === 1 && sent[0].frames === 5_001 && sent[0].audio.length === 500 + 2047, [sent[0]?.frames, sent[0]?.audio.length])
}

// 7e. A gateway that accepts the request and never answers: at most eight recordings wait for it, the rest are dropped
//     (a stalled gateway cannot grow the process without bound).
{
  let started = 0
  let clock = 1_000
  const spotter = recorder(async () => { started++; await new Promise(() => {}) }, () => clock)
  for (let command = 0; command < 11; command++) {
    spotter.announce(command, { source: COMMANDS, transcript: `команда ${command}` })
    spotter.addAudio(command, Buffer.alloc(100, command + 1))
    clock += 700
  }
  spotter.flush()
  await settle()
  check('a stalled gateway: eight uploads started, three dropped', started === 8, started)
}

// 7f. Making room for a new stream evicts the oldest utterance only; a stream that is still arriving is not touched.
{
  const spotter = recorder()
  spotter.announce(0, { source: COMMANDS, transcript: 'включи свет' })
  spotter.addAudio(0, Buffer.alloc(300, 9))
  for (let stream = 1; stream <= 16; stream++) spotter.addAudio(stream, Buffer.alloc(50))
  await settle()
  check('the oldest announced stream was posted when stream 17 opened', sent.length === 1 && sent[0].transcript === 'включи свет', sent.map(clip => clip.transcript))
  spotter.announce(5, { source: COMMANDS, transcript: 'громче' })
  spotter.flush()
  await settle()
  check('a stream already open keeps its audio and its announcement', sent.length === 2 && sent[1].transcript === 'громче' && sent[1].audio.length === 50, sent.map(clip => clip.audio.length))
}

// 8. Where the gateway lives.
{
  check('default: the STT gateway on loopback', gatewayClipUrl({}) === 'http://127.0.0.1:31121/spotter-clip', gatewayClipUrl({}))
  check('derived from the STT websocket url', gatewayClipUrl({ STT_GIGAAM_URL: 'ws://127.0.0.1:9999' }) === 'http://127.0.0.1:9999/spotter-clip', null)
  check('explicit override', gatewayClipUrl({ AISIS_SPOTTER_CLIP_URL: 'http://x.test/clip' }) === 'http://x.test/clip', null)
  check('a trailing slash does not double', gatewayClipUrl({ STT_GIGAAM_URL: 'ws://127.0.0.1:31121/' }) === 'http://127.0.0.1:31121/spotter-clip', gatewayClipUrl({ STT_GIGAAM_URL: 'ws://127.0.0.1:31121/' }))
  check('a query on the websocket url stays out of the path', gatewayClipUrl({ STT_GIGAAM_URL: 'ws://127.0.0.1:31121/?key=a' }) === 'http://127.0.0.1:31121/spotter-clip', gatewayClipUrl({ STT_GIGAAM_URL: 'ws://127.0.0.1:31121/?key=a' }))
  check('wss becomes https', gatewayClipUrl({ STT_GIGAAM_URL: 'wss://stt.test:443/x' }) === 'https://stt.test/spotter-clip', gatewayClipUrl({ STT_GIGAAM_URL: 'wss://stt.test:443/x' }))
}

console.log(`\nКЛИПЫ СПОТТЕРА: ${total - failed}/${total}`)
process.exit(failed === 0 ? 0 : 1)
