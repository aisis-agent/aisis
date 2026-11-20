#!/usr/bin/env python3
"""Build the quasar overlay that hands every air-conditioner command to the adapter instead of the container's own AC handlers.

The quasar fast-intent layer answers AC phrases itself, before the adapter, and does it wrongly (incident 2026-11-20 11:18):
every «включи кондиционер» starts the unit in COOL whatever was asked (`callService`: `hvac_mode: turnOn ? 'cool' : 'off'`),
«на обогрев» is not understood at all, a set-point on an off unit starts it in cool, a bare «кондиционер на втором этаже» is
answered with the fixed question «Включить или выключить кондиционер наверху?», and a call that outlives the 2 s timeout was
reported as «умный дом перезагружается». The adapter already has the grammar and the executor for all of it (`ha.climate`,
`ha.climate_delta`: floor from the phrase, mode by the phrase, verification, one log record per call).

The one change: `decideFastIntent` of `backend/ha-intents.ts` returns the `llm` tier for a single-sentence turn that would turn ON
or set a climate entity when the adapter's `climate.set` rule parses the WHOLE phrase (кондиционер or кондей, the words the adapter
grammar knows and `music-parse.ts` keeps out of music searches, with the verb, room, mode and temperature words of that rule). The
turn then continues to the brain tier, where the adapter's grammar route runs it. Not handed over (they keep their quasar
behaviour): turning the AC OFF (the adapter grammar has no AC-off rule), the cut-off reprompt («включи кондиционер на»), state
answers, the bare «кондиционер на втором этаже» question (the adapter has no two-turn clarification), phrases with extra words or a
second clause («… сейчас», «… в режиме обогрева», «… потом включи свет»), multi-sentence turns (the adapter matches whole
utterances), and every phrase without those words («сплит», «кондер» or a misheard «кондицинер» would be taken for a music
title once handed over).

    prepare_overlay.py --baseline ha-intents.ts --output-dir NEW_DIRECTORY

`--baseline` is the file taken from the ACTIVE image (`docker cp aisis-quasar-prod-<color>:/src/backend/ha-intents.ts`); its
SHA-256 must be the known one, the patch is an exact single-block replacement (no fuzz), and the result is checked against its
own SHA-256. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to /src): `NEW_DIRECTORY/backend/ha-intents.ts`.
Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

BASELINE_SHA256 = "f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3"
PATCHED_SHA256 = "de2ed07891e34ca5ceec68eca55c0dea1fb415399884926ec7545f10ccb56f92"

BEFORE = """\
export function decideFastIntent (rawText: string, reg: HaRegistry, defaultArea: string = DEFAULT_AREA): FastDecision {
  const sentences = splitSentences(rawText)
"""

AFTER = """\
// The adapter's climate.set rule (intent_grammar/grammar/ru.yaml) matches a WHOLE utterance of these shapes. Anything else holding
// an AC word («включи кондиционер сейчас», «… в режиме обогрева») ends in «Такого устройства я не знаю» with no call to the unit,
// so only phrases made of exactly these words are handed over; the rest keeps the quasar path.
const AC_WORD = '(?:кондиционер|кондей)[а-я]*'
const AC_ON_VERB = '(?:включи|включай|включить|зажги|засвети|вруби|запусти|запускай)'
const AC_SET_VERB = '(?:сделай|поставь|установи|выставь|поменяй|переключи|настрой|наставь|настроить)'
const AC_ROOM = '(?:гостин[а-я]*|кухн[а-я]*|спальн[а-я]*|студи[а-я]*|прихож[а-я]*|втор[а-я]* этаж[а-я]*|наверху|наверх)'
const AC_MODE = '(?:(?:обогрев|нагрев|отоплени|тепл|охлажд|холод|прохлад|осушени|сушк|вентиляц|автоматическ)[а-я]*|осуши|вентилятор|авто)'
const AC_TEMP = '(?:(?:1[6-9]|2[0-9]|30|двадцать(?: (?:один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять))?|тридцать|шестнадцать|семнадцать|восемнадцать|девятнадцать)(?: градус[а-я]*)?)'
const AC_ON_PHRASE = new RegExp(`^${AC_ON_VERB} ${AC_WORD}(?: (?:в|на))?(?: ${AC_ROOM})?(?: (?:на|в))?(?: ${AC_MODE})?(?: ${AC_TEMP})?$`)
const AC_SET_PHRASE = new RegExp(`^(?:${AC_SET_VERB} )?${AC_WORD}(?: (?:в|на))?(?: ${AC_ROOM})?(?: (?:на|в))?(?: ${AC_MODE}(?: ${AC_TEMP})?| ${AC_TEMP})$`)
// The adapter drops its lexicon's fillers anywhere and its lead-in and wake words at the edges before matching (normalize.py), so
// «включи, пожалуйста, кондиционер на втором этаже на обогрев» is the same phrase for it. The same words are dropped here.
const AC_FILLERS = new Set(['пожалуйста', 'плиз', 'вот', 'же', 'значит', 'короче', 'типа'])
const AC_EDGE_WORDS = new Set(['а', 'ну', 'слушай', 'эй', 'слышь', 'так', 'алиса', 'алис', 'алисы', 'алиску', 'алиска', 'тариса',
  'малис', 'лисы', 'элиса', 'илиса', 'олиса', 'толиса', 'яндекс', 'брачо'])

function adapterOwnsAcPhrase (words: string[]): boolean {
  const spoken = words.filter(word => !AC_FILLERS.has(word))
  let first = 0
  let last = spoken.length
  while (first < last && AC_EDGE_WORDS.has(spoken[first])) first++
  while (last > first && AC_EDGE_WORDS.has(spoken[last - 1])) last--
  const phrase = spoken.slice(first, last).join(' ')
  return AC_ON_PHRASE.test(phrase) || AC_SET_PHRASE.test(phrase)
}

export function decideFastIntent (rawText: string, reg: HaRegistry, defaultArea: string = DEFAULT_AREA): FastDecision {
  const sentences = splitSentences(rawText)
  // Air conditioning is switched on or set by the adapter (ac-defer-to-adapter overlay): its grammar reads the floor and the mode
  // from the phrase, its executor verifies the unit. The container's own handlers started every unit in COOL and did not know
  // «на обогрев». A single-sentence turn that would turn on or set a climate entity goes on to the brain tier only when the
  // adapter grammar parses the whole phrase (adapterOwnsAcPhrase). Not handed over: turning the AC OFF (no such rule in the
  // adapter), the cut-off reprompt, state answers, a bare «кондиционер на втором этаже» question (no two-turn clarification in
  // the adapter), «включи наверху кондиционер» (room before the AC word), extra words or a second clause («… сейчас», «… в режиме
  // обогрева», «… потом включи свет»), and multi-sentence turns (the adapter matches whole utterances); they keep the quasar path.
  if (sentences.length <= 1) {
    const local = decideSentence(rawText, reg, defaultArea)
    if ((local.tier === 'execute' || local.tier === 'confirm') && local.resolved.type === 'device' && local.resolved.device.kind !== 'off' &&
      local.resolved.device.targets.some(entityId => entityId.startsWith('climate.')) &&
      adapterOwnsAcPhrase(tokenize(normalize(rawText)))) return { tier: 'llm' }
    return local
  }
"""


class OverlayError(Exception):
    """The baseline is not the file this patch was made for, or the patch no longer applies exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_patch(baseline: str) -> str:
    """Replace BEFORE by AFTER; the anchor must occur exactly once."""
    if baseline.count(BEFORE) != 1:
        raise OverlayError("decideSentence of ha-intents.ts was not found exactly once")
    return baseline.replace(BEFORE, AFTER)


def prepare(baseline_path: Path, output_dir: Path, *, check_hashes: bool = True) -> Path:
    data = baseline_path.read_bytes()
    if check_hashes and sha256(data) != BASELINE_SHA256:
        raise OverlayError(f"baseline sha256 {sha256(data)} is not the expected {BASELINE_SHA256}")
    patched = apply_patch(data.decode("utf-8")).encode("utf-8")
    if check_hashes and sha256(patched) != PATCHED_SHA256:
        raise OverlayError(f"patched sha256 {sha256(patched)} is not the expected {PATCHED_SHA256}")
    target = output_dir / "backend" / "ha-intents.ts"
    target.parent.mkdir(parents=True, exist_ok=False)
    target.write_bytes(patched)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="directory to write into; its backend/ subdirectory must not exist yet")
    args = parser.parse_args(argv)
    try:
        target = prepare(args.baseline, args.output_dir)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
