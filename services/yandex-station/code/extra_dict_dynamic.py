#!/usr/bin/env python3
"""The dynamic layer of the extra dictionary (ticket #269): variants of the home's own words, found in what the recogniser really wrote.

The static file (extra_dict_ru.json) holds what somebody wrote down. This module builds a second layer at runtime from live sources, in the
same format, and never commits it:

  vocabulary   the words the owner's commands are made of: the friendly names and rooms of Home Assistant (voice_vocab.fetch_catalog: the
               read-only `/api/states` list and the area template of ha-cli's cache, nothing is actuated), and the imperatives and comparatives
               of the intent grammar (wake_word.command_verbs);
  evidence     the stored transcripts of voice_clips (`sent`, not the speaker's own spotter text, read-only) from the dialog database;
  entries      a word that is none of the known words (the vocabulary, every word of the grammar bundle, the catalog) but looks very like exactly one
               vocabulary word, and was written by the recogniser in at least MIN_SUPPORT distinct clips: «люстара» for «люстра» (a verb:
               kind `command`; a name or room: kind `phrase`), source `observed`, n = the number of clips. Another form of a vocabulary word
               («спальне», «гирлянды»: `inflected`) is a real word said every day, never a mishearing.

It never invents a correction from one occurrence (MIN_SUPPORT, and the loader refuses a dynamic layer below it), takes no word of a name with
a digit run, a dot, a colon, an at-sign, a slash or a long hexadecimal token (the layer holds names, never ids, addresses or serials), and is
bounded (MAX_ENTRIES entries, MAX_NAMES names, MAX_BYTES on disk). The file is written whole and atomically (`write_layer`); a refresh that
cannot read Home Assistant, produces an invalid or oversized layer, or fails to write keeps the previous file. The services read it with
`extra_dict.Live` (merged under the static file: static wins every conflict) and the shadow flag applies as to the static layer.

    extra_dict_dynamic.py refresh [--db FILE] [--hacli-url URL] [--out FILE]    build and install the layer (exit 3: Home Assistant unreadable, previous kept)
    extra_dict_dynamic.py show [--out FILE]                                       the installed layer's entries
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import extra_dict
import voice_vocab
import wake_word

LOG = logging.getLogger("aisis.extra_dict_dynamic")

DB = Path(os.environ.get("AISIS_DIALOGS_DB") or Path.home() / ".local/share/aisis/dialogs.sqlite3")
DEFAULT_HACLI_URL = "http://127.0.0.1:8795"
MIN_SUPPORT = extra_dict.MIN_DYNAMIC_SUPPORT   # distinct clips a variant needs: one occurrence is an anecdote, two a coincidence
MIN_SIMILARITY = 0.84                          # difflib ratio of the variant to its vocabulary word
MARGIN = 0.06                                  # the best word must beat the second best by this much: a variant between two words is nobody's
MIN_WORD_CHARS = 5                             # a short word is too easily another real word
MAX_ENTRIES = 100
MAX_NAMES = 120
MAX_BYTES = 64 * 1024
MAX_CLIPS = 5000
_SECRET_SHAPED = re.compile(r"\d{4,}|[.:@/\\]|\b[0-9a-f]{8,}\b|\b[0-9a-f]{2}(?::[0-9a-f]{2}){3,}\b", re.IGNORECASE)
_WORD = re.compile(r"[а-яё]+")


def clean_name(name: str) -> str | None:
    """A catalog name that may stand in the layer: words only. A digit run, a dot, a colon, an at-sign, a slash or a long hexadecimal token is not a name."""
    name = " ".join(name.split())
    if not name or len(name) > extra_dict.MAX_VOCABULARY_CHARS or _SECRET_SHAPED.search(name):
        return None
    return name


def grammar_words(bundle: str | os.PathLike[str] = wake_word.BUNDLE) -> frozenset[str]:
    """Every Cyrillic word the grammar bundle contains (its patterns, lexicon, canned lines): words the system already knows as words."""
    try:
        text = Path(bundle).read_text(encoding="utf-8")
    except OSError as exc:
        raise extra_dict.ExtraDictError(f"{bundle}: cannot be read ({type(exc).__name__})") from exc
    return frozenset(_WORD.findall(text.lower().replace("ё", "е")))


class Vocabulary:
    """The canonical words: verbs (kind `command`) and the words of the home's names (kind `phrase`), with the names they come from."""

    def __init__(self, verbs: Iterable[str], names: Iterable[str]) -> None:
        self.verbs = tuple(sorted({word for word in map(extra_dict.fold, verbs) if " " not in word and word}))
        self.names = tuple(sorted({folded for folded in map(extra_dict.fold, (clean for clean in map(clean_name, names) if clean)) if folded})[:MAX_NAMES])
        self.name_words = tuple(sorted({word for name in self.names for word in name.split()}))

    def words(self) -> frozenset[str]:
        return frozenset(self.verbs) | frozenset(self.name_words)

    def digest(self) -> str:
        """A short hash of the vocabulary: the layer says which entity set it was built from, a refresh that finds the same one changes nothing."""
        return hashlib.sha256("\n".join((*self.verbs, "--", *self.names)).encode("utf-8")).hexdigest()[:16]


def vocabulary_from(catalog: voice_vocab.Catalog) -> Vocabulary:
    return Vocabulary(wake_word.command_verbs(), (*catalog.entities, *catalog.areas))


def entry_id(canonical: str, variant: str) -> str:
    """A stable slug for the pair (the loader's ids are Latin slugs, the words are Russian): the same pair is the same entry in every refresh."""
    return "dyn-" + hashlib.sha256(f"{canonical}\n{variant}".encode("utf-8")).hexdigest()[:10]


def _best(word: str, candidates: Iterable[str]) -> tuple[str, float, float]:
    """(the most similar candidate, its ratio, the second best ratio)."""
    scored = sorted(((difflib.SequenceMatcher(None, word, candidate).ratio(), candidate) for candidate in candidates), reverse=True)
    if not scored:
        return "", 0.0, 0.0
    return scored[0][1], scored[0][0], scored[1][0] if len(scored) > 1 else 0.0


def inflected(word: str, target: str) -> bool:
    """`word` is `target` with another ending («спальне» for «спальня», «гирлянды» for «гирлянда») or an ending added or cut: a case or number form of a
    real word, which is far more often said than any mishearing and is never one. An edit inside the word, or one that keeps the last letter, is not."""
    common = 0
    for left, right in zip(word, target):
        if left != right:
            break
        common += 1
    tails = word[common:], target[common:]
    return max(map(len, tails)) <= 3 and tails[0][-1:] != tails[1][-1:]


def mine(transcripts: Iterable[tuple[str, str]], vocabulary: Vocabulary, known: frozenset[str], *, min_support: int = MIN_SUPPORT,
         min_similarity: float = MIN_SIMILARITY) -> list[dict[str, Any]]:
    """Layer entries from (clip id, transcript) pairs: unknown words that are one vocabulary word heard wrongly in at least `min_support` clips.

    A word counts once per clip. A word of the vocabulary, of the grammar or of the home's own names is a known word and never a variant.
    """
    activation = wake_word.lexicon()   # a correction may never write the activation word (the loader refuses it), so no entity named like it is a target
    canonical = [word for word in vocabulary.words() if len(word) >= MIN_WORD_CHARS and word not in activation]
    known = known | vocabulary.words()
    seen: dict[str, set[str]] = defaultdict(set)
    for clip_id, text in transcripts:
        for word in set(extra_dict.fold(text).split()):
            if len(word) >= MIN_WORD_CHARS and word not in known and _WORD.fullmatch(word):
                seen[word].add(clip_id)
    entries: list[dict[str, Any]] = []
    for word, clips in sorted(seen.items()):
        if len(clips) < min_support:
            continue
        if any(inflected(word, candidate) for candidate in canonical):
            continue
        target, ratio, runner_up = _best(word, canonical)
        if ratio < min_similarity or ratio - runner_up < MARGIN:
            continue
        is_verb = target in vocabulary.verbs and target not in vocabulary.name_words
        entries.append({"id": entry_id(target, word), "kind": "command" if is_verb else "phrase", "canonical": target, "context": "anywhere",
                        "variants": [{"text": word, "n": len(clips)}],
                        "evidence": {"source": "observed", "note": f"{len(clips)} stored clips, similarity {ratio:.2f}"}})
    entries.sort(key=lambda entry: (-entry["variants"][0]["n"], entry["id"]))
    return entries[:MAX_ENTRIES]


def build(catalog: voice_vocab.Catalog, transcripts: Iterable[tuple[str, str]], known: frozenset[str] | None = None, *, now: float | None = None,
          min_support: int = MIN_SUPPORT) -> dict[str, Any]:
    """The layer document for `catalog` and `transcripts`; validated by the dynamic loader before it is returned."""
    vocabulary = vocabulary_from(catalog)
    all_known = (known if known is not None else grammar_words()) | vocabulary.words()
    entries = mine(transcripts, vocabulary, all_known, min_support=min_support)
    document: dict[str, Any] = {"version": extra_dict.VERSION, "locale": extra_dict.LOCALE, "entries": entries,
                                "generated": {"at": int(time.time() if now is None else now), "vocabulary": vocabulary.digest(), "min_support": min_support},
                                "vocabulary": {"verbs": list(vocabulary.verbs), "names": list(vocabulary.names)}}
    extra_dict.parse(document, "dynamic layer", layer="dynamic")
    return document


def encode(document: dict[str, Any]) -> bytes:
    data = (json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise extra_dict.ExtraDictError(f"dynamic layer: {len(data)} bytes is over the {MAX_BYTES} byte cap")
    return data


def same_content(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """The layers agree on everything but the time they were made."""
    def strip(document: dict[str, Any]) -> dict[str, Any]:
        return {**document, "generated": {key: value for key, value in document.get("generated", {}).items() if key != "at"}}
    return strip(old) == strip(new)


def write_layer(path: str | os.PathLike[str], document: dict[str, Any]) -> bool:
    """Install `document` at `path` atomically (temp file in the same directory, fsync, rename) unless the installed layer already says the same.
    Returns whether the file was replaced. A document that fails the dynamic loader or the size cap raises before anything is written."""
    extra_dict.parse(document, "dynamic layer", layer="dynamic")
    data = encode(document)
    target = Path(path)
    try:
        if same_content(json.loads(target.read_text(encoding="utf-8")), document):
            return False
    except (OSError, ValueError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False, suffix=".tmp")   # mode 0600
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)   # a failed write leaves no stray file next to the installed layer
        raise
    return True


def stored_transcripts(db: str | os.PathLike[str], limit: int = MAX_CLIPS) -> list[tuple[str, str]]:
    """(clip id, transcript) of the newest `limit` Whisper clips that were sent on, read-only (never the speaker's own spotter text)."""
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, transcript FROM voice_clips WHERE status = 'sent' AND transcript IS NOT NULL AND transcript != '' "
                            "AND COALESCE(trigger, '') != 'spotter' ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [(clip_id, text) for clip_id, text in rows]


def refresh(out: str | os.PathLike[str], db: str | os.PathLike[str] = DB, hacli_url: str = DEFAULT_HACLI_URL,
            fetch: Callable[..., voice_vocab.Catalog] = voice_vocab.fetch_catalog) -> int:
    """Build the layer from Home Assistant and the stored clips and install it. 0: done (or nothing changed); 3: Home Assistant unreadable, the
    previous layer stays; 1: the layer could not be built or written, the previous layer stays."""
    try:
        catalog = fetch(hacli_url, strict=True)
    except voice_vocab.CatalogUnavailable as exc:
        LOG.warning("extra_dict_dynamic_ha_unreadable keeping the previous layer: %s", exc)
        return 3
    try:
        document = build(catalog, stored_transcripts(db))
        replaced = write_layer(out, document)
    except (extra_dict.ExtraDictError, OSError, sqlite3.Error) as exc:
        LOG.error("extra_dict_dynamic_failed class=%s keeping the previous layer: %s", type(exc).__name__, exc)
        return 1
    LOG.info("extra_dict_dynamic_%s entries=%d names=%d vocabulary=%s", "written" if replaced else "unchanged", len(document["entries"]),
             len(document["vocabulary"]["names"]), document["generated"]["vocabulary"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("refresh", "show"))
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--hacli-url", default=os.environ.get("AISIS_HACLI_URL", DEFAULT_HACLI_URL))
    parser.add_argument("--out", default=str(extra_dict.dynamic_path()))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "refresh":
        return refresh(args.out, args.db, args.hacli_url)
    try:
        layer = extra_dict.load(args.out, layer="dynamic")
    except extra_dict.ExtraDictError as err:
        print(f"no usable dynamic layer: {err}", file=sys.stderr)
        return 1
    for entry in layer.entries:
        print(entry.id, entry.kind, entry.canonical, [(variant.text, variant.n) for variant in entry.variants])
    return 0


if __name__ == "__main__":
    sys.exit(main())
