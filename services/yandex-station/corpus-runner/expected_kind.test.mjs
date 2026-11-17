import test from 'node:test'
import assert from 'node:assert/strict'
import { expectedKind, cannedRuleLine } from './expected_kind.mjs'

test('an emergency canned row is expected to be handed on', () => {
  assert.equal(expectedKind({ kind: 'canned', domain: 'EMERGENCY_REFUSAL' }), 'llm')
})

test('a canned row of any other domain stays canned (and so fails against a layer that never produces it)', () => {
  assert.equal(expectedKind({ kind: 'canned', domain: 'WEATHER' }), 'canned')
  assert.equal(expectedKind({ kind: 'canned' }), 'canned')
})

test('other kinds are untouched, whatever the domain', () => {
  for (const kind of ['cmd', 'clarify', 'llm', 'math', 'query', 'reprompt']) {
    assert.equal(expectedKind({ kind, domain: 'EMERGENCY_REFUSAL' }), kind)
  }
})

test('the rule line counts only the emergency rows', () => {
  const rows = [
    { expect: { kind: 'canned', domain: 'EMERGENCY_REFUSAL' } },
    { expect: { kind: 'canned', domain: 'OTHER' } },
    { expect: { kind: 'llm', domain: 'EMERGENCY_REFUSAL' } }
  ]
  assert.match(cannedRuleLine(rows), /EMERGENCY_REFUSAL \(1 строк\)/)
})
