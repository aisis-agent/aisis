#!/usr/bin/env -S node --import tsx
/**
 * Exercise the captured Quasar volume schema, converter and protobuf encoder.
 * The only substitutes are logging and unrelated alarm serialization at import.
 * No server, transport, speaker, credentials or audio are involved.
 */
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import {
  closeSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, openSync,
  readFileSync, readdirSync, rmSync, statSync, writeFileSync, writeSync
} from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('.', import.meta.url))
process.umask(0o077)
const logDir = mkdtempSync(join(tmpdir(), 'aisis-volume-zero-check-'))
const logPath = join(logDir, 'run.jsonl')
const logFd = openSync(logPath, 'wx', 0o600)
const started = performance.now()
let runtime = ''
let exitCode = 1

function record (stage: string, details: object = {}) {
  const event = { time: new Date().toISOString(), stage, ...details }
  const line = JSON.stringify(event)
  writeSync(logFd, line + '\n')
  console.log(line)
}

function digest (data: Buffer) {
  return createHash('sha256').update(data).digest('hex')
}

function verifyFixtures () {
  const provenance = JSON.parse(readFileSync(join(root, 'fixtures/provenance.json'), 'utf8'))
  assert.equal(provenance.zod, '4.4.3')
  const installed = JSON.parse(readFileSync(join(root, 'node_modules/zod/package.json'), 'utf8'))
  assert.equal(installed.version, provenance.zod, 'use the production Zod version')
  for (const [name, expected] of Object.entries(provenance.files)) {
    const data = readFileSync(join(root, 'fixtures', basename(name) + '.txt'))
    assert.equal(digest(data), expected, 'captured source changed: ' + name)
  }
  record('fixtures-verified', { zod: installed.version })
}

function prepareRuntime (name: string, providerPath: string) {
  const dir = join(runtime, name)
  const alice = join(dir, 'routers/alice')
  mkdirSync(alice, { recursive: true })
  mkdirSync(join(dir, 'protos'))
  copyFileSync(providerPath, join(alice, 'base-provider.ts'))
  copyFileSync(join(root, 'fixtures/directives.ts.txt'), join(alice, 'directives.ts'))
  copyFileSync(join(root, 'fixtures/protobuf.ts.txt'), join(dir, 'protobuf.ts'))
  // These are boundaries unrelated to volume validation or payload encoding.
  // Capture expected validation warnings rather than polluting the test output.
  writeFileSync(join(dir, 'logger.ts'), [
    'export const warnings: unknown[][] = []',
    'export function getLogger () {',
    '  return { info () {}, warn (...args: unknown[]) { warnings.push(args) } }',
    '}'
  ].join('\n'))
  // The provider constructs one unrelated alarm-stop constant during import.
  // Every other generated-protobuf access remains absent and would throw.
  writeFileSync(join(dir, 'protos/protos.ts'), [
    'export let alarmSerializations = 0',
    'export default {',
    '  NAlice: { TAlarmCapability: { TAlarmStopDirective: {',
    '    encode () { alarmSerializations++; return { finish: () => Buffer.alloc(0) } }',
    '  } } },',
    '  google: { protobuf: { NullValue: { NULL_VALUE: 0 } } }',
    '}'
  ].join('\n'))
  return dir
}

async function loadProvider (dir: string) {
  const module = await import(pathToFileURL(join(dir, 'routers/alice/base-provider.ts')).href)
  const logger = await import(pathToFileURL(join(dir, 'logger.ts')).href)
  const protos = await import(pathToFileURL(join(dir, 'protos/protos.ts')).href)
  assert.equal(protos.alarmSerializations, 1)
  return { provider: new module.BaseDirectiveFunctionProvider(), logger, protos }
}

function callVolume (provider: Awaited<ReturnType<typeof loadProvider>>['provider'], value: unknown) {
  // Metadata is deliberately empty: volume conversion must not depend on a device.
  return provider.convertToWrappedDirective({
    name: 'smart_speaker_set_sound_volume',
    arguments: { volume: value }
  }, {})
}

function assertVolume (result: unknown, level: number, percent: number) {
  assert.notEqual(result, null, 'integer zero must produce a volume directive')
  assert.deepEqual(result, {
    quasarDirective: {
      IsLedSilent: true,
      Name: 'sound_set_level',
      Payload: { fields: {
        new_level: { numberValue: level },
        new_percent_level: { numberValue: percent }
      } },
      Type: 'client_action'
    }
  })
}

function runApplier (baseline: string, output: string, expectedStatus: number) {
  const result = spawnSync('python3', [
    join(root, 'prepare_overlay.py'), '--baseline', baseline, '--output-dir', output
  ], { encoding: 'utf8' })
  record('applier', { status: result.status, stdout: result.stdout, stderr: result.stderr })
  assert.ifError(result.error)
  assert.equal(result.status, expectedStatus)
  return result
}

function assertRefusals (providerFile: string, overlay: string) {
  const existing = join(runtime, 'existing')
  mkdirSync(existing)
  const sentinel = join(existing, 'sentinel')
  writeFileSync(sentinel, 'preserve this directory')
  runApplier(providerFile, existing, 1)
  assert.equal(readFileSync(sentinel, 'utf8'), 'preserve this directory')
  assert.deepEqual(readdirSync(existing), ['sentinel'])

  const unknown = join(runtime, 'unknown.ts')
  writeFileSync(unknown, readFileSync(providerFile, 'utf8') + '// drift\n')
  const unknownOut = join(runtime, 'unknown-output')
  runApplier(unknown, unknownOut, 1)
  assert.equal(existsSync(unknownOut), false)

  const repeated = join(runtime, 'repeat-output')
  runApplier(join(overlay, 'routers/alice/base-provider.ts'), repeated, 1)
  assert.equal(existsSync(repeated), false)
  record('unsafe-inputs-refused')
}

async function verifyPatched (baseline: Awaited<ReturnType<typeof loadProvider>>) {
  const providerFile = join(root, 'fixtures/base-provider.ts.txt')
  const overlay = join(runtime, 'overlay')
  runApplier(providerFile, overlay, 0)
  const patchedFile = join(overlay, 'routers/alice/base-provider.ts')
  const before = readFileSync(providerFile, 'utf8')
  const oldLine = '      volume: z.number().int().min(1).max(10)\n'
  const newLine = '      volume: z.number().int().min(0).max(10)\n'
  assert.equal(before.split(oldLine).length, 2, 'the exact source anchor occurs once')
  assert.deepEqual(readFileSync(patchedFile), Buffer.from(before.replace(oldLine, newLine)),
    'every byte except the intended lower bound is preserved')
  assert.equal(statSync(patchedFile).mode & 0o777, 0o600)

  const patched = await loadProvider(prepareRuntime('patched', patchedFile))
  assert.deepEqual(Object.keys(patched.provider.getSupportedFunctions()),
    Object.keys(baseline.provider.getSupportedFunctions()))
  for (const [level, percent] of [[0, 0], [1, 10], [5, 50], [10, 100]]) {
    assertVolume(callVolume(patched.provider, level), level, percent)
    if (level !== 0) assertVolume(callVolume(baseline.provider, level), level, percent)
  }
  assert.equal(patched.logger.warnings.length, 0)
  record('valid-volume-payloads', { levels: [0, 1, 5, 10] })

  const invalid = [-1, 0.1, 0.5, 10.1, 11, '0', true, null, undefined, NaN, Infinity, -Infinity, {}]
  for (const value of invalid) {
    const beforeWarnings = patched.logger.warnings.length
    assert.equal(callVolume(patched.provider, value), null)
    assert.equal(patched.logger.warnings.length, beforeWarnings + 1)
    assert.equal(patched.logger.warnings.at(-1)[1].name, 'ZodError')
  }
  assert.equal(patched.protos.alarmSerializations, 1, 'volume does not use the alarm substitute')
  record('invalid-volume-arguments-rejected', { cases: invalid.length })
  assertRefusals(providerFile, overlay)
}

try {
  console.error('Log: ' + logPath)
  record('start', { baselineOnly: process.argv.includes('--baseline-only') })
  verifyFixtures()
  runtime = mkdtempSync(join(root, '.volume-zero-regression-'))
  const baseline = await loadProvider(prepareRuntime(
    'baseline', join(root, 'fixtures/base-provider.ts.txt')))
  assertVolume(callVolume(baseline.provider, 5), 5, 50)
  assert.equal(baseline.logger.warnings.length, 0, 'positive-volume control succeeds')
  const zero = callVolume(baseline.provider, 0)
  assert.equal(zero, null, 'the recorded baseline must exhibit the original defect')
  assert.equal(baseline.logger.warnings.length, 1)
  assert.equal(baseline.logger.warnings[0][1].name, 'ZodError')
  const issue = baseline.logger.warnings[0][1].issues[0]
  assert.equal(issue.code, 'too_small')
  assert.deepEqual(issue.path, ['volume'])
  assert.equal(issue.minimum, 1)
  record('baseline-zero-rejected', { issue: issue.code, path: issue.path, minimum: issue.minimum })
  if (process.argv.includes('--baseline-only')) {
    // Deliberate RED only after the exact validation cause and positive control.
    assertVolume(zero, 0, 0)
  }
  await verifyPatched(baseline)
  exitCode = 0
} catch (error) {
  record('failed', { error: error instanceof Error ? error.stack : String(error) })
} finally {
  if (runtime) rmSync(runtime, { recursive: true })
  record('complete', { returncode: exitCode, elapsedMs: Math.round(performance.now() - started) })
  closeSync(logFd)
  console.error('Completed with exit ' + exitCode + '. Log: ' + logPath)
  process.exitCode = exitCode
}
