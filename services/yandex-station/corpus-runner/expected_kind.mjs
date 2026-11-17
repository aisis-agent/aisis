// The one rule that lets a corpus row expect something else than the quasar layer produces: kept here, in code, so it is
// reviewed and tested. The quasar layer has no `canned` tier; the adapter's grammar route answers emergency requests
// («позвони в полицию») before it, and its own gate (`gate.safety`) pins that answer. So only rows of the EMERGENCY_REFUSAL
// domain are expected to come out of this layer as `llm` (handed on). Any other `canned` row stays `canned` and fails here.
export const CANNED_AS_LLM_DOMAIN = 'EMERGENCY_REFUSAL'

export function expectedKind (expect) {
  return expect.kind === 'canned' && expect.domain === CANNED_AS_LLM_DOMAIN ? 'llm' : expect.kind
}

export function cannedRuleLine (rows) {
  const n = rows.filter((row) => row.expect.kind === 'canned' && row.expect.domain === CANNED_AS_LLM_DOMAIN).length
  return `правило: canned -> llm только для домена ${CANNED_AS_LLM_DOMAIN} (${n} строк)`
}
