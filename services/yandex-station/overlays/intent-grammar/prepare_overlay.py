#!/usr/bin/env python3
"""Build the quasar overlay that asks the adapter's intent grammar first (no language model for ~90% of phrases).

The quasar fast-intent layer used to decide «который час», negation, wake-only and echo noise only after (or
without) the brain, and made an incomplete-slot phrase («Громкость.») wait ~0.96 s for the Haiku ASR corrector
(docs/research/intent-coverage-audit-2026-11.md §3.1, §4.3, §4.5). Three files change (README.md in this directory):

  backend/grammar-hook.ts  NEW, copied from the directory next to this script (adapter client, fail-open)
  backend/ha-intents.ts    the hook is called after a pending yes/no answer and the room follow-up were handled; its verdict is mapped
  backend/asr-correct.ts   the clarify tier is no longer eligible for the Haiku corrector

    prepare_overlay.py --ha-intents ha-intents.ts --asr-correct asr-correct.ts --output-dir NEW_DIRECTORY

The two inputs are files taken from the ACTIVE image (`docker cp aisis-quasar-prod-<color>:/src/backend/<file>`); each
SHA-256 must be a known one, every patch is an exact single-block replacement (no fuzz), and every result is
checked against its own SHA-256. `ha-intents.ts` is also accepted as the output of the speaker-room overlay
(`overlays/speaker-room`, which patches other blocks of the same file): the combined deploy builds that overlay first
and feeds this script its `backend/ha-intents.ts`. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to
/src): `NEW_DIRECTORY/backend/{ha-intents.ts,asr-correct.ts,grammar-hook.ts}`. Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

HOOK_SOURCE = Path(__file__).resolve().parent / "grammar-hook.ts"

# ha-intents.ts: SHA-256 of an accepted input -> SHA-256 of the patched result. The first is the active image's file, the
# second the speaker-room overlay's patched result (its README pins it); the hook blocks do not touch its hunks.
HA_INTENTS_PATCHED_SHA256 = {
    "245c920baafb0709493e30547c147ebc50d475513595c10c4663708478a3cca9": "782c69c48e5b3174e86e015bab1dd0f2b076314e5490f7d12ea18e84b26b39e1",
    "81fac5ca320c639471382b58fbf466f701b88ccf50beda895e75a443737e405b": "f4ab25c368fc5d6a39d280dcfa298eb3a11e261261fc9dae3dd7d82f36a494c3",
}
ASR_CORRECT_PATCHED_SHA256 = {
    "6587bc9624ff8268efd59589a73197882228e7ec12c20145ede656c75e27e5c5": "1f337435e1c4cebd6edf43480330229e128ad035afb3c80fa6664cd319afc48d",
}

HA_IMPORT_BEFORE = """\
import { composeNumeral, editDistance, NUMBER_WORDS, normalize, parseNumbers, splitSentences, tokenize } from './fuzzy-ru'
"""

HA_IMPORT_AFTER = HA_IMPORT_BEFORE + """\
import { grammarRoute } from './grammar-hook'
"""

HA_HOOK_BEFORE = """\
    // Pure arithmetic answers instantly: no HA roundtrip, no brain.
    const math = evaluateMath(s)
"""

HA_HOOK_AFTER = """\
    // Grammar first (adapter /intent/decide, no language model): after a pending answer was handled and the room
    // follow-up («а на кухне?», which needs the previous device command the grammar never sees) had its turn, before
    // the catalog and the brain. Fail-open: null (timeout, adapter down, GRAMMAR_HOOK=shadow|off) means the phrase goes
    // on through the tiers below exactly as before.
    const grammar = await grammarRoute(text, { dialogActive: options.dialogActive === true, speakerLevel })
    if (grammar !== null && grammar.route !== 'none') {
      let grammarResult: FastIntentResult | null = null
      if (grammar.route === 'reply' && grammar.text !== null) {
        grammarResult = grammar.listen
          ? { kind: 'reply', listen: true, quasarDirectives: [], text: grammar.text }
          : { kind: 'reply', quasarDirectives: [], text: grammar.text }
      } else if (grammar.route === 'silent') {
        grammarResult = DIALOG_END
      } else if (grammar.route === 'dialog_end') {
        pendingClarification = null
        grammarResult = DIALOG_END
      } else if (grammar.route === 'volume' && grammar.volume !== null) {
        const asked = grammar.volume
        const volume: VolumeIntent = { kind: 'set' }
        if (asked.target !== undefined) volume.target = asked.target
        else if (asked.deltaLevels !== undefined) {
          // Levels are the speaker's own 0..10 scale; without its reported level only the direction is known.
          if (speakerLevel !== null && Number.isFinite(speakerLevel)) volume.target = Math.min(1, Math.max(0, (speakerLevel + asked.deltaLevels) / MAX_LEVEL))
          else volume.steps = Math.sign(asked.deltaLevels)
        } else if (asked.steps !== undefined) volume.steps = asked.steps
        if (volume.target !== undefined || volume.steps !== undefined) {
          // «повтори» after a level change repeats the level it reached (a target), not the change: the recorded
          // command has no way to carry a delta in levels.
          if (trace !== undefined) trace.executed = { phrase: trace.phrase, resolved: { type: 'volume', volume } }
          // The step is read only for a relative ask, the same way executeResolved does.
          grammarResult = volumeResult(volume, speakerLevel, volume.steps === undefined ? DEFAULT_VOLUME_STEP_PCT : await volumeStepPct())
        }
      }
      if (grammarResult !== null) {
        logger.info(`Grammar route '${s}' -> ${grammar.intent ?? 'none'} (${grammar.route}, ${grammar.ms} ms)`)
        return grammarResult
      }
    }

    // Pure arithmetic answers instantly: no HA roundtrip, no brain.
    const math = evaluateMath(s)
"""

ASR_HEADER_BEFORE = """\
// ASR_CORRECT=on|shadow|off (default on). shadow: call and log, never act. Clear commands never reach this file:
// execute/answer/query/math decisions are not eligible, so they pay no latency.
"""

ASR_HEADER_AFTER = """\
// ASR_CORRECT=on|shadow|off (default on). shadow: call and log, never act. Clear commands never reach this file:
// execute/answer/query/math decisions are not eligible, so they pay no latency. Neither is the clarify tier («Громкость.»):
// that question is asked at once, and waiting ~850 ms for a corrector that times out only delayed it.
"""

ASR_TIERS_BEFORE = """\
const ELIGIBLE_TIERS: Record<string, true | undefined> = { clarify: true, confirm: true, llm: true, reprompt: true }
"""

ASR_TIERS_AFTER = """\
const ELIGIBLE_TIERS: Record<string, true | undefined> = { confirm: true, llm: true, reprompt: true }
"""

HA_INTENTS_PATCHES = ((HA_IMPORT_BEFORE, HA_IMPORT_AFTER, "the fuzzy-ru import"),
                      (HA_HOOK_BEFORE, HA_HOOK_AFTER, "the block after the pending-answer handling"))
ASR_CORRECT_PATCHES = ((ASR_HEADER_BEFORE, ASR_HEADER_AFTER, "the header comment"),
                       (ASR_TIERS_BEFORE, ASR_TIERS_AFTER, "ELIGIBLE_TIERS"))


class OverlayError(Exception):
    """A baseline is not the file this patch was made for, or the patch no longer applies exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_patches(baseline: str, patches: tuple[tuple[str, str, str], ...]) -> str:
    """Replace every BEFORE by its AFTER; each block must occur exactly once."""
    for before, after, label in patches:
        if baseline.count(before) != 1:
            raise OverlayError(f"{label} was not found exactly once")
        baseline = baseline.replace(before, after)
    return baseline


def patch_file(source: Path, patches: tuple[tuple[str, str, str], ...], accepted: dict[str, str], *, check_hashes: bool) -> bytes:
    """`accepted` maps the SHA-256 of each known input to the SHA-256 its patched result must have."""
    data = source.read_bytes()
    patched_sha = accepted.get(sha256(data))
    if check_hashes and patched_sha is None:
        raise OverlayError(f"{source.name} sha256 {sha256(data)} is not one of the expected {', '.join(accepted)}")
    patched = apply_patches(data.decode("utf-8"), patches).encode("utf-8")
    if check_hashes and sha256(patched) != patched_sha:
        raise OverlayError(f"patched {source.name} sha256 {sha256(patched)} is not the expected {patched_sha}")
    return patched


def prepare(ha_intents: Path, asr_correct: Path, output_dir: Path, *, check_hashes: bool = True,
            hook_source: Path = HOOK_SOURCE) -> Path:
    if output_dir.exists():
        raise OverlayError(f"{output_dir} already exists")
    files = {
        "ha-intents.ts": patch_file(ha_intents, HA_INTENTS_PATCHES, HA_INTENTS_PATCHED_SHA256, check_hashes=check_hashes),
        "asr-correct.ts": patch_file(asr_correct, ASR_CORRECT_PATCHES, ASR_CORRECT_PATCHED_SHA256, check_hashes=check_hashes),
        "grammar-hook.ts": hook_source.read_bytes(),
    }
    target = output_dir / "backend"
    target.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        (target / name).write_bytes(content)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ha-intents", type=Path, required=True)
    parser.add_argument("--asr-correct", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="must not exist yet")
    args = parser.parse_args(argv)
    try:
        target = prepare(args.ha_intents, args.asr_correct, args.output_dir)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
