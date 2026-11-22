#!/usr/bin/env python3
"""Offline replay of the extra dictionary over the stored clips (aisis ticket #268): what it would have changed, counted, nothing more.

  replay   every stored clip with a transcript (voice_clips status `sent` or `activation`, newest first, at most --limit) through the extra
           dictionary (extra_dict.py): n clips, n corrected, n whose routing changed, and, where the owner ruled on the clip (clip_truth), n
           changed wrongly (a clip the owner called correct or noise that the dictionary changed) and n moved toward the owner's text. The
           corrected clips are listed with their before and after so a human reads them.
  mine     the words at the head of the stored transcripts that could be a wake-word mishearing (within --max-edits of «алиса», or already
           known), with n, n of them the whole segment, the trigger split and who already covers each: the grammar's lexicon.wake, the
           sound rule of wake_word.py, the dictionary, or nobody.

Read-only: the database is opened with `mode=ro`, no audio is read, nothing is written, no recogniser or Home Assistant is called. The report
holds counts and the clips themselves; it makes no accuracy claim and extrapolates nothing: the owner's verdicts are a handful, and the clips are
one household's speech.

    bench_extra_dict.py replay [--db FILE] [--limit N]
    bench_extra_dict.py mine [--db FILE] [--limit N] [--max-edits K]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import extra_dict
import wake_word

DB = Path(os.environ.get("AISIS_DIALOGS_DB") or Path.home() / ".local/share/aisis/dialogs.sqlite3")
DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
MAX_EDITS = 3
STATUSES = ("sent", "activation")


def open_readonly(db: str | os.PathLike[str]) -> sqlite3.Connection:
    return sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)   # as_uri escapes '#' and '?' in the path


def stored(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    """The newest `limit` clips with a transcript and a status of STATUSES, each with the owner's verdict (clip_truth) when there is one."""
    rows = conn.execute(
        "SELECT c.id, c.ts, c.status, c.trigger, c.transcript, t.label, t.truth FROM voice_clips c "
        "LEFT JOIN clip_truth t ON t.clip_id = c.id WHERE c.transcript IS NOT NULL AND c.transcript != '' AND c.status IN (%s) "
        "ORDER BY c.ts DESC, c.id DESC LIMIT ?" % ",".join("?" * len(STATUSES)), (*STATUSES, limit)).fetchall()
    return [dict(zip(("id", "ts", "status", "trigger", "transcript", "label", "truth"), row)) for row in rows]


def request_of(text: str) -> str:
    """What routing receives for `text`: the activation word cut from the head (wake_word.split); an activation is the empty request."""
    return wake_word.split(text).clean


def toward(truth: str, after: str) -> bool:
    """The text after the dictionary is the owner's text, the name and punctuation aside."""
    return extra_dict.fold(request_of(after)) == extra_dict.fold(request_of(truth))


def replay(clips: list[dict[str, Any]], dictionary: extra_dict.Dictionary) -> dict[str, Any]:
    """Counts and the corrected clips: pure, takes the clips `stored` returns."""
    report: dict[str, Any] = {"n_clips": len(clips), "n_with_verdict": sum(1 for clip in clips if clip["label"]), "n_corrected": 0,
                              "n_routing_changed": 0, "n_not_at_head": 0, "n_changed_wrongly": 0, "n_moved_toward_truth": 0,
                              "n_changed_with_verdict_not_toward_truth": 0, "n_entry_source_clips": 0, "kinds": Counter(), "corrected": []}
    for clip in clips:
        found = dictionary.correct(clip["transcript"])
        if not found.hits:
            continue
        report["n_corrected"] += 1
        report["kinds"].update(hit.kind for hit in found.hits)
        before, after = wake_word.split(clip["transcript"]), wake_word.split(found.text)
        changed_routing = (before.clean, before.only) != (after.clean, after.only)
        report["n_routing_changed"] += changed_routing
        report["n_not_at_head"] += not _wake_hits_at_head(clip["transcript"], found.hits)
        verdict = clip["label"]
        outcome = ""
        if verdict in ("correct", "noise"):
            report["n_changed_wrongly"] += 1
            outcome = "changed_wrongly"
        elif verdict == "corrected":
            if toward(clip["truth"], found.text):
                report["n_moved_toward_truth"] += 1
                outcome = "toward_truth"
            else:
                report["n_changed_with_verdict_not_toward_truth"] += 1
                outcome = "not_toward_truth"
        own_source = any(clip["id"] in entry.note for entry in dictionary.entries if entry.id in {hit.entry for hit in found.hits})
        report["n_entry_source_clips"] += own_source   # a clip an entry was written from confirms nothing: the entry is its own evidence there
        report["corrected"].append({"id": clip["id"], "trigger": clip["trigger"], "verdict": verdict, "outcome": outcome, "before": clip["transcript"],
                                    "after": found.text, "routing_changed": changed_routing, "entries": sorted({hit.entry for hit in found.hits}),
                                    "entry_source": own_source})
    return report


def _wake_hits_at_head(text: str, hits: tuple[extra_dict.Hit, ...]) -> bool:
    """Every wake correction stands behind nothing but lead-ins and the corrections before it («Эй, Олеса, Олеся»)."""
    wake = sorted((hit for hit in hits if hit.kind == "wake"), key=lambda hit: hit.start)
    cursor = 0
    for hit in wake:
        if not all(word.lower() in wake_word.LEAD_INS for word in extra_dict.WORD.findall(text[cursor:hit.start])):
            return False
        cursor = hit.end
    return True


def sound_rule_covers(word: str) -> bool:
    return wake_word.is_wake(word, standalone=True) and word not in wake_word.lexicon()


def covered_by(word: str, dictionary: extra_dict.Dictionary) -> str:
    folded = extra_dict.fold(word)
    if folded in wake_word.lexicon():
        return "lexicon"
    if any(variant.text == folded for entry in dictionary.of_kind("wake") for variant in entry.variants):
        return "dictionary"
    if sound_rule_covers(folded):
        return "sound-rule (whole segment only)"
    return "nobody"


def mine(clips: list[dict[str, Any]], dictionary: extra_dict.Dictionary, max_edits: int = MAX_EDITS) -> list[dict[str, Any]]:
    """The head words that could be a wake-word mishearing, most frequent first."""
    seen: dict[str, dict[str, Any]] = {}
    for clip in clips:
        words = extra_dict.WORD.findall(clip["transcript"])
        if not words:
            continue
        head = extra_dict.fold(words[0])
        cover = covered_by(head, dictionary)
        known = cover != "nobody"
        if head == wake_word.NAME or not (known or wake_word._edits(head, wake_word.NAME) <= max_edits):
            continue
        row = seen.setdefault(head, {"word": head, "n": 0, "whole_segment": 0, "triggers": Counter(), "verdicts": Counter(), "covered_by": cover})
        row["n"] += 1
        row["whole_segment"] += len(words) == 1
        row["triggers"][clip["trigger"] or "-"] += 1
        row["verdicts"][clip["label"] or "none"] += 1
    return sorted(seen.values(), key=lambda row: (-row["n"], row["word"]))


def print_replay(report: dict[str, Any]) -> None:
    print("clips replayed (newest first, bounded): %d, of them with an owner verdict: %d" % (report["n_clips"], report["n_with_verdict"]))
    print("corrected by the dictionary: %d (kinds: %s)" % (report["n_corrected"], dict(report["kinds"]) or "-"))
    print("routing changed (the request or the activation verdict differs): %d" % report["n_routing_changed"])
    print("wake corrections away from the head of the text: %d (must be 0)" % report["n_not_at_head"])
    print("corrected clips with a verdict: changed wrongly (the owner called it correct or noise): %d; moved toward the owner's text: %d; "
          "changed but not toward it: %d" % (report["n_changed_wrongly"], report["n_moved_toward_truth"], report["n_changed_with_verdict_not_toward_truth"]))
    print("of the corrected clips, %d are the clip an entry was written from (its own evidence, not a check of it)" % report["n_entry_source_clips"])
    print("These are counts of one household's stored clips, not an accuracy figure.")
    for item in report["corrected"]:
        print("  %s trigger=%s verdict=%s %s%s | %r -> %r" % (item["id"], item["trigger"], item["verdict"] or "-", item["outcome"] or "-",
                                                          " (entry source)" if item["entry_source"] else "", item["before"], item["after"]))


def print_mine(rows: list[dict[str, Any]], n_clips: int) -> None:
    print("clips scanned (newest first, bounded): %d" % n_clips)
    print("%-14s %3s %6s  %-28s %-34s %s" % ("head word", "n", "whole", "covered by", "trigger split", "verdicts"))
    for row in rows:
        print("%-14s %3d %6d  %-28s %-34s %s" % (row["word"], row["n"], row["whole_segment"], row["covered_by"], dict(row["triggers"]), dict(row["verdicts"])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("replay", "mine"))
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="at most this many clips, newest first (max %d)" % MAX_LIMIT)
    parser.add_argument("--max-edits", type=int, default=MAX_EDITS)
    args = parser.parse_args(argv)
    if not 0 < args.limit <= MAX_LIMIT:
        parser.error("--limit must be 1..%d" % MAX_LIMIT)
    conn = open_readonly(args.db)
    try:
        clips = stored(conn, args.limit)
    finally:
        conn.close()
    dictionary = extra_dict.load()
    if args.command == "replay":
        print_replay(replay(clips, dictionary))
    else:
        print_mine(mine(clips, dictionary, args.max_edits), len(clips))
    return 0


if __name__ == "__main__":
    sys.exit(main())
