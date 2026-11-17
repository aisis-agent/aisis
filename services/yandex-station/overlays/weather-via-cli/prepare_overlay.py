#!/usr/bin/env python3
"""Build the quasar overlay that hands weather questions to the adapter (weather-cli) instead of the HA entity.

The quasar fast-intent layer answers «какая погода» / «сколько градусов на улице» itself, from the Home Assistant
weather entity (`matchEntityQuery` in backend/entity-index.ts), so the adapter never saw those turns and weather-cli
never ran. The one change: the questions in the handoff table (diverted_phrases.json: whole utterances the adapter is
proven to answer) return "none" there, the turn goes on to the brain tier, and the adapter's voice_skills answers it with
`weather say`. Every other question keeps its entity answer (README.md in this directory).

    prepare_overlay.py --baseline entity-index.ts --output-dir NEW_DIRECTORY

`--baseline` is the file taken from the ACTIVE image (`docker cp aisis-quasar-prod-<color>:/src/backend/entity-index.ts`);
its SHA-256 must be the known one, the patch is an exact single-block replacement (no fuzz), and the result is
checked against its own SHA-256. The output directory is a deploy.sh `--overlay SRC_DIR` (it maps to /src):
`NEW_DIRECTORY/backend/entity-index.ts`. Nothing is deployed or restarted here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

BASELINE_SHA256 = "73474b5c8e60daa38f62f700cdbe787cd1badb77690b3733a7c93315ba41235f"
PATCHED_SHA256 = "c24efb900b0c9ad4ad74aaf30723add07c55cd639799882e2430117f96308d10"

HANDOFF_PATH = Path(__file__).resolve().parent / "diverted_phrases.json"

# The patch has two exact anchors: the weather branch of matchEntityQuery and the entry-point banner above it
# (where the generated handoff table is inserted).
BEFORE = """\
  // «на улице», «какая погода»: the weather entity.
  if (parsed.outdoor || parsed.weather) {
    if (parsed.subject.length > 0 && !parsed.outdoor) return NONE // «погода в Париже»
"""

AFTER = """\
  // «на улице», «какая погода»: the weather entity, except the questions in the handoff table: those go on to the
  // brain tier, where the adapter answers them with weather-cli (one voice for every weather question, forecast and
  // rain decided from the same data). Anything not in the table keeps its entity answer.
  if (parsed.outdoor || parsed.weather) {
    if (parsed.subject.length > 0 && !parsed.outdoor) return NONE // «погода в Париже»
    if (handedToWeatherCli(text)) return NONE
"""

TABLE_ANCHOR = "// ── Entry point ──────────────────────────────────────────────────────────────\n"

TABLE = """\
// ── Weather handoff (weather-via-cli overlay) ────────────────────────────────
// Whole normalized utterances (lead-in words removed) that the adapter answers with weather-cli. Generated from
// diverted_phrases.json; the adapter's tests prove it answers every entry.
const WEATHER_LEAD_INS = new Set(@LEAD_INS@)
const WEATHER_HANDOFF = new Set(@PHRASES@)

function handedToWeatherCli (text: string): boolean {
  const words = tokenize(normalize(text)).filter(token => !WEATHER_LEAD_INS.has(token))
  return WEATHER_HANDOFF.has(words.join(' '))
}

"""


class OverlayError(Exception):
    """The baseline is not the file this patch was made for, or the patch no longer applies exactly."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_handoff(path: Path = HANDOFF_PATH) -> dict[str, list[str]]:
    """The handoff table; every entry must already be normalized (lower case, ё folded, single spaces, no punctuation)."""
    table = json.loads(path.read_text(encoding="utf-8"))
    for key in ("lead_ins", "phrases"):
        entries = table[key]
        if not entries or entries != sorted(set(entries)):
            raise OverlayError(f"{key} in {path.name} must be a non-empty sorted list without duplicates")
        for entry in entries:
            if entry != " ".join(re.sub(r"[^\w]+", " ", entry.lower().replace("ё", "е")).split()):
                raise OverlayError(f"{key} entry {entry!r} in {path.name} is not normalized")
    return table


def apply_patch(baseline: str, handoff: dict[str, list[str]]) -> str:
    """Replace BEFORE by AFTER and insert the handoff table; each anchor must occur exactly once."""
    if baseline.count(BEFORE) != 1:
        raise OverlayError("the weather branch of matchEntityQuery was not found exactly once")
    if baseline.count(TABLE_ANCHOR) != 1:
        raise OverlayError("the entry-point banner was not found exactly once")
    table = (TABLE.replace("@LEAD_INS@", json.dumps(handoff["lead_ins"], ensure_ascii=False))
                  .replace("@PHRASES@", json.dumps(handoff["phrases"], ensure_ascii=False)))
    return baseline.replace(BEFORE, AFTER).replace(TABLE_ANCHOR, table + TABLE_ANCHOR)


def prepare(baseline_path: Path, output_dir: Path, *, check_hashes: bool = True) -> Path:
    data = baseline_path.read_bytes()
    if check_hashes and sha256(data) != BASELINE_SHA256:
        raise OverlayError(f"baseline sha256 {sha256(data)} is not the expected {BASELINE_SHA256}")
    patched = apply_patch(data.decode("utf-8"), load_handoff()).encode("utf-8")
    if check_hashes and sha256(patched) != PATCHED_SHA256:
        raise OverlayError(f"patched sha256 {sha256(patched)} is not the expected {PATCHED_SHA256}")
    target = output_dir / "backend" / "entity-index.ts"
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
