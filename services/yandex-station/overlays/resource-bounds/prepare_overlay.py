#!/usr/bin/env python3
"""Build the quasar overlay that bounds the resources one heard phrase or one audio stream can take (ticket #202, P1 4 and 5).

Three source files of the active image, three findings of the retro-review of the aisis-quasar patch range (#186):

  backend/fuzzy-ru.ts       one 1M-letter «word» made every fuzzy vocabulary comparison O(length) in time and memory, in the one
                            process that serves every speaker: normalize refuses a text with a word over 64 letters, over 200 words or over 16 sentences (returns ''),
                            splitSentences returns nothing for it, editDistance keeps three rows instead of a matrix and answers «far apart» above 64 letters.
  codecs/opus-processor.ts  a packet over the 1 MiB limit threw but kept its segments (each a view that pins a whole transport frame)
                            and the processor kept accepting data: segments are copied, the processor is failed and emptied on a trip.
  codecs/ogg-stitcher.ts    the first audio page of every appended segment lost its duration (its end granule became the base):
                            granules now lie on the running total as they are, so the stitched stream's granule is the sum.

    prepare_overlay.py --baseline-dir DIR --output-dir NEW_DIRECTORY

`--baseline-dir` mirrors /src and holds the three files taken from the ACTIVE image
(`docker cp aisis-quasar-prod-<color>:/src/backend/fuzzy-ru.ts …`); each file's SHA-256 must be the known one, the patch is exact single-block
replacements (every anchor occurs exactly once, no fuzz), and each result is checked against its own SHA-256. No other pending overlay
touches these files. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to /src) holding only the three changed files.
Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

FUZZY_OLD = r"""export function normalize (text: string): string {
  return text.toLowerCase()
    .replaceAll('ё', 'е')
    .replace(/[^a-zа-я0-9\s+\-*/.,×÷]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

// ASR punctuates: «Кромкость 10.  Смотри, вот.» is two sentences. A dot inside a
// number («22.5») is not followed by whitespace and stays.
export function splitSentences (raw: string): string[] {
  return raw.split(/(?<=[.!?…])\s+/).map(part => part.trim()).filter(part => part !== '')
}

// Words of a normalized string. Sentence punctuation glued to a word («10.»,
// «свет,») is dropped; a dot or comma between digits stays (decimal «22,5»).
export function tokenize (normalized: string): string[] {
  return normalized.replace(/(?<!\d)[.,]|[.,](?!\d)/g, ' ').split(/\s+/).filter(token => token !== '')
}

// Optimal-string-alignment distance: Levenshtein where one adjacent swap
// («гормкость») costs 1 — the commonest ASR/typo slip.
export function editDistance (a: string, b: string): number {
  if (a === b) return 0
  if (a.length === 0) return b.length
  if (b.length === 0) return a.length
  const rows: number[][] = Array.from({ length: a.length + 1 }, (_, i) => {
    const row = new Array<number>(b.length + 1).fill(0)
    row[0] = i
    return row
  })
  for (let j = 0; j <= b.length; j++) rows[0][j] = j
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      let best = Math.min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        best = Math.min(best, rows[i - 2][j - 2] + 1)
      }
      rows[i][j] = best
    }
  }
  return rows[a.length][b.length]
}
"""

FUZZY_NEW = r"""// A heard or typed phrase is attacker-sized text: one 1M-letter «word» made every vocabulary comparison
// O(length) in time and memory (51 s and 2 GB of the one process serving every speaker), and every sentence is a
// full routing pass of its own (3.5 ms each). No command, Russian word or number approaches these bounds, so a
// text over any of them is no command at all: normalize returns '' and every router that starts from it (the exact
// layer, the catalog, entity and music queries) finds nothing, instead of acting on a cut-down remainder (a numeral
// cut short is another number, a cut-off «… на» no longer ends the phrase).
export const MAX_TOKEN_LENGTH = 64
export const MAX_TOKENS = 200
export const MAX_SENTENCES = 16
export const MAX_TEXT_CHARS = 2000

export function normalize (text: string): string {
  // A run of punctuation is one short «word» to tokenize yet makes the trailing-dot cleanup of the calculator quadratic
  // (60k dots: 2.5 s), so the whole text is bounded too, before any work is done on it.
  if (text.length > MAX_TEXT_CHARS) return ''
  if (text.trim().split(/(?<=[.!?…])\s+/).length > MAX_SENTENCES) return ''
  const normalized = text.toLowerCase()
    .replaceAll('ё', 'е')
    .replace(/[^a-zа-я0-9\s+\-*/.,×÷]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  // The words the routers will see (tokenize also splits «пожалуйста,пожалуйста»), not the space-separated chunks.
  const words = tokenize(normalized)
  return words.length > MAX_TOKENS || words.some(word => word.length > MAX_TOKEN_LENGTH) ? '' : normalized
}

// ASR punctuates: «Кромкость 10.  Смотри, вот.» is two sentences. A dot inside a
// number («22.5») is not followed by whitespace and stays. A text over any bound is no command (see normalize):
// none of its sentences is routed, not even the first ones.
export function splitSentences (raw: string): string[] {
  if (raw.length > MAX_TEXT_CHARS) return []
  const sentences = raw.split(/(?<=[.!?…])\s+/).map(part => part.trim()).filter(part => part !== '')
  return sentences.length > MAX_SENTENCES || normalize(raw) === '' ? [] : sentences
}

// Words of a normalized string. Sentence punctuation glued to a word («10.»,
// «свет,») is dropped; a dot or comma between digits stays (decimal «22,5»).
export function tokenize (normalized: string): string[] {
  return normalized.replace(/(?<!\d)[.,]|[.,](?!\d)/g, ' ').split(/\s+/).filter(token => token !== '')
}

// Optimal-string-alignment distance: Levenshtein where one adjacent swap
// («гормкость») costs 1 — the commonest ASR/typo slip. Three rolling rows, not a full matrix. A string
// longer than MAX_TOKEN_LENGTH is no word: «far apart», its own length, instead of an O(n·m) table.
export function editDistance (a: string, b: string): number {
  if (a === b) return 0
  if (a.length === 0) return b.length
  if (b.length === 0) return a.length
  if (a.length > MAX_TOKEN_LENGTH || b.length > MAX_TOKEN_LENGTH) return Math.max(a.length, b.length)
  let beforePrevious: number[] = new Array<number>(b.length + 1).fill(0)
  let previous: number[] = Array.from({ length: b.length + 1 }, (_, j) => j)
  let current: number[] = new Array<number>(b.length + 1).fill(0)
  for (let i = 1; i <= a.length; i++) {
    current[0] = i
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      let best = Math.min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        best = Math.min(best, beforePrevious[j - 2] + 1)
      }
      current[j] = best
    }
    const recycled = beforePrevious
    beforePrevious = previous
    previous = current
    current = recycled
  }
  return previous[b.length]
}
"""

PROCESSOR_CLASS_OLD = """\
export class OpusProcessor {
"""

PROCESSOR_CLASS_NEW = """\
const MAX_PACKET_BYTES = 1024 * 1024

export class OpusProcessor {
"""

PROCESSOR_FIELD_OLD = """\
  private streamPeak = 0
"""

PROCESSOR_FIELD_NEW = """\
  private streamPeak = 0
  // Set when a packet outgrew the limit: the stream is broken and a peer that keeps sending must not keep a buffer alive.
  private failed = false
"""

PROCESSOR_ENTRY_OLD = """\
  async handleAudioData (audioData: Buffer): Promise<void> {
"""

PROCESSOR_ENTRY_NEW = """\
  async handleAudioData (audioData: Buffer): Promise<void> {
    if (this.failed) throw new Error('Opus stream closed after an oversized packet')
"""

PROCESSOR_PAGE_OLD = """\
  private async handleOpusPage (page: OggPage): Promise<void> {
"""

PROCESSOR_PAGE_NEW = """\
  private fail (): void {
    this.failed = true
    this.packetSegments = []
    this.packetBytes = 0
    this.pageBytes = Buffer.alloc(0)
  }

  private async handleOpusPage (page: OggPage): Promise<void> {
"""

PROCESSOR_PUSH_OLD = """\
      this.packetSegments.push(segment)
      this.packetBytes += segment.length
      if (this.packetBytes > 1024 * 1024) throw new Error('Ogg packet exceeds voice input limit')
"""

PROCESSOR_PUSH_NEW = """\
      // A copy: a segment is a view into the whole transport frame (up to 1 MiB), and a packet that never
      // terminates would pin one frame per 255 bytes it collects.
      this.packetSegments.push(Buffer.from(segment))
      this.packetBytes += segment.length
      if (this.packetBytes > MAX_PACKET_BYTES) {
        this.fail()
        throw new Error('Ogg packet exceeds voice input limit')
      }
"""

STITCHER_BASE_OLD = """\
    if (this.serial === null) {
      this.serial = pages[0].streamSerialNumber
    }
    // Granule offset: first audio page's granule maps onto the running total.
    const baseGranule = audioPages[0].absoluteGranulePosition
"""

STITCHER_BASE_NEW = """\
    // A page's granule is the number of 48 kHz samples the decoder has produced up to its END. A later segment
    // is decoded in full (only the stream's first pre-skip is dropped), so its granules lie on the running total
    // as they are: the first audio page counts too, and so does the pre-roll of a segment whose first pages end
    // below its pre-skip. The total is the sum of the segments' final granules.
    if (this.serial === null) {
      this.serial = pages[0].streamSerialNumber
    }
"""

STITCHER_PAGE_OLD = """\
      const granule = this.granule + (page.absoluteGranulePosition - baseGranule)
"""

STITCHER_PAGE_NEW = """\
      const granule = this.granule + page.absoluteGranulePosition
"""

STITCHER_TOTAL_OLD = """\
    this.granule += (audioPages[audioPages.length - 1].absoluteGranulePosition - baseGranule)
"""

STITCHER_TOTAL_NEW = """\
    this.granule += audioPages[audioPages.length - 1].absoluteGranulePosition
"""

# path under /src -> (input sha256, result sha256, [(what, old block, new block)])
FILES = {
    "backend/fuzzy-ru.ts": (
        "10d7a97099c03da593db5ba55e89b823c3b57e0eb6a9964f0b716d0e71646262",
        "fc1ec494ce6fad2fa53888b970c05f7c2f45c727a2e44336a7d5c46e8e36e38e",
        [("the normalize, splitSentences, tokenize and editDistance block", FUZZY_OLD, FUZZY_NEW)],
    ),
    "codecs/opus-processor.ts": (
        "81719dcc18203bc373db6820aeb99b09b068209b6c9ddad2c6d16916cba38bab",
        "5499b93790566c9fd54b0f7fe4726228fdb10bf0ccfac042ef3e86f2214f72cc",
        [
            ("the class declaration", PROCESSOR_CLASS_OLD, PROCESSOR_CLASS_NEW),
            ("the stream peak field", PROCESSOR_FIELD_OLD, PROCESSOR_FIELD_NEW),
            ("handleAudioData", PROCESSOR_ENTRY_OLD, PROCESSOR_ENTRY_NEW),
            ("handleOpusPage", PROCESSOR_PAGE_OLD, PROCESSOR_PAGE_NEW),
            ("the packet segment push and its limit", PROCESSOR_PUSH_OLD, PROCESSOR_PUSH_NEW),
        ],
    ),
    "codecs/ogg-stitcher.ts": (
        "04951ba7885b12e518bbcec4a2478fc40241c22ff61abf85e3052ce5a8f8631f",
        "ce349c2f64525a34f34a73eba78622ef4a2e7375ec9a1bb3ff2096aac22329fc",
        [
            ("the base granule", STITCHER_BASE_OLD, STITCHER_BASE_NEW),
            ("the page granule", STITCHER_PAGE_OLD, STITCHER_PAGE_NEW),
            ("the running total", STITCHER_TOTAL_OLD, STITCHER_TOTAL_NEW),
        ],
    ),
}


class OverlayError(Exception):
    """An input is not the file this patch was made for, or the patch no longer applies exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def apply_blocks(text: str, blocks: list[tuple[str, str, str]]) -> str:
    """Replace each old block by its new one; every old block must occur exactly once in the ORIGINAL text, and no two may overlap."""
    for what, old, _new in blocks:
        if text.count(old) != 1:
            raise OverlayError(f"{what} was not found exactly once")
    placed = sorted((text.index(old), old, new) for _what, old, new in blocks)
    if any(first[0] + len(first[1]) > second[0] for first, second in zip(placed, placed[1:])):
        raise OverlayError("two blocks overlap")
    pieces, position = [], 0
    for start, old, new in placed:
        pieces.append(text[position:start])
        pieces.append(new)
        position = start + len(old)
    pieces.append(text[position:])
    return "".join(pieces)


def prepare(baseline_dir: Path, output_dir: Path, *, check_hashes: bool = True) -> list[Path]:
    results: dict[str, bytes] = {}
    for relative, (input_sha, result_sha, blocks) in FILES.items():
        data = (baseline_dir / relative).read_bytes()
        if check_hashes and sha256(data) != input_sha:
            raise OverlayError(f"{relative}: sha256 {sha256(data)} is not the expected {input_sha}")
        patched = apply_blocks(data.decode("utf-8"), blocks).encode("utf-8")
        if check_hashes and sha256(patched) != result_sha:
            raise OverlayError(f"{relative}: patched sha256 {sha256(patched)} is not the expected {result_sha}")
        results[relative] = patched
    written = []
    for relative, patched in results.items():
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise OverlayError(f"{target} exists already")
        target.write_bytes(patched)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline-dir", type=Path, required=True, help="a copy of the active image's /src with the three files")
    parser.add_argument("--output-dir", type=Path, required=True, help="directory to write backend/ and codecs/ into")
    args = parser.parse_args(argv)
    try:
        written = prepare(args.baseline_dir, args.output_dir)
    except (OverlayError, OSError) as err:
        print(f"prepare_overlay: {err}", file=sys.stderr)
        return 1
    for target in written:
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
