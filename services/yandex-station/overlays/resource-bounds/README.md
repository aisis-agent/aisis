# One heard phrase or one audio stream can no longer take the quasar's memory or its event loop

Three files of the quasar image (`/src/backend/fuzzy-ru.ts`, `/src/codecs/opus-processor.ts`, `/src/codecs/ogg-stitcher.ts`), two
findings (4 and 5) of the retro-review of the aisis-quasar patch range (ticket #186); the fix is ticket #202, and the same change is a commit
of the aisis-quasar fork (patch + `check_*.mts` + CI step), so the next tag carries it and this overlay is retired.

## Finding 4: `fuzzy-ru` had no input bound

**Why it matters.** Every phrase the fast-intent layer sees (the Station's speech through STT, a typed text turn) goes through `normalize`,
`splitSentences` and `tokenize` into `wordSimilarity` / `editDistance` against the catalog and the entity names. `editDistance` built an
`(n+1) × (m+1)` table of arrays, and `wordSimilarity` compares the WHOLE heard word with every vocabulary stem: one very long word costs time
and memory proportional to its length for every stem, in the one Node process that serves all speakers. Each sentence is also a full routing pass.

Measured on the real routing (`decideFastIntent("включи ссс…")`, one word of N letters, fixture registry of three entities):

| word length | before | after |
|---|---|---|
| 1 000 | 43 ms | 2 ms |
| 10 000 | 335 ms | 1 ms |
| 100 000 | 3.9 s | 6 ms |
| 1 000 000 | **51.5 s**, process RSS 771 MB → 1 979 MB | 53 ms, RSS 788 MB |

`editDistance` alone on two 5 000-letter words (25 M cells) is ~0.3 s of event loop before, instant after. The word COUNT is linear, not
quadratic (1 000 words 41 ms, 10 000 195 ms, 100 000 1.9 s before; 1 / 6 / 68 ms after). The sentence count is linear too but each sentence costs
~3.5 ms: 1 000 short sentences held the process **3.5 s** before (5 000: 16.5 s, 2 000: 13-15 s in the check), 63 ms after (60 000 sentences: 102 ms).

**Reachable?** Yes in principle: any text that reaches the fast-intent layer does it. The Station's recognizer rarely produces such text, so the
practical exposure is a text turn, or anyone able to open the quasar's WebSocket (its authentication is a separate finding). Impact:
availability only (the process stalls, memory spikes); no command executes from garbage, but the one process is every speaker's.

**The change** (`fuzzy-ru.ts`, one block: `normalize`, `splitSentences`, `tokenize`, `editDistance`):

* `normalize`, the entry point of every router (the exact layer, the catalog, entity and music queries all start from `normalize(text)`), returns
  `''` for a text longer than `MAX_TEXT_CHARS = 2000` characters (a run of 60 000 dots is one short «word» to `tokenize` but made the calculator's trailing-dot cleanup quadratic: 2.5 s; found by the review), with a word over `MAX_TOKEN_LENGTH = 64` letters, more than `MAX_TOKENS = 200` words (counted the way `tokenize` splits, commas included) or more than `MAX_SENTENCES = 16`
  sentences: such a text is no command and nothing matches it. The first design of this change cut the word / the list instead; the review of
  it showed why that is wrong: a numeral cut to 64 characters is another number (`громкость 000…010` became 1), and a cut list lost the
  last word that `endsMidPhrase` reads, so a cut-off «включи кондиционер на» behind 198 filler words executed. A refused text is never read
  from its remainder. `tokenize` is unchanged; `splitSentences` returns nothing for an over-limit text, so not even its first sentences are
  routed alone, and the whole-text pass sees `''`. The 466-row corpus's longest word is 13 letters, its longest phrase 6 words. Real text is unchanged.
* `editDistance` answers «far apart» (the longer length) for a string over 64 letters instead of building a table, and uses three rolling rows
  instead of a full matrix. Differentially tested against the old full-matrix implementation on 3 000 random pairs.

A refused text goes on to the brain as an unmatched phrase, exactly like any phrase the fast layer does not know. Corpus 466/466, dialogs 195/195
with the change. The check also pins the hostile cases that the first design got wrong (cut-off behind filler words, a 65-digit numeral,
a command in the 17th sentence): none of them executes.

## Finding 5: `opus-processor` retained packets after the limit; `ogg-stitcher` lost duration

**opus-processor.** A packet is assembled from page segments (lacing 255 = «more of the same packet»). Over 1 MiB it threw, but:
(a) the segments collected so far stayed in `packetSegments`, (b) each segment was a `subarray` of the transport frame, so one 255-byte segment
pinned a whole frame of up to 1 MiB, (c) the callers (`gigaam.ts`, `buffered.ts`) catch the error and feed the next message, which pushed another
segment before the limit check threw again. A peer sending a continued page of one 255-byte segment followed by junk up to the 1 MiB frame limit
made the process keep one frame per message, without end: 150 such messages retained **151 MiB** (the check); the growth is linear in the messages.

**Reachable?** The audio stream of the STT / audio-metadata session, i.e. anyone who can open the quasar WebSocket and send binary messages
(not only the Station); impact is memory exhaustion (availability). Not a correctness issue of normal speech.

**The change:** segments are copied (`Buffer.from`), a tripped limit calls `fail()` (clears the segments, the byte counter and the retained
partial page, sets `failed`), and a failed processor refuses every later message at the door. The same 150 messages retain 1.0 MiB. The
1 MiB limit is unchanged and is now one constant, `MAX_PACKET_BYTES`.

**ogg-stitcher.** `append` mapped the first audio page's END granule onto the running total (`baseGranule`), so the first audio page of every
segment after the first added no duration: a stitched stream of N sentences was shorter by the first page of N-1 sentences, and two pages
claimed the same end granule. Check: three segments with end granules `1920, 4800`; before `1920, 4800, 4800, 7680, 7680, 10560`, after
`1920, 4800, 6720, 9600, 11520, 14400`. A segment's granules are samples decoded from ITS start (its pre-skip included; the decoder drops only the
stream's first pre-skip), so they lie on the running total as they are; the same applies to a segment whose first pages end below its pre-skip
(before `120, 240, 360, 360, 480, 600`, after `120 … 720`). Impact: a wrong granule position / duration of the TTS stream (a player that trusts the
granules misreports or clips the length), not exploitable. NOT covered: (a) the encoder's pre-skip warm-up of every later segment is still decoded as
audio (a few ms at each join); removing it needs re-encoding. (b) A segment's final granule may exclude the encoder's end padding, which is still
decoded once the end-of-stream flag is removed from it; the exact sample count needs the packets' TOC durations (packets spanning pages must be
reassembled), so a few ms per join can still be unaccounted for. This is the old behavior, not a new one; the fix above removes the much larger loss.
(c) A segment is assumed to be a fresh encode whose granules start at zero (not verified against the TTS encoder); a segment cut out of a longer
stream with a nonzero granule origin would add that origin to the running total.

## Build, check, deploy

The input is the ACTIVE image's three files (SHA-256 pinned, no fuzz; no other pending overlay touches them, so this composes with
`correction-hook`, `ac-defer-to-adapter`, `speaker-room` and `uniproxy-close-log` in one merged overlay directory: it
owns `backend/fuzzy-ru.ts`, `codecs/opus-processor.ts`, `codecs/ogg-stitcher.ts` and nothing else).

```
overlay=$(./run_check.sh 2>/dev/null | tail -n 1)   # builds the overlay from the active colour's files (hash-checked), then in scratch
                                                    # containers with --network none: both checks WITH the overlay (pass) and WITHOUT it
                                                    # (both must fail); the last line is the overlay directory
python3 prepare_overlay.py --baseline-dir DIR --output-dir NEW_DIR   # what run_check.sh does; DIR mirrors /src (backend/, codecs/)
python3 -m unittest discover -s .                   # the patch applier (CI)
```

The deploy is part of ONE combined flip with the other pending overlays (deploy owner: Main), with a corpus receipt for the exact
candidate image (`--corpus-receipt`, [corpus-runner/README.md](../../corpus-runner/README.md)); nothing here builds, deploys or flips.

`check_fuzzy_bounds.mts` (mounted at `/src/backend/`) and `check_codecs.mts` (at `/src/codecs/`) are the fork's checks, byte for byte. Against the
unpatched image: fuzzy 16 FAIL of 28 (a 5 000-letter `editDistance`, the word / word-count / sentence bounds of `normalize`, `wordSimilarity` of a 1M word,
300k-letter word 13-16 s, 100k words 2 s, 2 000 sentences 13-15 s, a 65-digit numeral and four commands in over-limit texts that execute), codecs 6 FAIL of 10
(stitched granules ×2, 101 MiB retained below the limit, 151 MiB after it, no latched failure after the limit ×2); with the overlay 0 FAIL. The codecs check stubs `@discordjs/opus` (it never decodes).

## Provenance

Baseline = the active image `aisis-quasar:volume-zero-e859a37b` (tag `aisis-volume-zero-e859a37b` of the fork):

| file | baseline SHA-256 | patched SHA-256 |
|---|---|---|
| `backend/fuzzy-ru.ts` | `10d7a97099c03da593db5ba55e89b823c3b57e0eb6a9964f0b716d0e71646262` | `fc1ec494ce6fad2fa53888b970c05f7c2f45c727a2e44336a7d5c46e8e36e38e` |
| `codecs/opus-processor.ts` | `81719dcc18203bc373db6820aeb99b09b068209b6c9ddad2c6d16916cba38bab` | `5499b93790566c9fd54b0f7fe4726228fdb10bf0ccfac042ef3e86f2214f72cc` |
| `codecs/ogg-stitcher.ts` | `04951ba7885b12e518bbcec4a2478fc40241c22ff61abf85e3052ce5a8f8631f` | `ce349c2f64525a34f34a73eba78622ef4a2e7375ec9a1bb3ff2096aac22329fc` |

The patched files are byte-identical to the fork commit's. After a later deploy changes a file the hash changes and `prepare_overlay.py`
refuses until the patch is rebased: it must never be applied blind.

## Rollback

Deploy the previous image by its immutable ID through `deploy.sh` (the blue/green flip back), or drop this overlay from the merged overlay
directory and rebuild. The change is behavior-only for hostile input (words over 64 letters, more than 200 words, more than 16 sentences, an oversized
packet) and for the granules of stitched TTS; a normal phrase and a normal stream are unchanged.
