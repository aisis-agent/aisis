#!/usr/bin/env python3
"""Build the quasar overlay that stops the fast-intent layer from acting on a negated phrase and from mixing its speakers up.

Two defects of the layer (retro-review of the aisis-quasar patch range, ticket alex-mextner/aisis#186, fixes in #202):

1. Negation. The layer reads a verb and a noun and nothing else: «не включай чайник» switched the kettle plug on, «не выключай свет»
   switched the lights off, «не тише» lowered the volume, and «не делай», «не давай», «не точно», «нет, давай» were taken for a «да» to
   a pending question. The adapter grammar answers most negated phrases itself, but not «лучше не включай чайник» (a `none`
   verdict) and not any phrase while it is down or slow (the hook has a 250 ms budget and fails open).
2. Stations. The open question («Включить чайник?»), the turn memory («повтори», «а на кухне?»), the reprompt guard and the corrector's
   context were process-global: a «да» from one speaker answered a question another speaker was asked, «повтори» repeated another
   speaker's answer, and a station's question was offered to the ASR corrector as the context of another station's «давай».

The change is the fork's patch (aisis-quasar, `src/backend/negation.ts`, `turn-memory.ts`, `ha-intents.ts`, `asr-correct.ts`,
`routers/uniproxy/input-handler.ts`) applied to the exact sources of the running image, no fuzz.

    prepare_overlay.py --src IMAGE_SRC_DIR --output-dir NEW_DIRECTORY [--ha-intents FILE] [--input-handler FILE] [--pristine-only]

`--src` is a copy of the ACTIVE image's `/src` (`docker cp aisis-quasar-prod-<color>:/src/. DIR`; only the four files below are read).
Each patched file must be the image's own (SHA-256 pinned, and its result is pinned too), or the output of another overlay: then every
anchor must still occur exactly once and nothing else is checked, and the result's SHA-256 is printed. `--ha-intents` / `--input-handler`
(also `--turn-memory`, `--asr-correct`) take that other overlay's output instead of the file from `--src`; `--pristine-only` refuses
anything but the image's own files. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to /src) holding only the files
this overlay changes; it must not exist yet. Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

# image-relative path -> (SHA-256 of the image's file, SHA-256 of the patched result)
IMAGE_HASHES = {
    "backend/ha-intents.ts": ("f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3", "60733dc71ea252cc018f6854f58c836282ccd6ef7116e1ea7e679bcbb93d33a2"),
    "backend/turn-memory.ts": ("0c2ee7e4be189e6d5c159f23a5492459c2d4b850102b1b4690bfb64c84ef3c19", "5db6d505165f07571a01233b26756a2ab738100f205cfd03f7b45464128597df"),
    "backend/asr-correct.ts": ("1f337435e1c4cebd6edf43480330229e128ad035afb3c80fa6664cd319afc48d", "9eca3a1cf0975c6e7bdd37925b6aedbc8227f628ae9fed5a52ec9fe8e0d44131"),
    "routers/uniproxy/input-handler.ts": ("d01c3d5ef1a7959c4b3c866354963bf656d0e8a311ae9e613db9028703b432ba", "495b43f99c666169aee3328babee81087210a5144d04842e7ed56298c2dd0f5f"),
}
NEGATION_PATH = "backend/negation.ts"
NEGATION_SHA256 = "32c222c1f1bc2ebcff90f1439e490ce139b47c88a0b81116a2e89809e9928ac2"

NEGATION_TS = """\
// Negation in the owner's speech, for the fast-intent layer (ha-intents.ts).
//
// The home grammar reads a verb and a noun and nothing else, so «не включай чайник» (verb «включай», noun «чайник») would run
// the very action it forbids, and «не делай» (a confirmation word) would confirm what it refuses. The adapter's grammar guards
// its acting intents the same way (`not_negated`, lexicon: не, нельзя, ни, никогда); this is the guard for the turns that
// grammar does not answer (it is down or slow, or has no rule for the phrase: «лучше не включай чайник»).
//
// Pure module: tokens in, verdicts out.

const NEGATORS = ['не', 'ни', 'нельзя', 'никогда', 'ненадо', 'ненужно', 'неа', 'не-а']
// «никакого света», «никакой музыки»: the negative pronoun refuses what it stands before.
const NEGATIVE_PRONOUN = /^никак/
// What follows these opens a new clause: a negator before them does not reach past («не свет, а музыку»).
const CLAUSE_BREAKS = ['а', 'но', 'зато']
// «Алиса, не мог бы ты включить свет», «не можешь включить свет», «нельзя ли включить свет»: a polite request to the speaker, not a
// refusal. It must open the utterance (after the wake word and the like): «я знаю, что ты не можешь включить чайник» and «я не мог
// бы включить свет» are statements, and «мог» without «бы» is a statement of inability in the past.
const REQUEST_MODALS = ['можешь', 'можете', 'сможешь', 'сможете', 'ли']
const REQUEST_PAST_MODALS = ['мог', 'могла', 'могли']
const REQUEST_LEAD_INS = ['алиса', 'яндекс', 'пожалуйста', 'ну', 'а', 'и', 'ты', 'вы']

function asksPolitely (tokens: readonly string[], index: number): boolean {
  const [next = '', afterNext = '', pronoun = ''] = tokens.slice(index + 1, index + 4)
  const modal = REQUEST_MODALS.includes(next) || REQUEST_PAST_MODALS.includes(next) && afterNext === 'бы' && !['я', 'мы'].includes(pronoun)
  return modal && tokens.slice(0, index).every(word => REQUEST_LEAD_INS.includes(word))
}

function negates (tokens: readonly string[], index: number): boolean {
  const token = tokens[index] ?? ''
  if (NEGATIVE_PRONOUN.test(token)) return true
  if (!NEGATORS.includes(token)) return false
  return !((token === 'не' || token === 'нельзя') && asksPolitely(tokens, index))
}

/** True when any word of the phrase negates something. */
export function hasNegation (tokens: readonly string[]): boolean {
  return tokens.some((_, index) => negates(tokens, index))
}

/** True when a negator stands anywhere in the clause of the word at `index` («не включай пожалуйста этот свет», «свет не включай»). */
export function isNegatedAt (tokens: readonly string[], index: number): boolean {
  let start = index
  while (start > 0 && !CLAUSE_BREAKS.includes(tokens[start - 1] ?? '')) start--
  let end = index + 1
  while (end < tokens.length && !CLAUSE_BREAKS.includes(tokens[end] ?? '')) end++
  return tokens.slice(start, end).some((_, offset) => negates(tokens, start + offset))
}
"""

# Each row: (what, text in the image, text it becomes). The text must occur in the input exactly once.

TURN_MEMORY_PATCHES = (
    ('the process-level comment',
     """\
// from the adapter's dialog store (dialog_context.py) and repeats from there.
""",
     """\
// from the adapter's dialog store (dialog_context.py) and repeats from there.
//
// Per station: the home has several speakers, and «повтори» on one must not repeat what another heard, nor «а на кухне?» continue
// another's command. The key is the station's device id (`stationKey`); it comes from the connection, so every per-station map is
// bounded (`rememberFor`) however many ids show up.
"""),
    ('the end of the Turn interface',
     """\
  spoken: null | string // what the speaker said aloud; null for a silent turn
}
""",
     """\
  spoken: null | string // what the speaker said aloud; null for a silent turn
  station: string // `stationKey` of the speaker that heard it
}
"""),
    ('the ring and its writers',
     """\
const turns: Turn[] = []

export function recordTurn (turn: Turn): void {
  turns.push(turn)
  if (turns.length > RING_SIZE) turns.shift()
}

export function resetTurns (): void {
  turns.length = 0
}
""",
     """\
// A home has a handful of speakers.
export const MAX_STATIONS = 32

const rings = new Map<string, Turn[]>()

/** The key of a station in every per-station memory: its device id in lower case; '' for a caller that has none (the corpus runner). */
export function stationKey (deviceId: string | undefined): string {
  return deviceId?.trim().toLowerCase() ?? ''
}

/** Stores `value` for `station` in a per-station map; beyond MAX_STATIONS the station stored longest ago is forgotten. */
export function rememberFor<T> (memory: Map<string, T>, station: string, value: T): void {
  memory.delete(station)
  memory.set(station, value)
  const oldest = memory.keys().next().value
  if (memory.size > MAX_STATIONS && oldest !== undefined) memory.delete(oldest)
}

export function recordTurn (turn: Turn): void {
  rememberFor(rings, turn.station, [...rings.get(turn.station) ?? [], turn].slice(-RING_SIZE))
}

export function resetTurns (): void {
  rings.clear()
}
"""),
    ('newestFirst',
     """\
function newestFirst (now: number): Turn[] {
  return turns.filter(turn => now - turn.at <= REPEAT_TTL_MS).reverse()
}
""",
     """\
function newestFirst (station: string, now: number): Turn[] {
  return (rings.get(station) ?? []).filter(turn => now - turn.at <= REPEAT_TTL_MS).reverse()
}
"""),
    ('latestTurn',
     """\
export function latestTurn (want: RepeatTarget, now = Date.now()): null | Turn {
  const recent = newestFirst(now)
""",
     """\
export function latestTurn (want: RepeatTarget, station: string, now = Date.now()): null | Turn {
  const recent = newestFirst(station, now)
"""),
    ('latestAction',
     """\
export function latestAction (now = Date.now()): ActionRecord | null {
  const recent = turns.filter(turn => now - turn.at <= ANAPHORA_TTL_MS).reverse()
""",
     """\
export function latestAction (station: string, now = Date.now()): ActionRecord | null {
  const recent = (rings.get(station) ?? []).filter(turn => now - turn.at <= ANAPHORA_TTL_MS).reverse()
"""),
)

ASR_CORRECT_PATCHES = (
    ('the logger import',
     """\
import { getLogger } from '../logger'
""",
     """\
import { getLogger } from '../logger'
import { rememberFor } from './turn-memory'
"""),
    ('the last question of the station',
     """\
let lastAsked: null | { at: number, text: string } = null

/** The speaker asked the owner something (confirm / clarify / «не расслышала»): the next phrase may be its answer. */
export function noteAsked (text: string, now = Date.now()): void {
  lastAsked = { at: now, text }
}

export function recentTurns (now = Date.now()): Array<{ role: 'assistant' | 'user', text: string }> {
  if (lastAsked === null || now - lastAsked.at > SPOKEN_CONTEXT_MS) return []
  return [{ role: 'assistant' as const, text: lastAsked.text }].slice(-MAX_CONTEXT_TURNS)
}
""",
     """\
// What each station was last asked: the next phrase of THAT station may be its answer; another speaker's «давай» is not.
const lastAsked = new Map<string, { at: number, text: string }>()

/** The speaker `station` asked the owner something (confirm / clarify / «не расслышала»): its next phrase may be the answer. */
export function noteAsked (text: string, station: string, now = Date.now()): void {
  rememberFor(lastAsked, station, { at: now, text })
}

/** The owner dismissed the question of `station` («ничего», «хватит»): what is said next is not its answer. */
export function forgetAsked (station: string): void {
  lastAsked.delete(station)
}

export function recentTurns (station: string, now = Date.now()): Array<{ role: 'assistant' | 'user', text: string }> {
  const asked = lastAsked.get(station)
  if (asked === undefined || now - asked.at > SPOKEN_CONTEXT_MS) return []
  return [{ role: 'assistant' as const, text: asked.text }].slice(-MAX_CONTEXT_TURNS)
}
"""),
    ('the requestCorrection signature',
     """\
export async function requestCorrection (text: string, fetchImpl: typeof fetch = fetch): Promise<Correction | null> {
""",
     """\
export async function requestCorrection (text: string, station: string, fetchImpl: typeof fetch = fetch): Promise<Correction | null> {
"""),
    ('the corrector request body',
     """\
      body: JSON.stringify({ recent: recentTurns(), source: 'quasar', text }),
""",
     """\
      body: JSON.stringify({ recent: recentTurns(station), source: 'quasar', text }),
"""),
)

HA_INTENTS_PATCHES = (
    ('the music-parse import (the negation import follows it)',
     """\
import { type MediaAction, parseMediaControl } from './music-parse'
""",
     """\
import { type MediaAction, parseMediaControl } from './music-parse'
import { hasNegation, isNegatedAt } from './negation'
"""),
    ('the asr-correct import',
     """\
import { allowCorrection, confirmationQuestion, describeCorrection, eligible, endsMidPhrase, noteAsked, requestCorrection, shadowOnly } from './asr-correct'
""",
     """\
import { allowCorrection, confirmationQuestion, describeCorrection, eligible, endsMidPhrase, forgetAsked, noteAsked, requestCorrection, shadowOnly } from './asr-correct'
"""),
    ('the turn-memory import',
     """\
import { ActionRecord, latestAction, latestTurn, parseRepeat, recordTurn, RepeatTarget, Turn, TurnOrigin } from './turn-memory'
""",
     """\
import { ActionRecord, latestAction, latestTurn, parseRepeat, recordTurn, rememberFor, RepeatTarget, stationKey, Turn, TurnOrigin } from './turn-memory'
"""),
    ('the end of the TurnTrace interface',
     """\
  skipRecording: boolean
}
""",
     """\
  skipRecording: boolean
  station: string // `stationKey` of the speaker whose turn this is: the open question and the turn memory are theirs alone
}
"""),
    ('the pending clarification declaration',
     """\
let pendingClarification: PendingClarification | null = null
""",
     """\
// One open question per station: a «да» answers the question the speaker that says it was asked, never another speaker's. The map
// is bounded (turn-memory.ts): station ids come from connections.
const pendingClarifications = new Map<string, PendingClarification>()
"""),
    ('the reprompt timestamp declaration',
     """\
let lastRepromptAt = 0
""",
     """\
const lastReprompts = new Map<string, number>()

// The station whose turn is running (matchFastIntent puts it into the turn's trace); '' outside a turn.
function currentStation (): string {
  return turnTrace.getStore()?.station ?? ''
}

function getPending (): PendingClarification | null {
  return pendingClarifications.get(currentStation()) ?? null
}

function setPending (pending: PendingClarification | null): void {
  if (pending === null) pendingClarifications.delete(currentStation())
  else rememberFor(pendingClarifications, currentStation(), pending)
}
"""),
    ('the pending question of a setting',
     """\
    pendingClarification = { declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, mode: 'confirm', phrase: setting.id,
      remember: false, resolved: setting }
""",
     """\
    setPending({ declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, mode: 'confirm', phrase: setting.id,
      remember: false, resolved: setting })
"""),
    ('resolveClarification',
     """\
function resolveClarification (tokens: string[]): 'home' | 'llm' | 'yes' | 'no' | null {
  if (tokens.some(token => ['свет', 'света', 'свету', 'лампа', 'лампу'].includes(token))) return 'home'
  if (tokens.some(token => token.startsWith('музык'))) return 'llm'
  if (tokens.some(token => YES_WORDS.includes(token))) return 'yes'
  if (tokens.some(token => NO_WORDS.includes(token))) return 'no'
  return null
}
""",
     """\
// What the owner's answer picks. A choice word under a negator («не свет, а музыку») is not the choice, and a yes word beside a
// negation or a «нет» («не делай», «не давай», «нет, давай») is not a «да»: the question stays unanswered, never run on a misread.
function resolveClarification (tokens: string[]): 'home' | 'llm' | 'yes' | 'no' | null {
  const picked = (isChoice: (token: string) => boolean): boolean => tokens.some((token, index) => isChoice(token) && !isNegatedAt(tokens, index))
  if (picked(token => ['свет', 'света', 'свету', 'лампа', 'лампу'].includes(token))) return 'home'
  if (picked(token => token.startsWith('музык'))) return 'llm'
  const refused = hasNegation(tokens) || tokens.some(token => NO_WORDS.includes(token))
  if (!refused && tokens.some(token => YES_WORDS.includes(token))) return 'yes'
  return refused ? 'no' : null
}
"""),
    ('decideSentence',
     """\
function decideSentence (rawText: string, reg: HaRegistry, defaultArea: string): FastDecision {
  const decision = decideSentenceUncut(rawText, reg, defaultArea)
  return decision.tier === 'execute' && endsMidPhrase(tokenize(normalize(rawText))) ? { reason: 'cut-off', tier: 'reprompt' } : decision
}
""",
     """\
function decideSentence (rawText: string, reg: HaRegistry, defaultArea: string): FastDecision {
  const decision = decideSentenceUncut(rawText, reg, defaultArea)
  const words = tokenize(normalize(rawText))
  if (hasNegation(words) && actsOnHome(decision)) return { tier: 'llm' }
  return decision.tier === 'execute' && endsMidPhrase(words) ? { reason: 'cut-off', tier: 'reprompt' } : decision
}

// «не включай чайник», «лучше не включай», «кондиционер не включай» forbid what the verb and the noun name, and the grammar reads only
// those two: left alone it runs the action. A negated phrase is therefore no match for everything that would act (run, confirm,
// ask to fill in or to repeat); answers, state and setting reads and entity questions only talk and stay.
function actsOnHome (decision: FastDecision): boolean {
  switch (decision.tier) {
    case 'execute': return decision.resolved.type !== 'setting-query'
    case 'confirm': return decision.resolved.type !== 'say'
    case 'clarify':
    case 'reprompt': return true
    default: return false
  }
}
"""),
    ('the correction gate and request',
     """\
  if (options.noCorrect === true || !eligible(text, tier) || !allowCorrection()) return null
  const correction = await requestCorrection(text)
""",
     """\
  // A repair can only turn a negated phrase into its opposite («не включай» heard as «включай»): leave it alone.
  if (options.noCorrect === true || hasNegation(tokenize(normalize(text))) || !eligible(text, tier) || !allowCorrection()) return null
  const correction = await requestCorrection(text, currentStation())
"""),
    ('the pending question of a repaired phrase',
     """\
    pendingClarification = { declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, mode: 'confirm',
      phrase: normalize(text), resolved: heard.resolved }
""",
     """\
    setPending({ declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, mode: 'confirm',
      phrase: normalize(text), resolved: heard.resolved })
"""),
    ('the pending question read by a repeat',
     """\
  if (pendingClarification !== null && Date.now() < pendingClarification.expiresAt) {
    const question = latestTurn('answer')
    if (question?.asked === true) {
      pendingClarification.expiresAt = Date.now() + AMBIGUITY_TTL_MS
""",
     """\
  const station = currentStation()
  const pending = getPending()
  if (pending !== null && Date.now() < pending.expiresAt) {
    const question = latestTurn('answer', station)
    if (question?.asked === true) {
      pending.expiresAt = Date.now() + AMBIGUITY_TTL_MS
"""),
    ('the last turn read by a repeat',
     """\
  const turn = latestTurn(want)
""",
     """\
  const turn = latestTurn(want, station)
"""),
    ('the pending question of a repeat',
     """\
  pendingClarification = { declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, mode: 'confirm',
    phrase: turn.action.phrase, remember: false, resolved: turn.action.resolved }
""",
     """\
  setPending({ declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, mode: 'confirm',
    phrase: turn.action.phrase, remember: false, resolved: turn.action.resolved })
"""),
    ('the last action read by a room follow-up',
     """\
  const prior = latestAction()
""",
     """\
  const prior = latestAction(currentStation())
"""),
    ('the turn trace',
     """\
  const trace: TurnTrace = outer ?? { executed: null, phrase: '', skipRecording: false }
""",
     """\
  const trace: TurnTrace = outer ?? { executed: null, phrase: '', skipRecording: false, station: stationKey(options.deviceId) }
"""),
    ('the question noted for the corrector',
     """\
  if (result?.kind === 'reply' && result.listen === true && result.text !== null) noteAsked(result.text)
""",
     """\
  if (result?.kind === 'reply' && result.listen === true && result.text !== null) noteAsked(result.text, trace.station)
"""),
    ('the recorded turn',
     """\
      origin: options.origin ?? 'voice',
      spoken: result.text
    }
""",
     """\
      origin: options.origin ?? 'voice',
      spoken: result.text,
      station: trace.station
    }
"""),
    ('the sentence a room follow-up repeats',
     """\
  for (const token of tokenize(prior.phrase).filter(word => word !== 'алиса')) {
""",
     """\
  // The sentence that ran, not the chatter after it («Включи свет в гостиной. Не знаю.»): a negated one would make the follow-up refuse.
  const ran = splitSentences(prior.phrase).find(sentence => !hasNegation(tokenize(sentence))) ?? prior.phrase
  for (const token of tokenize(ran).filter(word => word !== 'алиса')) {
"""),
    ('the slot fill and its refusal',
     """\
          const filled = pending.fill?.(parseUtterance(s, ROOMS, [])) ?? null
          if (filled !== null) return await executeResolved(config, filled, speakerLevel)
          if (answer === 'no') return DIALOG_END
""",
     """\
          const filled = answer === 'no' || hasNegation(tokens) ? null : pending.fill?.(parseUtterance(s, ROOMS, [])) ?? null
          if (filled !== null) return await executeResolved(config, filled, speakerLevel)
          if (answer === 'no') {
            forgetAsked(currentStation())
            return DIALOG_END
          }
"""),
    ('the refusal of a confirmation',
     """\
            return pending.declineToBrain
""",
     """\
            // An explicit refusal («не делай») cancels; sending the phrase on to the brain is for «нет, я про другое». Either way the
            // refused question is no context for what is said next.
            forgetAsked(currentStation())
            return pending.declineToBrain && !hasNegation(tokens)
"""),
    ('the pending question read by the turn',
     """\
    if (pendingClarification !== null) {
      const pending = pendingClarification
      pendingClarification = null
""",
     """\
    const pending = getPending()
    if (pending !== null) {
      setPending(null)
"""),
    ('the dialog_end route',
     """\
      } else if (grammar.route === 'dialog_end') {
        pendingClarification = null
""",
     """\
      } else if (grammar.route === 'dialog_end') {
        setPending(null)
        forgetAsked(currentStation())
"""),
    ('the reprompt timestamp reset',
     """\
    if (decision.tier !== 'reprompt') lastRepromptAt = 0
""",
     """\
    if (decision.tier !== 'reprompt') lastReprompts.delete(currentStation())
"""),
    ('the correction gate of the turn',
     """\
    if (decision.tier !== 'llm' && !(decision.tier === 'reprompt' && Date.now() - lastRepromptAt < REPROMPT_LOOP_MS)) {
""",
     """\
    if (decision.tier !== 'llm' && !(decision.tier === 'reprompt' && Date.now() - (lastReprompts.get(currentStation()) ?? 0) < REPROMPT_LOOP_MS)) {
"""),
    ('the pending question of a confirm',
     """\
        pendingClarification = { declineToBrain: decision.declineToBrain, expiresAt: Date.now() + AMBIGUITY_TTL_MS,
          mode: 'confirm', phrase: s, resolved: decision.resolved }
""",
     """\
        setPending({ declineToBrain: decision.declineToBrain, expiresAt: Date.now() + AMBIGUITY_TTL_MS,
          mode: 'confirm', phrase: s, resolved: decision.resolved })
"""),
    ('the pending question of a clarify',
     """\
        pendingClarification = { declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, fill: decision.fill,
          mode: decision.mode, phrase: s, resolved: decision.resolved }
""",
     """\
        setPending({ declineToBrain: false, expiresAt: Date.now() + AMBIGUITY_TTL_MS, fill: decision.fill,
          mode: decision.mode, phrase: s, resolved: decision.resolved })
"""),
    ('the reprompt case',
     """\
        if (Date.now() - lastRepromptAt < REPROMPT_LOOP_MS) return { kind: 'reply', quasarDirectives: [], text: null }
        lastRepromptAt = Date.now()
""",
     """\
        if (Date.now() - (lastReprompts.get(currentStation()) ?? 0) < REPROMPT_LOOP_MS) return { kind: 'reply', quasarDirectives: [], text: null }
        rememberFor(lastReprompts, currentStation(), Date.now())
"""),
)

# Where the terminator handler («ничего», «хватит», «отмена») clears the open question it sits in one of two shapes: in the image, and one
# block deeper after the correction-hook overlay moved it into `endOrRepeat`. Exactly one of them must be present.
HA_INTENTS_ONE_OF = (
    ('the terminator handler of the image',
     (("""\
      pendingClarification = null
      if (end === 'stop' && options.dialogActive !== true) return await runMediaControl(config, 'stop') ?? DIALOG_END
""",
       """\
      setPending(null)
      forgetAsked(currentStation())
      if (end === 'stop' && options.dialogActive !== true) return await runMediaControl(config, 'stop') ?? DIALOG_END
"""),
      ("""\
        pendingClarification = null
        if (end === 'stop' && options.dialogActive !== true) return await runMediaControl(config, 'stop') ?? DIALOG_END
""",
       """\
        setPending(null)
        forgetAsked(currentStation())
        if (end === 'stop' && options.dialogActive !== true) return await runMediaControl(config, 'stop') ?? DIALOG_END
"""))),
)

INPUT_HANDLER_PATCHES = (
    ('the turn-memory import',
     """\
import { recordTurn, TurnOrigin } from '../../backend/turn-memory'
""",
     """\
import { recordTurn, stationKey, TurnOrigin } from '../../backend/turn-memory'
"""),
    ('the brain turn field',
     """\
  // The utterance the brain is answering: recorded with its answer so «повтори» can say it again (turn-memory.ts).
  private brainTurn: null | { heard: string, origin: TurnOrigin } = null
""",
     """\
  // The utterance the brain is answering and the station that said it: recorded with its answer so «повтори» can say it again to that
  // station only (turn-memory.ts).
  private brainTurn: null | { heard: string, origin: TurnOrigin, station: string } = null
"""),
    ('the brain turn assignment',
     """\
    this.brainTurn = { heard: input.text, origin: 'voice' }
""",
     """\
    this.brainTurn = { heard: input.text, origin: 'voice', station: stationKey(input.metadata.internal.deviceId) }
"""),
    ('the brain turn record',
     """\
        recordTurn({ action: null, asked: shouldListen, at: Date.now(), heard: this.brainTurn.heard, origin: this.brainTurn.origin,
          spoken: partialResponse.text })
""",
     """\
        recordTurn({ action: null, asked: shouldListen, at: Date.now(), heard: this.brainTurn.heard, origin: this.brainTurn.origin,
          spoken: partialResponse.text, station: this.brainTurn.station })
"""),
)


class OverlayError(Exception):
    """An input is not the file this patch was made for, or the patch no longer applies exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def locate(source: str, what: str, before: str, after: str) -> tuple[int, int, str]:
    count = source.count(before)
    if count != 1:
        raise OverlayError(f"{what}: found {count} times, expected exactly once")
    start = source.index(before)
    return start, start + len(before), after


def apply_patches(source: str, patches: tuple[tuple[str, str, str], ...], one_of: tuple = ()) -> str:
    """Replace every block; each anchor is looked up in the INPUT (not in a half-patched copy) and must occur exactly once, and no two
    blocks may overlap. `one_of`: groups of known shapes of one block, exactly one of which may be present."""
    spans = [locate(source, what, before, after) for what, before, after in patches]
    for what, shapes in one_of:
        present = [(before, after) for before, after in shapes if before in source]
        if len(present) != 1:
            raise OverlayError(f"{what}: {len(present)} of its {len(shapes)} known shapes found, expected exactly one")
        spans.append(locate(source, what, *present[0]))
    spans.sort(key=lambda span: span[0])
    for (_, end, _), (start, _, _) in zip(spans, spans[1:]):
        if start < end:
            raise OverlayError("two blocks of the patch overlap in the input")
    parts, position = [], 0
    for start, end, after in spans:
        parts.extend((source[position:start], after))
        position = end
    parts.append(source[position:])
    return "".join(parts)


TARGETS = (
    ("backend/ha-intents.ts", HA_INTENTS_PATCHES, HA_INTENTS_ONE_OF),
    ("backend/turn-memory.ts", TURN_MEMORY_PATCHES, ()),
    ("backend/asr-correct.ts", ASR_CORRECT_PATCHES, ()),
    ("routers/uniproxy/input-handler.ts", INPUT_HANDLER_PATCHES, ()),
)


def prepare(src_dir: Path, output_dir: Path, overrides: dict[str, Path] | None = None, *, pristine_only: bool = False) -> dict[str, str]:
    """Write the overlay; returns {path in the overlay: SHA-256}. Nothing is written unless every file patched."""
    overrides = overrides or {}
    if output_dir.exists():
        raise OverlayError(f"{output_dir} already exists")
    if (src_dir / NEGATION_PATH).exists():
        raise OverlayError(f"the image already has {NEGATION_PATH}: this overlay is obsolete")
    written: dict[str, bytes] = {}
    for relative, patches, one_of in TARGETS:
        data = overrides.get(relative, src_dir / relative).read_bytes()
        image_sha256, patched_sha256 = IMAGE_HASHES[relative]
        is_image_file = sha256(data) == image_sha256
        if pristine_only and not is_image_file:
            raise OverlayError(f"{relative}: sha256 {sha256(data)} is not the image's {image_sha256}")
        patched = apply_patches(data.decode("utf-8"), patches, one_of).encode("utf-8")
        if is_image_file and sha256(patched) != patched_sha256:
            raise OverlayError(f"{relative}: patched sha256 {sha256(patched)} is not the expected {patched_sha256}")
        written[relative] = patched
    written[NEGATION_PATH] = NEGATION_TS.encode("utf-8")
    if sha256(written[NEGATION_PATH]) != NEGATION_SHA256:
        raise OverlayError(f"{NEGATION_PATH}: sha256 {sha256(written[NEGATION_PATH])} is not the expected {NEGATION_SHA256}")
    for relative, data in written.items():
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {relative: sha256(data) for relative, data in written.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", type=Path, required=True, help="a copy of the active image's /src")
    parser.add_argument("--output-dir", type=Path, required=True, help="must not exist yet")
    parser.add_argument("--ha-intents", type=Path, help="another overlay's backend/ha-intents.ts instead of the image's")
    parser.add_argument("--input-handler", type=Path, help="another overlay's routers/uniproxy/input-handler.ts instead of the image's")
    parser.add_argument("--turn-memory", type=Path, help="another overlay's backend/turn-memory.ts instead of the image's")
    parser.add_argument("--asr-correct", type=Path, help="another overlay's backend/asr-correct.ts instead of the image's")
    parser.add_argument("--pristine-only", action="store_true", help="refuse any input that is not the image's own file")
    args = parser.parse_args(argv)
    overrides = {relative: path for relative, path in (
        ("backend/ha-intents.ts", args.ha_intents), ("routers/uniproxy/input-handler.ts", args.input_handler),
        ("backend/turn-memory.ts", args.turn_memory), ("backend/asr-correct.ts", args.asr_correct)) if path is not None}
    try:
        hashes = prepare(args.src, args.output_dir, overrides, pristine_only=args.pristine_only)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    for relative, digest in hashes.items():
        print(f"{args.output_dir / relative}  sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
