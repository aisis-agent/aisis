"""The extra dictionary: typical mishearings of the station's words, variant -> canonical (ticket #268).

Whisper writes the wake word «Алиса» as «Олеса», «Алекса», «Толиса», «Малис» ...; wake_word.py knows the grammar's `lexicon.wake` list and
one narrow sound rule (within two edits of «алиса» and containing «ис», whole segment only), so «Олеса» (no «ис») reached routing as a
request. This module is the data-driven complement: a reviewed, versioned file (extra_dict_ru.json) of variants with the context each one may
be corrected in, and the evidence (source, n stored clips) it was added on. The code never guesses a variant: an entry is a fact somebody wrote
down.

Entry kinds and contexts (a correction is made only where the entry's context allows it, so «Алекса» as a person in the middle of a sentence is
a person):
  wake     the canonical is the activation word. `standalone`: the whole segment is the variant. `address`: the variant is at the head, directly
           followed by a stop or comma (the vocative: «Олеса, включи свет»), directly by an imperative of the grammar («Алекса включи свет») or alone. `start`: at the head, whatever follows (for non-words).
           Lead-ins in front of the name («Эй, Олеса») are skipped like wake_word does.
  command  one word of a command, `anywhere` (whole words) or `standalone`.
  phrase   several words of a request, same contexts.
`correct(text)` returns the text with the variants replaced by their canonical and the list of hits; the wake word is then cut by the usual
wake_word.split, so the activation handling stays in one place. command and phrase entries are also exported as a phonetic extra confusables
file for the stt-cli core (`load(extra=[path])`): the correction dialog then offers the canonical as a candidate for the heard variant.

Mode (AISIS_EXTRA_DICT): `shadow` (the default) logs `extra_dict_would_correct` with the text before and after and changes nothing; `on`
applies the corrections; `off` disables the dictionary. Pure functions and a small file reader, standard library only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Any, Callable, NamedTuple

import wake_word

LOG = logging.getLogger("aisis.extra_dict")

DATA = Path(__file__).with_name("extra_dict_ru.json")
VERSION = 1
LOCALE = "ru"
DEFAULT_MODE = "shadow"
MODES = {"0": "off", "off": "off", "shadow": "shadow", "1": "on", "on": "on", "enforce": "on"}
KINDS = ("wake", "command", "phrase")
CONTEXTS = {"wake": ("standalone", "address", "start"), "command": ("anywhere", "standalone"), "phrase": ("anywhere", "standalone")}
SOURCES = ("owner", "verdict", "observed")
MAX_ENTRIES = 200
MAX_VARIANTS = 50
MAX_WORDS = 6
MIN_CHARS = 3
MIN_OBSERVED_COMMAND_N = 2   # a command or phrase seen once is an anecdote: it needs two stored clips unless the owner named it or ruled on it
MIN_DYNAMIC_SUPPORT = 3      # the runtime layer's floor: distinct stored clips a mined variant needs (extra_dict_dynamic.py)
DYNAMIC_FILE = "dynamic-ru.json"
RECHECK_S = 30.0
MAX_VOCABULARY = 200
MAX_VOCABULARY_CHARS = 64
ENTRY_KEYS = frozenset(("id", "kind", "canonical", "context", "variants", "evidence"))
TOP_KEYS = frozenset(("version", "locale", "note", "entries"))
DYNAMIC_KEYS = TOP_KEYS | {"generated", "vocabulary"}
EVIDENCE_KEYS = frozenset(("source", "note"))
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
WORD = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
VOCATIVE = ",.!?:;…-—–"
LEAD_INS = wake_word.LEAD_INS
VERB_LISTS = ("cmd_verbs", "on_verb", "off_verb", "set_verb", "play_verb")
VERB_FLOOR = frozenset(("включи", "выключи", "поставь"))   # only when the grammar bundle cannot be read


class ExtraDictError(ValueError):
    """The dictionary file is unusable; the message names the file and the entry."""


def parse_mode(raw: Any, default: str = DEFAULT_MODE) -> str:
    """AISIS_EXTRA_DICT: off/0, shadow, on/1/enforce; unset or unknown keeps `default` (shadow: an unknown value never switches behaviour on)."""
    return MODES.get(str(raw).strip().lower(), default) if raw is not None else default


def fold(text: str) -> str:
    return " ".join(match.group().lower().replace("ё", "е") for match in WORD.finditer(text))


@dataclass(frozen=True)
class Variant:
    text: str   # folded words, one space between
    n: int      # stored clips that carry it (0: named, not seen)


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    canonical: str
    context: str
    variants: tuple[Variant, ...]
    source: str
    note: str
    layer: str = "static"


class Hit(NamedTuple):
    entry: str
    kind: str
    variant: str    # the folded variant that matched
    before: str     # the text span as heard
    after: str      # what replaced it
    start: int
    end: int
    layer: str = "static"


class Corrected(NamedTuple):
    text: str
    hits: tuple[Hit, ...]


@dataclass(frozen=True)
class Dictionary:
    version: int
    entries: tuple[Entry, ...]

    def of_kind(self, *kinds: str) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries if entry.kind in kinds)

    def correct(self, text: str) -> Corrected:
        """`text` with every variant that stands in its entry's context replaced by the canonical; no hit returns the text itself."""
        words = list(WORD.finditer(text))
        if not words:
            return Corrected(text, ())
        folded = [fold(match.group()) for match in words]
        hits: list[Hit] = self._wake_hits(text, words, folded)
        hits.extend(self._phrase_hits(text, words, folded, hits))
        if not hits:
            return Corrected(text, ())
        hits.sort(key=lambda hit: hit.start)
        out, cursor = [], 0
        for hit in hits:
            out.append(text[cursor:hit.start])
            out.append(hit.after)
            cursor = hit.end
        out.append(text[cursor:])
        return Corrected("".join(out), tuple(hits))

    @cached_property
    def _wake_candidates(self) -> tuple[tuple[Variant, Entry], ...]:
        return tuple(sorted(((variant, entry) for entry in self.of_kind("wake") for variant in entry.variants), key=lambda pair: -len(pair[0].text.split())))

    @cached_property
    def _phrase_candidates(self) -> tuple[tuple[Variant, Entry], ...]:
        return tuple(sorted(((variant, entry) for entry in self.of_kind("command", "phrase") for variant in entry.variants),
                            key=lambda pair: (-len(pair[0].text.split()), pair[0].text)))

    # ── the activation word at the head ────────────────────────────────────
    def _wake_hits(self, text: str, words: list[re.Match], folded: list[str]) -> list[Hit]:
        """The variants at the head: a name said twice («Олеса, Олеся.») is two hits, like wake_word reads it as one name."""
        if text[:words[0].start()].strip(wake_word.SEPARATORS):
            return []                         # something other than separators in front of the first word: not the head
        first = 0
        if first < len(words) - 1 and folded[first] in LEAD_INS and not text[words[first].end():words[first + 1].start()].strip(wake_word.SEPARATORS):
            first += 1                        # at most one, as wake_word._cut: a second lead-in would leave a name the split cannot cut
        candidates = self._wake_candidates
        hits: list[Hit] = []
        while first < len(words):
            hit = next((hit for hit in (self._wake_at(text, words, folded, first, variant, entry) for variant, entry in candidates) if hit is not None), None)
            if hit is None:
                break
            hits.append(hit)
            first = next((index for index, word in enumerate(words) if word.start() >= hit.end), len(words))
            if first < len(words) and text[hit.end:words[first].start()].strip(wake_word.SEPARATORS):
                break
        return hits

    def _wake_at(self, text: str, words: list[re.Match], folded: list[str], first: int, variant: Variant, entry: Entry) -> Hit | None:
        found = _match(text, words, folded, first, variant.text)
        if found is None:
            return None
        start, end = found
        rest = text[end:]
        if entry.context == "standalone" and any(ch.isalnum() for ch in rest):
            return None
        if entry.context == "address" and not _addressed(rest):
            return None
        return Hit(entry.id, entry.kind, variant.text, text[start:end], entry.canonical, start, end, entry.layer)

    # ── command and phrase variants ────────────────────────────────────────
    def _phrase_hits(self, text: str, words: list[re.Match], folded: list[str], taken: list[Hit]) -> list[Hit]:
        found: list[Hit] = []
        for variant, entry in self._phrase_candidates:
            width = len(variant.text.split())
            for first in range(len(words) - width + 1):
                matched = _match(text, words, folded, first, variant.text)
                if matched is None:
                    continue
                start, end = matched
                if any(start < hit.end and hit.start < end for hit in taken + found):
                    continue
                if entry.context == "standalone" and (not _only_names_before(text[:start], taken) or text[end:].strip(wake_word.SEPARATORS)):
                    continue
                found.append(Hit(entry.id, entry.kind, variant.text, text[start:end], _case_of(text[start:end], entry.canonical), start, end, entry.layer))
        return found


def _only_names_before(prefix: str, taken: list[Hit]) -> bool:
    """The text in front of a phrase is nothing but separators, lead-ins and names of the wake word (heard ones and the ones corrected): the phrase is the whole request."""
    for match in WORD.finditer(prefix):
        if any(hit.kind == "wake" and hit.start <= match.start() and match.end() <= hit.end for hit in taken):
            continue
        if fold(match.group()) not in LEAD_INS and fold(match.group()) not in wake_word.lexicon():
            return False
    return not WORD.sub("", prefix).strip(wake_word.SEPARATORS + " \t\r\n")


def _addressed(rest: str) -> bool:
    """What follows a name that is being spoken to: nothing, a stop or comma (the vocative), or directly an imperative of the grammar («Алекса включи свет»)."""
    if not any(ch.isalnum() for ch in rest):
        return True
    if rest.lstrip(" \t\r\n")[:1] in VOCATIVE:
        return True
    following = WORD.search(rest)
    return following is not None and fold(following.group()) in command_verbs()


@lru_cache(maxsize=1)
def command_verbs() -> frozenset[str]:
    """The imperatives of the intent grammar's lexicon (on/off, set, play, cmd verbs), folded; a small floor when the bundle cannot be read."""
    try:
        with open(wake_word.BUNDLE, encoding="utf-8") as bundle:
            lexicon = json.load(bundle)["data"]["lexicon"]
        words = {str(word).lstrip("=").lower().replace("ё", "е") for key in VERB_LISTS for word in lexicon[key]}
        return frozenset(words) or VERB_FLOOR
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("extra_dict_verbs_unreadable class=%s", type(exc).__name__)
        return VERB_FLOOR


def _match(text: str, words: list[re.Match], folded: list[str], first: int, variant: str) -> tuple[int, int] | None:
    """(start, end) of `variant` (folded words) at word index `first` of the text, the words separated by blanks only; None when it is not there."""
    parts = variant.split()
    if first + len(parts) > len(words) or folded[first:first + len(parts)] != parts:
        return None
    for index in range(first, first + len(parts) - 1):
        if text[words[index].end():words[index + 1].start()].strip(" \t\r\n"):
            return None
    return words[first].start(), words[first + len(parts) - 1].end()


def _case_of(heard: str, canonical: str) -> str:
    return canonical[:1].upper() + canonical[1:] if heard[:1].isupper() else canonical


# ── the file ─────────────────────────────────────────────────────────────────

def parse(raw: Any, name: str = str(DATA), layer: str = "static") -> Dictionary:
    """The validated dictionary of an already parsed JSON document; any defect raises ExtraDictError naming `name` and the entry.

    `layer` is `static` (the reviewed file: at least one entry) or `dynamic` (the runtime layer of extra_dict_dynamic.py: command and phrase entries
    observed at least MIN_DYNAMIC_SUPPORT times, plus its `generated` and `vocabulary` records; it may hold no entry at all).
    """
    if layer not in ("static", "dynamic"):
        raise ValueError(f"layer must be static or dynamic, got {layer!r}")
    if not isinstance(raw, dict):
        raise ExtraDictError(f"{name}: the top level must be an object")
    _keys(raw, TOP_KEYS if layer == "static" else DYNAMIC_KEYS, {"version", "locale", "entries"} | (set() if layer == "static" else {"generated", "vocabulary"}), name)
    if type(raw["version"]) is not int or raw["version"] != VERSION:
        raise ExtraDictError(f"{name}: version {raw['version']!r} is not the supported {VERSION}")
    if raw["locale"] != LOCALE:
        raise ExtraDictError(f"{name}: locale {raw['locale']!r} is not {LOCALE!r}")
    if layer == "dynamic":
        _provenance(raw, name)
    entries_raw = raw["entries"]
    if not isinstance(entries_raw, list) or not (layer == "dynamic" or entries_raw) or len(entries_raw) > MAX_ENTRIES:
        raise ExtraDictError(f"{name}: entries must be a list of {0 if layer == 'dynamic' else 1}..{MAX_ENTRIES} entries")
    entries: list[Entry] = []
    ids: set[str] = set()
    claimed: dict[str, str] = {}
    for position, item in enumerate(entries_raw):
        entry = _entry(item, position, name, layer)
        if entry.id in ids:
            raise ExtraDictError(f"{name}: entry {entry.id!r}: the id is used twice")
        ids.add(entry.id)
        for variant in entry.variants:
            if variant.text in claimed:
                raise ExtraDictError(f"{name}: entry {entry.id!r}: variant {variant.text!r} is already claimed by entry {claimed[variant.text]!r}")
            claimed[variant.text] = entry.id
        entries.append(entry)
    canonicals = {fold(entry.canonical): entry.id for entry in entries}
    for entry in entries:
        for variant in entry.variants:
            if variant.text in canonicals:
                raise ExtraDictError(f"{name}: entry {entry.id!r}: variant {variant.text!r} is the canonical of entry {canonicals[variant.text]!r}")
    return Dictionary(raw["version"], tuple(entries))


def _provenance(raw: dict, name: str) -> None:
    """The `generated` and `vocabulary` records of a dynamic layer: plain facts about how it was made, bounded."""
    generated, vocabulary = raw["generated"], raw["vocabulary"]
    if (not isinstance(generated, dict) or set(generated) != {"at", "vocabulary", "min_support"} or type(generated["at"]) is not int
            or not isinstance(generated["vocabulary"], str) or type(generated["min_support"]) is not int):
        raise ExtraDictError(f"{name}: generated must be {{at, vocabulary, min_support}}")
    if not isinstance(vocabulary, dict) or set(vocabulary) != {"verbs", "names"}:
        raise ExtraDictError(f"{name}: vocabulary must be {{verbs, names}}")
    for key, words in vocabulary.items():
        if not isinstance(words, list) or len(words) > MAX_VOCABULARY or not all(isinstance(word, str) and len(word) <= MAX_VOCABULARY_CHARS for word in words):
            raise ExtraDictError(f"{name}: vocabulary.{key} must be a list of at most {MAX_VOCABULARY} short strings")


def _keys(item: dict, allowed: frozenset[str], required: set[str], where: str) -> None:
    unknown = sorted(set(item) - allowed)
    missing = sorted(required - set(item))
    if unknown or missing:
        raise ExtraDictError(f"{where}: unknown keys {unknown}, missing keys {missing}")


def _entry(item: Any, position: int, name: str, layer: str) -> Entry:
    where = f"{name}: entry #{position}"
    if not isinstance(item, dict):
        raise ExtraDictError(f"{where}: must be an object")
    entry_id = item.get("id")
    if not isinstance(entry_id, str) or not SLUG.match(entry_id):
        raise ExtraDictError(f"{where}: id must be a lowercase slug")
    where = f"{name}: entry {entry_id!r}"
    _keys(item, ENTRY_KEYS, ENTRY_KEYS, where)
    kind, context = item["kind"], item["context"]
    if kind not in KINDS:
        raise ExtraDictError(f"{where}: kind {kind!r} is not one of {KINDS}")
    if context not in CONTEXTS[kind]:
        raise ExtraDictError(f"{where}: context {context!r} is not one of {CONTEXTS[kind]} for kind {kind!r}")
    if layer == "dynamic" and (kind == "wake" or context != "anywhere" or not entry_id.startswith("dyn-")):
        raise ExtraDictError(f"{where}: a dynamic entry is a command or phrase, context anywhere, id dyn-...")
    canonical = item["canonical"]
    if (not isinstance(canonical, str) or not canonical.strip() or not 0 < len(fold(canonical).split()) <= MAX_WORDS
            or not canonical[0].isalnum() or not canonical[-1].isalnum()):
        raise ExtraDictError(f"{where}: canonical must be 1..{MAX_WORDS} words, from a letter or digit to a letter or digit")
    if kind == "wake" and fold(canonical) not in wake_word.lexicon():
        raise ExtraDictError(f"{where}: the canonical {canonical!r} of a wake entry must be a name of the grammar's lexicon.wake")
    if kind != "wake" and wake_word.lexicon().intersection(fold(canonical).split()):
        raise ExtraDictError(f"{where}: the canonical {canonical!r} of a {kind} entry holds a name of the grammar's lexicon.wake: a correction never writes the activation word")
    evidence = item["evidence"]
    if not isinstance(evidence, dict):
        raise ExtraDictError(f"{where}: evidence must be an object")
    _keys(evidence, EVIDENCE_KEYS, {"source"}, f"{where}: evidence")
    source = evidence["source"]
    if source not in SOURCES or (layer == "dynamic" and source != "observed"):
        raise ExtraDictError(f"{where}: evidence.source {source!r} is not one of {SOURCES if layer == 'static' else ('observed',)}")
    note = evidence.get("note", "")
    if not isinstance(note, str) or len(note) > 600:
        raise ExtraDictError(f"{where}: evidence.note must be a string of at most 600 characters")
    variants = _variants(item["variants"], where, source, kind)
    if layer == "dynamic" and any(variant.n < MIN_DYNAMIC_SUPPORT for variant in variants):
        raise ExtraDictError(f"{where}: a dynamic variant needs n >= {MIN_DYNAMIC_SUPPORT}: one or two occurrences make no correction")
    if fold(canonical) in {variant.text for variant in variants}:
        raise ExtraDictError(f"{where}: a variant equals the canonical")
    return Entry(entry_id, kind, canonical, context, variants, source, note, layer)


def _variants(raw: Any, where: str, source: str, kind: str) -> tuple[Variant, ...]:
    if not isinstance(raw, list) or not 0 < len(raw) <= MAX_VARIANTS:
        raise ExtraDictError(f"{where}: variants must be a list of 1..{MAX_VARIANTS} objects")
    out: list[Variant] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"text", "n"} or not isinstance(item.get("text"), str):
            raise ExtraDictError(f"{where}: a variant is an object {{\"text\": ..., \"n\": ...}}")
        text = fold(item["text"])
        n = item.get("n", 0)
        if not text or len(text.split()) > MAX_WORDS or len(text.replace(" ", "")) < MIN_CHARS:
            raise ExtraDictError(f"{where}: variant {item['text']!r} must be 1..{MAX_WORDS} words of at least {MIN_CHARS} letters")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ExtraDictError(f"{where}: variant {text!r}: n must be a non-negative integer")
        if text in seen:
            raise ExtraDictError(f"{where}: variant {text!r} is listed twice")
        if source in ("verdict", "observed") and n < 1:
            raise ExtraDictError(f"{where}: variant {text!r}: evidence source {source!r} needs n >= 1")
        if source == "observed" and kind != "wake" and n < MIN_OBSERVED_COMMAND_N:
            raise ExtraDictError(f"{where}: variant {text!r}: an observed {kind} needs n >= {MIN_OBSERVED_COMMAND_N}")
        if wake_word.lexicon().intersection(text.split()):
            raise ExtraDictError(f"{where}: variant {text!r} holds a name of the grammar's lexicon.wake: the activation word is never (part of) a variant")
        lead_in = LEAD_INS.intersection(text.split()) if kind == "wake" else {text} & LEAD_INS
        if lead_in:
            raise ExtraDictError(f"{where}: variant {text!r} is or holds a lead-in word ({', '.join(sorted(LEAD_INS))}): wake_word cuts those in front of a name")
        seen.add(text)
        out.append(Variant(text, n))
    return tuple(out)


def load(path: str | os.PathLike[str] | None = None, layer: str = "static") -> Dictionary:
    """The committed dictionary (or the file at `path`, a static file or a dynamic `layer`), validated; an unreadable or invalid file raises ExtraDictError."""
    target = Path(path) if path is not None else DATA
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExtraDictError(f"{target}: cannot be read as JSON ({type(exc).__name__})") from exc
    return parse(raw, str(target), layer)


def load_or_none() -> Dictionary | None:
    """For the services: a dictionary that cannot be loaded is logged and the service runs without it, never fails a turn on it."""
    try:
        return load()
    except ExtraDictError as exc:
        LOG.error("extra_dict_unusable error=%s", exc)
        return None


def dynamic_path() -> Path:
    """Where `extra_dict_dynamic.py refresh` installs the runtime layer (never committed)."""
    return cache_dir() / DYNAMIC_FILE


def merge(static: Dictionary, *layers: Dictionary) -> Dictionary:
    """`static` with the dynamic `layers` under it, in one deterministic order: every static entry stays and wins every conflict.

    A dynamic variant is dropped when the static file already claims it, when it is the canonical of a static entry or of another surviving dynamic
    entry, or when a stronger dynamic entry claims it (higher n, then the canonical, then the id: the result never depends on the order of the layers).
    """
    claimed = {variant.text for entry in static.entries for variant in entry.variants}
    canonicals = {fold(entry.canonical) for entry in static.entries}
    candidates = sorted(((entry, variant) for layer in layers for entry in layer.entries for variant in entry.variants),
                        key=lambda pair: (-pair[1].n, fold(pair[0].canonical), pair[0].id, pair[1].text))
    winners: dict[str, tuple[Entry, Variant]] = {}
    for entry, variant in candidates:
        if variant.text not in claimed and variant.text not in canonicals and variant.text not in winners:
            winners[variant.text] = (entry, variant)
    dynamic_canonicals = {fold(entry.canonical) for entry, _ in winners.values()}
    kept: dict[str, list[Variant]] = {}
    entries: dict[str, Entry] = {}
    for entry, variant in winners.values():
        if variant.text in dynamic_canonicals:
            continue
        entries.setdefault(entry.id, entry)
        kept.setdefault(entry.id, []).append(variant)
    dynamic = [replace(entries[entry_id], variants=tuple(sorted(variants, key=lambda variant: (-variant.n, variant.text))))
               for entry_id, variants in kept.items()]
    dynamic.sort(key=lambda entry: (-entry.variants[0].n, entry.id))
    return Dictionary(static.version, (*static.entries, *dynamic))


class Live:
    """The merged dictionary the services read: the static file plus the dynamic layer, re-read when the layer's file changes.

    The layer is looked at most every `recheck_s` seconds and parsed only when its (mtime, size) changed. A layer that cannot be read or fails
    validation is logged once and the previous merged dictionary stays; a layer that disappears leaves the static dictionary alone.
    `version` counts the dictionaries handed out, so a holder of derived data (the engine's confusables) knows when to rebuild it.
    """

    def __init__(self, static: Dictionary, path: str | os.PathLike[str] | None = None, clock: Callable[[], float] = time.monotonic,
                 recheck_s: float = RECHECK_S) -> None:
        self._static = static
        self._path = Path(path) if path is not None else dynamic_path()
        self._clock = clock
        self._recheck = recheck_s
        self._checked = float("-inf")
        self._stamp: tuple[int, int] | None = None
        self._merged = static
        self.version = 0

    @classmethod
    def from_disk(cls) -> "Live | None":
        """A Live over the committed static file, or None (logged as `extra_dict_unusable`) when that file is unusable."""
        static = load_or_none()
        return cls(static) if static is not None else None

    def get(self) -> Dictionary:
        now = self._clock()
        if now - self._checked >= self._recheck:
            self._checked = now
            self._reload()
        return self._merged

    def _reload(self) -> None:
        try:
            status = self._path.stat()
            stamp: tuple[int, int] | None = (status.st_mtime_ns, status.st_size)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        previous, self._stamp = self._stamp, stamp
        if stamp is None:
            self._merged = self._static
        else:
            try:
                self._merged = merge(self._static, load(self._path, layer="dynamic"))
            except ExtraDictError as exc:
                LOG.warning("extra_dict_dynamic_unusable keeping the previous dictionary: %s", exc)
                return
        if stamp is not None or previous is not None:
            self.version += 1


# ── the stt-cli core: a phonetic extra confusables file ──────────────────────

def confusables_document(dictionary: Dictionary) -> dict[str, Any] | None:
    """The command and phrase entries as stt-cli confusables data (`kind: phonetic`, two groups: the canonical and its variants), or None."""
    sets = [{"id": f"extra-{entry.id}", "kind": "phonetic",
             "note": f"extra_dict.py entry {entry.id}: the variants are mishearings of the canonical",
             "groups": [{"label": fold(entry.canonical), "forms": [fold(entry.canonical)]},
                        {"label": entry.variants[0].text, "forms": [variant.text for variant in entry.variants]}]}
            for entry in dictionary.of_kind("command", "phrase")]
    return {"version": 1, "locale": LOCALE, "sets": sets} if sets else None


def write_confusables(dictionary: Dictionary, directory: str | os.PathLike[str]) -> Path | None:
    """Write the extra confusables file atomically into `directory` and return its path (None: the dictionary has no command or phrase entry)."""
    document = confusables_document(dictionary)
    if document is None:
        return None
    target = Path(directory) / "extra-confusables-ru.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False, suffix=".tmp") as handle:
        try:
            json.dump(document, handle, ensure_ascii=False, indent=1)
        except BaseException:
            handle.close()
            os.unlink(handle.name)   # a failed write leaves no temp file behind
            raise
    os.replace(handle.name, target)
    return target


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "aisis" / "extra-dict"
