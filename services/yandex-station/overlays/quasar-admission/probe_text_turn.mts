// Sends ONE text turn to a quasar UniProxy WebSocket with a device id of your choice and prints what the server answers.
//
//   tsx probe_text_turn.mts WS_URL DEVICE_ID "text" [--header name=value]...
//
// It exists to show (and to keep showing) that a client which only knows the address can run the fast-intent layer: no
// station certificate, token or registered device is needed. Point it at the STAGE stack (dry-run Home Assistant); never at prod.
// Run it inside the quasar image so the protobuf definitions are the server's own, e.g.
//
//   docker run --rm --network host --entrypoint /node_modules/.bin/tsx -v "$PWD/probe_text_turn.mts:/src/routers/probe_text_turn.mts:ro" \
//     IMAGE /src/routers/probe_text_turn.mts ws://127.0.0.1:31415/uni.ws proof-$RANDOM "включи люстру"
//
// Exit codes: 0 the server accepted the connection and answered the turn, 3 the upgrade was refused (HTTP status printed),
// 4 accepted but nothing came back in time, 2 usage.
import { randomUUID } from 'node:crypto'
import { WebSocket } from 'ws'

import proto from '../protos/protos'

const ANSWER_TIMEOUT_MS = 8000

function parseArguments (argv: string[]): { deviceId: string, headers: Record<string, string>, text: string, url: string } {
  const [url, deviceId, text, ...rest] = argv
  if (!url || !deviceId || !text) {
    console.error('usage: probe_text_turn.mts WS_URL DEVICE_ID "text" [--header name=value]...')
    process.exit(2)
  }
  const headers: Record<string, string> = {}
  for (let index = 0; index < rest.length; index += 2) {
    const pair = rest[index + 1]
    if (rest[index] !== '--header' || !pair?.includes('=')) {
      console.error('usage: probe_text_turn.mts WS_URL DEVICE_ID "text" [--header name=value]...')
      process.exit(2)
    }
    headers[pair.slice(0, pair.indexOf('='))] = pair.slice(pair.indexOf('=') + 1)
  }
  return { deviceId, headers, text, url }
}

export function textInputFrame (text: string): Buffer {
  const message = proto.NAlice.NAliceApi.TClientMessage.encode({
    Event: {
      Header: { MessageId: randomUUID(), Name: 'TextInput', Namespace: 'Vins' },
      TextInput: {
        Header: { DialogId: randomUUID(), RequestId: randomUUID(), SequenceNumber: 1 },
        Request: {
          DeviceStateRaw: proto.NAlice.NAliceApi.TDeviceState.encode({}).finish(),
          EnvironmentStateRaw: proto.NAlice.NAliceApi.TEnvironmentState.encode({}).finish(),
          Event: { Text: text, Type: proto.NAlice.EEventType.text_input }
        }
      }
    }
  }).finish()
  return Buffer.concat([Buffer.from('AAPI', 'ascii'), message])
}

function describe (frame: Buffer): string {
  if (frame.length < 4 || frame.subarray(0, 4).toString('ascii') !== 'AAPI') return `binary frame ${frame.length} bytes`
  const server = proto.NAlice.NAliceApi.TServerMessage.decode(frame.subarray(4))
  const event = server.Event
  if (!event) return 'server message without an event'
  const kind = Object.keys(event.toJSON()).filter(key => key !== 'Header')
  const spoken = event.AliceResponse ? ` ${JSON.stringify(event.AliceResponse.toJSON()).slice(0, 160)}` : ''
  return `${kind.join('+') || 'Header only'}${spoken}`
}

async function main (): Promise<void> {
  const { deviceId, headers, text, url } = parseArguments(process.argv.slice(2))
  const socket = new WebSocket(url, { headers: { 'x-uprx-device-id': deviceId, 'x-uprx-device-model': 'yandexmini_2', ...headers } })
  const outcome = await new Promise<number>(resolve => {
    const timer = setTimeout(() => { console.log('accepted: no answer in time'); resolve(4) }, ANSWER_TIMEOUT_MS)
    let answered = false
    socket.on('unexpected-response', (_request, response) => {
      clearTimeout(timer)
      console.log(`refused: HTTP ${response.statusCode}`)
      resolve(3)
    })
    socket.on('error', error => {
      if (!/Unexpected server response/.test(String(error))) console.log(`error: ${String(error)}`)
    })
    socket.on('close', (code, reason) => {
      clearTimeout(timer)
      console.log(`closed: code=${code}${reason.length > 0 ? ` reason=${JSON.stringify(reason.toString('utf8'))}` : ''}`)
      resolve(answered ? 0 : 3)
    })
    socket.on('open', () => {
      console.log(`accepted: device=${deviceId}`)
      socket.send(textInputFrame(text), { binary: true })
    })
    socket.on('message', (data: Buffer) => {
      answered = true
      console.log(`answer: ${describe(Buffer.from(data))}`)
      clearTimeout(timer)
      setTimeout(() => { socket.close(); }, 500)
    })
  })
  process.exit(outcome)
}

if (process.argv[1]?.endsWith('probe_text_turn.mts')) await main()
