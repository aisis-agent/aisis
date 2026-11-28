"""Proactive clarification (#307): a phrase nobody could read is answered with the most likely actions, never with «повтори».

When the grammar, the quasar layer and the skills read nothing (or the brain says it did not understand), the speaker looks for what
the owner most plausibly meant and asks ONE short question about it («Поставить телевизор на паузу?»; two options at most). The
answer goes through the repair dialog (correction.py): «да» runs the option, a choice word picks one, «нет» offers the kinds of action
this home has. Nothing runs from a guess without that spoken «да».

Evidence, all of it real (nothing is made up that the home or the grammar does not have):
  Home Assistant     the entity catalog and the live states (`Home.entities`): a TV that is playing can be paused, a paused one continued,
                     a lamp that is on switched off. An option is one STANDARD service call on a catalog entity (media_player.media_pause,
                     light.turn_off, ...): no script, no per-device code, the same table for every device of a domain.
  the grammar        the pinned phrases of the adapter's own intents (`Grammar.intents[].examples`) nearest to what was heard, only those the
                     router runs itself and that carry no value to guess (a number, a duration, a free text).
  the dialog         a phrase close to one of the station's last commands is a little more likely; so is a light off at night.
Nearness is a word-by-word comparison that forgives what far-field speech does to a word: a prefix («ставьте» for «поставь»), an unstressed
vowel, a voiced consonant, a synonym of the grammar's own lexicon («телик» for «телевизор»), one edit.

`Home` is the one seam to Home Assistant (names, states, one call); everything else here is pure.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Mapping, Protocol, Sequence

import ha_actions
import utterance_trace
import voice_vocab
from correction import state_word
from intent_grammar import Decision, Grammar
from intent_grammar.normalize import edit_distance_le1, fold, phonetic_key, stem

ACT_FAILED = ha_actions.REPLY_FAILED

LOG = logging.getLogger("aisis.clarify")

ASK_AT = 0.75        # a question before the brain: the phrase is nearly an action of this home
FALLBACK_AT = 0.5    # a question after the brain gave up: anything this close is better than asking nothing
SECOND_WITHIN = 0.12  # a second option is offered when it scores this close to the best one
MAX_RECENT = 8
RECENT_S = 600.0
MAX_HEARD_CHARS = 60
ASK_MAX_WORDS = 6    # before the brain a longer phrase is chit-chat or a question
GRAMMAR_EXTRA_COST = 0.3   # a heard word a pinned phrase does not hold costs this much (a device name's extra word costs 0.1)
CATALOG_MAX_AGE_S = 5.0    # the live states are reused for this long

# Words that carry no action: heard or in a phrase they neither confirm nor contradict it.
_PREPOSITIONS = frozenset(("на", "в", "во", "по", "к", "с", "со", "из", "у", "о", "об", "для", "про", "за", "от", "до"))
_NOISE = _PREPOSITIONS | frozenset(("и", "а", "но", "же", "ли", "бы", "это", "эту", "этот", "их", "его", "ее", "им", "ей", "мне", "нам", "ты", "вы",
                                    "я", "мы", "они", "он", "она", "там", "тут", "вот", "ну", "так", "пожалуйста", "давай", "алиса", "брачо"))
_PREFIXES = ("по", "за", "вы", "при", "про", "от", "раз", "воз", "об")
_WORD = re.compile(r"[a-zа-я0-9]+")
_NAME_MISSING = 0.1       # the entity's name was not heard (the only player that plays is meant): a little less sure
_EXTRA_CONTENT = 0.1      # a heard word of its own that the phrase does not hold
_MAX_EXTRA = 0.3

# What a standard service of a domain is called by a person, by the state it applies to. `{n}` is the entity's spoken name. The services are
# Home Assistant's own; the table says only how they are asked for (imperative phrases to match, the infinitive of the question) and
# which state proves them done. A domain or a state that is not here has no option: the speaker never offers what it cannot do.
@dataclass(frozen=True)
class Verb:
    service: str
    phrases: tuple[str, ...]
    ask: str
    expect: tuple[str, ...]
    done: str            # «{name} на паузе.» ({state} agrees with the name for on/off)
    nameless: bool = False   # may be meant without naming the device (the only one that plays)


PAUSE = Verb("media_pause", ("поставь {n} на паузу", "пауза {n}", "{n} на паузу", "приостанови {n}"), "поставить {n} на паузу", ("paused", "idle"),
             "{N} на паузе.", nameless=True)
CONTINUE = Verb("media_play", ("продолжи {n}", "продолжи воспроизведение {n}", "возобнови {n}", "включи воспроизведение {n}"), "продолжить {n}",
                ("playing", "buffering"), "{N} продолжается.", nameless=True)
OFF = Verb("turn_off", ("выключи {n}", "отключи {n}", "вырубай {n}"), "выключить {n}", ("off",), "{N} {off}.")
ON = Verb("turn_on", ("включи {n}", "вруби {n}", "запусти {n}"), "включить {n}", ("on", "playing", "idle", "paused"), "{N} {on}.")

_ON_OFF_DOMAINS = frozenset(("light",))
_PLAYING = frozenset(("playing", "buffering"))
_DEAD = frozenset(("unavailable", "unknown"))
_PLAYER_OWN = "media_player.aisis_speaker_"
_DEVICE_CLASS_NAMES = {"tv": "телевизор", "speaker": "колонка", "receiver": "ресивер"}   # Home Assistant's media_player device classes, as said aloud


@dataclass(frozen=True)
class Entity:
    entity_id: str
    name: str
    state: str

    @property
    def domain(self) -> str:
        return self.entity_id.partition(".")[0]


@dataclass(frozen=True)
class HaCall:
    """One standard Home Assistant service call on one catalog entity, and the states that prove it was done."""
    domain: str
    service: str
    entity_id: str
    name: str
    expect: tuple[str, ...]
    done: str


@dataclass(frozen=True)
class Option:
    """An action the owner may have meant. `phrase`: what the dialog quotes and a «да» runs through the normal route (always something the
    grammar reads); `call`: a Home Assistant call to make instead of running the phrase."""
    phrase: str
    ask: str
    score: float
    source: str            # home | grammar
    detail: str            # the entity id or the intent id (the trace)
    says: tuple[str, ...]  # what a reply may say to pick it: the whole phrase, the device's name, the verb
    call: HaCall | None = None


class Home(Protocol):
    """The seam to Home Assistant: the catalog with the live states, and one service call. Both may fail; the caller degrades."""
    async def entities(self) -> Sequence[Entity]: ...
    async def act(self, call: HaCall) -> str: ...


def entities_from(states: object) -> tuple[Entity, ...]:
    """The speakable entities of a `/api/states` answer: voice_vocab's own rule for a name a person says aloud."""
    if not isinstance(states, list):
        return ()
    found = []
    for state in states:
        if not isinstance(state, Mapping) or not isinstance(state.get("entity_id"), str) or not isinstance(state.get("state"), str):
            continue
        if state["entity_id"].startswith(_PLAYER_OWN) or ha_actions.JUNK_LIGHT.search(state["entity_id"]):
            continue
        names = voice_vocab.spoken_entity_names([state])
        attributes = state.get("attributes") if isinstance(state.get("attributes"), Mapping) else {}
        # a name nobody says aloud («Chromecast — пульт») falls back to what Home Assistant itself says the device is (its device_class)
        name = names[0] if names else _DEVICE_CLASS_NAMES.get(attributes.get("device_class"), "") if state["entity_id"].startswith("media_player.") else ""
        if name or state["entity_id"].startswith("media_player."):   # a player with a technical name is still «the one that plays»
            found.append(Entity(state["entity_id"], name, state["state"]))
    return tuple(found)


class HaHome:
    """`Home` over the callables the adapter already uses to reach Home Assistant (`get(path)`, `post(path, body) -> HaReply`)."""

    def __init__(self, get: Callable[[str], Awaitable[object]], post: Callable[[str, dict], Awaitable[ha_actions.HaReply]]) -> None:
        self._get = get
        self._post = post

    async def entities(self) -> tuple[Entity, ...]:
        return entities_from(await self._get("/api/states"))

    async def act(self, call: HaCall) -> str:
        """The call, then what was OBSERVED: the confirmed call's verdict, else the state read back. Never what was asked for."""
        started = time.monotonic()
        reply = await self._post(f"/api/services/{call.domain}/{call.service}", {"entity_id": call.entity_id})
        utterance_trace.call("ha", service=f"{call.domain}.{call.service}", entity=call.entity_id, status=reply.status, verify=reply.verify,
                             ok=200 <= reply.status < 300, ms=int((time.monotonic() - started) * 1000))
        verify = (reply.verify or {}).get("result")
        if reply.status == 202:
            return ha_actions.REPLY_QUEUED
        if verify == "failed" or not 200 <= reply.status < 300:
            return ha_actions.REPLY_REJECTED if 400 <= reply.status < 500 else ha_actions.REPLY_NOT_CONFIRMED
        if verify == "ok":
            return self._said(call)
        now = {entity.entity_id: entity.state for entity in await self.entities()}.get(call.entity_id)
        return self._said(call) if now in call.expect else ha_actions.REPLY_NOT_CONFIRMED

    @staticmethod
    def _said(call: HaCall) -> str:
        name = call.name or "воспроизведение"
        return call.done.format(N=name[:1].upper() + name[1:], on=state_word(name, True), off=state_word(name, False))


# ── nearness of words ──────────────────────────────────────────────────────────

def tokens(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def _key(word: str) -> str:
    """A word as far-field speech blurs it: stem, no verb prefix, sound classes."""
    base = stem(word)
    for prefix in _PREFIXES:
        if base.startswith(prefix) and len(base) - len(prefix) >= 4:
            base = base[len(prefix):]
            break
    return phonetic_key(base)


def _close(a: str, b: str) -> bool:
    """One word key is the other with at most two letters more at the end (a polite or plural ending), or one edit apart."""
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 4 and long_.startswith(short) and len(long_) - len(short) <= 2:
        return True
    return len(short) >= 5 and edit_distance_le1(a, b)


class Words:
    """How near two words are, with the synonyms of the grammar's own lexicon («телик», «телевизор»)."""

    def __init__(self, groups: Mapping[str, Mapping[str, Sequence[str]]]) -> None:
        self._groups = [[tuple(fold(form) for form in forms) for forms in group.values()] for group in groups.values()]

    def member(self, word: str, forms: Sequence[str]) -> bool:
        """`word` is one of `forms` (a lexicon entry: an exact word, or a prefix when it ends with `*`)."""
        return any(word.startswith(fold(form)[:-1]) if form.endswith("*") else word == fold(form) for form in forms)

    def same_thing(self, a: str, b: str) -> bool:
        return any(self.member(a, forms) and self.member(b, forms) for group in self._groups for forms in group)

    def score(self, heard: str, wanted: str) -> float:
        if heard == wanted or stem(heard) == stem(wanted):
            return 1.0
        if self.same_thing(heard, wanted):
            return 0.95
        kh, kw = _key(heard), _key(wanted)
        if kh == kw:
            return 0.9
        return 0.8 if _close(kh, kw) or _close(stem(heard), stem(wanted)) else 0.0


def nearness(words: Words, heard: Sequence[str], phrase: Sequence[str], name: frozenset[str] = frozenset(),
             extra_cost: float = _EXTRA_CONTENT) -> tuple[float, bool]:
    """How much of `phrase` the heard words hold (prepositions count a third), less a little for each heard word of its own, and whether the
    words of `name` were absent (they are then left out of the comparison)."""
    named = any(words.score(h, n) > 0 for h in heard for n in name) if name else True
    wanted = [w for w in phrase if named or w not in name]
    if not wanted:
        return 0.0, not named
    free = [h for h in heard if h not in _NOISE or h in wanted]
    matched = total = 0.0
    for w in wanted:
        weight = 0.3 if w in _PREPOSITIONS else 1.0
        total += weight
        best, taken = 0.0, None
        for index, h in enumerate(free):
            got = words.score(h, w)
            if got > best:
                best, taken = got, index
        if taken is not None:
            free.pop(taken)
            matched += weight * best
    extra = sum(1 for h in free if h not in _NOISE)
    return matched / total - min(_MAX_EXTRA, extra * extra_cost), not named


_INDECLINABLE = frozenset(("ультра", "мини", "макси", "мега", "супер", "гипер", "экстра", "плюс", "про", "макс"))   # foreign name parts keep their form


def _feminine_noun(word: str) -> bool:
    return len(word) > 3 and word not in _INDECLINABLE and word.endswith(("а", "я")) and not word.endswith(("ая", "яя"))


def accusative(name: str) -> str:
    """«лампа в студии» -> «лампу в студии», «станция мини гостиная» -> «станцию мини гостиную», «ультра лампа» -> «ультра лампу»: when the
    name before its first preposition has a feminine noun (the first one is its head), the head and the feminine adjectives next to it take
    the direct-object case. A masculine head («телевизор гостиная») and indeclinable foreign parts (мини, ультра) are unchanged."""
    words = name.split()
    stop = next((index for index, word in enumerate(words) if word in _PREPOSITIONS and index > 0), len(words))
    head = next((index for index in range(stop) if _feminine_noun(words[index])), None)
    if head is None:
        return name
    for index in range(stop):
        word = words[index]
        if index == head:
            words[index] = word[:-1] + ("у" if word.endswith("а") else "ю")
        elif len(word) > 3 and word.endswith(("ая", "яя")):
            words[index] = word[:-2] + ("ую" if word.endswith("ая") else "юю")
    return " ".join(words)


def _verbs(entity: Entity) -> tuple[Verb, ...]:
    """The services of this entity's domain that make sense in its state. Only what is safe to do on a guess that a spoken yes confirms: a
    playing player pauses, a paused one continues, a lamp that is on switches off, one that is off switches on. Sockets (the kettle), the TV's
    power, climate and locks are risky (correction.SAFE_EXECUTORS) and are never offered."""
    state, domain = entity.state, entity.domain
    if state in _DEAD:
        return ()
    if domain == "media_player":
        return (PAUSE,) if state in _PLAYING else (CONTINUE,) if state in ("paused", "idle") else ()
    if domain in _ON_OFF_DOMAINS:
        return (OFF,) if state == "on" else (ON,) if state == "off" else ()
    return ()


# ── the clarifier ─────────────────────────────────────────────────────────────

class Clarifier:
    """Candidates for an unread phrase of one speaker, from the home, the grammar and the dialog."""

    def __init__(self, grammar: Grammar, classify: Callable[[str], Decision | None], executable: Callable[[Decision, str], bool],
                 home: Home | None = None, *, now: Callable[[], datetime], monotonic: Callable[[], float],
                 abilities: Callable[[], Sequence[str]] = lambda: ()) -> None:
        self.grammar = grammar
        self.classify = classify
        self.executable = executable
        self.home = home
        self.now = now
        self.monotonic = monotonic
        self.abilities = abilities
        self.words = Words(grammar.groups)
        self.recent: dict[str, list[tuple[float, list[str]]]] = {}
        self._pinned: list[tuple[str, str, Decision]] | None = None
        self._entities: tuple[Entity, ...] = ()
        self._read_at = 0.0

    def remember(self, station: str, text: str) -> None:
        """A phrase the speaker answered: a later doubtful one near it is a little more likely."""
        kept = [item for item in self.recent.get(station, []) if self.monotonic() - item[0] < RECENT_S][-(MAX_RECENT - 1):]
        self.recent[station] = kept + [(self.monotonic(), tokens(text))]

    # -- evidence --------------------------------------------------------------
    def _pinned_phrases(self) -> list[tuple[str, str, Decision]]:
        """(phrase, intent id, its decision) for the pinned examples of intents that act or answer without a value to guess."""
        found = []
        for intent in self.grammar.intents:
            if intent.tier not in ("execute", "query") or intent.contexts or intent.priority < 0 or not intent.action:
                continue
            if intent.action["executor"].startswith(("repair.", "dialog.")) or any(slot.type in _VALUE_SLOTS for slot in intent.slots.values()):
                continue
            for example in intent.examples:
                decision = self.classify(example.say)
                if decision is not None and decision.intent == intent.id and decision.tier in ("execute", "query") and not decision.fuzzy:
                    found.append((example.say.strip(" .!?"), intent.id, decision))
        return found

    def _from_grammar(self, station: str, heard: list[str]) -> list[Option]:
        options: dict[str, Option] = {}
        if self._pinned is None:
            self._pinned = self._pinned_phrases()
        for phrase, intent_id, decision in self._pinned:
            if not self.executable(decision, station):
                continue
            score, _ = nearness(self.words, heard, tokens(phrase), extra_cost=GRAMMAR_EXTRA_COST)   # «который час в Токио» is a question for the brain, not «который час»
            best = options.get(intent_id)
            if best is None or score > best.score:
                options[intent_id] = Option(phrase, ask_form(phrase), score, "grammar", intent_id, _says(phrase, ()))
        return list(options.values())

    def _room_conflict(self, heard: Sequence[str], name: frozenset[str]) -> bool:
        """The owner named a room the entity's own name does not name («свет в спальне» against «Свет в студии»): another room's device is never offered."""
        for forms in self.grammar.groups.get("rooms", {}).values():
            said = any(self.words.member(word, forms) for word in heard)
            named = any(self.words.member(word, forms) for word in name)
            if said and not named:
                return True
        return False

    def _from_home(self, heard: list[str], entities: Sequence[Entity]) -> list[Option]:
        found = []
        for entity in entities:
            name = fold(entity.name)
            name_words = frozenset(tokens(name))
            if self._room_conflict(heard, name_words):
                continue
            for verb in _verbs(entity):
                if not name and not verb.nameless:
                    continue   # a device whose name nobody can say is only ever «the one that plays»
                best = 0.0
                for template in verb.phrases:
                    score, missing = nearness(self.words, heard, tokens(template.format(n=name)), name_words)
                    if missing and not verb.nameless:
                        continue
                    best = max(best, score - (_NAME_MISSING if missing else 0.0))
                if best <= 0:
                    continue
                phrase = " ".join(verb.phrases[0].format(n=name).split())
                call = HaCall(entity.domain, verb.service, entity.entity_id, entity.name, verb.expect, verb.done)
                found.append(Option(phrase, " ".join(verb.ask.format(n=accusative(name)).split()), best, "home", entity.entity_id,
                                    _says(phrase, tuple(dict.fromkeys(tokens(name)))), call))
        return found

    def _prior(self, option: Option, station: str) -> float:
        """A tie-breaker of at most a few hundredths: a phrase near one of the station's recent commands, a light off at night."""
        bonus = 0.0
        phrase = tokens(option.phrase)
        for at, spoken in self.recent.get(station, ()):
            if self.monotonic() - at < RECENT_S and nearness(self.words, spoken, phrase)[0] >= 0.8:
                bonus = 0.05
        if option.call is not None and option.call.domain == "light":
            hour = self.now().hour
            if option.call.service == "turn_off" and (hour >= 23 or hour < 6):
                bonus += 0.04
            if option.call.service == "turn_on" and 17 <= hour < 23:
                bonus += 0.04
        return bonus

    async def options(self, station: str, heard_text: str, *, floor: float) -> list[Option]:
        """The options at or above `floor`, best first: at most two, the second only when it scores close to the best. Before the brain
        (`floor` ASK_AT) a long phrase is chit-chat or a question and gets none."""
        heard = tokens(heard_text)
        if not heard or (floor >= ASK_AT and len(heard) > ASK_MAX_WORDS):
            return []
        entities = await self.refresh()
        pool = self._from_home(heard, entities) + self._from_grammar(station, heard)
        scored = sorted(((option.score + self._prior(option, station), option) for option in pool), key=lambda item: -item[0])
        ranked = [(score, option) for score, option in scored if score >= floor]
        picked: list[Option] = []
        for score, option in ranked:
            if any(option.phrase == other.phrase or option.ask == other.ask
                   or (option.call and other.call and option.call.entity_id == other.call.entity_id) for other in picked):
                continue
            if picked and (len(picked) == 2 or ranked[0][0] - score > SECOND_WITHIN or option.ask.startswith(MEANT)):
                continue
            picked.append(option)
        return picked

    # -- wording ---------------------------------------------------------------
    @staticmethod
    def question(options: Sequence[Option]) -> str:
        first = options[0].ask
        if len(options) == 1:
            return f"{first[:1].upper()}{first[1:]}?"
        return f"{first[:1].upper()}{first[1:]} или {options[1].ask}?"

    async def refresh(self, max_age: float = CATALOG_MAX_AGE_S) -> Sequence[Entity]:
        """The catalog with the live states; a read younger than `max_age` is reused (a turn that reaches the brain must not wait for Home Assistant
        twice), the last good copy when Home Assistant does not answer (it names the kinds of action)."""
        if self.home is not None and (not self._entities or self.monotonic() - self._read_at > max_age):
            try:
                self._entities = tuple(await self.home.entities())
                self._read_at = self.monotonic()
            except Exception as err:   # Home Assistant unreachable: the grammar's phrases and the abilities still help
                LOG.warning("clarify_home_unavailable class=%s", type(err).__name__)
        return self._entities

    def kinds(self) -> tuple[str, ...]:
        """The kinds of action this home has, in the owner's words: from the last read catalog and the abilities the adapter has."""
        domains = {entity.domain for entity in self._entities}
        found: list[str] = []
        if "light" in domains:
            found.append("свет")
        if any(entity.entity_id.startswith("climate.") for entity in self._entities):
            found.append("кондиционер")
        found += [fold(entity.name) for entity in self._entities if entity.domain == "media_player" and entity.name][:2]
        found += list(self.abilities())
        return tuple(dict.fromkeys(found))[:5]

    def describe(self, heard_text: str, denied: bool = False) -> str:
        """What the speaker says when no option is left: what was heard (or that nothing was) and what can be asked for. No request to repeat."""
        spoken = heard_text.strip(" .,!?…;:-—«»\"'")[:MAX_HEARD_CHARS]
        kinds = self.kinds()
        if denied:
            return f"Тогда что сделать: {_or_list(kinds)}?" if kinds else "Тогда что сделать?"
        if not spoken:
            return f"Ничего не расслышал. Что сделать: {_or_list(kinds)}?" if kinds else "Ничего не расслышал. Что сделать?"
        return f"Прозвучало «{spoken}», но что сделать, не ясно: {_or_list(kinds)}?" if kinds else f"Прозвучало «{spoken}», но что сделать, не ясно."


_VALUE_SLOTS = frozenset(("number", "ordinal", "percent", "delta", "duration", "clock", "text", "word"))
_INFINITIVE = {"включи": "включить", "включай": "включить", "выключи": "выключить", "выключай": "выключить", "погаси": "погасить",
               "потуши": "потушить", "отключи": "отключить", "поставь": "поставить", "запусти": "запустить", "останови": "остановить",
               "продолжи": "продолжить", "возобнови": "возобновить", "сделай": "сделать", "открой": "открыть", "закрой": "закрыть",
               "покажи": "показать", "скажи": "сказать", "назови": "назвать", "расскажи": "рассказать", "отмени": "отменить",
               "убавь": "убавить", "добавь": "добавить", "прибавь": "прибавить", "поменяй": "поменять", "зажги": "зажечь", "вруби": "включить"}


def infinitive(phrase: str) -> str:
    """«поставь телевизор на паузу» -> «поставить телевизор на паузу»; a phrase that does not open with a known verb is asked as it is."""
    head, _, rest = phrase.partition(" ")
    verb = _INFINITIVE.get(fold(head))
    return f"{verb} {rest}".strip() if verb else phrase


MEANT = "вы имели в виду"


def ask_form(phrase: str) -> str:
    """The question of a grammar option: the infinitive of an imperative («поставить телевизор на паузу»), else «вы имели в виду «…»»."""
    spoken = infinitive(phrase)
    return spoken if spoken != phrase else f"{MEANT} «{phrase}»"


def _says(phrase: str, name: tuple[str, ...]) -> tuple[str, ...]:
    """What a reply may say to pick this option: the whole phrase, the device's name, the verb."""
    head = phrase.partition(" ")[0]
    return tuple(dict.fromkeys(item for item in (phrase, " ".join(name), head) if item))


def _or_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " или " + items[-1]
